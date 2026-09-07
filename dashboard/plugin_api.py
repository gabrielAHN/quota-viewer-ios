from __future__ import annotations

import json
import os
import re
import socket
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Annotated, Any

from fastapi import APIRouter, Query, Response

from agent.account_usage import fetch_account_usage

router = APIRouter()

# Nice labels for the providers Hermes ships account-usage support for. Any other
# slug still works — it just gets a title-cased label.
_KNOWN_LABELS = {
    "openrouter": "OpenRouter",
    "anthropic": "Claude",
    "openai-codex": "Codex",
}
_DEFAULT_PROVIDERS = "openrouter,anthropic,openai-codex"


def _label_for(slug: str) -> str:
    return _KNOWN_LABELS.get(slug, slug.replace("-", " ").replace("_", " ").title())


def _configured_providers() -> tuple[tuple[str, str], ...]:
    """Which providers this gateway's dashboard reports on.

    Configurable per gateway via the ``PROVIDER_QUOTA_PROVIDERS`` env var
    (comma-separated ``slug`` or ``slug=Label`` items), so anyone can reuse this
    plugin with their own gateway's provider set instead of a hardcoded list.
    Defaults to the providers Hermes ships usage support for. Whatever the set,
    every quota is read through this gateway's own ``account_usage`` credentials,
    so the plugin stays linked to the gateway it's installed in.
    """
    raw = os.environ.get("PROVIDER_QUOTA_PROVIDERS", "").strip() or _DEFAULT_PROVIDERS
    providers: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        slug, sep, label = item.partition("=")
        slug = slug.strip()
        if not slug or slug in seen:
            continue
        seen.add(slug)
        providers.append((slug, label.strip() if sep and label.strip() else _label_for(slug)))
    return tuple(providers)


CACHE_SECONDS = 60
_cache: dict[str, Any] | None = None
_cache_at = 0.0
_lock = Lock()


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


def _file_anthropic_token() -> str | None:
    """Resolve the gateway's `claude login` OAuth token from the credentials FILE
    (refreshing if needed). On macOS, Hermes' account_usage reads the login
    Keychain first, whose Claude session can be expired even though the file token
    (kept fresh by the ACP bridge) is valid — that's why Claude can read as
    "authentication required" on the gateway despite a working `claude` login.
    Reading the file directly sidesteps that, and stays linked to the gateway's
    own subscription."""
    try:
        from agent.anthropic_adapter import (
            _read_claude_code_credentials_from_file,
            _refresh_oauth_token,
            is_claude_code_token_valid,
        )
        creds = _read_claude_code_credentials_from_file()
        if not creds:
            return None
        if is_claude_code_token_valid(creds):
            return (creds.get("accessToken") or "").strip() or None
        return (_refresh_oauth_token(creds) or "").strip() or None
    except Exception:
        return None


def _anthropic_direct(label: str) -> dict[str, Any] | None:
    """Query the Anthropic OAuth usage API with the gateway's file token, used as
    a fallback when account_usage can't (stale login-Keychain session). Mirrors
    account_usage's anthropic window mapping."""
    token = _file_anthropic_token()
    if not token or not token.startswith("sk-ant-oat"):
        return None
    try:
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
                "anthropic-beta": "oauth-2025-04-20",
                "User-Agent": "claude-code/2.1.0",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.load(resp) or {}
    except Exception:
        return None
    windows = []
    for key, wlabel in (
        ("five_hour", "Current session"),
        ("seven_day", "Current week"),
        ("seven_day_opus", "Opus week"),
        ("seven_day_sonnet", "Sonnet week"),
    ):
        window = payload.get(key) or {}
        util = window.get("utilization")
        if util is None:
            continue
        # `utilization` from the OAuth usage API is a PERCENT (0-100), e.g. 34.0 /
        # 2.0 — not a 0-1 fraction. Take it as-is (clamped). The old "if <= 1,
        # multiply by 100" guess inverted a barely-used window into a full one — a
        # fresh session at 1% utilization became 100% used → "no quota" — and
        # disagreed with the primary account_usage path, which uses the raw percent.
        used = max(0.0, min(100.0, float(util)))
        windows.append({
            "label": wlabel,
            "used_percent": used,
            "remaining_percent": 100.0 - used,
            "remaining_amount": None,
            "currency": None,
            "resets_at": window.get("resets_at"),
            "detail": None,
            "warning": used >= 85.0,
        })
    if not windows:
        return None
    return {
        "provider": "anthropic",
        "label": label,
        "status": "ok",
        "source": "oauth_usage_api (file token)",
        "plan": None,
        "fetched_at": _iso(datetime.now(timezone.utc)),
        "windows": windows,
        "details": [],
        "message": None,
    }


