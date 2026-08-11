import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from token_limits.adapters import _http_json
from token_limits.core import (
    evaluate_notifications,
    normalize_account,
    parse_reset,
    process_accounts,
)


NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


class ResetParsingTests(unittest.TestCase):
    def test_accepts_iso_and_epoch_seconds(self):
        self.assertEqual(parse_reset("2026-08-09T12:00:00Z"), 1786276800)
        self.assertEqual(parse_reset(1786276800), 1786276800)

    def test_rejects_invalid_reset(self):
        self.assertIsNone(parse_reset("tomorrow-ish"))


class AccountNormalizationTests(unittest.TestCase):
    def test_keeps_session_and_weekly_windows_separate(self):
        account = normalize_account(
            provider="anthropic",
            alias="Personal",
            raw={
                "available": True,
                "session": {"used_percent": 42.5, "reset_at": "2026-08-08T15:00:00Z"},
                "weekly": {"used_percent": 81, "reset_at": "2026-08-12T12:00:00Z"},
            },
            now=NOW,
        )
        self.assertEqual(account["id"], "anthropic:Personal")
        self.assertEqual(account["windows"]["session"]["used_percent"], 42.5)
        self.assertEqual(account["windows"]["weekly"]["used_percent"], 81.0)
        self.assertEqual(account["windows"]["session"]["reset_in_seconds"], 10800)

    def test_clamps_percent_and_derives_availability(self):
        account = normalize_account(
            "codex", "Work", {"session": {"used_percent": 112}}, now=NOW
        )
        self.assertEqual(account["windows"]["session"]["used_percent"], 100.0)
        self.assertFalse(account["available"])


class NotificationTests(unittest.TestCase):
    def test_threshold_fires_once_per_reset_cycle(self):
        account = normalize_account(
            "anthropic",
            "Personal",
            {"available": True, "weekly": {"used_percent": 82, "reset_at": 1786536000}},
            now=NOW,
        )
        events, state = evaluate_notifications(account, {}, {"session": 80, "weekly": 80})
        self.assertEqual([e["kind"] for e in events], ["threshold"])
        events_again, _ = evaluate_notifications(account, state, {"session": 80, "weekly": 80})
        self.assertEqual(events_again, [])

    def test_threshold_can_be_configured_per_account(self):
        account = normalize_account(
            "minimax", "Work", {"weekly": {"used_percent": 91}}, now=NOW
        )
        events, _ = evaluate_notifications(account, {}, {"weekly": 95})
        self.assertEqual(events, [])

    def test_recovery_fires_after_account_becomes_available(self):
        unavailable = normalize_account(
            "codex", "Personal", {"available": False, "session": {"used_percent": 100}}, now=NOW
        )
        _, state = evaluate_notifications(unavailable, {}, {"session": 80})
        available = normalize_account(
            "codex", "Personal", {"available": True, "session": {"used_percent": 2}}, now=NOW
        )
        events, _ = evaluate_notifications(available, state, {"session": 80})
        self.assertIn("available_again", [e["kind"] for e in events])


