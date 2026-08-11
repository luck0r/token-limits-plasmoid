"""Provider adapters. Secrets are read only by this local process."""
from __future__ import annotations

import json
import math
import os
import random
import re
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


_RETRY_BUDGET_REMAINING: ContextVar[float | None] = ContextVar(
    "provider_retry_budget_remaining", default=None
)
_MIN_RETRY_TIMEOUT = 5.0


@contextmanager
def provider_retry_budget(seconds: float):
    token = _RETRY_BUDGET_REMAINING.set(max(0.0, seconds))
    try:
        yield
    finally:
        _RETRY_BUDGET_REMAINING.reset(token)


def _load_json(path: str) -> dict[str, Any]:
    with open(os.path.expandvars(os.path.expanduser(path)), encoding="utf-8") as handle:
        return json.load(handle)


def _open_json(
    request: urllib.request.Request,
    timeout: int = 20,
    max_retries: int = 2,
    retry_base_delay: float = 1.0,
    retry_max_delay: float = 10.0,
) -> dict[str, Any]:
    attempt = 0
    last_rate_limit_error: urllib.error.HTTPError | None = None
    while True:
        configured_timeout = float(timeout)
        attempt_timeout = configured_timeout
        if attempt > 0:
            remaining_budget = _RETRY_BUDGET_REMAINING.get()
            if remaining_budget is not None:
                attempt_timeout = min(attempt_timeout, remaining_budget)
        retry_timeout_capped = attempt_timeout < configured_timeout
        try:
            track_retry_time = attempt > 0 and _RETRY_BUDGET_REMAINING.get() is not None
            started_at = time.monotonic() if track_retry_time else None
            try:
                with urllib.request.urlopen(request, timeout=attempt_timeout) as response:
                    return json.load(response)
            finally:
                if started_at is not None:
                    elapsed = max(0.0, time.monotonic() - started_at)
                    remaining = _RETRY_BUDGET_REMAINING.get()
                    if remaining is not None:
                        _RETRY_BUDGET_REMAINING.set(max(0.0, remaining - elapsed))
        except urllib.error.HTTPError as error:
            if error.code != 429 or attempt >= max_retries:
                raise
            last_rate_limit_error = error
            retry_after = error.headers.get("Retry-After") if error.headers else None
            if retry_after is None:
                delay = None
            else:
                try:
                    delay = float(retry_after)
                except ValueError:
                    try:
                        parsed = parsedate_to_datetime(retry_after)
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        delay = parsed.timestamp() - time.time()
                    except (TypeError, ValueError, OverflowError):
                        delay = None
            if delay is None:
                delay = min(
                    retry_max_delay,
                    retry_base_delay * (2 ** attempt) + random.uniform(0, retry_base_delay),
                )
            else:
                if not math.isfinite(delay) or delay > retry_max_delay:
                    raise
                delay = max(retry_base_delay, delay)
            remaining_budget = _RETRY_BUDGET_REMAINING.get()
            if remaining_budget is not None:
                request_budget = remaining_budget - delay
                if request_budget < _MIN_RETRY_TIMEOUT:
                    raise
                _RETRY_BUDGET_REMAINING.set(request_budget)
            time.sleep(delay)
            attempt += 1
        except (urllib.error.URLError, TimeoutError) as error:
            is_timeout = isinstance(error, TimeoutError) or isinstance(
                getattr(error, "reason", None), TimeoutError
            )
            if (
                attempt > 0
                and last_rate_limit_error is not None
                and is_timeout
                and retry_timeout_capped
            ):
                raise last_rate_limit_error from error
            raise


def _http_json(
    url: str,
    headers: dict[str, str],
    timeout: int = 20,
    max_retries: int = 2,
    retry_base_delay: float = 1.0,
    retry_max_delay: float = 10.0,
) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=headers)
    return _open_json(
        request,
        timeout=timeout,
        max_retries=max_retries,
        retry_base_delay=retry_base_delay,
        retry_max_delay=retry_max_delay,
    )