def _provider(provider: str, label: str) -> dict[str, Any]:
    result = _provider_via_account_usage(provider, label)
    # Claude on a gateway whose login-Keychain session is stale reads as
    # authentication_required even though the `claude login` file token works —
    # fall back to querying usage with that file token so it still shows.
    if provider == "anthropic" and result.get("status") != "ok":
        fallback = _anthropic_direct(label)
        if fallback is not None:
            return fallback
    return result


def _provider_via_account_usage(provider: str, label: str) -> dict[str, Any]:
    snapshot = fetch_account_usage(provider)
    if snapshot is None:
        return {
            "provider": provider,
            "label": label,
            "status": "authentication_required",
            "source": None,
            "plan": None,
            "fetched_at": None,
            "windows": [],
            "details": [],
            "message": f"Sign in to {label} on the gateway.",
        }
    windows = []
    for window in snapshot.windows:
        used = None if window.used_percent is None else max(0.0, min(100.0, float(window.used_percent)))
        remaining_amount = getattr(window, "remaining_amount", None)
        remaining_amount = None if remaining_amount is None else max(0.0, float(remaining_amount))
        window_label = window.label
        if provider == "openai-codex" and window.reset_at is not None:
            # The Codex/ChatGPT usage API only conveys the window length via
            # limit_window_seconds, which account_usage drops before we see it —
            # and on Plus the single returned window is the 7-day allowance yet
            # core hardcodes its label to "Session". Re-derive from the reset
            # distance: a reset >2 days out is the weekly quota, otherwise it's
            # the ~5h session window.
            now = datetime.now(window.reset_at.tzinfo or timezone.utc)
            seconds_to_reset = (window.reset_at - now).total_seconds()
            window_label = "Weekly" if seconds_to_reset > 2 * 86400 else "Session"
        windows.append(
            {
                "label": window_label,
                "used_percent": used,
                "remaining_percent": None if used is None else 100.0 - used,
                "remaining_amount": remaining_amount,
                "currency": getattr(window, "currency", None),
                "resets_at": _iso(window.reset_at),
                "detail": window.detail,
                "warning": (used is not None and used >= 85.0)
                or (remaining_amount is not None and remaining_amount <= 0.0),
            }
        )
    details = list(snapshot.details)
    if provider == "openrouter":
        # Surface the OpenRouter credit balance instead of a bare "ok". account_usage
        # only reports it as a "Credits balance: $N" detail string (from the gateway's
        # own credentials — respecting the gateway's access), so lift the number into
        # an "Account credits" window the renderer can show, and drop the now-duplicate
        # detail line while keeping the rest (e.g. "API key usage: …").
        balance = None
        for line in details:
            match = re.search(r"Credits balance:\s*\$([0-9]+(?:\.[0-9]+)?)", line)
            if match:
                balance = float(match.group(1))
                break
        if balance is not None:
            details = [d for d in details if not d.startswith("Credits balance:")]
            windows.insert(0, {
                "label": "Account credits",
                "used_percent": None,
                "remaining_percent": None,
                "remaining_amount": balance,
                "currency": "USD",
                "resets_at": None,
                "detail": f"${balance:.2f} available",
                "warning": balance <= 0.0,
            })
    status = "ok" if snapshot.available else "unavailable"
    return {
        "provider": provider,
        "label": label,
        "status": status,
        "source": snapshot.source,
        "plan": snapshot.plan,
        "fetched_at": _iso(snapshot.fetched_at),
        "windows": windows,
        "details": details,
        "message": snapshot.unavailable_reason,
    }


def _load(refresh: bool) -> dict[str, Any]:
    global _cache, _cache_at
    now = time.monotonic()
    with _lock:
        if not refresh and _cache is not None and now - _cache_at < CACHE_SECONDS:
            return _cache
        configured = _configured_providers()
        providers = [_provider(slug, label) for slug, label in configured]
        _cache = {
            # broker identifies the gateway host these quotas belong to, so a
            # client can tell which gateway it's linked to.
            "broker": socket.gethostname(),
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "cache_seconds": CACHE_SECONDS,
            "providers": providers,
        }
        _cache_at = time.monotonic()
        return _cache


@router.get("/quotas")
def quotas(refresh: Annotated[bool, Query()] = False) -> dict[str, Any]:
    return _load(refresh)


