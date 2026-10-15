# Extracted verbatim by AST from macmini ~/.hermes/plugins/quota.
# Offline regression fixture; no credentials or captured user payloads.
from __future__ import annotations
from typing import Any, Callable, Optional
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
import functools, json, queue, threading, time, urllib.error, urllib.request

class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse redirects so a bearer credential is never replayed to another host.

    ``urllib``'s default ``HTTPRedirectHandler`` re-sends the ``Authorization``
    and ``Cookie`` headers to the redirect target, so a 302 from a provider
    endpoint would hand the credential to whichever host named in ``Location``.
    Every authenticated provider request must go through :func:`urlopen_no_redirect`.
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ARG002
        return None

urlopen_no_redirect = urllib.request.build_opener(NoRedirectHandler()).open

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
class AccountBalance:
    """Account money, not a key cap or a percentage denominator.

    Decimal strings preserve the precision returned by the provider.
    """

    currency: str
    total_balance: str
    granted_balance: Optional[str] = None
    topped_up_balance: Optional[str] = None

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
    account_balances: list[AccountBalance] = field(default_factory=list)
    api_calls_available: Optional[bool] = None

    def has_data(self) -> bool:
        return (bool(self.windows) or bool(self.details) or bool(self.account_balances)) and self.unavailable_reason is None

def build_unavailable(label: str, reason: str) -> QuotaResult:
    return QuotaResult(label=label, windows=[], plan=None, unavailable_reason=reason)

PROVIDER_FETCHERS: dict[str, Callable[[], object]] = {}

def register(provider_id: str):
    """Register a fetcher, enforcing the fail-open contract at the seam.

    ``add-provider.md``: "Fail-open, never raise. Wrap the whole body; every
    failure path returns ``build_unavailable(...)``". Nine of fifteen fetchers
    did that; the other six could raise out of the registered callable -- a
    DNS failure in ``gemini`` escaped as ``OSError``, a non-numeric
    ``expiry_date`` as ``ValueError``. ``quota_cache._fetch_one`` caught them,
    so the sweep survived, but the provider recorded the generic
    ``fetch-error`` rather than a truthful reason, which the same guide
    forbids.

    Guarding here rather than in each body keeps the existing error handling
    readable and makes the contract hold for a provider added later. The
    reason code stays ``fetch-error`` so no currently-correct mapping changes.
    """

    def _wrap(fn: Callable[[], object]):
        @functools.wraps(fn)
        def _fail_open() -> QuotaResult:
            try:
                return fn()  # type: ignore[return-value]
            except Exception:  # noqa: BLE001 - fail-open by contract
                return build_unavailable(provider_id, "fetch-error")

        PROVIDER_FETCHERS[provider_id] = _fail_open
        return fn

    return _wrap

_BASE_URL = "https://openrouter.ai/api/v1"

_FETCH_BUDGET_S = 10.0

_HTTP_TIMEOUT_S = 7.0

def _env_value(name):
    # Core handles the active profile and deliberate .env edits over stale
    # inherited values. Do not scan unrelated environment variables ourselves.
    from hermes_cli.config import get_env_value_prefer_dotenv

    return get_env_value_prefer_dotenv(name)

def _resolve_credentials() -> list[tuple[str, str]]:
    """Read native credentials + saved pool without selecting/rotating it."""
    candidates = []
    resolution_failed = False
    try:
        from hermes_cli.runtime_provider import resolve_runtime_provider

        # Pin the quota endpoint, bypassing runtime pool load/select, which can
        # seed/persist credentials and alter rotation/cooldown state. Never use
        # the currently selected model's custom endpoint for billing requests.
        primary = resolve_runtime_provider(
            requested="openrouter", explicit_base_url=_BASE_URL,
            explicit_api_key=_env_value("OPENROUTER_API_KEY") or None,
        ).get("api_key")
        candidates.append(("native", primary))
    except Exception:
        resolution_failed = True
    try:
        from hermes_cli.auth import read_credential_pool

        for entry in read_credential_pool("openrouter"):
            if not isinstance(entry, dict):
                continue
            source = entry.get("source")
            if isinstance(source, str) and source.startswith("env:"):
                # Env-backed rows can be secret-free on disk; do not resurrect
                # a stale stored token if that environment reference is absent.
                try:
                    secret = _env_value(source.split(":", 1)[1].strip())
                except Exception:
                    resolution_failed = True
                    continue
                candidates.append(("environment", secret))
            else:
                candidates.append(("saved", entry.get("access_token") or entry.get("runtime_api_key")))
    except Exception:
        resolution_failed = True
    result, seen = [], set()
    for source, secret in candidates:
        if not isinstance(secret, str) or not secret.strip():
            continue
        secret = secret.strip()
        if secret in seen:
            continue
        seen.add(secret)  # Equality only, private: no IDs, hashes or key suffixes.
        result.append((f"Key {len(result) + 1} ({source})", secret))
    if not result and resolution_failed:
        raise RuntimeError("credential-resolution-failed") from None
    return result

_urlopen = urlopen_no_redirect

def _get_json(url, secret):
    """Return (payload, safe reason), never response bodies or exception text."""
    try:
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {secret}", "Accept": "application/json",
        })
        with _urlopen(req, timeout=_HTTP_TIMEOUT_S) as response:
            body = response.read(1024 * 1024 + 1)
        if len(body) > 1024 * 1024:
            return None, "response-too-large"
        try:
            return json.loads(body), None
        except (ValueError, UnicodeError):
            return None, "bad-json"
    except urllib.error.HTTPError as exc:
        code = exc.code
        if exc.fp is not None:
            exc.close()
        return None, "auth-failed" if code == 401 else f"http-{code}"
    except TimeoutError:
        return None, "timeout"
    except Exception:
        return None, "fetch-error"

