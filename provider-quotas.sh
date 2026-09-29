#!/usr/bin/env bash
set -euo pipefail
exec /usr/bin/env python3 - "$@" <<'PY'
import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

LABEL = "io.github.gabrielahn.provider-quotas"
BREW_LABEL = "homebrew.mxcl.provider-quotas"
HELPERS = {"hermes": "hermes-desktop-quotas", "local": "hermes-local-quotas"}
HOME = Path.home()
SUPPORT = HOME / "Library/Application Support/Hermes"
USE_COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


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


def defaults_read(key):
    try:
        out = subprocess.run(["defaults", "read", LABEL, key], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def enabled_sources():
    enabled = [kind for kind in HELPERS if defaults_read(f"gatewayEnabled.{kind}") == "1"]
    return enabled or ["hermes"]


def run_helper(kind, timeout):
    path = helper_path(HELPERS[kind])
    if path is None:
        return {"source": kind, "helper": None, "exit": 127, "seconds": 0.0, "stderr": f"{HELPERS[kind]} is not installed", "payload": None}
    start = time.monotonic()
    try:
        proc = subprocess.run([str(path)], capture_output=True, timeout=timeout)
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


def hermes_connection():
    try:
        conns = json.loads((SUPPORT / "connections.json").read_text())
    except (OSError, ValueError):
        return None, None
    entries = conns.get("connections") or []
    by_id = {entry.get("id"): entry for entry in entries if isinstance(entry, dict)}
    primary = by_id.get(conns.get("primary")) or (entries[0] if entries else None)
    if not isinstance(primary, dict):
        return None, None
    return primary.get("kind"), (primary.get("url") or "").rstrip("/") or None


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
    repo = defaults_read("sourceRepo")
    if repo:
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
    enabled = [kind for kind in HELPERS if defaults_read(f"gatewayEnabled.{kind}") == "1"]
    states.append(check("enabled in menu", "ok" if enabled else "warn", ", ".join(enabled) if enabled else "none (open the menu → Sources)"))
    kind, url = hermes_connection()
    if kind is None:
        states.append(check("Hermes Desktop connection", "warn" if "hermes" in enabled else "info", "not configured"))
    elif kind == "local":
        states.append(check("Hermes Desktop connection", "info", "local backend"))
    else:
        states.append(check("Hermes Desktop connection", "info", f"{kind} {url}"))
        try:
            tokens = json.loads((SUPPORT / "native-oauth-tokens.json").read_text())
            signed_in = any(str(key).strip("/") == url.strip("/") for key in tokens)
        except (OSError, ValueError, AttributeError):
            signed_in = False
        states.append(check("Hermes Desktop session", "ok" if signed_in else "fail", "saved for this gateway" if signed_in else "missing — sign in to Hermes Desktop"))
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
    args = parser.parse_args()
    if args.command == "doctor":
        return cmd_doctor(args)
    if args.command == "logs":
        return cmd_logs(args)
    return cmd_status(args)


try:
    sys.exit(main())
except KeyboardInterrupt:
    sys.exit(130)
PY
