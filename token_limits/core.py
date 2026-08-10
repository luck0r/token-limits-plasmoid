"""Core normalization and notification policy for Token Limits."""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any


def parse_reset(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(float(value))
        except ValueError:
            try:
                return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())
            except ValueError:
                return None
    return None


def _window(raw: dict[str, Any] | None, now: datetime) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        percent = max(0.0, min(100.0, float(raw.get("used_percent", 0))))
    except (TypeError, ValueError):
        percent = 0.0
    reset_at = parse_reset(raw.get("reset_at"))
    return {
        "used_percent": round(percent, 2),
        "remaining_percent": round(100.0 - percent, 2),
        "reset_at": reset_at,
        "reset_in_seconds": max(0, reset_at - int(now.timestamp())) if reset_at else None,
    }


def normalize_account(provider: str, alias: str, raw: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    windows = {name: win for name in ("session", "weekly") if (win := _window(raw.get(name), now)) is not None}
    derived_available = not any(win["used_percent"] >= 100 for win in windows.values())
    return {
        "id": f"{provider}:{alias}",
        "provider": provider,
        "alias": alias,
        "available": bool(raw.get("available", derived_available)),
        "windows": windows,
        "error": raw.get("error"),
        "details": raw.get("details", {}),
        "updated_at": int(now.timestamp()),
    }


def evaluate_notifications(account: dict[str, Any], previous: dict[str, Any], thresholds: dict[str, Any]) -> tuple[list[dict[str, str]], dict[str, Any]]:
    events: list[dict[str, str]] = []
    notified = dict(previous.get("notified", {}))
    for name, win in account["windows"].items():
        threshold = float(thresholds.get(name, 80))
        cycle = str(win.get("reset_at") or "unknown")
        marker = f"{name}:{cycle}:{threshold:g}"
        if win["used_percent"] >= threshold and not notified.get(marker):
            events.append({
                "kind": "threshold",
                "account_id": account["id"],
                "title": f"{account['provider'].title()} · {account['alias']}",
                "message": f"{name.title()}-Limit bei {win['used_percent']:g}% (Schwelle {threshold:g}%)",
            })
            notified[marker] = True
    if previous.get("available") is False and account["available"] is True:
        events.append({
            "kind": "available_again",
            "account_id": account["id"],
            "title": f"{account['provider'].title()} · {account['alias']} wieder verfügbar",
            "message": "Das Limit wurde zurückgesetzt; das Konto kann wieder verwendet werden.",
        })
    return events, {"available": account["available"], "notified": notified}


def process_accounts(config: dict[str, Any], previous_state: dict[str, Any], now: datetime | None = None):
    from .adapters import provider_retry_budget

    try:
        budget_seconds = float(config.get("retry_budget_seconds", 90))
        if not math.isfinite(budget_seconds) or budget_seconds < 0:
            raise ValueError
    except (TypeError, ValueError):
        budget_seconds = 90.0
    with provider_retry_budget(budget_seconds):
        return _process_accounts(config, previous_state, now)


def _process_accounts(config: dict[str, Any], previous_state: dict[str, Any], now: datetime | None = None):
    from .adapters import fetch_account

    now = now or datetime.now(timezone.utc)
    result_accounts: list[dict[str, Any]] = []
    events: list[dict[str, str]] = []
    next_state: dict[str, Any] = {"accounts": {}}
    seen: set[str] = set()
    defaults = config.get("notifications", {"session": 80, "weekly": 80})

    for entry in config.get("accounts", []):
        if entry.get("enabled", True) is False:
            continue
        provider = str(entry["provider"]).lower()
        alias = str(entry["alias"])
        account_id = f"{provider}:{alias}"
        if account_id in seen:
            raise ValueError(f"duplicate account id: {account_id}")
        seen.add(account_id)
        previous_account = previous_state.get("accounts", {}).get(account_id, {})
        fetch_failed = False
        try:
            raw = fetch_account(entry)
        except Exception as exc:  # Collector must keep other accounts alive.
            fetch_failed = True
            error_message = str(exc)
            if exc.__cause__ is not None:
                error_message = f"{error_message} (caused by {exc.__cause__})"
            raw = {
                "available": False,
                "error": error_message,
            }
        account = normalize_account(provider, alias, raw, now)
        result_accounts.append(account)
        thresholds = dict(defaults)
        thresholds.update(entry.get("thresholds", {}))
        if fetch_failed:
            account_events = []
            account_state = dict(previous_account)
            account_state["notified"] = dict(previous_account.get("notified", {}))
        else:
            account_events, account_state = evaluate_notifications(
                account, previous_account, thresholds
            )
        events.extend(account_events)
        next_state["accounts"][account_id] = account_state

    return {"generated_at": int(now.timestamp()), "accounts": result_accounts}, events, next_state
