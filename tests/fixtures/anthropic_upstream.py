# Extracted verbatim by AST from macmini ~/.hermes/hermes-agent/agent/account_usage.py
# and ~/.hermes/plugins/quota/quota_providers/builtin.py.
# Offline regression fixture; no credentials or captured user payloads.
from __future__ import annotations
from typing import Any, Optional
from dataclasses import dataclass
from datetime import datetime, timezone

def _utc_now() -> datetime:
    return datetime.now(timezone.utc)

@dataclass(frozen=True)
class AccountUsageWindow:
    label: str
    used_percent: Optional[float] = None
    reset_at: Optional[datetime] = None
    detail: Optional[str] = None

@dataclass(frozen=True)
class AccountUsageSnapshot:
    provider: str
    source: str
    fetched_at: datetime
    title: str = "Account limits"
    plan: Optional[str] = None
    windows: tuple[AccountUsageWindow, ...] = ()
    details: tuple[str, ...] = ()
    unavailable_reason: Optional[str] = None
    # Exact decoded provider response body (no headers/credentials) for integrations that need
    # fields Hermes does not normalize yet. Only populated by providers that fetch a JSON body.
    raw: Optional[dict] = None

    @property
    def available(self) -> bool:
        return bool(self.windows or self.details) and not self.unavailable_reason

def _snapshot(provider: str, source: str, windows: list, details: list, **kw: Any) -> AccountUsageSnapshot:
    return AccountUsageSnapshot(provider=provider, source=source, fetched_at=_utc_now(), windows=tuple(windows), details=tuple(details), **kw)

def _parse_dt(value: Any) -> Optional[datetime]:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if not isinstance(value, str) or not (text := value.strip()):
        return None
    text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return None

def _is_num(v: Any) -> TypeGuard[float]:
    return isinstance(v, (int, float))

def _get_json(url: str, headers: dict[str, str], *, timeout: float) -> dict:
    with httpx.Client(timeout=timeout) as client:
        response = client.get(url, headers=headers)
        response.raise_for_status()
    return response.json() or {}

def _usage_windows(
    source: dict, mapping: tuple[tuple[str, str], ...], used_key: str, reset_key: str, *, fraction: bool = False
) -> list[AccountUsageWindow]:
    """Build windows from ``source[key][used_key]``; ``fraction`` scales values <= 1 to percent."""
    windows: list[AccountUsageWindow] = []
    for key, label in mapping:
        window = source.get(key) or {}
        used = window.get(used_key)
        if used is None:
            continue
        used = float(used)
        if fraction and used <= 1:
            used *= 100
        windows.append(AccountUsageWindow(label=label, used_percent=used, reset_at=_parse_dt(window.get(reset_key))))
    return windows

def _fetch_anthropic_account_usage(
    base_url: Optional[str] = None, api_key: Optional[str] = None
) -> Optional[AccountUsageSnapshot]:
    token = (resolve_anthropic_token() or "").strip()
    if not token:
        return None
    if not _is_oauth_token(token):
        return _snapshot("anthropic", "oauth_usage_api", [], [],
                         unavailable_reason="Anthropic account limits are only available for OAuth-backed Claude accounts.")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "Content-Type": "application/json",
               "anthropic-beta": "oauth-2025-04-20", "User-Agent": "claude-code/2.1.0"}
    payload = _get_json("https://api.anthropic.com/api/oauth/usage", headers, timeout=15.0)
    windows = _usage_windows(
        payload, (("five_hour", "Current session"), ("seven_day", "Current week"), ("seven_day_opus", "Opus week"),
                  ("seven_day_sonnet", "Sonnet week")), "utilization", "resets_at", fraction=True,
    )
    details: list[str] = []
    extra = payload.get("extra_usage") or {}
    used_credits, monthly_limit = extra.get("used_credits"), extra.get("monthly_limit")
    if extra.get("is_enabled") and _is_num(used_credits) and _is_num(monthly_limit):
        details.append(f"Extra usage: {used_credits:.2f} / {monthly_limit:.2f} {extra.get('currency') or 'USD'}")
    return _snapshot("anthropic", "oauth_usage_api", windows, details)

def _snapshot_to_result(snapshot) -> QuotaResult:
    provider = getattr(snapshot, "provider", "unknown")
    windows = []
    for w in getattr(snapshot, "windows", ()) or ():
        used = getattr(w, "used_percent", None)
        reset = getattr(w, "reset_at", None)
        reset_iso = None
        if reset is not None:
            from datetime import datetime, timezone

            if reset.tzinfo is None:
                reset = reset.replace(tzinfo=timezone.utc)
            reset_iso = reset.isoformat()
        windows.append(
            QuotaWindow(
                label=str(getattr(w, "label", "") or "window"),
                used_percent=float(used) if used is not None else None,
                reset_at=reset_iso,
            )
        )
    return QuotaResult(
        label=str(provider),
        windows=windows,
        plan=getattr(snapshot, "plan", None),
        unavailable_reason=getattr(snapshot, "unavailable_reason", None),
        details=[str(d) for d in (getattr(snapshot, "details", ()) or ())],
    )

def _core_fetch_account_usage(provider_id: str):
    """Thin seam over the core dispatcher (kept importable/mockable for tests)."""
    from agent.account_usage import fetch_account_usage

    return fetch_account_usage(provider_id)

def _core_anthropic_token() -> Optional[str]:
    """Resolvable Anthropic token per core auth, or None. Never raises."""
    try:
        from agent.anthropic_credentials import resolve_anthropic_token

        token = (resolve_anthropic_token() or "").strip()
        return token or None
    except Exception:  # noqa: BLE001 - standalone install / locked store
        return None

def _fetch_anthropic() -> QuotaResult:
    """Anthropic adapter: like the generic one, but a ``None`` snapshot is
    diagnosed — no resolvable token means ``no-credentials``; a token that
    still produced no snapshot means the vendor call failed (``fetch-error``).
    The core collapses every failure to ``None`` (fail-open), so this extra
    read is the only way to keep the reason honest."""
    try:
        snap = _core_fetch_account_usage("anthropic")
    except ImportError:
        return build_unavailable("anthropic", "fetcher-unavailable")
    except Exception:
        return build_unavailable("anthropic", "fetch-error")
    if snap is None:
        reason = "fetch-error" if _core_anthropic_token() else "no-credentials"
        return build_unavailable("anthropic", reason)
    return _snapshot_to_result(snap)
