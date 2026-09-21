#!/bin/bash
# Install the single Hermes gateway plugin this menu-bar app reads. It serves
# BOTH endpoints:
#   - /api/plugins/provider-quota/quotas    (per-provider quota)
#   - /api/plugins/provider-quota/activity  (live sessions, per provider)
# The gateway serves these; the Hermes Desktop app — and the menu-bar via the
# Desktop's gateway session — read them. Run this ON THE GATEWAY HOST (where
# `hermes` and ~/.hermes live). For a REMOTE gateway, run it there, not on the
# client Mac that only runs the menu-bar.
set -euo pipefail

service_home="$(cd "$(dirname "$0")" && pwd)"
plugin_target="$HOME/.hermes/plugins/provider-quota"

if ! command -v hermes >/dev/null 2>&1 || [ ! -d "$HOME/.hermes" ]; then
    echo "Run this on the GATEWAY host (where the 'hermes' CLI and ~/.hermes live)." >&2
    echo "On a client Mac, skip it — install-menubar.sh runs it for you when a local gateway is present." >&2
    exit 1
fi

mkdir -p "$plugin_target/dashboard"
install -m 644 "$service_home/plugin.yaml" "$plugin_target/plugin.yaml"
install -m 644 "$service_home/dashboard/manifest.json" "$plugin_target/dashboard/manifest.json"
install -m 644 "$service_home/dashboard/plugin_api.py" "$plugin_target/dashboard/plugin_api.py"
install -m 755 "$service_home/dashboard/refresh_lastgood.py" "$plugin_target/dashboard/refresh_lastgood.py"
rm -rf "$plugin_target/dashboard/__pycache__" 2>/dev/null || true
hermes plugins enable provider-quota >/dev/null 2>&1 || true
echo "Installed + enabled plugin: provider-quota (quotas + activity)."

# Last-good refresher: a short-lived launchd job that re-reads each provider in a
# FRESH process every couple of minutes and writes the plugin's last-good disk
# cache. A long-lived dashboard can rot in-process (expired-token / rate-limit
# reads that a fresh process doesn't hit); this keeps its fallback current so the
# menu never blanks to "sign in" while the credentials are actually fine — no
# dashboard restart needed. Runs ON THE GATEWAY HOST (needs the venv + creds).
py="$HOME/.hermes/hermes-agent/venv/bin/python"
refresh_label="io.github.gabrielahn.provider-quota-refresh"
refresh_plist="$HOME/Library/LaunchAgents/$refresh_label.plist"
if [ -x "$py" ]; then
    mkdir -p "$HOME/Library/LaunchAgents" "$HOME/.hermes/logs"
    plutil -create xml1 "$refresh_plist"
    plutil -insert Label -string "$refresh_label" "$refresh_plist"
    plutil -insert ProgramArguments -array "$refresh_plist"
    plutil -insert ProgramArguments.0 -string "$py" "$refresh_plist"
    plutil -insert ProgramArguments.1 -string "$plugin_target/dashboard/refresh_lastgood.py" "$refresh_plist"
    plutil -insert StartInterval -integer 120 "$refresh_plist"
    plutil -insert RunAtLoad -bool true "$refresh_plist"
    plutil -insert ProcessType -string Background "$refresh_plist"
    plutil -insert StandardOutPath -string "$HOME/.hermes/logs/provider-quota-refresh.log" "$refresh_plist"
    plutil -insert StandardErrorPath -string "$HOME/.hermes/logs/provider-quota-refresh.log" "$refresh_plist"
    refresh_domain="gui/$(id -u)"
    if launchctl print "$refresh_domain/$refresh_label" >/dev/null 2>&1; then
        launchctl bootout "$refresh_domain/$refresh_label" 2>/dev/null || true
        for _ in $(seq 1 40); do
            launchctl print "$refresh_domain/$refresh_label" >/dev/null 2>&1 || break
            sleep 0.25
        done
    fi
    if launchctl bootstrap "$refresh_domain" "$refresh_plist" 2>/dev/null; then
        echo "Registered last-good refresher (every 120s) — dashboard quota self-heals without a restart."
    else
        echo "→ Could not register the last-good refresher; run refresh_lastgood.py from cron instead." >&2
    fi
else
    echo "→ Skipped last-good refresher (no gateway venv at $py)." >&2
fi

# Restart the dashboard so the new routes load — auto-detect, best-effort, so a
# fresh install is a single command with no manual restart step.
restarted=""
dash_label=$(launchctl list 2>/dev/null | awk 'tolower($3) ~ /hermes.*dashboard/ {print $3; exit}')
if [ -n "${dash_label:-}" ]; then
    launchctl kickstart -k "gui/$(id -u)/$dash_label" >/dev/null 2>&1 && restarted="launchctl:$dash_label"
fi
if [ -z "$restarted" ] && hermes dashboard restart >/dev/null 2>&1; then
    restarted="hermes dashboard restart"
fi
if [ -n "$restarted" ]; then
    echo "Restarted the dashboard ($restarted) — the routes are live."
else
    echo "→ Restart the gateway dashboard to load the routes (couldn't auto-detect it)."
fi