# --- Live sessions ---
# Reports the turns RUNNING right now so the menu-bar pet can light a per-provider
# session indicator — including work the gateway REST never marks is_active,
# notably tui_gateway turns. Runs ON the gateway host, so it reads the gateway
# LOGS (gui.log turn lifecycle + agent.log model/provider) and the active-sessions
# REGISTRY (desktop-surface leases). It NEVER touches the session DB or web_server
# internals (that gets a plugin auto-disabled), so it stays enabled. Each live
# turn carries its model so the client colours it by provider (a "vendor/model"
# slug is OpenRouter, claude-* is Claude, gpt-*/codex is Codex).
_ACTIVITY_MAX_AGE = 900.0            # ignore log events older than this (s)
_RE_LOG_TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")
_RE_TUI_START = re.compile(r"tui prompt accepted:.*agent_session_id=(\S+)")
_RE_TUI_END = re.compile(r"tui turn (?:finished|failed|cancell?ed|aborted|error):.*agent_session_id=(\S+)")
_RE_MODEL = re.compile(r"\[(\d{8}_\d{6}_[0-9a-fA-F]+)\].*?model=(\S+)\s+provider=(\S+)")
# A turn (top-level OR a background/skill-review/subagent turn) opens with
# "conversation turn: session=X" and closes with "Turn ended: ... session=X".
_RE_TURN_START = re.compile(r"conversation turn: session=(\S+)")
_RE_TURN_END = re.compile(r"Turn ended:.*?\bsession=(\S+)")


def _hermes_home() -> Path:
    for mod in ("agent.paths", "hermes_cli.paths", "agent.config"):
        try:
            m = __import__(mod, fromlist=["get_hermes_home"])
            return Path(m.get_hermes_home())
        except Exception:
            continue
    return Path.home() / ".hermes"


def _tail_text(path: Path, max_bytes: int = 262144) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
            return fh.read().decode("utf-8", "replace")
    except Exception:
        return ""


def _log_dirs(home: Path) -> list[Path]:
    # The default profile logs to ~/.hermes/logs; each named profile (e.g. a
    # channel/ACP BOT running under `helper`) has its own ~/.hermes/profiles/<p>/logs.
    dirs = [home / "logs"]
    try:
        dirs += sorted((home / "profiles").glob("*/logs"))
    except Exception:
        pass
    return [d for d in dirs if d.is_dir()]


def _read_log(home: Path, name: str) -> str:
    # Tail `<name>` from the root AND every profile's log dir, so a session running
    # under ANY profile is seen — not just the default profile's root logs. The
    # per-line age filters downstream drop stale profiles' old lines.
    return "\n".join(_tail_text(d / name) for d in _log_dirs(home))


def _log_ts(line: str) -> float | None:
    m = _RE_LOG_TS.match(line)
    if not m:
        return None
    try:
        # naive local time on THIS host; time.time() below is the same clock, so
        # the freshness delta is self-consistent regardless of the host's TZ.
        return datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").timestamp()
    except Exception:
        return None


def _active_tui_sessions(home: Path, now: float) -> dict[str, dict[str, Any]]:
    state: dict[str, tuple[float, str]] = {}
    for line in _read_log(home, "gui.log").splitlines():
        ts = _log_ts(line)
        if ts is None or now - ts > _ACTIVITY_MAX_AGE:
            continue
        m = _RE_TUI_START.search(line)
        if m:
            state[m.group(1)] = (ts, "start")
            continue
        m = _RE_TUI_END.search(line)
        if m:
            state[m.group(1)] = (ts, "end")
    return {
        sid: {"started_at": ts, "surface": "tui"}
        for sid, (ts, kind) in state.items()
        if kind == "start"
    }


def _active_agent_turns(home: Path, now: float) -> dict[str, dict[str, Any]]:
    # A session is RUNNING while it's mid-turn: from "conversation turn: session=X"
    # until the matching "Turn ended: ... session=X". This catches turns the tui +
    # lease paths miss — background/skill-review turns that run AFTER a tui turn is
    # marked finished, SUBAGENT turns (their model calls log under the parent
    # session), and bot/cli turns that hold no desktop lease — and keeps a session
    # lit through long reasoning/tool gaps between API calls (explicit start/end
    # means a finished turn never lingers).
    start: dict[str, float] = {}
    end: dict[str, float] = {}
    subagent: set[str] = set()
    for line in _read_log(home, "agent.log").splitlines():
        ts = _log_ts(line)
        if ts is None or now - ts > _ACTIVITY_MAX_AGE:
            continue
        m = _RE_TURN_START.search(line)
        if m:
            start[m.group(1)] = ts
            # A delegated subagent runs under its OWN session id but is part of the
            # PARENT session's work (`platform=subagent`). Don't surface it as a
            # separate session — it's counted within the parent, which stays shown
            # while it orchestrates the subagent.
            if "platform=subagent" in line:
                subagent.add(m.group(1))
            continue
        m = _RE_TURN_END.search(line)
        if m:
            end[m.group(1)] = ts
    return {
        sid: {"started_at": ts, "surface": "agent"}
        for sid, ts in start.items()
        if ts > end.get(sid, 0.0) and sid not in subagent
    }


