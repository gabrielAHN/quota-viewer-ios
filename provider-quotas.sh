#!/usr/bin/env bash
set -euo pipefail
exec /usr/bin/env python3 - "$@" <<'PY'
import argparse
import ctypes
import functools
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LABEL = "io.github.gabrielahn.provider-quotas"
BREW_LABEL = "homebrew.mxcl.provider-quotas"
HELPERS = {"hermes": "hermes-desktop-quotas", "local": "hermes-local-quotas"}
HOME = Path.home()
SUPPORT = HOME / "Library/Application Support/Hermes"
USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
DEFAULTS = os.environ.get("PROVIDER_QUOTAS_DEFAULTS") or LABEL
SETTINGS_CHANGED = LABEL + ".settings"
LOCATIONS = {
    "hermes": ("Hermes", "PROVIDER_QUOTAS_HERMES_HOME", "Hermes home: Hermes Desktop's gateway connection and sign-in"),
    "claude": ("Claude", "PROVIDER_QUOTAS_CLAUDE_HOME", "Claude Code folder: login, sessions and plan"),
    "codex": ("Codex", "PROVIDER_QUOTAS_CODEX_HOME", "Codex home: auth.json and sessions"),
    "opencode": ("opencode", "PROVIDER_QUOTAS_OPENCODE_HOME", "opencode data folder: auth.json and sessions"),
    "antigravity": ("Antigravity", "PROVIDER_QUOTAS_ANTIGRAVITY_HOME", "Antigravity CLI folder: sign-in token"),
    "openrouter": ("OpenRouter", "PROVIDER_QUOTAS_OPENROUTER_ENV", "file with OPENROUTER_API_KEY"),
}
SOURCES = {"hermes": "Hermes", "local": "This Mac"}
SLUGS = {"claude": "anthropic", "codex": "openai-codex", "opencode": "opencode", "antigravity": "antigravity",
         "openrouter": "openrouter"}


@functools.lru_cache(maxsize=None)
def app_settings():
    try:
        out = subprocess.run(["defaults", "export", DEFAULTS, "-"], capture_output=True, timeout=5)
        settings = plistlib.loads(out.stdout) if out.returncode == 0 and out.stdout.strip() else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return {}
    return settings if isinstance(settings, dict) else {}


def app_setting(key):
    return app_settings().get(key)


def as_path(value):
    value = value.strip() if isinstance(value, str) else ""
    return Path(os.path.abspath(os.path.expanduser(value))) if value else None


def hermes_folder(path):
    return path.parent.parent if path.parent.name.lower() == "profiles" else path


def automatic_location(key):
    env = os.environ
    if key == "hermes":
        home = as_path(env.get("HERMES_HOME"))
        return (hermes_folder(home), "HERMES_HOME") if home else (HOME / ".hermes", "default")
    if key == "claude":
        folder = as_path(env.get("CLAUDE_CONFIG_DIR"))
        return (folder, "CLAUDE_CONFIG_DIR") if folder else (HOME / ".claude", "default")
    if key == "codex":
        folder = as_path(env.get("CODEX_HOME"))
        return (folder, "CODEX_HOME") if folder else (HOME / ".codex", "default")
    if key == "opencode":
        data = as_path(env.get("XDG_DATA_HOME"))
        return (data / "opencode", "XDG_DATA_HOME") if data else (HOME / ".local/share/opencode", "default")
    if key == "antigravity":
        return HOME / ".gemini/antigravity-cli", "default"
    return location("hermes")[0] / ".env", "Hermes home"


@functools.lru_cache(maxsize=None)
def location(key):
    variable = LOCATIONS[key][1]
    for origin, value in ((variable, os.environ.get(variable)), ("custom", app_setting(f"location.{key}"))):
        path = as_path(value)
        if path:
            return (hermes_folder(path) if key == "hermes" else path), origin, True
    return (*automatic_location(key), False)


def hermes_root():
    return location("hermes")[:2]


def helper_env():
    env = dict(os.environ)
    for key, (_, variable, _) in LOCATIONS.items():
        path, _, custom = location(key)
        if custom or key == "hermes":
            env[variable] = str(path)
        else:
            env.pop(variable, None)
    return env


def claude_services(folder):
    base = "Claude Code-credentials"
    path = unicodedata.normalize("NFC", str(folder))
    hashed = [base + "-" + hashlib.sha256(text.encode()).hexdigest()[:8] for text in (path, path + "/")]
    return [base, hashed[0]] if folder == HOME / ".claude" else hashed


def keychain_has(service, account=None):
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", service] + (["-a", account] if account else []),
                             capture_output=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return False
    return out.returncode == 0


