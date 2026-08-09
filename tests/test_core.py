import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

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
