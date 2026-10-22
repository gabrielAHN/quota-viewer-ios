"""Per-account Claude quota for a gateway's named Claude providers.

Hermes Desktop can list one provider per Claude subscription: a ``providers.<slug>``
entry on ``api.anthropic.com`` whose key comes from ``key_cmd`` (for example a
personal plan and a team seat side by side). The official quota plugin reads only
the gateway's single built-in ``anthropic`` login, so those rows never had quota.
This module reads each account with its own ``key_cmd`` token from Anthropic's
OAuth usage endpoint and writes ``$HERMES_HOME/provider_quota_accounts.json``, which
the dashboard plugin merges next to the official cache.

It runs only in the plugin's fresh refresh subprocess (Hermes importable), never in
the long-lived dashboard process. Tokens stay in memory: they are sent only to
api.anthropic.com and never written, logged or returned.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlparse

CACHE_NAME = "provider_quota_accounts.json"
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
_TIMEOUT_S = 15.0
# The plan badge (Team / Max 5x / ...) changes rarely; re-read it a few times a day.
_PLAN_TTL_S = 6 * 60 * 60
_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json",
    "anthropic-beta": "oauth-2025-04-20",
    "User-Agent": "claude-code/2.1.0",
}
# Same windows and metadata as local-quotas.sh's Claude reader.
_WINDOWS = (
    ("five_hour", "Current session", "account", 18000),
    ("seven_day", "Current week", "account", 604800),
    ("seven_day_opus", "Opus week", "opus", 604800),
    ("seven_day_sonnet", "Sonnet week", "sonnet", 604800),
)


def named_accounts(config: Any) -> list[tuple[str, str, str]]:
    """``(slug, label, key_cmd)`` per enabled named provider on api.anthropic.com with a key_cmd."""
    providers = config.get("providers") if isinstance(config, dict) else None
    accounts = []
    for slug, row in (providers.items() if isinstance(providers, dict) else ()):
        if not isinstance(row, dict) or row.get("enabled") is False:
            continue
        api = str(row.get("api") or row.get("base_url") or row.get("url") or "")
        key_cmd = str(row.get("key_cmd") or "").strip()
        if key_cmd and urlparse(api).hostname == "api.anthropic.com":
            accounts.append((str(slug), str(row.get("name") or slug), key_cmd))
    return accounts


def hidden_builtins(config: Any, accounts: list) -> list[str]:
    """Built-in rows the accounts replace: once the gateway hides ``anthropic`` from its
    picker, that row is the gateway's default login again under a second name."""
    catalog = config.get("model_catalog") if isinstance(config, dict) else None
    excluded = catalog.get("excluded_providers") if isinstance(catalog, dict) else None
    return ["anthropic"] if accounts and isinstance(excluded, list) and "anthropic" in excluded else []


def is_oauth_token(token: str) -> bool:
    """Hermes's OAuth shape test: Console keys use x-api-key and have no plan windows."""
    if not token or token.startswith(("sk-ant-api", "sk-ant-usr", "sk-ant-svc", "sk-ant-admin")):
        return False
    return token.startswith(("sk-ant-", "eyJ", "cc-"))


def mint_token(key_cmd: str, label: str) -> str:
    """Run the account's key_cmd the way Hermes does (bare token, or JSON ``access_token``)."""
    try:
        from agent.command_token_source import build_command_token_provider
    except Exception:
        build_command_token_provider = None
    if build_command_token_provider is not None:
        source = build_command_token_provider(key_cmd, label)
        return (source() if source is not None else "").strip()
    out = subprocess.run(key_cmd, shell=True, capture_output=True, text=True, timeout=_TIMEOUT_S,
                         stdin=subprocess.DEVNULL, check=True).stdout.strip()
    if out.startswith("{"):
        token = json.loads(out).get("access_token")
        return token.strip() if isinstance(token, str) else ""
    return "" if "\n" in out else out


def _get_json(url: str, token: str) -> Any:
    request = urllib.request.Request(url, headers={**_HEADERS, "Authorization": f"Bearer {token}"}, method="GET")
    with urllib.request.urlopen(request, timeout=_TIMEOUT_S) as resp:
        return json.loads(resp.read())


def _window(label: str, used: float, reset: Any, seconds: Optional[int], scope: str, window_id: str) -> dict:
    if isinstance(reset, str) and reset.endswith("Z"):
        reset = reset[:-1] + "+00:00"
    return {"label": label, "used_percent": used, "reset_at": reset if isinstance(reset, str) and reset else None,
            "window_seconds": seconds, "scope": scope, "window_id": window_id}