def env_file_has(path, name):
    try:
        lines = path.read_text().splitlines()
    except (OSError, UnicodeDecodeError):
        return False
    for line in lines:
        line = line.strip()
        if line.startswith(name + "=") and not line.startswith("#"):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            if value and value != "CHANGEME":
                return True
    return False


def location_status(key, path, custom):
    folder = path.is_dir()
    if key == "openrouter":
        if not custom and (os.environ.get("OPENROUTER_API_KEY") or "").strip():
            return "ok", "Using OPENROUTER_API_KEY from the environment."
        if env_file_has(path, "OPENROUTER_API_KEY"):
            return "ok", "OPENROUTER_API_KEY found."
        if not custom:
            return "info", "No OPENROUTER_API_KEY found."
        return "warn", "No OPENROUTER_API_KEY in this file." if path.is_file() else "This file doesn't exist."
    if custom and not folder:
        return "warn", "This folder doesn't exist."
    unset = ("info", "Not set up on this Mac.")
    if key == "hermes":
        if not folder:
            return unset
        good, text = hermes_status(path)
        return ("ok" if good else "warn"), text
    if key == "claude":
        service = next((name for name in claude_services(path) if keychain_has(name)), None)
        if service:
            return "ok", f"Signed in (Keychain: {service})."
        try:
            oauth = json.loads((path / ".credentials.json").read_text()).get("claudeAiOauth")
        except (OSError, ValueError, AttributeError):
            oauth = None
        if isinstance(oauth, dict) and str(oauth.get("accessToken") or "").strip():
            return "ok", "Signed in (.credentials.json)."
        return ("warn", "No Claude Code login here. Run claude and sign in.") if folder else unset
    if key == "codex":
        if (path / "auth.json").is_file():
            return "ok", "Signed in (auth.json)."
        if not custom and (HOME / ".config/goose/chatgpt_codex/tokens.json").is_file():
            return "ok", "Signed in (Goose)."
        return ("warn", "No auth.json here. Run codex login.") if folder else unset
    if key == "opencode":
        try:
            entry = json.loads((path / "auth.json").read_text()).get("openrouter")
        except (OSError, ValueError, AttributeError):
            entry = None
        if isinstance(entry, dict) and str(entry.get("key") or "").strip():
            return "ok", "Signed in (auth.json)."
        return ("info", "Not signed in. Run opencode auth login.") if folder else unset
    if (path / "antigravity-oauth-token").is_file():
        return "ok", "Signed in (antigravity-oauth-token)."
    if not custom and keychain_has("gemini", "antigravity"):
        return "ok", "Signed in (Keychain)."
    return ("info", "Not signed in. Run agy to sign in.") if folder else unset


def notify_app():
    if DEFAULTS != LABEL:
        return
    try:
        ctypes.CDLL("/usr/lib/libSystem.B.dylib").notify_post(SETTINGS_CHANGED.encode())
    except (OSError, AttributeError):
        pass


def desktop_store(root=None):
    root = root or hermes_root()[0]
    return root / "gateway-connections.json", root / "gateway-connection-tokens.json"


def paint(text, code):
    return f"\033[{code}m{text}\033[0m" if USE_COLOR else text


def ok(text):
    return paint(text, "32")


def warn(text):
    return paint(text, "33")


def bad(text):
    return paint(text, "31")


def dim(text):
    return paint(text, "2")


def helper_path(name):
    candidates = []
    override = os.environ.get("PROVIDER_QUOTAS_BIN_DIR")
    if override:
        candidates.append(Path(override) / name)
    candidates += [HOME / ".local/bin" / name, Path("/opt/homebrew/bin") / name, Path("/usr/local/bin") / name]
    candidates += [Path(p) / name for p in os.environ.get("PATH", "").split(":") if p]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def enabled_sources():
    enabled = [kind for kind in HELPERS if app_setting(f"gatewayEnabled.{kind}") in (True, 1)]
    return enabled or ["hermes"]


def run_helper(kind, timeout):
    path = helper_path(HELPERS[kind])
    if path is None:
        return {"source": kind, "helper": None, "exit": 127, "seconds": 0.0, "stderr": f"{HELPERS[kind]} is not installed", "payload": None}
    start = time.monotonic()
    try:
        proc = subprocess.run([str(path)], capture_output=True, timeout=timeout, env=helper_env())
        code, out, err = proc.returncode, proc.stdout, proc.stderr.decode(errors="replace").strip()
    except subprocess.TimeoutExpired:
        code, out, err = 124, b"", f"timed out after {timeout:g}s"
    elapsed = time.monotonic() - start
    payload = None
    if code == 0:
        try:
            payload = json.loads(out)
        except ValueError:
            code, err = 65, "helper printed invalid JSON"
    return {"source": kind, "helper": str(path), "exit": code, "seconds": elapsed, "stderr": err, "payload": payload}