def _post_form_json(url: str, form: dict[str, str], timeout: int = 20) -> dict[str, Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(form).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    return _open_json(request, timeout=timeout)


def _anthropic(entry: dict[str, Any]) -> dict[str, Any]:
    path = entry.get("credential_file", "~/.claude/.credentials.json")
    credentials = _load_json(path)["claudeAiOauth"]
    payload = _http_json(
        "https://api.anthropic.com/api/oauth/usage",
        {
            "Authorization": f"Bearer {credentials['accessToken']}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "token-limits-plasmoid/0.1",
        },
    )
    def window(name: str):
        value = payload.get(name)
        return {"used_percent": value.get("utilization", 0), "reset_at": value.get("resets_at")} if value else None
    return {
        "available": not any((window(name) or {}).get("used_percent", 0) >= 100 for name in ("five_hour", "seven_day")),
        "session": window("five_hour"),
        "weekly": window("seven_day"),
        "details": {"subscription": credentials.get("subscriptionType", "")},
    }


def _codex(entry: dict[str, Any]) -> dict[str, Any]:
    auth = _load_json(entry.get("credential_file", "~/.codex/auth.json"))
    tokens = auth["tokens"]
    payload = _http_json(
        entry.get("usage_url", "https://chatgpt.com/backend-api/wham/usage"),
        {
            "Authorization": f"Bearer {tokens['access_token']}",
            "ChatGPT-Account-Id": tokens["account_id"],
            "User-Agent": "token-limits-plasmoid/0.1",
        },
    )
    rate = payload.get("rate_limit") or {}
    result: dict[str, Any] = {
        "available": bool(rate.get("allowed", not rate.get("limit_reached", False))),
        "details": {"plan": payload.get("plan_type", "")},
    }
    for raw in (rate.get("primary_window"), rate.get("secondary_window")):
        if not raw:
            continue
        target = "weekly" if int(raw.get("limit_window_seconds", 0)) >= 6 * 86400 else "session"
        result[target] = {"used_percent": raw.get("used_percent", 0), "reset_at": raw.get("reset_at")}
    return result


def parse_grok_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidate = payload.get("config")
    config: dict[str, Any] = candidate if isinstance(candidate, dict) else payload
    percent = float(config.get("creditUsagePercent", 0))
    period = config.get("currentPeriod") if isinstance(config.get("currentPeriod"), dict) else {}
    reset_at = period.get("end") or config.get("billingPeriodEnd")
    return {
        "available": percent < 100,
        "weekly": {"used_percent": percent, "reset_at": reset_at},
    }


def _save_json_private(path: str, payload: dict[str, Any]) -> None:
    destination = Path(os.path.expanduser(path))
    fd, temporary = tempfile.mkstemp(prefix=destination.name + ".", dir=destination.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _grok(entry: dict[str, Any]) -> dict[str, Any]:
    path = entry.get("credential_file", "~/.grok/auth.json")
    auth = _load_json(path)
    account_key = entry.get("account_key")
    if account_key:
        record = auth[account_key]
    else:
        record = next((value for value in auth.values() if isinstance(value, dict) and value.get("key")), None)
    if not record:
        raise ValueError("no Grok OAuth account found")
    assert isinstance(record, dict)
    headers = {
        "Authorization": f"Bearer {record['key']}",
        "x-grok-client-version": entry.get("client_version", "0.2.111"),
        "Accept": "application/json",
        "User-Agent": "token-limits-plasmoid/0.1",
    }
    url = entry.get("usage_url", "https://cli-chat-proxy.grok.com/v1/billing?format=credits")
    try:
        return parse_grok_payload(_http_json(url, headers))
    except urllib.error.HTTPError as error:
        if error.code != 401 or not record.get("refresh_token"):
            raise
    refreshed = _post_form_json(
        "https://auth.x.ai/oauth2/token",
        {
            "grant_type": "refresh_token",
            "refresh_token": record["refresh_token"],
            "client_id": record.get("oidc_client_id", "b1a00492-073a-47ea-816f-4c329264a828"),
        },
    )
    record["key"] = refreshed["access_token"]
    if refreshed.get("refresh_token"):
        record["refresh_token"] = refreshed["refresh_token"]
    _save_json_private(path, auth)
    headers["Authorization"] = f"Bearer {record['key']}"
    return parse_grok_payload(_http_json(url, headers))


def _epoch_seconds(value: Any) -> Any:
    if isinstance(value, (int, float)) and value > 1_000_000_000_000:
        return value / 1000
    return value


def _used_percent(row: dict[str, Any], prefix: str) -> float:
    remaining = row.get(prefix + "_remaining_percent")
    if remaining is not None:
        return max(0.0, min(100.0, 100.0 - float(remaining)))
    total = float(row.get(prefix + "_total_count") or 0)
    used = float(row.get(prefix + "_usage_count") or 0)
    return (used / total * 100.0) if total else 0.0


def parse_minimax_payload(payload: dict[str, Any], product_name: str | None = None) -> dict[str, Any]:
    candidate = payload.get("data")
    data: dict[str, Any] = candidate if isinstance(candidate, dict) else payload
    raw_rows = data.get("model_remains") or data.get("modelRemains") or []
    rows = [item for item in raw_rows if isinstance(item, dict)] if isinstance(raw_rows, list) else []
    if not rows:
        raise ValueError("MiniMax response contains no coding-plan interval")
    def normalized_name(item: dict[str, Any]) -> str:
        return str(item.get("model_name") or item.get("modelName") or "").casefold()

    non_coding_products = {"video", "image", "music", "audio", "voice", "speech"}

    def is_non_coding_product(item: dict[str, Any]) -> bool:
        tokens = set(filter(None, re.split(r"[-_.\s]+", normalized_name(item))))
        return bool(tokens & non_coding_products)

    def is_coding_product(item: dict[str, Any]) -> bool:
        name = normalized_name(item)
        if is_non_coding_product(item):
            return False
        return (
            re.fullmatch(r"coding(?:[-_.][a-z0-9]+)*", name) is not None
            or re.fullmatch(r"minimax[-_.]m\d+(?:[-_.][a-z0-9]+)*", name) is not None
        )

    preferred_name = str(product_name or "").casefold()
    products = [item.get("model_name") or item.get("modelName") for item in rows]
    if preferred_name:
        row = next((item for item in rows if normalized_name(item) == preferred_name), None)
        if row is None:
            raise ValueError(f"MiniMax response has no product {product_name!r} (products: {products})")
    else:
        row = next((item for item in rows if normalized_name(item) == "general"), None)
        if row is None and len(rows) == 1:
            row = None if is_non_coding_product(rows[0]) else rows[0]
        if row is None and len(rows) > 1:
            coding_rows = [item for item in rows if is_coding_product(item)]
            if len(coding_rows) == 1:
                row = coding_rows[0]
    if row is None:
        raise ValueError(f"MiniMax response contains no identifiable coding-plan quota (products: {products})")
    def has_window(prefix: str) -> bool:
        return (
            row.get(f"{prefix}_remaining_percent") is not None
            or bool(row.get(f"{prefix}_total_count"))
        )

    has_session = has_window("current_interval")
    has_weekly = has_window("current_weekly")
    if not has_session and not has_weekly:
        raise ValueError("MiniMax coding-plan quota has no recognized quota windows")

    result: dict[str, Any] = {"available": True}
    if has_session:
        session_percent = _used_percent(row, "current_interval")
        result["session"] = {
            "used_percent": session_percent,
            "reset_at": _epoch_seconds(row.get("end_time") or row.get("endTime")),
        }
        result["available"] = session_percent < 100
    if has_weekly:
        weekly_percent = _used_percent(row, "current_weekly")
        result["weekly"] = {
            "used_percent": weekly_percent,
            "reset_at": _epoch_seconds(row.get("weekly_end_time") or row.get("weeklyEndTime")),
        }
        result["available"] = result["available"] and weekly_percent < 100
    return result


def _minimax(entry: dict[str, Any]) -> dict[str, Any]:
    token = ""
    if entry.get("api_key_file"):
        token = Path(os.path.expanduser(entry["api_key_file"])).read_text(encoding="utf-8").strip()
    elif entry.get("api_key_env"):
        token = os.environ.get(entry["api_key_env"], "")
        if not token and entry.get("env_file"):
            variable = entry["api_key_env"]
            for line in Path(os.path.expanduser(entry["env_file"])).read_text(encoding="utf-8").splitlines():
                candidate = line.strip()
                if candidate.startswith("export "):
                    candidate = candidate[7:].lstrip()
                if candidate.startswith(variable + "="):
                    token = candidate.split("=", 1)[1].strip().strip('"\'')
                    break
    if not token:
        raise ValueError("MiniMax requires api_key_file or api_key_env")
    base = entry.get("base_url", "https://api.minimax.io").rstrip("/")
    payload = _http_json(
        entry.get("usage_url", base + "/v1/api/openplatform/coding_plan/remains"),
        {"Authorization": f"Bearer {token}", "User-Agent": "token-limits-plasmoid/0.1"},
    )
    return parse_minimax_payload(payload, entry.get("product_name"))


def _command(entry: dict[str, Any]) -> dict[str, Any]:
    command = entry.get("command")
    if not command:
        raise ValueError("command adapter requires 'command'")
    argv = shlex.split(command) if isinstance(command, str) else list(command)
    completed = subprocess.run(argv, check=True, capture_output=True, text=True, timeout=int(entry.get("timeout", 20)))
    return json.loads(completed.stdout)


def _json_url(entry: dict[str, Any]) -> dict[str, Any]:
    headers = dict(entry.get("headers", {}))
    token_file = entry.get("bearer_token_file")
    if token_file:
        headers["Authorization"] = "Bearer " + Path(os.path.expanduser(token_file)).read_text().strip()
    return _http_json(entry["url"], headers, int(entry.get("timeout", 20)))


def fetch_account(entry: dict[str, Any]) -> dict[str, Any]:
    adapter = entry.get("adapter", entry.get("provider"))
    if adapter == "fixture":
        return dict(entry.get("fixture", {}))
    if adapter == "anthropic":
        return _anthropic(entry)
    if adapter == "codex":
        return _codex(entry)
    if adapter == "grok":
        return _grok(entry)
    if adapter == "minimax":
        return _minimax(entry)
    if adapter == "command":
        return _command(entry)
    if adapter == "json_url":
        return _json_url(entry)
    raise ValueError(f"unknown adapter: {adapter}")