def parse_usage(payload: Any) -> tuple[list[dict], list[str]]:
    """Usage windows (official-cache shape plus window metadata) and details, as local-quotas.sh reads them."""
    payload = payload if isinstance(payload, dict) else {}
    windows = []
    for key, label, scope, seconds in _WINDOWS:
        window = payload.get(key) or {}
        util = window.get("utilization") if isinstance(window, dict) else None
        if util is None or isinstance(util, bool):
            continue
        try:
            used = float(util)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(used):
            continue
        # A percent (0-100), never a fraction: a fresh 1% session is not "used up".
        windows.append(_window(label, max(0.0, min(100.0, used)), window.get("resets_at"), seconds, scope, key))
    models = {w["scope"] for w in windows if w["scope"] != "account"}
    limits = payload.get("limits")
    for limit in limits if isinstance(limits, list) else []:
        if not isinstance(limit, dict) or limit.get("kind") != "weekly_scoped":
            continue
        percent = limit.get("percent")
        if not isinstance(percent, (int, float)) or isinstance(percent, bool) or not math.isfinite(percent):
            continue
        scope = limit.get("scope")
        model = scope.get("model") if isinstance(scope, dict) else None
        if not isinstance(model, dict):
            continue
        name = next((v.strip() for v in (model.get("display_name"), model.get("id"))
                     if isinstance(v, str) and v.strip()), "")
        if not name or name.lower() in models:
            continue
        models.add(name.lower())
        reset = limit.get("resets_at")
        try:
            datetime.fromisoformat(reset[:-1] + "+00:00" if reset.endswith("Z") else reset)
        except (AttributeError, TypeError, ValueError):
            reset = None
        windows.append(_window(f"{name} week", max(0.0, min(100.0, float(percent))), reset,
                               604800 if limit.get("group") == "weekly" else None,
                               f"model:{name}", f"weekly_scoped:{name}"))
    details = []
    extra = payload.get("extra_usage") or {}
    if isinstance(extra, dict) and extra.get("is_enabled"):
        used_credits, monthly_limit = extra.get("used_credits"), extra.get("monthly_limit")
        currency = extra.get("currency") or "USD"
        if all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (used_credits, monthly_limit)):
            details.append(f"Extra usage: {used_credits:.2f} / {monthly_limit:.2f} {currency}")
    return windows, details


def plan_label(profile: Any) -> Optional[str]:
    """Short plan badge from /api/oauth/profile: Team, Enterprise, Max 5x/20x, Pro."""
    org = profile.get("organization") if isinstance(profile, dict) else None
    if not isinstance(org, dict):
        return None
    kind = str(org.get("organization_type") or "").lower()
    tier = str(org.get("rate_limit_tier") or "").lower()
    if "team" in kind:
        return "Team"
    if "enterprise" in kind:
        return "Enterprise"
    if "max" in kind or "max" in tier:
        for multiple in ("20x", "5x"):
            if multiple in tier:
                return f"Max {multiple}"
        return "Max"
    if "pro" in kind or "pro" in tier:
        return "Pro"
    return None


def read_cache(home: Path) -> dict[str, Any]:
    try:
        with open(home / CACHE_NAME, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("providers"), dict):
            return data
    except Exception:
        pass
    return {"fetched_at": None, "hide": [], "providers": {}}


def fetch_account(label: str, key_cmd: str, previous: Optional[dict] = None, *,
                  mint: Callable[[str, str], str] = mint_token,
                  get_json: Callable[[str, str], Any] = _get_json) -> dict[str, Any]:
    """One account's record in the official-cache shape, marked as a Claude account."""
    now = time.time()
    record: dict[str, Any] = {"label": label, "plan": None, "unavailable_reason": None, "details": [],
                              "windows": [], "family": "anthropic", "account": True,
                              "fetched_at": datetime.now(timezone.utc).isoformat()}
    try:
        token = mint(key_cmd, label)
    except Exception:
        token = ""
    if not token:
        record["unavailable_reason"] = "no-credentials"
        return record
    if not is_oauth_token(token):
        record["unavailable_reason"] = "Account limits are only available for OAuth-backed Claude accounts."
        return record
    try:
        record["windows"], record["details"] = parse_usage(get_json(_USAGE_URL, token))
    except urllib.error.HTTPError as exc:
        record["unavailable_reason"] = "auth-failed" if exc.code in (401, 403) else f"http-{exc.code}"
    except Exception as exc:  # noqa: BLE001 - one account must never break the sweep
        record["unavailable_reason"] = f"fetch-error:{type(exc).__name__}"
    previous = previous if isinstance(previous, dict) else {}
    checked = previous.get("plan_checked_at")
    if previous.get("plan") and isinstance(checked, (int, float)) and now - checked < _PLAN_TTL_S:
        record["plan"], record["plan_checked_at"] = previous["plan"], checked
    else:
        try:
            record["plan"], record["plan_checked_at"] = plan_label(get_json(_PROFILE_URL, token)), now
        except Exception:  # noqa: BLE001 - the badge is optional
            record["plan"] = previous.get("plan")
    return record


def refresh_accounts_cache(home: Optional[Path] = None, config: Optional[dict] = None, **fetch_kwargs) -> dict:
    """Re-read every named Claude account in parallel and write the accounts cache."""
    home = Path(home or os.environ.get("HERMES_HOME") or Path.home() / ".hermes")
    if config is None:
        from hermes_cli.config import load_config
        config = load_config()
    accounts = named_accounts(config)
    previous = read_cache(home).get("providers") or {}
    records: dict[str, dict] = {}

    def read(slug: str, label: str, key_cmd: str) -> None:
        records[slug] = fetch_account(label, key_cmd, previous.get(slug), **fetch_kwargs)
    threads = [threading.Thread(target=read, args=account, daemon=True) for account in accounts]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3 * _TIMEOUT_S)
    data = {"fetched_at": datetime.now(timezone.utc).isoformat(), "hide": hidden_builtins(config, accounts),
            "providers": {slug: records[slug] for slug, _, _ in accounts if slug in records}}
    tmp = home / (CACHE_NAME + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, home / CACHE_NAME)
    return data
