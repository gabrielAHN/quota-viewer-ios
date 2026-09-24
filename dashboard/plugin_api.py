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
# The shortest interval a forced ?refresh=true is allowed to trigger a real re-fetch.
# The menu-bar reader asks for a refresh on every poll (a few seconds apart); each
# real refresh makes a live usage call per provider, and Anthropic's usage endpoint
# 429s under that cadence. Keep forced refreshes from out-pacing upstream.
_MIN_REFRESH_SECONDS = 30
_cache: dict[str, Any] | None = None
_cache_at = 0.0
_lock = Lock()

# Per-provider last-good snapshot: a successful reading kept so a TRANSIENT blip
# (rate limit, 5xx, a momentary token-refresh gap) doesn't blank a provider that
# actually has quota. Keyed by slug; also stamped with a monotonic time so a very
# stale reading eventually gives way to the real error.
_last_good: dict[str, dict[str, Any]] = {}
_last_good_at: dict[str, float] = {}
# Wall-clock stamp for each in-process reading, kept ALONGSIDE the monotonic
# _last_good_at so an in-process reading can be age-compared against the on-disk
# mirror (which is wall-clock stamped by whatever process wrote it). Monotonic
# clocks are per-process and can't be compared across the refresher and the
# dashboard, so the cross-source "which is newer" decision uses wall time.
_last_good_wall: dict[str, float] = {}
# How long a last-good reading is trusted after a failure. The gateway process can
# get into a state where a provider's live usage read fails for a while (an
# in-process credential/rate-limit condition that a fresh process doesn't hit),
# so keep the last good reading long enough to ride that out instead of blanking
# the menu back to "sign in".
_LAST_GOOD_TTL = 6 * 60 * 60  # seconds
# Last-good is also mirrored to disk so it survives a gateway restart AND lets a
# fresh short-lived reader (which succeeds) hand a good reading to the long-lived
# dashboard process that is currently failing in-process.
_LAST_GOOD_PATH = os.path.join(
    os.environ.get("TMPDIR", "/tmp").rstrip("/"), "provider-quota-lastgood.json"
)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(timezone.utc).isoformat()


# --- Quota source: the official `quota` plugin (github.com/rarf/hermes-quota-plugin) ---
# This plugin no longer fetches provider usage itself. The reviewed catalog plugin
# owns the fetchers and writes `$HERMES_HOME/quota_cache.json`; we adapt that cache
# to the QuotaPayload shape the menu-bar app already speaks, keeping the last-good
# masking so a transient fetch failure never blanks a provider that has quota.

# Official-cache unavailable_reason → our status. Reasons that mean "the gateway has
# no credential" map to authentication_required (the menu renders a sign-in row);
# everything else (fetch-error, timeout, no-data) is a transient unavailable that
# last-good masking absorbs.
_AUTH_REASONS = {"no-credentials", "not-logged-in", "opt-in-disabled"}


def _read_official_cache() -> dict[str, Any]:
    """Read the official plugin's quota_cache.json directly (schema documented in
    the plugin: {fetched_at, providers: {slug: {label, plan, unavailable_reason,
    details, windows: [{label, used_percent, reset_at}]}}})."""
    try:
        with open(_hermes_home() / "quota_cache.json", encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and isinstance(data.get("providers"), dict):
            return data
    except Exception:
        pass
    return {"fetched_at": None, "providers": {}}


def _cache_age_seconds(cache: dict[str, Any]) -> float | None:
    ts = cache.get("fetched_at")
    if not ts:
        return None
    try:
        fetched = datetime.fromisoformat(ts)
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - fetched).total_seconds()
    except Exception:
        return None


