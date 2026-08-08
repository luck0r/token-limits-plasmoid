import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from token_limits.adapters import _minimax, parse_grok_payload, parse_minimax_payload


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
            "current_interval_total_count": 1000,
            "current_interval_usage_count": 250,
            "end_time": 1786536000
        }]}})
        self.assertEqual(result["session"]["used_percent"], 25)
        self.assertEqual(result["session"]["reset_at"], 1786536000)
        self.assertTrue(result["available"])

    def test_reads_api_key_from_private_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / ".env"
            env_file.write_text("MINIMAX_API_KEY=secret-test-value\n", encoding="utf-8")
            payload = {"data": {"model_remains": [{
                "current_interval_total_count": 100,
                "current_interval_usage_count": 10,
                "end_time": 1786536000
            }]}}
            with patch("token_limits.adapters._http_json", return_value=payload) as request:
                result = _minimax({"env_file": str(env_file), "api_key_env": "MINIMAX_API_KEY"})
            self.assertEqual(result["session"]["used_percent"], 10)
            self.assertEqual(request.call_args.args[1]["Authorization"], "Bearer secret-test-value")


if __name__ == "__main__":
    unittest.main()
