#!/bin/bash
set -euo pipefail

response="$("$HOME/.hermes/hermes-agent/venv/bin/python" - <<'PY'
import importlib.util
import json
from pathlib import Path

path = Path.home() / ".hermes/plugins/provider-quota/dashboard/plugin_api.py"
spec = importlib.util.spec_from_file_location("provider_quota_verify", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
print(json.dumps(module._load(True)))
PY
)"
jq -e '.broker | type == "string" and length > 0' <<<"$response" >/dev/null
jq -e '[.providers[].provider] == ["openrouter", "anthropic", "openai-codex"]' <<<"$response" >/dev/null
jq -e '.providers[] | .status | IN("ok", "unavailable", "authentication_required")' <<<"$response" >/dev/null
jq -e '.providers[] | select(.provider == "openrouter") | if .status == "ok" then ([.windows[].label] == ["Account credits"] and .windows[0].remaining_amount != null and .windows[0].used_percent == null and (.details | length) == 0) else true end' <<<"$response" >/dev/null

# The last-good refresher keeps the dashboard's disk fallback current so a rotted
# long-lived process never blanks the menu. Confirm the script installed and the
# launchd job is loaded (best-effort: a warning, not a hard failure, since the
# quota endpoint itself is what the checks above prove).
refresh_script="$HOME/.hermes/plugins/provider-quota/dashboard/refresh_lastgood.py"
refresh_label="io.github.gabrielahn.provider-quota-refresh"
if [ ! -f "$refresh_script" ]; then
    echo "⚠ last-good refresher not installed ($refresh_script missing) — re-run install.sh" >&2
elif ! launchctl print "gui/$(id -u)/$refresh_label" >/dev/null 2>&1; then
    echo "⚠ last-good refresher script present but launchd job not loaded — re-run install.sh" >&2
else
    echo "✓ last-good refresher registered ($refresh_label)."
fi
