# Extracted verbatim by AST from macmini ~/.hermes/plugins/quota.
# Offline regression fixture; no credentials or captured user payloads.
from __future__ import annotations
from typing import Any, Optional
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json, os, threading, time

def _fetch_codex_with_models() -> QuotaResult:
    try:
        from agent.account_usage import _resolve_codex_usage_credentials
        try:
            from agent.account_usage import _codex_backend_urls
        except ImportError:
            _codex_backend_urls = None
        try:
            from agent.account_usage import _resolve_codex_usage_url
        except ImportError:
            _resolve_codex_usage_url = None
        if _codex_backend_urls is None and _resolve_codex_usage_url is None:
            raise ImportError("no Codex usage URL helper")
    except Exception:
        return build_unavailable("openai-codex", "fetcher-unavailable")

    import httpx

    try:
        token, base_url, account_id = _resolve_codex_usage_credentials(None, None)
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "codex-cli",
        }
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        if _codex_backend_urls is not None:
            usage_url = _codex_backend_urls(base_url)[0]
        else:
            usage_url = _resolve_codex_usage_url(base_url)
        with httpx.Client(timeout=15.0) as client:
            response = client.get(usage_url, headers=headers)
            response.raise_for_status()
        payload = response.json() or {}
    except Exception:
        return build_unavailable("openai-codex", "fetch-error")

    from datetime import datetime, timezone

    def _iso(ts):
        if not isinstance(ts, (int, float)):
            return None
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).isoformat()

    def _window(raw: dict, label: str) -> Optional[QuotaWindow]:
        used = raw.get("used_percent")
        if not isinstance(used, (int, float)) or isinstance(used, bool):
            return None
        return QuotaWindow(
            label=label,
            used_percent=float(used),
            reset_at=_iso(raw.get("reset_at")),
        )

    windows: list[QuotaWindow] = []
    rate_limit = payload.get("rate_limit") or {}
    for key, label in (("primary_window", "Session"), ("secondary_window", "Weekly")):
        w = _window(rate_limit.get(key) or {}, label)
        if w is not None:
            windows.append(w)

    # Per-model limits (research-preview models like Codex Spark).
    for extra in payload.get("additional_rate_limits") or []:
        if not isinstance(extra, dict):
            continue
        model_name = str(extra.get("limit_name") or "").strip()
        if not model_name:
            continue
        short = model_name.replace("GPT-", "").replace("-Codex-", " Codex ")
        inner = extra.get("rate_limit") or {}
        for key, label in (("primary_window", "5h"), ("secondary_window", "Weekly")):
            w = _window(inner.get(key) or {}, f"{short} · {label}")
            if w is not None:
                windows.append(w)

    details: list[str] = []
    reset_credits = payload.get("rate_limit_reset_credits") or {}
    banked = reset_credits.get("available_count")
    if isinstance(banked, (int, float)) and int(banked) > 0:
        count = int(banked)
        plural = "s" if count != 1 else ""
        details.append(f"You have {count} reset{plural} banked - use /usage reset to activate")
    credits = payload.get("credits") or {}
    if credits.get("has_credits"):
        balance = credits.get("balance")
        if isinstance(balance, (int, float)):
            details.append(f"Credits balance: ${float(balance):.2f}")
        elif credits.get("unlimited"):
            details.append("Credits balance: unlimited")

    plan = str(payload.get("plan_type") or "").strip()
    plan = plan.title() if plan else None
    if not windows and not details:
        return build_unavailable("openai-codex", "no-data")
    return QuotaResult(
        label="openai-codex",
        windows=windows,
        plan=plan,
        unavailable_reason=None,
        details=details,
    )

@dataclass
class QuotaWindow:
    """One billing window (session / weekly / monthly) for a provider."""

    label: str
    used_percent: Optional[float] = None  # provider-reported *used* fraction 0..100
    reset_at: Optional[str] = None  # ISO-8601 UTC timestamp

    def remaining_pct(self) -> Optional[int]:
        if self.used_percent is None:
            return None
        try:
            rem = 100.0 - float(self.used_percent)
        except (TypeError, ValueError):
            return None
        rem = max(0.0, min(100.0, rem))
        return int(round(rem))

