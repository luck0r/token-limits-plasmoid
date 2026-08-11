import io
import tempfile
import unittest
import urllib.error
from email.message import Message
from email.utils import formatdate
from pathlib import Path
from unittest.mock import patch

from token_limits.adapters import (
    _http_json,
    _minimax,
    _post_form_json,
    parse_grok_payload,
    parse_minimax_payload,
    provider_retry_budget,
)


class HttpRetryTests(unittest.TestCase):
    def test_retry_budget_keeps_configured_timeout_for_retry(self):
        headers = Message()
        headers["Retry-After"] = "1"
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        response = io.BytesIO(b'{"ok": true}')
        with provider_retry_budget(30), \
                patch("token_limits.adapters.urllib.request.urlopen", side_effect=[error, response]) as request, \
                patch("time.monotonic", return_value=100), \
                patch("time.sleep") as sleep:
            result = _http_json("https://example.test/usage", {})

        self.assertEqual(result, {"ok": True})
        self.assertEqual([call.kwargs["timeout"] for call in request.call_args_list], [20.0, 20.0])
        sleep.assert_called_once_with(1.0)

    def test_retry_budget_re_raises_429_when_next_delay_does_not_fit(self):
        headers = Message()
        headers["Retry-After"] = "1"
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )

        with provider_retry_budget(6.5), \
                patch("token_limits.adapters.urllib.request.urlopen", side_effect=error) as request, \
                patch("time.sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                _http_json("https://example.test/usage", {})

        self.assertIs(raised.exception, error)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_retry_runtime_consumes_shared_budget(self):
        headers = Message()
        headers["Retry-After"] = "1"
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        clock = {"now": 100.0}
        calls = 0

        def open_url(_request, timeout):
            nonlocal calls
            calls += 1
            self.assertEqual(timeout, {1: 20.0, 2: 9.0, 3: 20.0}[calls])
            if calls in (1, 3):
                raise error
            clock["now"] = 104.0
            return io.BytesIO(b'{"ok": true}')

        with provider_retry_budget(10), \
                patch("token_limits.adapters.urllib.request.urlopen", side_effect=open_url), \
                patch("time.monotonic", side_effect=lambda: clock["now"]), \
                patch("time.sleep") as sleep:
            self.assertEqual(_http_json("https://example.test/first", {}), {"ok": True})
            with self.assertRaises(urllib.error.HTTPError):
                _http_json("https://example.test/second", {})

        self.assertEqual(calls, 3)
        sleep.assert_called_once_with(1.0)

    def test_retry_timeout_re_raises_original_rate_limit_error(self):
        headers = Message()
        headers["Retry-After"] = "1"
        rate_limit = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        timeout = urllib.error.URLError(TimeoutError("timed out"))
        with provider_retry_budget(10), \
                patch("token_limits.adapters.urllib.request.urlopen", side_effect=[rate_limit, timeout]), \
                patch("time.monotonic", return_value=100), \
                patch("time.sleep"):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                _http_json("https://example.test/usage", {})

        self.assertIs(raised.exception, rate_limit)

    def test_uncapped_retry_timeout_surfaces_network_error(self):
        headers = Message()
        headers["Retry-After"] = "1"
        rate_limit = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        timeout = urllib.error.URLError(TimeoutError("timed out"))
        with provider_retry_budget(30), \
                patch("token_limits.adapters.urllib.request.urlopen", side_effect=[rate_limit, timeout]), \
                patch("time.monotonic", return_value=100), \
                patch("time.sleep"):
            with self.assertRaises(urllib.error.URLError) as raised:
                _http_json("https://example.test/usage", {})

        self.assertIs(raised.exception, timeout)

    def test_retry_connection_error_is_not_mislabeled_as_rate_limit(self):
        headers = Message()
        headers["Retry-After"] = "1"
        rate_limit = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        connection_error = urllib.error.URLError(ConnectionRefusedError("refused"))
        with provider_retry_budget(30), \
                patch(
                    "token_limits.adapters.urllib.request.urlopen",
                    side_effect=[rate_limit, connection_error],
                ), \
                patch("time.monotonic", return_value=100), \
                patch("time.sleep"):
            with self.assertRaises(urllib.error.URLError) as raised:
                _http_json("https://example.test/usage", {})

        self.assertIs(raised.exception, connection_error)

    def test_retries_rate_limited_oauth_form_post(self):
        headers = Message()
        headers["Retry-After"] = "1"
        error = urllib.error.HTTPError(
            "https://example.test/token", 429, "Too Many Requests",
            headers, None,
        )
        response = io.BytesIO(b'{"access_token": "dummy"}')
        with patch("token_limits.adapters.urllib.request.urlopen", side_effect=[error, response]) as request, \
                patch("time.sleep") as sleep:
            result = _post_form_json("https://example.test/token", {"grant_type": "refresh_token"})

        self.assertEqual(result, {"access_token": "dummy"})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(1.0)

    def test_retries_429_after_short_retry_after_delay(self):
        headers = Message()
        headers["Retry-After"] = "2"
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        response = io.BytesIO(b'{"ok": true}')
        with patch("token_limits.adapters.urllib.request.urlopen", side_effect=[error, response]) as request, \
                patch("time.sleep") as sleep:
            result = _http_json("https://example.test/usage", {})

        self.assertEqual(result, {"ok": True})
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_does_not_shorten_long_retry_after_or_block_collector(self):
        headers = Message()
        headers["Retry-After"] = "120"
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        with patch("token_limits.adapters.urllib.request.urlopen", side_effect=error) as request, \
                patch("time.sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                _http_json("https://example.test/usage", {})

        self.assertIs(raised.exception, error)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_uses_exponential_backoff_with_jitter_without_retry_after(self):
        errors = [
            urllib.error.HTTPError(
                "https://example.test/usage", 429, "Too Many Requests",
                Message(), None,
            )
            for _ in range(2)
        ]
        response = io.BytesIO(b'{"ok": true}')
        with patch("token_limits.adapters.urllib.request.urlopen", side_effect=[*errors, response]), \
                patch("random.uniform", return_value=0.25), \
                patch("time.sleep") as sleep:
            result = _http_json("https://example.test/usage", {})

        self.assertEqual(result, {"ok": True})
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.25, 2.25])

    def test_supports_http_date_retry_after(self):
        headers = Message()
        headers["Retry-After"] = formatdate(1002, usegmt=True)
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        response = io.BytesIO(b'{"ok": true}')
        with patch("token_limits.adapters.urllib.request.urlopen", side_effect=[error, response]), \
                patch("time.time", return_value=1000), \
                patch("random.uniform", return_value=0), \
                patch("time.sleep") as sleep:
            result = _http_json("https://example.test/usage", {})

        self.assertEqual(result, {"ok": True})
        sleep.assert_called_once_with(2.0)

    def test_missing_http_error_headers_uses_backoff(self):
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            None, None,  # type: ignore[arg-type]
        )
        response = io.BytesIO(b'{"ok": true}')
        with patch("token_limits.adapters.urllib.request.urlopen", side_effect=[error, response]), \
                patch("random.uniform", return_value=0), \
                patch("time.sleep") as sleep:
            result = _http_json("https://example.test/usage", {})

        self.assertEqual(result, {"ok": True})
        sleep.assert_called_once_with(1.0)

    def test_zero_retry_after_is_floored_to_base_delay(self):
        headers = Message()
        headers["Retry-After"] = "0"
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        response = io.BytesIO(b'{"ok": true}')
        with patch("token_limits.adapters.urllib.request.urlopen", side_effect=[error, response]), \
                patch("time.sleep") as sleep:
            result = _http_json("https://example.test/usage", {})

        self.assertEqual(result, {"ok": True})
        sleep.assert_called_once_with(1.0)

    def test_invalid_retry_after_uses_backoff(self):
        headers = Message()
        headers["Retry-After"] = "soon"
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        response = io.BytesIO(b'{"ok": true}')
        with patch("token_limits.adapters.urllib.request.urlopen", side_effect=[error, response]), \
                patch("random.uniform", return_value=0), \
                patch("time.sleep") as sleep:
            result = _http_json("https://example.test/usage", {})

        self.assertEqual(result, {"ok": True})
        sleep.assert_called_once_with(1.0)

    def test_past_http_date_is_floored_to_base_delay(self):
        headers = Message()
        headers["Retry-After"] = formatdate(998, usegmt=True)
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        response = io.BytesIO(b'{"ok": true}')
        with patch("token_limits.adapters.urllib.request.urlopen", side_effect=[error, response]), \
                patch("time.time", return_value=1000), \
                patch("time.sleep") as sleep:
            result = _http_json("https://example.test/usage", {})

        self.assertEqual(result, {"ok": True})
        sleep.assert_called_once_with(1.0)

    def test_non_finite_retry_after_is_not_retried(self):
        headers = Message()
        headers["Retry-After"] = "nan"
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            headers, None,
        )
        with patch("token_limits.adapters.urllib.request.urlopen", side_effect=error) as request, \
                patch("time.sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError):
                _http_json("https://example.test/usage", {})

        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_configured_backoff_cap_is_enforced(self):
        error = urllib.error.HTTPError(
            "https://example.test/usage", 429, "Too Many Requests",
            Message(), None,
        )
        response = io.BytesIO(b'{"ok": true}')
        with patch("token_limits.adapters.urllib.request.urlopen", side_effect=[error, response]), \
                patch("random.uniform", return_value=0), \
                patch("time.sleep") as sleep:
            result = _http_json(
                "https://example.test/usage", {},
                retry_base_delay=4, retry_max_delay=2,
            )

        self.assertEqual(result, {"ok": True})
        sleep.assert_called_once_with(2)

    def test_non_429_error_is_not_retried(self):
        error = urllib.error.HTTPError(
            "https://example.test/usage", 401, "Unauthorized",
            Message(), None,
        )
        with patch("token_limits.adapters.urllib.request.urlopen", side_effect=error) as request, \
                patch("time.sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                _http_json("https://example.test/usage", {})

        self.assertIs(raised.exception, error)
        self.assertEqual(request.call_count, 1)
        sleep.assert_not_called()

    def test_stops_after_max_retries_and_raises_last_429(self):
        errors = [
            urllib.error.HTTPError(
                "https://example.test/usage", 429, "Too Many Requests",
                Message(), None,
            )
            for _ in range(3)
        ]
        with patch("token_limits.adapters.urllib.request.urlopen", side_effect=errors) as request, \
                patch("random.uniform", return_value=0), \
                patch("time.sleep") as sleep:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                _http_json("https://example.test/usage", {})

        self.assertIs(raised.exception, errors[-1])
        self.assertEqual(request.call_count, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1.0, 2.0])


class GrokAdapterTests(unittest.TestCase):
    def test_maps_weekly_credit_window(self):
        result = parse_grok_payload({"config": {"creditUsagePercent": 63, "currentPeriod": {"end": "2026-08-10T00:00:00Z"}}})
        self.assertEqual(result["weekly"]["used_percent"], 63)
        self.assertEqual(result["weekly"]["reset_at"], "2026-08-10T00:00:00Z")
        self.assertTrue(result["available"])


class MiniMaxAdapterTests(unittest.TestCase):
    def test_maps_real_coding_plan_remaining_percent_and_weekly_window(self):
        result = parse_minimax_payload({"model_remains": [{
            "model_name": "general",
            "current_interval_total_count": 0,
            "current_interval_usage_count": 0,
            "current_interval_remaining_percent": 95,
            "current_interval_status": 1,
            "end_time": 1786219200000,
            "current_weekly_total_count": 0,
            "current_weekly_usage_count": 0,
            "current_weekly_remaining_percent": 74,
            "current_weekly_status": 1,
            "weekly_end_time": 1786320000000
        }]})
        self.assertEqual(result["session"]["used_percent"], 5)
        self.assertEqual(result["weekly"]["used_percent"], 26)
        self.assertEqual(result["session"]["reset_at"], 1786219200)
        self.assertEqual(result["weekly"]["reset_at"], 1786320000)
        self.assertTrue(result["available"])

    def test_maps_current_coding_plan_interval(self):
        result = parse_minimax_payload({"data": {"model_remains": [{
            "model_name": "general",
            "current_interval_total_count": 1000,
            "current_interval_usage_count": 250,
            "end_time": 1786536000
        }]}})
        self.assertEqual(result["session"]["used_percent"], 25)
        self.assertEqual(result["session"]["reset_at"], 1786536000)
        self.assertTrue(result["available"])

    def test_rejects_video_product_row(self):
        with self.assertRaisesRegex(ValueError, "identifiable coding-plan quota"):
            parse_minimax_payload({"model_remains": [{
                "model_name": "video",
                "current_interval_remaining_percent": 10,
                "end_time": 1786536000000,
            }]})

    def test_rejects_other_labeled_non_coding_product_row(self):
        with self.assertRaisesRegex(ValueError, "products: \\['image'\\]"):
            parse_minimax_payload({"model_remains": [{
                "model_name": "image",
                "current_interval_remaining_percent": 10,
                "end_time": 1786536000000,
            }]})

    def test_rejects_music_product_that_shares_minimax_m_prefix(self):
        with self.assertRaisesRegex(ValueError, "MiniMax-Music"):
            parse_minimax_payload({"model_remains": [{
                "model_name": "MiniMax-Music-2.0",
                "current_interval_remaining_percent": 10,
                "end_time": 1786536000000,
            }]})

    def test_accepts_minimax_m_series_product_name(self):
        result = parse_minimax_payload({"model_remains": [{
            "model_name": "MiniMax-M2",
            "current_interval_remaining_percent": 78,
            "end_time": 1786536000000,
        }]})
        self.assertEqual(result["session"]["used_percent"], 22)

    def test_rejects_non_coding_suffix_on_minimax_m_series(self):
        with self.assertRaisesRegex(ValueError, "MiniMax-M2-Voice"):
            parse_minimax_payload({"model_remains": [{
                "model_name": "MiniMax-M2-Voice",
                "current_interval_remaining_percent": 78,
                "end_time": 1786536000000,
            }]})

    def test_uses_configured_product_name_override(self):
        result = parse_minimax_payload({"model_remains": [
            {
                "model_name": "video",
                "current_interval_remaining_percent": 1,
                "end_time": 1786536000000,
            },
            {
                "model_name": "plan-pro",
                "current_interval_remaining_percent": 69,
                "end_time": 1786536000000,
            },
        ]}, product_name="plan-pro")
        self.assertEqual(result["session"]["used_percent"], 31)

    def test_configured_product_name_must_match(self):
        for model_name in ("general", "video"):
            with self.subTest(model_name=model_name), \
                    self.assertRaisesRegex(ValueError, "no product 'plan-pro'"):
                parse_minimax_payload({"model_remains": [{
                    "model_name": model_name,
                    "current_interval_remaining_percent": 69,
                    "end_time": 1786536000000,
                }]}, product_name="plan-pro")

    def test_rejects_selected_row_without_interval_data(self):
        with self.assertRaisesRegex(ValueError, "no recognized quota windows"):
            parse_minimax_payload({"model_remains": [{
                "model_name": "general",
                "end_time": 1786536000000,
            }]})

    def test_accepts_weekly_only_coding_plan_row(self):
        result = parse_minimax_payload({"model_remains": [{
            "model_name": "general",
            "current_weekly_remaining_percent": 63,
            "weekly_end_time": 1787140800000,
        }]})
        self.assertNotIn("session", result)
        self.assertEqual(result["weekly"]["used_percent"], 37)

    def test_missing_interval_usage_count_means_zero_usage(self):
        result = parse_minimax_payload({"model_remains": [{
            "model_name": "general",
            "current_interval_total_count": 1000,
            "end_time": 1786536000000,
        }]})
        self.assertEqual(result["session"]["used_percent"], 0)

    def test_rejects_malformed_product_rows_with_value_error(self):
        with self.assertRaisesRegex(ValueError, "no coding-plan interval"):
            parse_minimax_payload({"model_remains": ["general", 123, None]})

    def test_accepts_consistent_minimax_model_separators(self):
        for model_name in ("MiniMax_M2", "MiniMax.M2"):
            with self.subTest(model_name=model_name):
                result = parse_minimax_payload({"model_remains": [{
                    "model_name": model_name,
                    "current_interval_remaining_percent": 76,
                    "end_time": 1786536000000,
                }]})
                self.assertEqual(result["session"]["used_percent"], 24)

    def test_accepts_unlabeled_single_coding_plan_row(self):
        result = parse_minimax_payload({"model_remains": [{
            "current_interval_remaining_percent": 82,
            "end_time": 1786536000000,
        }]})
        self.assertEqual(result["session"]["used_percent"], 18)

    def test_accepts_coding_prefix_product_name(self):
        result = parse_minimax_payload({"model_remains": [{
            "model_name": "coding-v2",
            "current_interval_remaining_percent": 80,
            "end_time": 1786536000000,
        }]})
        self.assertEqual(result["session"]["used_percent"], 20)

    def test_accepts_unrecognized_labeled_single_row(self):
        result = parse_minimax_payload({"model_remains": [{
            "model_name": "plan-basic",
            "current_interval_remaining_percent": 80,
            "end_time": 1786536000000,
        }]})
        self.assertEqual(result["session"]["used_percent"], 20)

    def test_weekly_only_exhaustion_marks_account_unavailable(self):
        result = parse_minimax_payload({"model_remains": [{
            "model_name": "general",
            "current_weekly_remaining_percent": 0,
            "weekly_end_time": 1787140800000,
        }]})
        self.assertFalse(result["available"])

    def test_session_exhaustion_marks_account_unavailable(self):
        result = parse_minimax_payload({"model_remains": [{
            "model_name": "general",
            "current_interval_remaining_percent": 0,
            "end_time": 1786536000000,
        }]})
        self.assertFalse(result["available"])

    def test_rejects_ambiguous_mixed_rows_without_general_product(self):
        with self.assertRaisesRegex(ValueError, "identifiable coding-plan quota"):
            parse_minimax_payload({"model_remains": [
                {
                    "model_name": "video",
                    "current_interval_remaining_percent": 10,
                    "end_time": 1786536000000,
                },
                {
                    "current_interval_remaining_percent": 70,
                    "end_time": 1786536000000,
                },
            ]})

    def test_rejects_multiple_coding_products_without_general(self):
        with self.assertRaisesRegex(ValueError, "identifiable coding-plan quota"):
            parse_minimax_payload({"model_remains": [
                {
                    "model_name": "MiniMax-M2",
                    "current_interval_remaining_percent": 75,
                    "end_time": 1786536000000,
                },
                {
                    "model_name": "coding-pro",
                    "current_interval_remaining_percent": 65,
                    "end_time": 1786536000000,
                },
            ]})

    def test_accepts_camel_case_general_product_name(self):
        result = parse_minimax_payload({"modelRemains": [{
            "modelName": "general",
            "current_interval_remaining_percent": 81,
            "end_time": 1786536000000,
        }]})
        self.assertEqual(result["session"]["used_percent"], 19)

    def test_prefers_general_row_over_other_coding_products(self):
        result = parse_minimax_payload({"model_remains": [
            {
                "model_name": "coding-lite",
                "current_interval_remaining_percent": 1,
                "end_time": 1786536000000,
            },
            {
                "model_name": "general",
                "current_interval_remaining_percent": 73,
                "end_time": 1786536000000,
            },
        ]})
        self.assertEqual(result["session"]["used_percent"], 27)

    def test_reads_and_strips_api_key_file(self):
        with tempfile.TemporaryDirectory() as directory:
            key_file = Path(directory) / "minimax-api-key"
            key_file.write_text("dummy-key\n", encoding="utf-8")
            payload = {"model_remains": [{
                "model_name": "plan-pro",
                "current_interval_remaining_percent": 90,
                "end_time": 1786536000000,
            }]}
            with patch.dict("os.environ", {"HOME": directory}), \
                    patch("token_limits.adapters._http_json", return_value=payload) as request:
                result = _minimax({
                    "api_key_file": "~/minimax-api-key",
                    "product_name": "plan-pro",
                })
            self.assertEqual(result["session"]["used_percent"], 10)
            self.assertEqual(request.call_args.args[1]["Authorization"], "Bearer dummy-key")

    def test_reads_api_key_from_private_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("MINIMAX_API_KEY=dummy-key\n", encoding="utf-8")
            payload = {"data": {"model_remains": [{
                "model_name": "general",
                "current_interval_total_count": 100,
                "current_interval_usage_count": 10,
                "end_time": 1786536000
            }]}}
            with patch("token_limits.adapters._http_json", return_value=payload) as request:
                result = _minimax({"env_file": str(env_file), "api_key_env": "MINIMAX_API_KEY"})
            self.assertEqual(result["session"]["used_percent"], 10)
            self.assertEqual(request.call_args.args[1]["Authorization"], "Bearer dummy-key")


if __name__ == "__main__":
    unittest.main()
