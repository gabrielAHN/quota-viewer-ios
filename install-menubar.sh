#!/bin/bash
set -euo pipefail

service_home="$(cd "$(dirname "$0")" && pwd)"
app_target="$HOME/Applications/Provider Quotas.app"
binary_target="$app_target/Contents/MacOS/ProviderQuotaMenuBar"
plist_target="$HOME/Library/LaunchAgents/io.github.gabrielahn.provider-quotas.plist"
launch_target="gui/$(id -u)"
label="io.github.gabrielahn.provider-quotas"

# Preflight: the app is built with the Swift compiler from Xcode's command-line
# tools. Fail early with the one-line fix instead of a cryptic xcrun error.
if ! xcrun --find swiftc >/dev/null 2>&1; then
    echo "→ Xcode command-line tools are required (for swiftc)." >&2
    echo "  Accept the macOS install prompt that opens, then re-run: bash install-menubar.sh" >&2
    xcode-select --install >/dev/null 2>&1 || true
    exit 1
fi

# Record where we installed from so the app's "Check for Updates" can `git pull`
# this checkout and rebuild (update.sh). No-op for non-git installs.
defaults write "$label" sourceRepo "$service_home" 2>/dev/null || true

# Also install the gateway plugin this app reads — provider-quota (quota) AND
# session-activity (live sessions) — so a fresh menu install sets up BOTH
# endpoints. Best-effort: only when a Hermes gateway is present on THIS machine.
# For a REMOTE gateway, run install.sh on the gateway host instead.
if command -v hermes >/dev/null 2>&1 && [ -d "$HOME/.hermes/plugins" ]; then
    bash "$service_home/install.sh" || echo "  (gateway plugin install skipped)"
else
    echo "  (no local Hermes gateway detected — install plugins on the gateway host: bash install.sh)"
fi

mkdir -p "$app_target/Contents/MacOS" "$HOME/Library/LaunchAgents" "$HOME/.hermes/logs" "$HOME/.local/bin"
# Data sources: the Hermes Desktop app's gateway API (desktop-quotas.sh), and a
# gateway-agnostic fallback that queries each provider's usage API directly from
# local credentials (local-quotas.sh) — so quotas show under Goose, or with no
# gateway running, not just Hermes.
install -m 755 "$service_home/desktop-quotas.sh" "$HOME/.local/bin/hermes-desktop-quotas"
install -m 755 "$service_home/local-quotas.sh" "$HOME/.local/bin/hermes-local-quotas"
install -m 755 "$service_home/provider-quotas.sh" "$HOME/.local/bin/provider-quotas"

# Curated pets: Boba / Capy / Scoop from the petdex catalog into ~/.hermes/pets so
# the menu offers them (shared with the Homebrew formula). Best-effort.
bash "$service_home/bootstrap-pets.sh" || true

xcrun swiftc -O -parse-as-library -framework AppKit "$service_home/menubar/ProviderQuotaMenuBar.swift" -o "$binary_target"
install -m 644 "$service_home/menubar/Info.plist" "$app_target/Contents/Info.plist"
codesign --force --sign - "$app_target" >/dev/null

plutil -create xml1 "$plist_target"
plutil -insert Label -string "$label" "$plist_target"
plutil -insert ProgramArguments -array "$plist_target"
plutil -insert ProgramArguments.0 -string "$binary_target" "$plist_target"
plutil -insert RunAtLoad -bool true "$plist_target"
plutil -insert ProcessType -string Interactive "$plist_target"
plutil -insert StandardOutPath -string "$HOME/.hermes/logs/provider-quotas.log" "$plist_target"
plutil -insert StandardErrorPath -string "$HOME/.hermes/logs/provider-quotas-error.log" "$plist_target"

if launchctl print "$launch_target/$label" >/dev/null 2>&1; then
    launchctl bootout "$launch_target/$label"
    for attempt in $(seq 1 40); do
        if ! launchctl print "$launch_target/$label" >/dev/null 2>&1; then
            break
        fi
        sleep 0.25
    done
fi
launchctl bootstrap "$launch_target" "$plist_target"

# Confirm it came up, and point at the menu bar so the install ends with a clear
# "what now".
for _ in $(seq 1 20); do
    pgrep -f "$binary_target" >/dev/null 2>&1 && break
    sleep 0.25
done
if pgrep -f "$binary_target" >/dev/null 2>&1; then
    echo "✓ Provider Quotas is running — its icon is in the menu bar (top-right)."
    echo "  Click it, then turn on Hermes and/or Local under \"Sources\"."
else
    echo "⚠ It didn't start — check ~/.hermes/logs/provider-quotas-error.log" >&2
fi
