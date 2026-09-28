#!/usr/bin/env bash
# The "Local" source — quotas for the providers authenticated on THIS Mac,
# queried from each provider's own usage API using the credentials already here.
# It never talks to a gateway, only to the providers, so it works with no gateway
# at all (the menu-bar app's "Local" option).
#
# Sources, all read locally and best-effort (a provider is shown only when its
# credential exists here):
#   • Claude  — ~/.claude/.credentials.json or the login Keychain
#               ("Claude Code-credentials") → api.anthropic.com/api/oauth/usage
#   • Codex   — the freshest of ~/.codex/auth.json or a Goose
#               chatgpt_codex/tokens.json → chatgpt.com/backend-api/wham/usage
#   • OpenRouter — OPENROUTER_API_KEY from the env or ~/.hermes/.env
#               → openrouter.ai/api/v1/credits
#   • opencode — the OpenRouter key opencode itself authenticated with
#               (~/.local/share/opencode/auth.json) → the same credits API,
#               shown as its own row so it reads like the Hermes source's
#               OpenRouter entry
#
# Portable: system python3 + security(1) only (no Hermes venv / httpx). Prints
# the same QuotaPayload JSON the gateway plugin returns on stdout; on failure
# (no local provider credentials at all) prints a one-line reason to stderr and
# exits non-zero, so the app can show a "sign in" state.
set -euo pipefail
exec /usr/bin/env python3 - "$@" <<'PY'
import base64, json, os, subprocess, sys, time, urllib.request, urllib.error
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

HOME = Path.home()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"


def fail(msg):
    sys.stderr.write(msg.rstrip() + "\n")
    sys.exit(1)


def _get(url, headers, timeout=15):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def _iso(value):
    return value if isinstance(value, str) and value else None


def _pct(value):
    if value is None:
        return None
    try:
        used = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, used))


def _window(label, used, resets_at=None, remaining_amount=None, currency=None, detail=None):
    used = _pct(used)
    warning = (used is not None and used >= 85.0) or (
        remaining_amount is not None and remaining_amount <= 0.0
    )
    return {
        "label": label,
        "used_percent": used,
        "remaining_percent": None if used is None else 100.0 - used,
        "remaining_amount": remaining_amount,
        "currency": currency,
        "resets_at": _iso(resets_at),
        "detail": detail,
        "warning": warning,
    }


def _provider(provider, label, status, windows, source=None, plan=None, details=None, message=None):
    return {
        "provider": provider,
        "label": label,
        "status": status,
        "source": source,
        "plan": plan,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "windows": windows or [],
        "details": details or [],
        "message": message,
    }


def _antigravity_cli():
    candidates = [
        HOME / ".local" / "bin" / "agy",
        Path("/opt/homebrew/bin/agy"),
        Path("/usr/local/bin/agy"),
    ]
    path_dirs = [Path(value) for value in os.environ.get("PATH", "").split(os.pathsep) if value]
    candidates.extend(directory / "agy" for directory in path_dirs)
    return next((path for path in candidates if path.is_file() and os.access(path, os.X_OK)), None)


def _antigravity_secret(value):
    if not value:
        return None
    raw = value.strip()
    prefix = b"go-keyring-base64:"
    if raw.startswith(prefix):
        try:
            raw = base64.b64decode(raw[len(prefix):], validate=True)
        except Exception:
            return None
    try:
        return json.loads(raw.decode())
    except Exception:
        return None


