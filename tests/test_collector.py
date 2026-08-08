import subprocess
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from token_limits.collector import main, notify, play_sound


class NotificationTransportTests(unittest.TestCase):
    def test_unresponsive_desktop_notification_service_does_not_break_collection(self):
        event = {"title": "Test", "message": "Test", "kind": "threshold", "account_id": "x:y"}
        with patch("token_limits.collector.subprocess.run", side_effect=subprocess.TimeoutExpired("notify-send", 2)):
            self.assertFalse(notify(event))

    def test_successful_desktop_notification_is_reported(self):
        event = {"title": "Test", "message": "Test", "kind": "threshold", "account_id": "x:y"}
        with patch("token_limits.collector.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertTrue(notify(event))

    def test_threshold_sound_uses_configured_file_and_volume(self):
        event = {"title": "Test", "message": "Test", "kind": "threshold", "account_id": "x:y"}
        sound = {"enabled": True, "file": "/tmp/alert.oga", "volume": 50}
        with patch("token_limits.collector.shutil.which", side_effect=lambda name: "/bin/paplay" if name == "paplay" else None), \
             patch("token_limits.collector.subprocess.run") as run:
            run.return_value.returncode = 0
            self.assertTrue(play_sound(event, sound))
        self.assertEqual(run.call_args.args[0], ["/bin/paplay", "--volume=32768", "/tmp/alert.oga"])

    def test_recovery_popup_does_not_play_threshold_sound(self):
        event = {"title": "Test", "message": "Test", "kind": "available_again", "account_id": "x:y"}
        with patch("token_limits.collector.subprocess.run") as run:
            self.assertTrue(play_sound(event, {"enabled": True, "file": "/tmp/alert.oga"}))
            run.assert_not_called()


class StatusOutputTests(unittest.TestCase):
    def test_print_status_emits_json_for_plasma_data_engine(self):
        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            status.write_text('{"accounts": []}\n', encoding="utf-8")
            output = StringIO()
            with redirect_stdout(output):
                result = main(["--status", str(status), "--print-status"])
            self.assertEqual(result, 0)
            self.assertEqual(output.getvalue().strip(), '{"accounts": []}')


if __name__ == "__main__":
    unittest.main()