def _registry_sessions(home: Path) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    regs = [home / "runtime" / "active_sessions.json"]
    try:
        regs += list((home / "profiles").glob("*/runtime/active_sessions.json"))
    except Exception:
        pass
    for reg in regs:
        try:
            data = json.loads(reg.read_text(encoding="utf-8"))
        except Exception:
            continue
        for e in (data.get("entries") if isinstance(data, dict) else data) or []:
            meta = e.get("metadata") if isinstance(e.get("metadata"), dict) else {}
            # `session_id` here is the AGENT session id (what agent.log tags model
            # lines with); `metadata.live_session_id` is the UI session — use the
            # agent id so the model lookup resolves.
            sid = str(e.get("session_id") or meta.get("live_session_id") or "").strip()
            if sid:
                out[sid] = {"started_at": e.get("started_at"), "surface": e.get("surface") or "desktop"}
    return out


def _models_for(home: Path, sids: set[str]) -> dict[str, list[tuple[str, str]]]:
    # All DISTINCT (model, provider) pairs each session ran, in first-seen order —
    # a session can use several models (mixture-of-agents, fallback, model switch).
    if not sids:
        return {}
    out: dict[str, list[tuple[str, str]]] = {}
    for line in _read_log(home, "agent.log").splitlines():
        m = _RE_MODEL.search(line)
        if m and m.group(1) in sids:
            pair = (m.group(2), m.group(3))
            lst = out.setdefault(m.group(1), [])
            if pair not in lst:
                lst.append(pair)
    return out


# A registry lease counts as "running" only if its session logged activity this
# recently. A desktop lease is held by the always-alive dashboard pid, so the
# gateway never prunes it — it lingers for HOURS after the Desktop chat closed.
# A genuinely running turn writes agent.log every few seconds (reasoning gaps
# aside), so this window drops those stale/phantom leases while keeping live and
# just-finished turns.
_LEASE_ACTIVE_WINDOW = 120.0


def _recent_session_ids(home: Path, sids: set[str], window: float, now: float) -> set[str]:
    if not sids:
        return set()
    recent: set[str] = set()
    for name in ("agent.log", "gui.log"):
        for line in _read_log(home, name).splitlines():
            ts = _log_ts(line)
            if ts is None or now - ts > window:
                continue
            for sid in sids:
                if sid not in recent and sid in line:
                    recent.add(sid)
        if recent == sids:
            break
    return recent


# A webchat bot (helper-chat server.py, :8090) drives `hermes -p <profile> acp`,
# whose turns log to STDERR (discarded by the server), so they NEVER reach
# agent.log or the registry — the log/lease paths above can't see them. Read the
# webchat's own /api/running for its live bot sessions. Best-effort: a no-op if the
# service isn't present. Provider follows the same bot→provider map the webchat uses.
_WEBCHAT_URL = "http://127.0.0.1:8090/api/running"
_WEBCHAT_KEY_FILE = Path.home() / ".hermes" / "helper-chat" / ".key"
_BOT_PROVIDER = {"helper": "openrouter", "carto": "copilot-acp", "default": "anthropic"}


_webchat_seen: dict[str, dict[str, Any]] = {}   # sid -> {"t": last_seen, "provider": …}
_WEBCHAT_HOLD = 8.0   # keep a bot dot lit briefly after its (often quick) turn ends


def _webchat_bot_sessions() -> list[dict[str, Any]]:
    now = time.time()
    try:
        key = _WEBCHAT_KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            req = urllib.request.Request(_WEBCHAT_URL, headers={"X-Helper-Key": key})
            with urllib.request.urlopen(req, timeout=1.5) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            for s in data.get("sessions") or []:
                sid = str(s.get("id") or "").strip()
                if sid:
                    prov = _BOT_PROVIDER.get(str(s.get("profile") or s.get("bot") or "default"), "anthropic")
                    _webchat_seen[sid] = {"t": now, "provider": prov}
    except Exception:
        pass
    # A short HOLD so a brief (fast GLM) turn stays visible a few seconds after it
    # clears from /api/running, instead of flashing by unnoticed.
    out: list[dict[str, Any]] = []
    for sid, info in list(_webchat_seen.items()):
        if now - info["t"] > _WEBCHAT_HOLD:
            del _webchat_seen[sid]
            continue
        out.append({
            "session_id": sid,
            "surface": "webchat",
            "started_at": None,
            "model": "",
            "provider": info["provider"],   # bot→provider; the client colours by this
            "models": [],
            "is_active": True,
        })
    return out