@dataclass
class QuotaResult:
    """Normalized quota for one provider, ready to cache."""

    label: str
    windows: list[QuotaWindow] = field(default_factory=list)
    plan: Optional[str] = None
    unavailable_reason: Optional[str] = None
    # Extra provider facts shown under the windows in the widget (e.g. Codex
    # "Credits balance: $12.50", "You have 2 resets banked").
    details: list[str] = field(default_factory=list)

    def has_data(self) -> bool:
        return (bool(self.windows) or bool(self.details)) and self.unavailable_reason is None

def build_unavailable(label: str, reason: str) -> QuotaResult:
    return QuotaResult(label=label, windows=[], plan=None, unavailable_reason=reason)

def _result_to_record(res: QuotaResult) -> dict[str, Any]:
    return {
        "label": res.label,
        "plan": res.plan,
        "unavailable_reason": res.unavailable_reason,
        "details": list(res.details or []),
        "windows": [
            {"label": w.label, "used_percent": w.used_percent, "reset_at": w.reset_at}
            for w in res.windows
        ],
    }

def _unavailable_record(provider_id: str, reason: str) -> dict[str, Any]:
    return {
        "label": provider_id,
        "plan": None,
        "unavailable_reason": reason,
        "details": [],
        "windows": [],
    }

def _fetch_one(provider_id: str, fetcher: Any) -> dict[str, Any]:
    """Run one fetcher. Fail-open by contract — never raises."""
    try:
        res = fetcher()
    except Exception:
        logger.debug("quota_cache ▸ fetcher %s crashed", provider_id, exc_info=True)
        return _unavailable_record(provider_id, "fetch-error")
    if res is None:
        return _unavailable_record(provider_id, "no-data")
    return _result_to_record(res)

def refresh_quota_cache(*, budget: Optional[float] = None) -> dict[str, Any]:
    """Run every registered provider fetcher concurrently and write the cache.

    Bounded by ``budget`` seconds (``REFRESH_BUDGET_S`` default) and run on
    daemon threads, so one hung provider can neither stretch the call nor hold
    the short-lived CLI process open: whatever finished is written, the rest is
    recorded as ``timeout`` and keeps its previous value. Fail-open per
    provider: a fetcher that raises, returns nothing, or misses the deadline
    leaves an ``unavailable_reason`` record rather than aborting the sweep.
    Returns the cache dict that was written.
    """
    budget_s = REFRESH_BUDGET_S if budget is None else max(0.0, float(budget))
    items = list(PROVIDER_FETCHERS.items())
    results: dict[str, Any] = {}
    lock = threading.Lock()

    def _worker(pid: str, fetcher: Any, event: threading.Event) -> None:
        record = _fetch_one(pid, fetcher)
        with lock:
            results[pid] = record
        event.set()

    events: dict[str, threading.Event] = {}
    for provider_id, fetcher in items:
        event = threading.Event()
        events[provider_id] = event
        threading.Thread(
            target=_worker,
            args=(provider_id, fetcher, event),
            name=f"quota-fetch-{provider_id}",
            daemon=True,
        ).start()

    deadline = time.monotonic() + budget_s
    for event in events.values():
        event.wait(max(0.0, deadline - time.monotonic()))

    with lock:
        providers: dict[str, Any] = {
            provider_id: results.get(provider_id)
            or _unavailable_record(provider_id, "timeout")
            for provider_id, _fetcher in items
        }

    cache = {"fetched_at": datetime.now(timezone.utc).isoformat(), "providers": providers}

    try:
        with _CACHE_LOCK:
            path = _cache_path()
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cache, fh, indent=2, sort_keys=True)
            os.replace(tmp, path)
    except Exception:
        logger.debug("quota_cache ▸ write failed", exc_info=True)

    return cache