def parse_time(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def countdown(value):
    parsed = parse_time(value)
    if parsed is None:
        return "no reset reported"
    seconds = int((parsed - datetime.now(timezone.utc)).total_seconds())
    if seconds <= 0:
        return "reset due"
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        return f"resets in {days}d {hours}h"
    if hours:
        return f"resets in {hours}h {minutes}m"
    return f"resets in {minutes}m"


def age(value):
    parsed = parse_time(value)
    if parsed is None:
        return None
    seconds = max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))
    return f"{seconds}s ago" if seconds < 120 else f"{seconds // 60}m ago"


def period(window):
    seconds = window.get("window_seconds")
    if isinstance(seconds, (int, float)) and not isinstance(seconds, bool) and seconds > 0:
        seconds = int(seconds)
        if seconds == 18000:
            return "5h"
        if seconds == 604800:
            return "weekly"
        if seconds % 86400 == 0:
            return f"{seconds // 86400}d"
        if seconds % 3600 == 0:
            return f"{seconds // 3600}h"
        return f"{seconds}s"
    if isinstance(window.get("remaining_amount"), (int, float)):
        return "balance"
    return "period not reported"


def percent(value):
    if value > 0 and value < 1:
        return "<1%"
    if value > 99 and value < 100:
        return ">99%"
    return f"{round(value)}%"


def usage(window):
    amount = window.get("remaining_amount")
    if isinstance(amount, (int, float)) and not isinstance(amount, bool):
        currency = window.get("currency") or ""
        prefix = "$" if currency == "USD" else ""
        suffix = "" if prefix or not currency else f" {currency}"
        return f"{prefix}{amount:,.2f}{suffix} available"
    left = window.get("remaining_percent")
    used = window.get("used_percent")
    if not isinstance(left, (int, float)) and isinstance(used, (int, float)):
        left = 100 - used
    if isinstance(left, (int, float)) and not isinstance(left, bool):
        left = max(0.0, min(100.0, float(left)))
        return f"{percent(100 - left)} used · {percent(left)} left"
    return "usage not reported"


def status_text(status):
    if status == "ok":
        return ok("ok")
    if status == "authentication_required":
        return bad("sign-in required")
    return warn(str(status or "unknown").replace("_", " "))


def print_payload(result, verbose):
    kind = result["source"]
    payload = result["payload"]
    header = f"{kind.upper()} source"
    if payload is None:
        print(f"{paint(header, '1')}  {bad('failed')} (exit {result['exit']}): {result['stderr'] or 'no output'}")
        return
    meta = [f"{result['seconds']:.1f}s"]
    if payload.get("broker"):
        meta.append(f"broker {payload['broker']}")
    fetched = age(payload.get("generated_at"))
    if fetched:
        meta.append(f"generated {fetched}")
    print(f"{paint(header, '1')}  {dim(' · '.join(meta))}")
    for provider in payload.get("providers") or []:
        name = provider.get("label") or provider.get("provider")
        plan = f" [{provider['plan']}]" if provider.get("plan") else ""
        extra = []
        if verbose and provider.get("fetched_at"):
            extra.append(f"fetched {age(provider['fetched_at'])}")
        if verbose and provider.get("source"):
            extra.append(str(provider["source"]))
        suffix = f"  {dim(' · '.join(extra))}" if extra else ""
        print(f"  {paint(name, '1')}{plan}  {status_text(provider.get('status'))}{suffix}")
        if provider.get("message"):
            print(f"    {warn(provider['message'])}")
        for window in provider.get("windows") or []:
            scope = window.get("scope")
            scope_text = f"  {dim('scope ' + scope)}" if verbose and scope else ""
            flag = f" {warn('!')}" if window.get("warning") else ""
            print(f"    {window.get('label', 'window'):<24} {usage(window):<26} {period(window):<20} {countdown(window.get('resets_at'))}{flag}{scope_text}")
        if verbose:
            for detail in provider.get("details") or []:
                print(f"    {dim(detail)}")