def _refresh_official_cache() -> None:
    """Run the official plugin's sweep in a FRESH subprocess, never in-process.

    The long-lived dashboard process rots: after hours, credential reads that
    work in a fresh interpreter fail in-process (anthropic → no-credentials,
    openrouter → no-data), which is exactly the false-signout class this app
    exists to avoid. A fresh interpreter per sweep sidesteps the rot entirely;
    the plugin's own REFRESH_BUDGET_S bounds the sweep and our timeout backstops
    a wedged child. The interpreter must be the HERMES VENV python (so the
    plugin's imports — hermes_constants, agent.account_usage — resolve): inside
    the dashboard that is sys.executable, but the last-good refresher runs under
    system python3, so probe the known venv locations first."""
    import subprocess
    import sys
    root = _hermes_home() / "plugins" / "quota"
    if not (root / "quota_cache.py").is_file():
        return
    python = next(
        (str(c) for c in (
            Path.home() / ".hermes/hermes-agent/venv/bin/python3",
            Path.home() / ".hermes/hermes-agent/.venv/bin/python3",
        ) if c.is_file()),
        sys.executable,
    )
    env = dict(os.environ)
    env["HERMES_HOME"] = str(_hermes_home())
    code = (
        "import sys; sys.path.insert(0, %r); "
        "from quota.quota_cache import refresh_quota_cache; refresh_quota_cache()"
    ) % str(root.parent)
    try:
        subprocess.run(
            [python, "-c", code],
            env=env, timeout=60,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _adapt_record(slug: str, label: str, rec: dict[str, Any], fetched_at: str | None) -> dict[str, Any]:
    """One official-cache provider record → the menu-bar QuotaPayload provider shape."""
    reason = rec.get("unavailable_reason")
    windows = []
    for w in rec.get("windows") or []:
        used = w.get("used_percent")
        used = None if used is None else max(0.0, min(100.0, float(used)))
        windows.append({
            "label": w.get("label") or "window",
            "used_percent": used,
            "remaining_percent": None if used is None else 100.0 - used,
            "remaining_amount": None,
            "currency": None,
            "resets_at": w.get("reset_at"),
            "detail": None,
            "warning": used is not None and used >= 85.0,
        })
    details = [str(d) for d in (rec.get("details") or [])]
    if slug == "openrouter":
        # Same courtesy as before: lift the credits balance into a window row.
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
    if reason in _AUTH_REASONS:
        status = "authentication_required"
        message = f"Sign in to {label} on the gateway."
    elif reason:
        status = "unavailable"
        message = f"{label} usage is unavailable ({reason}) — try again shortly."
    else:
        status = "ok" if windows else "unavailable"
        message = None if windows else f"{label} reported no usage windows."
    return {
        "provider": slug,
        "label": label,
        "status": status,
        "source": "quota plugin cache",
        "plan": rec.get("plan"),
        "fetched_at": fetched_at,
        "windows": windows,
        "details": details,
        "message": message,
    }


def _provider(provider: str, label: str, cache: dict[str, Any]) -> dict[str, Any]:
    rec = cache.get("providers", {}).get(provider)
    if isinstance(rec, dict):
        result = _adapt_record(provider, label, rec, cache.get("fetched_at"))
    else:
        result = {
            "provider": provider,
            "label": label,
            "status": "unavailable",
            "source": None,
            "plan": None,
            "fetched_at": None,
            "windows": [],
            "details": [],
            "message": f"{label} has no reading yet — run `hermes quota refresh` on the gateway.",
        }
    # A good reading refreshes the last-good cache; a bad one is masked by a recent
    # last-good so a transient failure never blanks a provider that has quota.
    if result.get("status") == "ok" and result.get("windows"):
        _record_last_good(provider, result)
        return result
    cached = _fresh_last_good(provider)
    if cached is not None:
        return cached
    return result


def _record_last_good(provider: str, snapshot: dict[str, Any]) -> None:
    _last_good[provider] = snapshot
    _last_good_at[provider] = time.monotonic()
    _last_good_wall[provider] = time.time()
    # Mirror to disk (wall-clock stamped) so it survives a restart and can be
    # shared with other reader processes.
    try:
        disk = _read_last_good_file()
        disk[provider] = {"at": time.time(), "snapshot": snapshot}
        tmp = _LAST_GOOD_PATH + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(disk, fh)
        os.replace(tmp, _LAST_GOOD_PATH)
    except Exception:
        pass


def _read_last_good_file() -> dict[str, Any]:
    try:
        with open(_LAST_GOOD_PATH) as fh:
            data = json.load(fh)
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _fresh_last_good(provider: str) -> dict[str, Any] | None:
    # Two sources of a last-good reading, both wall-clock stamped so they can be
    # compared across processes: this process's own in-process reading, and the
    # on-disk mirror another reader may keep current. Return the NEWER of the two
    # within TTL — critically, NOT "in-process first": a wedged process's stale
    # in-process reading must not shadow a fresh disk reading.
    now = time.time()
    in_proc = _last_good.get(provider)
    in_proc_wall = _last_good_wall.get(provider, 0.0)
    in_proc_fresh = bool(in_proc) and (now - in_proc_wall) < _LAST_GOOD_TTL

    disk_snapshot = None
    disk_wall = 0.0
    entry = _read_last_good_file().get(provider)
    if isinstance(entry, dict):
        stamped = entry.get("at", 0)
        snapshot = entry.get("snapshot")
        if isinstance(stamped, (int, float)) and (now - stamped) < _LAST_GOOD_TTL and isinstance(snapshot, dict):
            disk_snapshot = snapshot
            disk_wall = float(stamped)

    if in_proc_fresh and (disk_snapshot is None or in_proc_wall >= disk_wall):
        return in_proc
    if disk_snapshot is not None:
        return disk_snapshot
    return in_proc if in_proc_fresh else None


def _load(refresh: bool) -> dict[str, Any]:
    global _cache, _cache_at
    now = time.monotonic()
    with _lock:
        # Serve the cache for a normal (non-refresh) read within the cache window.
        if not refresh and _cache is not None and now - _cache_at < CACHE_SECONDS:
            return _cache
        # A forced refresh (?refresh=true) still honours a MINIMUM interval: the
        # menu-bar reader requests refresh on every poll, and each real refresh
        # runs the plugin's provider sweep (live upstream usage calls). Anthropic's
        # usage API rate-limits that quickly, so refreshing faster than the
        # upstream tolerates is what BLANKS the quota. Below the floor a forced
        # refresh returns the last payload instead of hammering upstream.
        if _cache is not None and now - _cache_at < _MIN_REFRESH_SECONDS:
            return _cache
        official = _read_official_cache()
        age = _cache_age_seconds(official)
        # Re-sweep when the official cache is stale (or a refresh was asked and it
        # is older than the refresh floor); the plugin's own budget bounds it.
        if age is None or age > CACHE_SECONDS or (refresh and age > _MIN_REFRESH_SECONDS):
            _refresh_official_cache()
            official = _read_official_cache()
        configured = _configured_providers()
        providers = [_provider(slug, label, official) for slug, label in configured]
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