class MultiAccountTests(unittest.TestCase):
    def test_collector_retry_budget_stops_long_rate_limit_waits(self):
        headers = Message()
        headers["Retry-After"] = "2"
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        config = {
            "retry_budget_seconds": 1,
            "accounts": [{"provider": "anthropic", "alias": "Personal"}],
        }

        def fetch(_entry):
            return _http_json("https://example.test/usage", {})

        with patch("token_limits.adapters.fetch_account", side_effect=fetch), \
                patch("token_limits.adapters.urllib.request.urlopen", side_effect=error) as request, \
                patch("time.monotonic", return_value=100), \
                patch("time.sleep") as sleep:
            result, _events, _state = process_accounts(config, {}, now=NOW)

        sleep.assert_not_called()
        self.assertEqual(request.call_args.kwargs["timeout"], 20.0)
        self.assertIn("429", result["accounts"][0]["error"])

    def test_expired_retry_budget_does_not_skip_later_accounts_initial_request(self):
        headers = Message()
        headers["Retry-After"] = "2"
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        response = io.BytesIO(b'{"available": true}')
        config = {
            "retry_budget_seconds": 1,
            "accounts": [
                {"provider": "anthropic", "alias": "First"},
                {"provider": "anthropic", "alias": "Second"},
            ],
        }
        clock = {"now": 100.0}
        calls = []

        def open_url(_request, timeout):
            calls.append(timeout)
            if len(calls) == 1:
                clock["now"] = 102.0
                raise error
            return response

        def fetch(_entry):
            return _http_json("https://example.test/usage", {})

        with patch("token_limits.adapters.fetch_account", side_effect=fetch), \
                patch("token_limits.adapters.urllib.request.urlopen", side_effect=open_url), \
                patch("time.monotonic", side_effect=lambda: clock["now"]), \
                patch("time.sleep") as sleep:
            result, _events, _state = process_accounts(config, {}, now=NOW)

        sleep.assert_not_called()
        self.assertEqual(calls, [20.0, 20.0])
        self.assertIsNone(result["accounts"][1]["error"])

    def test_default_budget_allows_two_ten_second_retry_after_delays(self):
        headers = Message()
        headers["Retry-After"] = "10"
        errors = [
            urllib.error.HTTPError(
                "https://example.test/usage", 429, "Too Many Requests",
                headers, None,
            )
            for _ in range(2)
        ]
        response = io.BytesIO(b'{"available": true}')
        config = {"accounts": [{"provider": "anthropic", "alias": "Personal"}]}

        def fetch(_entry):
            return _http_json("https://example.test/usage", {})

        clock = {"now": 100.0}

        def advance_clock(delay):
            clock["now"] += delay

        with patch("token_limits.adapters.fetch_account", side_effect=fetch), \
                patch("token_limits.adapters.urllib.request.urlopen", side_effect=[*errors, response]) as request, \
                patch("time.monotonic", side_effect=lambda: clock["now"]), \
                patch("time.sleep", side_effect=advance_clock) as sleep:
            result, _events, _state = process_accounts(config, {}, now=NOW)

        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [10.0, 10.0])
        self.assertIsNone(result["accounts"][0]["error"])

    def test_smaller_budget_stops_before_second_retry(self):
        headers = Message()
        headers["Retry-After"] = "10"
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        config = {
            "retry_budget_seconds": 15,
            "accounts": [{"provider": "anthropic", "alias": "Personal"}],
        }
        clock = {"now": 100.0}

        def fetch(_entry):
            return _http_json("https://example.test/usage", {})

        def advance_clock(delay):
            clock["now"] += delay

        with patch("token_limits.adapters.fetch_account", side_effect=fetch), \
                patch("token_limits.adapters.urllib.request.urlopen", side_effect=error) as request, \
                patch("time.monotonic", side_effect=lambda: clock["now"]), \
                patch("time.sleep", side_effect=advance_clock) as sleep:
            result, _events, _state = process_accounts(config, {}, now=NOW)

        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(10.0)
        self.assertIn("429", result["accounts"][0]["error"])

    def test_zero_retry_budget_disables_retries_but_keeps_initial_request(self):
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            Message(), None,
        )
        config = {
            "retry_budget_seconds": 0,
            "accounts": [{"provider": "anthropic", "alias": "Personal"}],
        }

        def fetch(_entry):
            return _http_json("https://example.test/usage", {})

        with patch("token_limits.adapters.fetch_account", side_effect=fetch), \
                patch("token_limits.adapters.urllib.request.urlopen", side_effect=error) as request, \
                patch("time.monotonic", return_value=100), \
                patch("time.sleep") as sleep:
            result, _events, _state = process_accounts(config, {}, now=NOW)

        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()
        self.assertIn("429", result["accounts"][0]["error"])

    def test_negative_retry_budget_uses_default_instead_of_disabling_retries(self):
        headers = Message()
        headers["Retry-After"] = "1"
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        response = io.BytesIO(b'{"available": true}')
        config = {
            "retry_budget_seconds": -5,
            "accounts": [{"provider": "anthropic", "alias": "Personal"}],
        }

        def fetch(_entry):
            return _http_json("https://example.test/usage", {})

        with patch("token_limits.adapters.fetch_account", side_effect=fetch), \
                patch("token_limits.adapters.urllib.request.urlopen", side_effect=[error, response]) as request, \
                patch("time.monotonic", return_value=100), \
                patch("time.sleep"):
            result, _events, _state = process_accounts(config, {}, now=NOW)

        self.assertEqual(request.call_count, 2)
        self.assertIsNone(result["accounts"][0]["error"])

    def test_invalid_retry_budget_falls_back_without_aborting_collection(self):
        config = {
            "retry_budget_seconds": "invalid",
            "accounts": [{
                "provider": "anthropic", "alias": "Personal",
                "adapter": "fixture", "fixture": {"available": True},
            }],
        }
        result, _events, _state = process_accounts(config, {}, now=NOW)
        self.assertTrue(result["accounts"][0]["available"])

    def test_first_fetch_failure_does_not_create_false_recovery_state(self):
        config = {"accounts": [{"provider": "anthropic", "alias": "Personal"}]}
        with patch("token_limits.adapters.fetch_account", side_effect=RuntimeError("HTTP 429")):
            _result, events, state = process_accounts(config, {}, now=NOW)

        self.assertEqual(events, [])
        self.assertNotIn("available", state["accounts"]["anthropic:Personal"])
        with patch("token_limits.adapters.fetch_account", return_value={"available": True}):
            _result, recovered_events, _state = process_accounts(config, state, now=NOW)
        self.assertNotIn("available_again", [event["kind"] for event in recovered_events])

    def test_payload_warning_with_fresh_windows_still_evaluates_thresholds(self):
        config = {"accounts": [{"provider": "command", "alias": "Local"}]}
        payload = {
            "available": True,
            "session": {"used_percent": 95, "reset_at": 1786536000},
            "error": "stale cache",
        }
        with patch("token_limits.adapters.fetch_account", return_value=payload):
            _result, events, _state = process_accounts(config, {}, now=NOW)

        self.assertIn("threshold", [event["kind"] for event in events])


    def test_payload_reported_unavailability_still_allows_recovery_event(self):
        config = {"accounts": [{"provider": "command", "alias": "Local"}]}
        payload = {"available": False, "error": "quota exhausted"}
        with patch("token_limits.adapters.fetch_account", return_value=payload):
            _result, _events, state = process_accounts(config, {}, now=NOW)

        with patch("token_limits.adapters.fetch_account", return_value={"available": True}):
            _result, recovered_events, _state = process_accounts(config, state, now=NOW)
        self.assertIn("available_again", [event["kind"] for event in recovered_events])

    def test_fetch_error_includes_chained_retry_cause(self):
        config = {"accounts": [{"provider": "anthropic", "alias": "Personal"}]}
        rate_limit = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            Message(), None,
        )

        def fail(_entry):
            try:
                raise urllib.error.URLError(TimeoutError("timed out"))
            except urllib.error.URLError as cause:
                raise rate_limit from cause

        with patch("token_limits.adapters.fetch_account", side_effect=fail):
            result, _events, _state = process_accounts(config, {}, now=NOW)

        self.assertIn("429", result["accounts"][0]["error"])
        self.assertIn("timed out", result["accounts"][0]["error"])

    def test_fetch_error_preserves_availability_and_does_not_fake_recovery(self):
        config = {"accounts": [{"provider": "anthropic", "alias": "Personal"}]}
        previous = {"accounts": {
            "anthropic:Personal": {"available": True, "notified": {}},
        }}
        with patch("token_limits.adapters.fetch_account", side_effect=RuntimeError("HTTP 429")):
            result, events, state = process_accounts(config, previous, now=NOW)

        self.assertFalse(result["accounts"][0]["available"])
        self.assertEqual(result["accounts"][0]["error"], "HTTP 429")
        self.assertEqual(events, [])
        self.assertEqual(state["accounts"]["anthropic:Personal"], previous["accounts"]["anthropic:Personal"])

        with patch("token_limits.adapters.fetch_account", return_value={"available": True}):
            _result, recovered_events, _state = process_accounts(config, state, now=NOW)
        self.assertNotIn("available_again", [event["kind"] for event in recovered_events])

    def test_same_provider_accounts_stay_distinct(self):
        config = {
            "accounts": [
                {"provider": "anthropic", "alias": "Personal", "adapter": "fixture", "fixture": {"weekly": {"used_percent": 20}}},
                {"provider": "anthropic", "alias": "Work", "adapter": "fixture", "fixture": {"weekly": {"used_percent": 70}}},
            ]
        }
        result, _events, _state = process_accounts(config, {}, now=NOW)
        self.assertEqual([a["id"] for a in result["accounts"]], ["anthropic:Personal", "anthropic:Work"])
        self.assertEqual(result["accounts"][1]["windows"]["weekly"]["used_percent"], 70.0)

    def test_duplicate_provider_alias_is_rejected(self):
        config = {"accounts": [
            {"provider": "grok", "alias": "Work", "adapter": "fixture", "fixture": {}},
            {"provider": "grok", "alias": "Work", "adapter": "fixture", "fixture": {}},
        ]}
        with self.assertRaisesRegex(ValueError, "duplicate account id"):
            process_accounts(config, {}, now=NOW)

    def test_disabled_accounts_are_skipped(self):
        config = {"accounts": [
            {"provider": "anthropic", "alias": "Personal", "adapter": "fixture", "fixture": {}},
            {"provider": "anthropic", "alias": "Work", "enabled": False, "adapter": "fixture", "fixture": {}},
        ]}
        result, _events, _state = process_accounts(config, {}, now=NOW)
        self.assertEqual([a["id"] for a in result["accounts"]], ["anthropic:Personal"])


if __name__ == "__main__":
    unittest.main()
