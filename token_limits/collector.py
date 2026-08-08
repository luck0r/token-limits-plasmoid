#!/usr/bin/env python3
"""Collect provider quotas, write status atomically, and emit desktop notifications."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from .core import process_accounts


def load_json(path: Path, fallback):
    try:
        with path.open(encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return fallback


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def notify(event: dict[str, str]) -> bool:
    try:
        result = subprocess.run(
            ["notify-send", "--app-name=Token Limits", "--icon=dialog-information", event["title"], event["message"]],
            check=False,
            timeout=2,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        # Notification transport must never stop quota collection. Plasma's
        # D-Bus notification service can briefly be unavailable during login.
        return False


def play_sound(event: dict[str, str], sound: dict) -> bool:
    """Play a configured sound for selected notification event kinds."""
    if not sound.get("enabled", False):
        return True
    if event.get("kind") not in sound.get("events", ["threshold"]):
        return True
    sound_file = str(Path(sound.get("file", "/usr/share/sounds/freedesktop/stereo/message.oga")).expanduser())
    volume = max(0, min(100, int(sound.get("volume", 70))))
    paplay = shutil.which("paplay")
    if paplay:
        command = [paplay, f"--volume={round(65536 * volume / 100)}", sound_file]
    else:
        pw_play = shutil.which("pw-play")
        if not pw_play:
            return False
        command = [pw_play, "--volume", str(volume / 100), sound_file]
    try:
        result = subprocess.run(
            command,
            check=False,
            timeout=5,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return result.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Collect AI subscription usage limits")
    parser.add_argument("--config", default="~/.config/token-limits/config.json")
    parser.add_argument("--status", default="~/.local/share/token-limits/status.json")
    parser.add_argument("--state", default="~/.local/state/token-limits/state.json")
    parser.add_argument("--no-notify", action="store_true")
    parser.add_argument("--print-status", action="store_true", help="print cached status for the Plasma data engine")
    args = parser.parse_args(argv)

    config_path = Path(args.config).expanduser()
    status_path = Path(args.status).expanduser()
    state_path = Path(args.state).expanduser()
    if args.print_status:
        try:
            print(json.dumps(load_json(status_path, {"generated_at": 0, "accounts": []}), ensure_ascii=False))
            return 0
        except Exception as exc:
            print(json.dumps({"generated_at": 0, "accounts": [], "error": str(exc)}, ensure_ascii=False))
            return 1
    try:
        config = load_json(config_path, None)
        if config is None:
            raise FileNotFoundError(f"Konfiguration fehlt: {config_path}")
        previous = load_json(state_path, {})
        status, events, state = process_accounts(config, previous)
        atomic_json(status_path, status)
        notifications_enabled = config.get("notifications", {}).get("enabled", True) and not args.no_notify
        delivered = True
        if notifications_enabled:
            sound = config.get("notifications", {}).get("sound", {})
            delivered = all(notify(event) and play_sound(event, sound) for event in events)
        # Failed notifications remain pending and are retried next tick.
        if not notifications_enabled or delivered:
            atomic_json(state_path, state)
        errors = sum(bool(account.get("error")) for account in status["accounts"])
        return 2 if errors == len(status["accounts"]) and errors else 0
    except Exception as exc:
        print(f"token-limits: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