def _antigravity_credentials():
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-a", "antigravity", "-s", "gemini", "-w"],
            stderr=subprocess.DEVNULL,
            timeout=5,
        )
        data = _antigravity_secret(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    path = HOME / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _antigravity_access_token(credentials, cli):
    token = credentials.get("token") if isinstance(credentials.get("token"), dict) else credentials
    access_token = token.get("access_token") if isinstance(token, dict) else None
    expiry = token.get("expiry") if isinstance(token, dict) else None
    if isinstance(access_token, str) and access_token:
        try:
            expires = datetime.fromisoformat(str(expiry).replace("Z", "+00:00"))
            if expires > datetime.now(timezone.utc) + timedelta(seconds=60):
                return access_token
        except Exception:
            pass
    if cli is not None:
        try:
            subprocess.run(
                [str(cli), "models"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=8,
                check=False,
            )
            refreshed = _antigravity_credentials() or {}
            refreshed_token = refreshed.get("token") if isinstance(refreshed.get("token"), dict) else refreshed
            refreshed_access = refreshed_token.get("access_token") if isinstance(refreshed_token, dict) else None
            if isinstance(refreshed_access, str) and refreshed_access:
                return refreshed_access
        except Exception:
            pass
    return access_token if isinstance(access_token, str) and access_token else None


def _antigravity_fraction(bucket):
    for source in (bucket, bucket.get("remaining")):
        if not isinstance(source, dict):
            continue
        for key in ("remainingFraction", "remaining_fraction"):
            value = source.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return max(0.0, min(1.0, float(value)))
            if isinstance(value, str):
                try:
                    return max(0.0, min(1.0, float(value)))
                except ValueError:
                    pass
        if source.get("case") == "remainingFraction":
            try:
                return max(0.0, min(1.0, float(source.get("value"))))
            except (TypeError, ValueError):
                pass
    return None


def _antigravity_windows(payload):
    root = payload if isinstance(payload, dict) else {}
    groups = root.get("groups")
    if not isinstance(groups, list):
        for key in ("response", "summary"):
            nested = root.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("groups"), list):
                groups = nested["groups"]
                break
    if not isinstance(groups, list):
        return []
    windows = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = str(group.get("displayName") or group.get("name") or "Models").strip()
        lower_name = name.lower()
        family = "Gemini" if "gemini" in lower_name else "Claude/GPT" if "claude" in lower_name or "gpt" in lower_name else name.title()
        buckets = group.get("buckets")
        if not isinstance(buckets, list):
            continue
        for index, bucket in enumerate(buckets):
            if not isinstance(bucket, dict):
                continue
            disabled = bucket.get("disabled")
            if disabled is True or str(disabled).lower() in ("1", "true"):
                continue
            fraction = _antigravity_fraction(bucket)
            if fraction is None:
                continue
            identity = next((str(bucket[key]) for key in ("bucketId", "id", "displayName", "name", "window") if bucket.get(key)), f"bucket:{index}")
            period = str(bucket.get("displayName") or bucket.get("name") or bucket.get("window") or "Quota").strip()
            reset = next((bucket.get(key) for key in ("resetTime", "reset_time", "resetAt", "reset_at") if bucket.get(key)), None)
            remaining = fraction * 100.0
            result = _window(f"{family} {period.lower()}", 100.0 - remaining, resets_at=reset, detail=f"{remaining:.0f}% left")
            result.update(scope=f"model-family:{family.lower()}", window_id=identity)
            windows.append(result)
    return windows


def antigravity_provider():
    cli = _antigravity_cli()
    credentials = _antigravity_credentials()
    if cli is None and credentials is None:
        return None
    if credentials is None:
        return _provider("antigravity", "Antigravity", "unavailable", [], message="Antigravity is installed — run `agy` to sign in.")
    token = _antigravity_access_token(credentials, cli)
    if not token:
        return _provider("antigravity", "Antigravity", "unavailable", [], message="Antigravity sign-in expired — run `agy` to sign in again.")
    request = urllib.request.Request(
        "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
        data=b"{}",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "User-Agent": "antigravity/1.2.0 Darwin/arm64",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _provider("antigravity", "Antigravity", "unavailable", [], message="Antigravity sign-in expired — run `agy` to sign in again.")
        return _provider("antigravity", "Antigravity", "unavailable", [], message=f"Antigravity usage error (HTTP {exc.code}).")
    except Exception as exc:
        return _provider("antigravity", "Antigravity", "unavailable", [], message=f"Could not reach Antigravity usage: {exc}")
    windows = _antigravity_windows(payload)
    if not windows:
        return _provider("antigravity", "Antigravity", "unavailable", [], message="Antigravity did not report quota windows.")
    plan = credentials.get("plan_tier") or credentials.get("plan")
    return _provider("antigravity", "Antigravity", "ok", windows, source="cloud_code_quota_api", plan=str(plan) if plan else None)


# --- Claude (Anthropic OAuth usage) --------------------------------------------
def _anthropic_token():
    path = HOME / ".claude" / ".credentials.json"
    if path.exists():
        try:
            data = json.loads(path.read_text())
            token = ((data.get("claudeAiOauth") or {}).get("accessToken") or "").strip()
            if token:
                return token
        except Exception:
            pass
    try:
        raw = subprocess.check_output(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            stderr=subprocess.DEVNULL,
        ).decode()
        return ((json.loads(raw).get("claudeAiOauth") or {}).get("accessToken") or "").strip() or None
    except Exception:
        return None


def anthropic_provider():
    token = _anthropic_token()
    if not token:
        return None
    if not token.startswith("sk-ant-oat"):
        return _provider(
            "anthropic", "Claude", "unavailable", [],
            message="Claude account limits need an OAuth (Claude subscription) login.",
        )
    try:
        payload = _get(
            "https://api.anthropic.com/api/oauth/usage",
            {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-code/2.1.0",
            },
        )
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _provider("anthropic", "Claude", "unavailable", [],
                             message="Sign in to Claude (claude login) to see limits.")
        return _provider("anthropic", "Claude", "unavailable", [], message=f"Claude usage error (HTTP {exc.code}).")
    except Exception as exc:
        return _provider("anthropic", "Claude", "unavailable", [], message=f"Could not reach Claude usage: {exc}")
    windows = []
    for key, wlabel, scope, seconds in (
        ("five_hour", "Current session", "account", 18000),
        ("seven_day", "Current week", "account", 604800),
        ("seven_day_opus", "Opus week", "opus", 604800),
        ("seven_day_sonnet", "Sonnet week", "sonnet", 604800),
    ):
        window = payload.get(key) or {}
        util = window.get("utilization")
        if util is None:
            continue
        # The OAuth usage API reports `utilization` as a PERCENT (0-100), e.g. 34.0
        # or 2.0 — never a 0-1 fraction. Take it as-is (clamped). The old "if <= 1,
        # multiply by 100" fraction guess inverted a barely-used window into a full
        # one — a fresh session at 1% utilization became 100% used → "no quota".
        used = max(0.0, min(100.0, float(util)))
        result = _window(wlabel, used, resets_at=window.get("resets_at"))
        result.update(window_seconds=seconds, scope=scope, window_id=key)
        windows.append(result)
    details = []
    extra = payload.get("extra_usage") or {}
    if extra.get("is_enabled"):
        used_credits, monthly_limit = extra.get("used_credits"), extra.get("monthly_limit")
        currency = extra.get("currency") or "USD"
        if isinstance(used_credits, (int, float)) and isinstance(monthly_limit, (int, float)):
            details.append(f"Extra usage: {used_credits:.2f} / {monthly_limit:.2f} {currency}")
    return _provider("anthropic", "Claude", "ok", windows, source="oauth_usage_api", details=details)


# --- Codex (ChatGPT usage) -----------------------------------------------------
def _jwt_exp(token):
    try:
        part = token.split(".")[1]
        pad = "=" * (-len(part) % 4)
        return json.loads(base64.urlsafe_b64decode(part + pad)).get("exp")
    except Exception:
        return None


def _codex_creds():
    # Every candidate ChatGPT/Codex token store on this Mac; pick the one whose
    # access token is valid the longest so a stale Goose token never shadows a
    # fresh Codex CLI login (or vice-versa).
    candidates = []
    for path in (HOME / ".codex" / "auth.json", HOME / ".config/goose/chatgpt_codex/tokens.json"):
        if not path.exists():
            continue
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        tokens = data.get("tokens") if isinstance(data.get("tokens"), dict) else data
        token = (tokens.get("access_token") or "").strip()
        if not token:
            continue
        candidates.append((_jwt_exp(token) or 0, token, (tokens.get("account_id") or "").strip() or None))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1], candidates[0][2]


def _codex_windows(rate_limit, scope="account", prefix=None):
    windows = []
    if not isinstance(rate_limit, dict):
        return windows
    for key in ("primary_window", "secondary_window"):
        window = rate_limit.get(key)
        if not isinstance(window, dict):
            continue
        try:
            used = float(window.get("used_percent"))
        except (TypeError, ValueError, OverflowError):
            continue
        if not -float("inf") < used < float("inf"):
            continue
        window_seconds = window.get("limit_window_seconds")
        if isinstance(window_seconds, bool) or not isinstance(window_seconds, (int, float)):
            window_seconds = None
        else:
            try:
                window_seconds = float(window_seconds)
            except OverflowError:
                window_seconds = None
        if window_seconds is not None and not 0 < window_seconds < float("inf"):
            window_seconds = None
        if window_seconds == 18000:
            wlabel = "Session"
        elif window_seconds == 604800:
            wlabel = "Weekly"
        elif window_seconds is None:
            wlabel = key.removesuffix("_window").title() + " quota"
        else:
            unit, divisor = next(((unit, divisor) for unit, divisor in
                                  (("d", 86400), ("h", 3600), ("m", 60))
                                  if window_seconds % divisor == 0 and
                                  (unit != "d" or window_seconds > 86400)), ("s", 1))
            wlabel = f"{window_seconds / divisor:g}{unit} quota"
        # `reset_at` is a Unix timestamp (Hermes reports an ISO instant, so convert
        # to match); also accept an ISO string, or derive from reset_after_seconds.
        reset_at = window.get("reset_at")
        resets_iso = None
        if isinstance(reset_at, bool):
            pass
        elif isinstance(reset_at, (int, float)) and 0 < reset_at < float("inf"):
            try:
                resets_iso = datetime.fromtimestamp(reset_at, timezone.utc).isoformat()
            except (OverflowError, ValueError, OSError):
                resets_iso = None
        elif isinstance(reset_at, str) and reset_at:
            try:
                resets_iso = datetime.fromisoformat(reset_at.replace("Z", "+00:00")).isoformat()
            except Exception:
                resets_iso = None
        if resets_iso is None:
            after = window.get("reset_after_seconds")
            if (not isinstance(after, bool) and isinstance(after, (int, float))
                    and 0 < after < float("inf")):
                try:
                    resets_iso = (datetime.now(timezone.utc) + timedelta(seconds=after)).isoformat()
                except (OverflowError, ValueError):
                    resets_iso = None
        result = _window(f"{prefix} {wlabel}" if prefix else wlabel, used, resets_at=resets_iso)
        result.update(window_seconds=window_seconds, scope=scope, window_id=key.removesuffix("_window"))
        windows.append(result)
    return windows


def codex_provider():
    creds = _codex_creds()
    if not creds:
        return None
    token, account_id = creds
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json", "User-Agent": "codex-cli"}
    if account_id:
        headers["ChatGPT-Account-Id"] = account_id
    try:
        payload = _get("https://chatgpt.com/backend-api/wham/usage", headers)
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _provider("openai-codex", "Codex", "unavailable", [],
                             message="Codex token expired — re-run your Codex/ChatGPT sign-in.")
        return _provider("openai-codex", "Codex", "unavailable", [], message=f"Codex usage error (HTTP {exc.code}).")
    except Exception as exc:
        return _provider("openai-codex", "Codex", "unavailable", [], message=f"Could not reach Codex usage: {exc}")
    windows = _codex_windows(payload.get("rate_limit") or {})
    additional = payload.get("additional_rate_limits")
    if not isinstance(additional, list):
        additional = []
    for index, extra in enumerate(additional):
        if not isinstance(extra, dict):
            continue
        scope = str(extra.get("metered_feature") or extra.get("limit_name") or f"additional:{index}")
        prefix = str(extra.get("limit_name") or extra.get("metered_feature") or f"Additional {index + 1}")
        windows.extend(_codex_windows(extra.get("rate_limit") or {}, scope, prefix))
    details = []
    reset_credits = payload.get("rate_limit_reset_credits") or {}
    banked = reset_credits.get("available_count")
    if isinstance(banked, (int, float)) and int(banked) > 0:
        count = int(banked)
        details.append(f"You have {count} reset{'s' if count != 1 else ''} banked - use /usage reset to activate")
    credits = payload.get("credits") or {}
    if credits.get("has_credits"):
        balance = credits.get("balance")
        if isinstance(balance, (int, float)):
            details.append(f"Credits balance: ${float(balance):.2f}")
        elif credits.get("unlimited"):
            details.append("Credits balance: unlimited")
    plan = payload.get("plan_type")
    plan = str(plan).replace("_", " ").title() if plan else None
    return _provider("openai-codex", "Codex", "ok", windows, source="usage_api", plan=plan, details=details)


# --- OpenRouter (credits API) --------------------------------------------------
def _openrouter_key():
    key = (os.environ.get("OPENROUTER_API_KEY") or "").strip()
    if key:
        return key
    env_path = HOME / ".hermes" / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text().splitlines():
                line = line.strip()
                if line.startswith("OPENROUTER_API_KEY=") and not line.startswith("#"):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if value and value != "CHANGEME":
                        return value
        except Exception:
            pass
    return None


# Shared credits-API fetch: one OpenRouter-shaped row per credential holder, so
# the env/~/.hermes key (provider "openrouter") and opencode's own key
# (provider "opencode") each get their own row, the way the Hermes source lists
# OpenRouter from the gateway's credentials.
def _openrouter_credits_provider(slug, label, key, source, reject_message):
    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json", "User-Agent": UA}
    try:
        credits = (_get("https://openrouter.ai/api/v1/credits", headers, timeout=10).get("data") or {})
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return _provider(slug, label, "unavailable", [], message=reject_message)
        return _provider(slug, label, "unavailable", [], message=f"OpenRouter error (HTTP {exc.code}).")
    except Exception as exc:
        return _provider(slug, label, "unavailable", [], message=f"Could not reach OpenRouter: {exc}")
    total = float(credits.get("total_credits") or 0.0)
    usage = float(credits.get("total_usage") or 0.0)
    remaining = max(0.0, total - usage)
    windows = [_window("Account credits", None, remaining_amount=remaining, currency="USD",
                       detail=f"${remaining:.2f} of ${total:.2f} left" if total > 0 else f"${remaining:.2f} available")]
    # Match the Hermes source, which lists an "API key quota" % window (the per-key
    # spend limit) alongside the credit balance. OpenRouter's /api/v1/key returns
    # the key's `limit` + `usage`; a null limit means "no cap" so we skip the window.
    try:
        kd = (_get("https://openrouter.ai/api/v1/key", headers, timeout=10).get("data") or {})
        klimit, kusage = kd.get("limit"), kd.get("usage")
        if isinstance(klimit, (int, float)) and klimit > 0 and isinstance(kusage, (int, float)):
            used = min(100.0, max(0.0, kusage / klimit * 100.0))
            windows.append(_window("API key quota", used,
                                   detail=f"${max(0.0, klimit - kusage):.2f} of ${klimit:.2f} key limit left"))
    except Exception:
        pass
    for window, window_id in zip(windows, ("account_credits", "api_key_limit")):
        window.update(scope="account" if window_id == "account_credits" else "api-key", window_id=window_id)
    return _provider(slug, label, "ok", windows, source=source)


def openrouter_provider():
    key = _openrouter_key()
    if not key:
        return None
    return _openrouter_credits_provider(
        "openrouter", "OpenRouter", key, "credits_api",
        "OpenRouter key rejected — check OPENROUTER_API_KEY.")


# --- opencode (OpenRouter credits through opencode's own login) ----------------
def _opencode_key():
    path = HOME / ".local" / "share" / "opencode" / "auth.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return None
    entry = data.get("openrouter")
    if not isinstance(entry, dict):
        return None
    return (entry.get("key") or "").strip() or None


def opencode_provider():
    key = _opencode_key()
    if not key:
        return None
    return _openrouter_credits_provider(
        "opencode", "opencode", key, "opencode_auth + credits_api",
        "opencode's OpenRouter key was rejected — re-run `opencode auth login`.")


_provider_fns = (anthropic_provider, codex_provider, antigravity_provider, openrouter_provider, opencode_provider)
# Fetch every provider CONCURRENTLY. Each hits a different remote usage API (and
# some, like Antigravity, shell out to a CLI), so running them sequentially made
# a fast provider like Claude wait behind the slowest one — the source lagged as
# a whole. A per-future guard keeps one provider's failure from sinking the rest.
def _safe(fn):
    try:
        return fn()
    except Exception:
        return None

with ThreadPoolExecutor(max_workers=len(_provider_fns)) as _pool:
    providers = [p for p in _pool.map(_safe, _provider_fns) if p is not None]
if not providers:
    fail("Sign in to Claude, Codex, Antigravity, OpenRouter, or opencode to see quotas.")

print(json.dumps({
    "broker": os.uname().nodename,
    "generated_at": datetime.now(timezone.utc).isoformat(),
    "source": "local",
    "providers": providers,
}))
PY