@router.get("/activity")
def activity() -> dict[str, Any]:
    now = time.time()
    home = _hermes_home()
    # In-flight tui turns (latest gui.log event is a prompt-accepted, not a
    # turn-finished) are running by definition.
    tui = _active_tui_sessions(home, now)
    # Sessions mid-turn per agent.log — catches background/skill-review and SUBAGENT
    # turns (and bot/cli turns) that aren't tui turns and may hold no lease.
    turns = _active_agent_turns(home, now)
    active: dict[str, dict[str, Any]] = dict(tui)
    for sid, info in turns.items():
        active.setdefault(sid, info)
    # Desktop leases are candidates — but only "running" with RECENT log activity,
    # so a stale lease the gateway never released is dropped (the phantom session).
    leases = _registry_sessions(home)
    lease_recent = _recent_session_ids(home, set(leases) - set(active), _LEASE_ACTIVE_WINDOW, now)
    for sid, info in leases.items():
        if sid in active or sid in lease_recent:
            active.setdefault(sid, info)
    models = _models_for(home, set(active))
    out = []
    for sid, info in active.items():
        pairs = models.get(sid, [])
        model, provider = pairs[-1] if pairs else ("", "")   # primary = latest
        out.append({
            "session_id": sid,
            "surface": info.get("surface"),
            "started_at": info.get("started_at"),
            "model": model,          # latest model (single-dot / back-compat)
            "provider": provider,
            "models": [m for m, _ in pairs],   # every distinct model this session ran
            "is_active": True,
        })
    # Merge webchat bot sessions (ACP turns that never hit agent.log), deduped.
    seen = {o["session_id"] for o in out}
    for s in _webchat_bot_sessions():
        if s["session_id"] not in seen:
            out.append(s)
            seen.add(s["session_id"])
    return {"broker": socket.gethostname(), "sessions": out}


# --- Pets: expose the pets installed ON THIS GATEWAY so a remote client (e.g.
# the menu-bar app) can list and fetch pets that were downloaded here, since a
# remote Hermes Desktop downloads pets to the gateway, not the client. ---
def _pets_dirs() -> list[Path]:
    # Mirror ONLY the profile the gateway serves (get_hermes_home()/pets — the
    # dashboard's profile), which is exactly the pets the remote Hermes Desktop's
    # pet.install / pet.remove RPCs operate on. Scanning other profiles would
    # surface pets the Desktop can't manage, so a delete there would never
    # reflect in the menu (that was the "deleted pet still showing" bug).
    try:
        from agent.pet.store import pets_dir
        return [pets_dir()]
    except Exception:
        return [Path.home() / ".hermes" / "pets"]


def _installed_pets() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for base in _pets_dirs():
        try:
            children = sorted(base.iterdir())
        except Exception:
            continue
        for directory in children:
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            meta_path = directory / "pet.json"
            if not meta_path.exists():
                continue
            try:
                meta = json.loads(meta_path.read_text())
            except Exception:
                continue
            pet_id = meta.get("id", directory.name)
            if pet_id in seen:
                continue
            sheet = directory / (meta.get("spritesheetPath") or "spritesheet.webp")
            if not sheet.exists():
                continue
            seen.add(pet_id)
            out.append({
                "id": pet_id,
                "displayName": meta.get("displayName", directory.name),
                "spritesheetPath": sheet.name,
                "path": sheet,
            })
    return out


@router.get("/pets")
def pets() -> dict[str, Any]:
    return {"pets": [{"id": p["id"], "displayName": p["displayName"],
                      "spritesheetPath": p["spritesheetPath"]} for p in _installed_pets()]}


@router.get("/pets/{pet_id}/spritesheet")
def pet_spritesheet(pet_id: str) -> Response:
    for pet in _installed_pets():
        if pet["id"] == pet_id:
            path: Path = pet["path"]
            media = "image/webp" if path.suffix.lower() == ".webp" else "image/png"
            return Response(content=path.read_bytes(), media_type=media)
    return Response(status_code=404)