def cmd_status(args):
    sources = list(HELPERS) if args.source == "all" else [args.source] if args.source else enabled_sources()
    results = [run_helper(kind, args.timeout) for kind in sources]
    if args.json:
        output = {r["source"]: r["payload"] if r["payload"] is not None else {"error": r["stderr"], "exit": r["exit"]} for r in results}
        print(json.dumps(output if len(results) > 1 else next(iter(output.values())), indent=2))
    else:
        for index, result in enumerate(results):
            if index:
                print()
            print_payload(result, args.verbose)
    codes = [r["exit"] for r in results]
    return 0 if all(code == 0 for code in codes) else 2 if 2 in codes else 1


def check(label, state, detail=""):
    marks = {"ok": ok("✓"), "warn": warn("!"), "fail": bad("✗"), "info": dim("·")}
    print(f"  {marks[state]} {label}{(': ' + detail) if detail else ''}")
    return state


def launchd_state(label):
    try:
        out = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{label}"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    info = {}
    for line in out.stdout.splitlines():
        line = line.strip()
        for key in ("state", "pid", "last exit code", "program"):
            if line.startswith(key + " = "):
                info[key] = line.split(" = ", 1)[1]
    return info


def app_processes():
    try:
        out = subprocess.run(["pgrep", "-fl", "ProviderQuotaMenuBar"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.stdout.splitlines() if "/Contents/MacOS/ProviderQuotaMenuBar" in line]


def log_files():
    paths = [HOME / ".hermes/logs/provider-quotas-error.log", HOME / ".hermes/logs/provider-quotas.log"]
    brew = shutil.which("brew")
    prefixes = [Path("/opt/homebrew"), Path("/usr/local")]
    if brew:
        try:
            prefixes.insert(0, Path(subprocess.run([brew, "--prefix"], capture_output=True, text=True, timeout=10).stdout.strip()))
        except (OSError, subprocess.SubprocessError):
            pass
    for prefix in prefixes:
        paths += [prefix / "var/log/provider-quotas-error.log", prefix / "var/log/provider-quotas.log"]
    seen, found = set(), []
    for path in paths:
        if path not in seen and path.is_file():
            seen.add(path)
            found.append(path)
    return found


def tail(path, lines):
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            data = handle.read().decode(errors="replace").splitlines()
    except OSError:
        return []
    return data[-lines:]


def hermes_connection(root=None):
    registry = desktop_store(root)[0]
    legacy = SUPPORT / "connections.json"
    try:
        conns = json.loads((registry if registry.exists() else legacy).read_text())
    except (OSError, ValueError):
        return None, None, None
    entries = [entry for entry in (conns.get("connections") or []) if isinstance(entry, dict)]
    by_id = {entry.get("id"): entry for entry in entries}
    ordered = [entry for entry in (by_id.get(conns.get("lastUsed")), by_id.get(conns.get("primary"))) if entry] + entries
    active = next((entry for entry in ordered if entry.get("kind") != "local" and entry.get("url")), None) \
        or (ordered[0] if ordered else None)
    if not isinstance(active, dict):
        return None, None, None
    return active.get("kind"), (active.get("url") or "").rstrip("/") or None, active.get("id")


def url_key(value):
    parts = urllib.parse.urlsplit(str(value or "").strip())
    return "%s://%s%s" % (parts.scheme.lower(), parts.netloc.rpartition("@")[2].lower(), parts.path.rstrip("/"))


def desktop_session(conn_id, url, root=None):
    registry, tokens = desktop_store(root)
    migrated = registry.exists()
    path = tokens if migrated else SUPPORT / "native-oauth-tokens.json"
    try:
        store = json.loads(path.read_text())
    except (OSError, ValueError):
        return path, None
    key = url_key(url)
    if not migrated:
        return path, next((v for k, v in store.items() if url_key(k) == key), None)
    bound = store.get("tokens") or {}
    for entry in [bound.get(conn_id)] + list(bound.values()):
        if isinstance(entry, dict) and url_key(entry.get("baseUrl")) == key:
            return path, entry
    for section in ("unbound", "undecrypted"):
        found = next((v for k, v in (store.get(section) or {}).items() if url_key(k) == key), None)
        if found:
            return path, found
    return path, None


def span(seconds):
    hours, minutes = divmod(int(seconds) // 60, 60)
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def hermes_status(home):
    if not home.is_dir():
        return False, "This folder doesn't exist."
    older = "" if desktop_store(home)[0].exists() else " (older Hermes Desktop files)"
    kind, url, conn_id = hermes_connection(home)
    if kind is None:
        return False, "No Hermes Desktop connection found here."
    if kind == "local" or not url:
        return True, f"Hermes Desktop uses This device{older}."
    name = url_key(url).split("://", 1)[-1]
    session = desktop_session(conn_id, url, home)[1]
    if not isinstance(session, dict):
        return False, f"{name}: not signed in. Sign in from Hermes Desktop{older}."
    saved = session
    if session.get("encoding") == "plain":
        try:
            saved = json.loads(session.get("value") or "")
        except ValueError:
            saved = session
    expires = saved.get("expiresAt") if isinstance(saved, dict) else None
    if isinstance(expires, (int, float)) and not isinstance(expires, bool) \
            and (expires / 1000 if expires > 1e12 else expires) < time.time():
        return False, f"{name}: sign-in expired. Open Hermes Desktop to renew it{older}."
    return True, f"{name}: signed in{older}."


def shown(path):
    text = str(path)
    return "~" + text[len(str(HOME)):] if text == str(HOME) or text.startswith(str(HOME) + "/") else text


def mode_text(origin, custom):
    if origin == "custom":
        return "custom"
    if custom:
        return f"custom, {origin}"
    return "automatic" if origin == "default" else f"automatic, {origin}"


def source_on(source):
    return app_setting(f"gatewayEnabled.{source}") in (True, 1)


def provider_on(source, slug):
    return app_setting(f"menuBarHidden.{source}:{slug}") not in (True, 1)


def hermes_providers():
    slugs, labels = app_setting("lastProviderSlugs.hermes"), app_setting("lastProviderLabels.hermes")
    if not isinstance(slugs, list) or not isinstance(labels, list):
        return []
    return [(slug, label) for slug, label in zip(slugs, labels) if isinstance(slug, str) and isinstance(label, str)]


def config_target(value):
    if value in LOCATIONS or value in SOURCES:
        return value
    if value.startswith("hermes:"):
        slug = value.split(":", 1)[1]
        known = [known for known, _ in hermes_providers()]
        if slug in known:
            return value
        hint = "Hermes lists " + ", ".join(f"hermes:{name}" for name in known) if known \
            else "Hermes hasn't listed its providers yet"
        raise argparse.ArgumentTypeError(f"no Hermes provider named '{slug}' ({hint})")
    names = ", ".join(["hermes", "local"] + [key for key in LOCATIONS if key != "hermes"])
    raise argparse.ArgumentTypeError(f"invalid choice: '{value}' (choose from {names} or hermes:<provider>)")


def location_record(key):
    path, origin, custom = location(key)
    state, text = location_status(key, path, custom)
    source = "hermes" if key == "hermes" else "local"
    return {"provider": key, "label": LOCATIONS[key][0], "source": source,
            "on": source_on(source) if key == "hermes" else provider_on(source, SLUGS[key]),
            "set_up": custom or state != "info", "mode": "custom" if custom else "automatic",
            "origin": origin, "path": str(path), "state": state, "status": text}


def config_record(target):
    if target in LOCATIONS:
        return location_record(target)
    if target == "local":
        return {"provider": "local", "label": SOURCES["local"], "source": "local", "on": source_on("local")}
    slug = target.split(":", 1)[1]
    return {"provider": target, "label": dict(hermes_providers()).get(slug, slug), "source": "hermes",
            "on": provider_on("hermes", slug)}


def config_records():
    return ([config_record("hermes")] + [config_record(f"hermes:{slug}") for slug, _ in hermes_providers()]
            + [config_record("local")] + [config_record(key) for key in LOCATIONS if key != "hermes"])


def save_settings(changes):
    for key, value in changes.items():
        if value is None:
            command = ["defaults", "delete", DEFAULTS, key]
        elif isinstance(value, bool):
            command = ["defaults", "write", DEFAULTS, key, "-bool", "true" if value else "false"]
        else:
            command = ["defaults", "write", DEFAULTS, key, "-string", value]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        if value is not None and result.returncode != 0:
            raise OSError(result.stderr.strip() or f"defaults exited {result.returncode}")
    app_settings.cache_clear()
    location.cache_clear()
    notify_app()


def config_change(args):
    given = [name for name, value in (("path", args.path is not None), ("automatic", args.automatic),
                                      ("on", args.on), ("off", args.off)) if value]
    if len(given) > 1:
        return None, "give only one of <path>, --automatic, --on or --off"
    if given and not args.provider:
        return None, "name the provider to change"
    if not given:
        return {}, None
    target = args.provider
    if given[0] in ("path", "automatic"):
        if target == "local":
            return None, "local has no location of its own; set one for each provider"
        if target not in LOCATIONS:
            return None, f"{target} is read through Hermes; set the Hermes home instead"
        path = as_path(args.path) if given[0] == "path" else None
        changes = {f"location.{target}": str(path) if path else None}
        if path and target in SLUGS and not provider_on("local", SLUGS[target]):
            changes[f"menuBarHidden.local:{SLUGS[target]}"] = False
        return changes, None
    if target in SOURCES:
        return {f"gatewayEnabled.{target}": args.on}, None
    source, slug = target.split(":", 1) if ":" in target else ("local", SLUGS[target])
    return {f"menuBarHidden.{source}:{slug}": not args.on}, None


def print_config(records):
    marks = {"ok": ok("✓"), "warn": warn("!"), "info": dim("·")}
    rows = [record for record in records if record["provider"] not in ("local",)]
    places = [record for record in rows if "path" in record]
    names = max((len(record["provider"]) for record in rows), default=0)
    modes = max((len(mode_text(record["origin"], record["mode"] == "custom")) for record in places), default=0)
    paths = max((len(shown(record["path"])) for record in places), default=0)
    groups = [(source, [record for record in records if record["source"] == source]) for source in SOURCES]
    first = True
    for source, members in groups:
        if not members:
            continue
        missing = [record for record in members if "path" in record and source == "local" and not record["set_up"]
                   and len(records) > 1]
        if not first:
            print()
        first = False
        state = ok("on") if source_on(source) else dim("off")
        print(paint(f"{SOURCES[source]} ({source})", "1") + f": {state}")
        for record in members:
            if record["provider"] == "local" or record in missing:
                continue
            switch = "   " if record["provider"] == "hermes" else (ok("on ") if record["on"] else dim("off"))
            line = f"  {switch}  {record['provider']:<{names}}"
            if "path" in record:
                mode = mode_text(record["origin"], record["mode"] == "custom")
                line += f"  {mode:<{modes}}  {shown(record['path']):<{paths}}  {marks[record['state']]} {record['status']}"
            else:
                line += f"  {record['label']}"
            print(line.rstrip())
        if missing:
            print(dim("  Not set up on this Mac: " + ", ".join(
                f"{record['provider']} ({shown(record['path'])})" for record in missing)))


def cmd_config(args):
    changes, problem = config_change(args)
    if problem:
        print(f"provider-quotas config: {problem}", file=sys.stderr)
        return 2
    if changes:
        try:
            save_settings(changes)
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"provider-quotas config: could not save the setting: {exc}", file=sys.stderr)
            return 1
    records = [config_record(args.provider)] if args.provider else config_records()
    if args.json:
        print(json.dumps(records[0] if args.provider else records, indent=2))
        return 0
    if args.provider in SOURCES:
        records = [record for record in config_records() if record["source"] == args.provider]
    print_config(records)
    target = args.provider
    turned_on = bool(changes) and (args.on or args.path is not None) and target in SLUGS
    if turned_on and not source_on("local"):
        print(warn("\nThis Mac is off, so nothing from it shows yet. Turn it on: provider-quotas config local --on"))
    for record in records:
        variable = LOCATIONS.get(record["provider"], (None, None))[1]
        if variable and record.get("origin") == variable:
            print(warn(f"\n{variable} is set in this shell, so it wins here; the menu app uses its own setting."))
    print(dim("\nChange one:      provider-quotas config <provider> <path>"))
    print(dim("Back to default: provider-quotas config <provider> --automatic"))
    print(dim("Show or hide:    provider-quotas config <provider> --on | --off   (hermes or local turns a whole source)"))
    print(dim("The same choices are in the menu: gear button → Settings."))
    return 0


def session_check(path, session):
    where = str(path).replace(str(HOME), "~", 1)
    if not isinstance(session, dict):
        return check("Hermes Desktop session", "fail", f"missing from {where} — sign in to Hermes Desktop")
    expires = session.get("expiresAt")
    if isinstance(expires, bool) or not isinstance(expires, (int, float)):
        return check("Hermes Desktop session", "ok", f"saved for this gateway in {where}")
    left = (expires / 1000 if expires > 1e12 else expires) - time.time()
    if left > 0:
        return check("Hermes Desktop session", "ok", f"saved for this gateway in {where}, access token valid {span(left)}")
    return check("Hermes Desktop session", "warn",
                 f"saved in {where}, but its access token expired {span(-left)} ago — Hermes Desktop renews it while running")


def gateway_reachable(url, timeout):
    request = urllib.request.Request(url + "/api/status", headers={"Accept": "application/json"})
    start = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, time.monotonic() - start
    except urllib.error.HTTPError as exc:
        return exc.code, time.monotonic() - start
    except Exception as exc:
        return str(getattr(exc, "reason", exc)), time.monotonic() - start


def cmd_doctor(args):
    states = []
    print(paint("Install", "1"))
    for kind, name in HELPERS.items():
        path = helper_path(name)
        states.append(check(name, "ok" if path else "fail", str(path) if path else "not found on PATH, ~/.local/bin or Homebrew"))
    app_candidates = [HOME / "Applications/Provider Quotas.app"]
    brew = shutil.which("brew")
    if brew:
        try:
            prefix = subprocess.run([brew, "--prefix", "provider-quotas"], capture_output=True, text=True, timeout=10)
            if prefix.returncode == 0 and prefix.stdout.strip():
                app_candidates.insert(0, Path(prefix.stdout.strip()) / "Provider Quotas.app")
        except (OSError, subprocess.SubprocessError):
            pass
    apps = [app for app in app_candidates if (app / "Contents/MacOS/ProviderQuotaMenuBar").is_file()]
    for app in apps:
        binary = app / "Contents/MacOS/ProviderQuotaMenuBar"
        built = datetime.fromtimestamp(binary.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        states.append(check("app bundle", "ok", f"{app} (built {built})"))
    if not apps:
        states.append(check("app bundle", "fail", "Provider Quotas.app not found"))
    repo = app_setting("sourceRepo")
    if isinstance(repo, str) and repo:
        head = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%h %s"], capture_output=True, text=True, timeout=10)
        branch = subprocess.run(["git", "-C", repo, "branch", "--show-current"], capture_output=True, text=True, timeout=10)
        detail = f"{repo} ({branch.stdout.strip() or 'detached'} @ {head.stdout.strip()})" if head.returncode == 0 else f"{repo} (not a git checkout)"
        states.append(check("update source", "info", detail))

    print(paint("\nRunning", "1"))
    services = [(LABEL, launchd_state(LABEL)), (BREW_LABEL, launchd_state(BREW_LABEL))]
    loaded = [(label, info) for label, info in services if info is not None]
    for label, info in loaded:
        detail = f"state {info.get('state', '?')}, pid {info.get('pid', '-')}, last exit {info.get('last exit code', '-')}"
        running = info.get("state") in ("running", "active") and info.get("pid")
        states.append(check(f"launchd {label}", "ok" if running else "warn", detail))
    if not loaded:
        states.append(check("launchd service", "warn", "not loaded (start with `brew services start provider-quotas` or ./install-menubar.sh)"))
    if len(loaded) > 1:
        states.append(check("launchd service", "warn", "both the Homebrew and script services are loaded; two menus may run"))
    procs = [line.split(" ", 1)[0] for line in app_processes()]
    states.append(check("menu-bar process", "ok" if procs else "fail", f"pid {', '.join(procs)}" if procs else "not running"))

    print(paint("\nSources", "1"))
    enabled = [kind for kind in HELPERS if app_setting(f"gatewayEnabled.{kind}") in (True, 1)]
    states.append(check("enabled in menu", "ok" if enabled else "warn", ", ".join(enabled) if enabled else "none (open the menu → Sources)"))
    root, origin, custom = location("hermes")
    state, text = location_status("hermes", root, custom)
    if state == "info" and "hermes" in enabled:
        state, text = "warn", "This folder doesn't exist. Set it in the menu's Settings or with provider-quotas config hermes <path>."
    states.append(check("Hermes home", state, f"{shown(root)} ({mode_text(origin, custom)}) — {text}"))
    kind, url, conn_id = hermes_connection()
    if kind is None:
        states.append(check("Hermes Desktop connection", "warn" if "hermes" in enabled else "info", "not configured"))
    elif kind == "local":
        states.append(check("Hermes Desktop connection", "info", "local backend"))
    else:
        states.append(check("Hermes Desktop connection", "info", f"{kind} {url}"))
        states.append(session_check(*desktop_session(conn_id, url)))
        if url:
            status, elapsed = gateway_reachable(url, args.timeout)
            reachable = isinstance(status, int) and status < 500
            detail = f"HTTP {status} in {elapsed:.1f}s (unauthenticated probe)" if isinstance(status, int) else f"{status} after {elapsed:.1f}s"
            states.append(check("gateway reachable", "ok" if reachable else "fail", detail))

    for kind in HELPERS:
        if kind not in enabled and not args.all_sources:
            continue
        result = run_helper(kind, args.timeout)
        if result["payload"] is None:
            meaning = {2: "sign-in required", 124: "timed out", 127: "not installed"}.get(result["exit"], "failed")
            states.append(check(f"{kind} read", "fail", f"{meaning} (exit {result['exit']}, {result['seconds']:.1f}s): {result['stderr']}"))
            continue
        providers = result["payload"].get("providers") or []
        summary = ", ".join(f"{p.get('label') or p.get('provider')} {p.get('status')}" for p in providers) or "no providers"
        healthy = all(p.get("status") == "ok" for p in providers) and providers
        states.append(check(f"{kind} read", "ok" if healthy else "warn", f"{result['seconds']:.1f}s — {summary}"))
        for provider in providers:
            if provider.get("status") != "ok" and provider.get("message"):
                states.append(check(provider.get("label") or provider.get("provider"), "warn", provider["message"]))

    print(paint("\nProvider locations", "1"))
    for key in LOCATIONS:
        if key == "hermes":
            continue
        path, origin, custom = location(key)
        state, text = location_status(key, path, custom)
        states.append(check(LOCATIONS[key][0], state, f"{shown(path)} ({mode_text(origin, custom)}) — {text}"))

    logs = log_files()
    print(paint("\nLogs", "1"))
    if not logs:
        check("logs", "info", "none found")
    for path in logs:
        lines = [line for line in tail(path, args.log_lines) if line.strip()]
        modified = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
        check(str(path), "info", f"last written {modified}" if lines else "empty")
        for line in lines:
            print(f"      {dim(line)}")

    failures = states.count("fail")
    warnings = states.count("warn")
    print()
    if failures:
        print(bad(f"{failures} problem(s), {warnings} warning(s)"))
    elif warnings:
        print(warn(f"No problems, {warnings} warning(s)"))
    else:
        print(ok("All checks passed"))
    return 1 if failures else 0


def cmd_logs(args):
    logs = log_files()
    if not logs:
        print("No Provider Quotas logs found.", file=sys.stderr)
        return 1
    for index, path in enumerate(logs):
        if index:
            print()
        print(paint(str(path), "1"))
        for line in tail(path, args.lines):
            print(line)
    return 0


def main():
    parser = argparse.ArgumentParser(
        prog="provider-quotas",
        description="Inspect and debug the Provider Quotas menu-bar app from the terminal.",
    )
    sub = parser.add_subparsers(dest="command")
    status = sub.add_parser("status", help="show quotas as the menu reads them (default)")
    for target in (parser, status):
        target.add_argument("--source", choices=["hermes", "local", "all"], help="source to read (default: those enabled in the menu)")
        target.add_argument("--json", action="store_true", help="print the raw helper payload")
        target.add_argument("-v", "--verbose", action="store_true", help="include scopes, fetch ages and provider details")
        target.add_argument("--timeout", type=float, default=25, help="seconds to wait for each helper (default 25)")
    doctor = sub.add_parser("doctor", help="check install, service, sources, gateway and recent logs")
    doctor.add_argument("--all-sources", action="store_true", help="also read sources that are disabled in the menu")
    doctor.add_argument("--timeout", type=float, default=25, help="seconds to wait for each check (default 25)")
    doctor.add_argument("--log-lines", type=int, default=5, help="recent log lines to show per file (default 5)")
    logs = sub.add_parser("logs", help="print recent menu-bar app logs")
    logs.add_argument("-n", "--lines", type=int, default=40, help="lines per log file (default 40)")
    config = sub.add_parser(
        "config", help="show or set which providers are read and shown, and where from",
        description="Show or set which providers Provider Quotas reads and shows as activity, and where each one's "
                    "sign-in is read from. Automatic uses the provider's standard location; a path makes it custom. "
                    "The menu's Settings window shows the same choices.",
        epilog="examples:\n  provider-quotas config\n  provider-quotas config claude ~/work/.claude\n"
               "  provider-quotas config claude --automatic\n  provider-quotas config openrouter ~/keys/openrouter.env\n"
               "  provider-quotas config codex --off\n  provider-quotas config local --on\n"
               "  provider-quotas config hermes:copilot --off",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    config.add_argument("provider", nargs="?", type=config_target, metavar="provider",
                        help="hermes or local (a whole source), a provider on this Mac (claude, codex, opencode, "
                             "antigravity, openrouter), or hermes:<provider>")
    config.add_argument("path", nargs="?", help="folder to read instead (a file holding OPENROUTER_API_KEY for openrouter)")
    config.add_argument("--automatic", action="store_true", help="go back to the provider's automatic location")
    config.add_argument("--on", action="store_true", help="read it and show it as activity")
    config.add_argument("--off", action="store_true", help="stop showing it (for hermes or local, stop reading the source)")
    config.add_argument("--json", action="store_true", help="print the settings as JSON")
    args = parser.parse_args()
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "logs":
        return cmd_logs(args)
    if args.command == "config":
        return cmd_config(args)
    return cmd_status(args)


try:
    sys.exit(main())
except KeyboardInterrupt:
    sys.exit(130)
PY