def _amount(value):
    """Finite bounded decimal; do not coerce booleans or extreme exponents."""
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        return None
    text = str(value)
    if len(text) > 64:
        return None
    try:
        number = Decimal(text)
        if not number.is_finite() or number.copy_abs() > Decimal("1e15") or (number and number.adjusted() < -15):
            return None
        return number
    except (InvalidOperation, ValueError):
        return None

def _key_result(label, secret):
    try:
        payload, error = _get_json(f"{_BASE_URL}/key", secret)
        if error:
            return [], [f"{label}: unavailable ({error})"]
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return [], [f"{label}: unavailable (parse-pending)"]
        details, windows = [], []
        limit, left = _amount(data.get("limit")), _amount(data.get("limit_remaining"))
        if limit is not None and limit >= 0:
            text = f"{label}: key cap USD {limit:.2f}"
            if left is not None:
                text += f"; remaining USD {left:.2f}"
                if left <= 0:
                    text += " (key cap exhausted; account wallet is separate)"
                if limit > 0 and 0 <= left <= limit:
                    windows.append(QuotaWindow(
                        label=f"{label} quota", used_percent=float((limit - left) / limit * 100),
                    ))
            reset = data.get("limit_reset")
            if reset in ("daily", "weekly", "monthly"):
                text += f"; resets {reset}"
            details.append(text)
        elif "limit" in data and data["limit"] is None:
            details.append(f"{label}: no key-specific cap (account wallet still applies)")
        for field, name in (("usage", "total"), ("usage_daily", "today UTC"),
                            ("usage_weekly", "this week UTC"), ("usage_monthly", "this month UTC")):
            value = _amount(data.get(field))
            if value is not None:
                details.append(f"{label}: usage {name} USD {value:.4f}")
        if not details:
            details.append(f"{label}: unavailable (parse-pending)")
        return windows, details
    except Exception:
        return [], [f"{label}: unavailable (fetch-error)"]

def _wallet_result(label, secret):
    try:
        payload, error = _get_json(f"{_BASE_URL}/credits", secret)
        data = payload.get("data") if isinstance(payload, dict) else None
        total = _amount(data.get("total_credits")) if isinstance(data, dict) else None
        used = _amount(data.get("total_usage")) if isinstance(data, dict) else None
        if error or total is None or used is None:
            return False, f"Account credits unavailable via {label} ({error or 'parse-pending'})"
        return True, (
            f"Account wallet via {label}: USD {total - used:.2f} remaining "
            "(shared account credits, shown once; not summed across keys)"
        )
    except Exception:
        return False, f"Account credits unavailable via {label} (fetch-error)"

@register("openrouter")
def fetch_openrouter_quota() -> QuotaResult:
    try:
        credentials = _resolve_credentials()
        if not credentials:
            return build_unavailable("openrouter", "no-credentials")
        details = [
            f"Locally configured credentials: {len(credentials)} (deduplicated); not an inventory of all account keys",
            "Management API key listing not queried; credentials from Hermes native auth and saved pool only",
        ]
        completed = queue.Queue()
        deadline = time.monotonic() + _FETCH_BUDGET_S

        def worker(index, fetcher, label, secret):
            completed.put((index, fetcher(label, secret)))

        # Key failures and wallet failures are independent, including a hung
        # wallet. Daemon workers never mutate returned results or the cache.
        for index, (label, secret) in enumerate(credentials):
            threading.Thread(target=worker, args=(index, _key_result, label, secret), daemon=True).start()
        primary_label, primary_secret = credentials[0]
        threading.Thread(target=worker, args=(-1, _wallet_result, primary_label, primary_secret), daemon=True).start()
        results = {}
        while len(results) < len(credentials) + 1:
            try:
                index, result = completed.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                break
            results[index] = result
        has_data, wallet = results.get(-1, (False, f"Account credits unavailable via {primary_label} (timeout)"))
        details.append(wallet)
        details.append("Account membership of other keys is unverified; wallet is not a total across accounts")
        windows = []
        for index, (label, _) in enumerate(credentials):
            ws, ds = results.get(index, ([], [f"{label}: unavailable (timeout)"]))
            has_data = has_data or bool(ws) or any(not d.startswith(f"{label}: unavailable (") for d in ds)
            windows.extend(ws)
            details.extend(ds)
        return QuotaResult(label="openrouter", windows=windows, details=details,
                           unavailable_reason=None if has_data else "no-data")
    except Exception:
        return build_unavailable("openrouter", "fetch-error")

def _result_to_record(res: QuotaResult) -> dict[str, Any]:
    return {
        "label": res.label,
        "plan": res.plan,
        "unavailable_reason": res.unavailable_reason,
        "details": list(res.details or []),
        "account_balances": [
            {"currency": b.currency, "total_balance": b.total_balance,
             "granted_balance": b.granted_balance, "topped_up_balance": b.topped_up_balance}
            for b in res.account_balances
        ],
        "api_calls_available": res.api_calls_available,
        "windows": [
            {"label": w.label, "used_percent": w.used_percent, "reset_at": w.reset_at}
            for w in res.windows
        ],
    }
