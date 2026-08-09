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
