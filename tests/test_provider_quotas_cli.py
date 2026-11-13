import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone


CLI = Path(__file__).resolve().parents[1] / "provider-quotas.sh"
FAKE_SECURITY = """#!/bin/bash
service="" account=""
while [ $# -gt 0 ]; do
    case "$1" in
        -s) service=$2; shift 2 ;;
        -a) account=$2; shift 2 ;;
        *) shift ;;
    esac
done
IFS=, read -ra entries <<< "$FAKE_KEYCHAIN"
for entry in "${entries[@]}"; do
    name=${entry%%@*}
    owner=""
    case "$entry" in *@*) owner=${entry#*@} ;; esac
    if [ "$name" = "$service" ] && { [ -z "$account" ] || [ "$owner" = "$account" ]; }; then
        exit 0
    fi
done
exit 44
"""


def iso(delta):
    return (datetime.now(timezone.utc) + delta).isoformat()


def payload():
    return {
        "broker": "test-broker",
        "generated_at": iso(timedelta(seconds=-5)),
        "providers": [
            {
                "provider": "anthropic", "label": "Claude", "status": "ok", "plan": "Team",
                "fetched_at": iso(timedelta(seconds=-5)), "source": "fixture",
                "windows": [
                    {"label": "Current session", "used_percent": 34.0, "remaining_percent": 66.0,
                     "resets_at": iso(timedelta(hours=2, minutes=5)), "window_seconds": 18000, "scope": "account"},
                    {"label": "Fable week", "used_percent": 0.4, "remaining_percent": 99.6,
                     "resets_at": iso(timedelta(days=5, hours=3)), "window_seconds": 604800, "scope": "model:Fable"},
                ],
                "details": ["Extra usage: 1.00 / 2.00 USD"],
            },
            {
                "provider": "openrouter", "label": "OpenRouter", "status": "ok",
                "windows": [{"label": "Account credits", "used_percent": None, "remaining_percent": None,
                             "remaining_amount": 10.41, "currency": "USD", "resets_at": None}],
            },
            {
                "provider": "openai-codex", "label": "Codex", "status": "authentication_required",
                "windows": [], "message": "Sign in to Codex.",
            },
        ],
    }


class ProviderQuotasCliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "home"
        self.bin = Path(self.tmp.name) / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        self.calls = Path(self.tmp.name) / "calls.jsonl"
        self.prefs = Path(self.tmp.name) / "prefs.plist"
        self.payload = payload()
        self.fake_bin = Path(self.tmp.name) / "fake-bin"
        self.fake_bin.mkdir()
        (self.fake_bin / "security").write_text(FAKE_SECURITY)
        (self.fake_bin / "security").chmod(0o755)

    def helper(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(f"""
            import json, os, sys, time
            with open({str(self.calls)!r}, "a") as handle:
                handle.write(json.dumps({{"helper": {name!r}, "argv": sys.argv[1:],
                                          "home": os.environ.get("PROVIDER_QUOTAS_HERMES_HOME"),
                                          "locations": {{k: v for k, v in os.environ.items()
                                                        if k.startswith("PROVIDER_QUOTAS_") and k.endswith(("_HOME", "_ENV"))}}}}) + "\\n")
        """) + textwrap.dedent(body))
        path.chmod(0o755)

    def payload_helper(self, name, payload=None):
        self.helper(name, f"sys.stdout.write({json.dumps(json.dumps(payload or self.payload))})\n")

    def write_prefs(self, **values):
        with open(self.prefs, "wb") as handle:
            plistlib.dump(values, handle)

    def read_prefs(self):
        if not self.prefs.exists():
            return {}
        with open(self.prefs, "rb") as handle:
            return plistlib.load(handle)

    def run_cli(self, *args, timeout=60, **extra):
        env = {
            "HOME": str(self.home),
            "PATH": f"{self.fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin",
            "PROVIDER_QUOTAS_BIN_DIR": str(self.bin),
            "PROVIDER_QUOTAS_DEFAULTS": str(self.prefs),
            "NO_COLOR": "1",
            **extra,
        }
        return subprocess.run(["bash", str(CLI), *args], capture_output=True, text=True, env=env, timeout=timeout)

    def helper_calls(self):
        if not self.calls.exists():
            return []
        return [json.loads(line) for line in self.calls.read_text().splitlines()]

    def test_status_renders_used_left_period_balance_and_reset(self):
        self.payload_helper("hermes-desktop-quotas")
        result = self.run_cli("--source", "hermes")
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout
        self.assertIn("HERMES source", out)
        self.assertIn("broker test-broker", out)
        self.assertRegex(out, r"Current session\s+34% used · 66% left\s+5h\s+resets in 2h [45]m")
        self.assertRegex(out, r"Fable week\s+<1% used · >99% left\s+weekly\s+resets in 5d [23]h")
        self.assertRegex(out, r"Account credits\s+\$10\.41 available\s+balance\s+no reset reported")
        self.assertIn("Claude [Team]  ok", out)
        self.assertIn("Codex  sign-in required", out)
        self.assertIn("Sign in to Codex.", out)
        self.assertNotIn("scope model:Fable", out)
        self.assertNotIn("Extra usage", out)

    def test_verbose_adds_scope_source_and_details(self):
        self.payload_helper("hermes-desktop-quotas")
        result = self.run_cli("--source", "hermes", "-v")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("scope model:Fable", result.stdout)
        self.assertIn("scope account", result.stdout)
        self.assertIn("fixture", result.stdout)
        self.assertIn("Extra usage: 1.00 / 2.00 USD", result.stdout)

    def test_helpers_are_run_without_arguments(self):
        self.payload_helper("hermes-desktop-quotas")
        self.payload_helper("hermes-local-quotas")
        self.assertEqual(self.run_cli("--source", "all", PROVIDER_QUOTAS_CODEX_HOME="").returncode, 0)
        home = str(self.home / ".hermes")
        self.assertEqual(self.helper_calls(), [
            {"helper": "hermes-desktop-quotas", "argv": [], "home": home, "locations": {"PROVIDER_QUOTAS_HERMES_HOME": home}},
            {"helper": "hermes-local-quotas", "argv": [], "home": home, "locations": {"PROVIDER_QUOTAS_HERMES_HOME": home}},
        ])

    def test_hermes_home_set_in_the_app_reaches_doctor_and_helpers(self):
        self.payload_helper("hermes-desktop-quotas")
        chosen = self.home / "Work Hermes"
        chosen.mkdir()
        (chosen / "gateway-connections.json").write_text(json.dumps({
            "version": 2, "primary": "gateway",
            "connections": [{"id": "gateway", "kind": "remote", "url": "http://127.0.0.1:9/work"}]}))
        self.write_prefs(**{"location.hermes": str(chosen / "profiles" / "coder"), "gatewayEnabled.hermes": True})
        out = self.run_cli("doctor", "--timeout", "2").stdout
        self.assertIn("enabled in menu: hermes", out)
        self.assertIn("! Hermes home: ~/Work Hermes (custom) — 127.0.0.1:9/work: not signed in. Sign in from Hermes Desktop.", out)
        self.assertIn("Hermes Desktop connection: remote http://127.0.0.1:9/work", out)
        self.assertIn("Hermes Desktop session: missing from ~/Work Hermes/gateway-connection-tokens.json", out)
        self.assertEqual({call["home"] for call in self.helper_calls()}, {str(chosen)})

    def test_doctor_reports_the_default_hermes_home_and_a_missing_folder(self):
        self.payload_helper("hermes-desktop-quotas")
        self.assertIn("· Hermes home: ~/.hermes (automatic) — Not set up on this Mac.",
                      self.run_cli("doctor", "--timeout", "2").stdout)
        self.write_prefs(**{"gatewayEnabled.hermes": True})
        self.assertIn("! Hermes home: ~/.hermes (automatic) — This folder doesn't exist. Set it in the menu's Settings "
                      "or with provider-quotas config hermes <path>.", self.run_cli("doctor", "--timeout", "2").stdout)
        (self.home / ".hermes").mkdir()
        self.assertIn("! Hermes home: ~/.hermes (automatic) — No Hermes Desktop connection found here.",
                      self.run_cli("doctor", "--timeout", "2").stdout)

    def test_config_lists_every_provider_and_how_to_change_it(self):
        (self.home / ".codex").mkdir()
        (self.home / ".codex/auth.json").write_text("{}")
        (self.home / ".claude").mkdir()
        self.write_prefs(**{"gatewayEnabled.hermes": True, "menuBarHidden.local:openai-codex": True,
                            "menuBarHidden.hermes:copilot": True,
                            "lastProviderSlugs.hermes": ["openrouter", "copilot"],
                            "lastProviderLabels.hermes": ["OpenRouter", "Copilot"]})
        result = self.run_cli("config", FAKE_KEYCHAIN="Claude Code-credentials")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "Hermes (hermes): on")
        self.assertRegex(lines[1], r"^\s+hermes\s+automatic\s+~/\.hermes\s+· Not set up on this Mac\.$")
        self.assertRegex(lines[2], r"^  on   hermes:openrouter\s+OpenRouter$")
        self.assertRegex(lines[3], r"^  off  hermes:copilot\s+Copilot$")
        self.assertEqual(lines[5], "This Mac (local): off")
        self.assertRegex(lines[6], r"^  on   claude\s+automatic\s+~/\.claude\s+✓ Signed in \(Keychain: Claude Code-credentials\)\.$")
        self.assertRegex(lines[7], r"^  off  codex\s+automatic\s+~/\.codex\s+✓ Signed in \(auth\.json\)\.$")
        self.assertEqual(lines[8], "  Not set up on this Mac: opencode (~/.local/share/opencode), "
                                   "antigravity (~/.gemini/antigravity-cli), openrouter (~/.hermes/.env)")
        self.assertIn("Change one:      provider-quotas config <provider> <path>", result.stdout)
        self.assertIn("Back to default: provider-quotas config <provider> --automatic", result.stdout)
        self.assertIn("Show or hide:    provider-quotas config <provider> --on | --off", result.stdout)
        records = json.loads(self.run_cli("config", "--json", FAKE_KEYCHAIN="Claude Code-credentials").stdout)
        self.assertEqual([(record["provider"], record["on"], record.get("set_up")) for record in records], [
            ("hermes", True, False), ("hermes:openrouter", True, None), ("hermes:copilot", False, None),
            ("local", False, None), ("claude", True, True), ("codex", False, True), ("opencode", True, False),
            ("antigravity", True, False), ("openrouter", True, False)])
        single = self.run_cli("config", "antigravity")
        self.assertRegex(single.stdout, r"on   antigravity\s+automatic\s+~/\.gemini/antigravity-cli\s+· Not set up on this Mac\.")

    def test_config_sets_and_clears_a_custom_location_that_helpers_receive(self):
        self.payload_helper("hermes-local-quotas")
        work = self.home / "Work Codex"
        work.mkdir()
        result = self.run_cli("config", "codex", "~/Work Codex/")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_prefs(), {"location.codex": str(work)})
        self.assertRegex(result.stdout, r"codex\s+custom\s+~/Work Codex\s+! No auth\.json here\. Run codex login\.")
        self.assertEqual(self.run_cli("--source", "local").returncode, 0)
        self.assertEqual(self.helper_calls()[-1]["locations"], {
            "PROVIDER_QUOTAS_HERMES_HOME": str(self.home / ".hermes"), "PROVIDER_QUOTAS_CODEX_HOME": str(work)})
        record = json.loads(self.run_cli("config", "codex", "--json").stdout)
        self.assertEqual(record, {"provider": "codex", "label": "Codex", "source": "local", "on": True, "set_up": True,
                                  "mode": "custom", "origin": "custom", "path": str(work), "state": "warn",
                                  "status": "No auth.json here. Run codex login."})
        result = self.run_cli("config", "codex", "--automatic")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_prefs(), {})
        self.assertRegex(result.stdout, r"codex\s+automatic\s+~/\.codex\s+· Not set up on this Mac\.")
        self.assertEqual(self.run_cli("--source", "local").returncode, 0)
        self.assertNotIn("PROVIDER_QUOTAS_CODEX_HOME", self.helper_calls()[-1]["locations"])

    def test_config_keeps_other_app_settings_and_flags_shell_overrides(self):
        self.write_prefs(**{"gatewayEnabled.hermes": True, "appAppearance": "dark"})
        self.assertEqual(self.run_cli("config", "openrouter", str(self.home / "keys.env")).returncode, 0)
        self.assertEqual(self.read_prefs(), {"gatewayEnabled.hermes": True, "appAppearance": "dark",
                                             "location.openrouter": str(self.home / "keys.env")})
        result = self.run_cli("config", "claude", PROVIDER_QUOTAS_CLAUDE_HOME=str(self.home / "elsewhere"))
        self.assertRegex(result.stdout, r"claude\s+custom, PROVIDER_QUOTAS_CLAUDE_HOME\s+~/elsewhere\s+! This folder doesn't exist\.")
        self.assertIn("PROVIDER_QUOTAS_CLAUDE_HOME is set in this shell, so it wins here; the menu app uses its own setting.",
                      result.stdout)

    def test_config_rejects_contradictory_arguments(self):
        for args in (("claude", "/tmp/x", "--automatic"), ("claude", "--on", "--off"), ("codex", "/tmp/x", "--off")):
            result = self.run_cli("config", *args)
            self.assertEqual(result.returncode, 2, args)
            self.assertIn("give only one of <path>, --automatic, --on or --off", result.stderr)
        result = self.run_cli("config", "--off")
        self.assertEqual(result.returncode, 2)
        self.assertIn("name the provider to change", result.stderr)
        result = self.run_cli("config", "copilot")
        self.assertEqual(result.returncode, 2)
        self.assertIn("invalid choice: 'copilot'", result.stderr)
        result = self.run_cli("config", "hermes:copilot", "--off")
        self.assertEqual(result.returncode, 2)
        self.assertIn("no Hermes provider named 'copilot' (Hermes hasn't listed its providers yet)", result.stderr)
        result = self.run_cli("config", "local", "/tmp/x")
        self.assertEqual(result.returncode, 2)
        self.assertIn("local has no location of its own", result.stderr)
        self.assertFalse(self.prefs.exists())
        self.write_prefs(**{"lastProviderSlugs.hermes": ["openrouter"], "lastProviderLabels.hermes": ["OpenRouter"]})
        result = self.run_cli("config", "hermes:openrouter", "/tmp/x")
        self.assertEqual(result.returncode, 2)
        self.assertIn("hermes:openrouter is read through Hermes; set the Hermes home instead", result.stderr)
        result = self.run_cli("config", "hermes:copilot", "--on")
        self.assertIn("no Hermes provider named 'copilot' (Hermes lists hermes:openrouter)", result.stderr)

    def test_config_turns_sources_and_providers_on_and_off(self):
        slugs = {"lastProviderSlugs.hermes": ["openrouter", "copilot"],
                 "lastProviderLabels.hermes": ["OpenRouter", "Copilot"]}
        self.write_prefs(**slugs, appAppearance="dark")
        steps = [
            (("local", "--on"), {"gatewayEnabled.local": True}),
            (("hermes", "--off"), {"gatewayEnabled.hermes": False}),
            (("codex", "--off"), {"menuBarHidden.local:openai-codex": True}),
            (("claude", "--off"), {"menuBarHidden.local:anthropic": True}),
            (("claude", "--on"), {"menuBarHidden.local:anthropic": False}),
            (("hermes:copilot", "--off"), {"menuBarHidden.hermes:copilot": True}),
        ]
        expected = {**slugs, "appAppearance": "dark"}
        for args, change in steps:
            with self.subTest(args=args):
                result = self.run_cli("config", *args)
                self.assertEqual(result.returncode, 0, result.stderr)
                expected.update(change)
                self.assertEqual(self.read_prefs(), expected)
        self.assertIn("  off  hermes:copilot", self.run_cli("config", "hermes:copilot").stdout)
        group = self.run_cli("config", "hermes").stdout.splitlines()
        self.assertEqual(group[0], "Hermes (hermes): off")
        self.assertRegex(group[2], r"^  on   hermes:openrouter\s+OpenRouter$")
        self.assertRegex(group[3], r"^  off  hermes:copilot\s+Copilot$")
        self.assertEqual(json.loads(self.run_cli("config", "local", "--json").stdout),
                         {"provider": "local", "label": "This Mac", "source": "local", "on": True})

    def test_config_choosing_a_location_turns_a_hidden_provider_back_on(self):
        self.write_prefs(**{"menuBarHidden.local:antigravity": True})
        folder = self.home / "agy"
        folder.mkdir()
        result = self.run_cli("config", "antigravity", str(folder))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_prefs(), {"menuBarHidden.local:antigravity": False, "location.antigravity": str(folder)})
        self.assertIn("This Mac is off, so nothing from it shows yet. Turn it on: provider-quotas config local --on",
                      result.stdout)
        self.assertNotIn("This Mac is off", self.run_cli("config", "antigravity", "--automatic").stdout)
        self.assertEqual(self.read_prefs(), {"menuBarHidden.local:antigravity": False})

    def test_doctor_lists_provider_locations(self):
        self.payload_helper("hermes-desktop-quotas")
        missing = self.home / "gone"
        self.write_prefs(**{"location.claude": str(missing)})
        out = self.run_cli("doctor", "--timeout", "2").stdout
        self.assertIn("Provider locations", out)
        self.assertIn("! Claude: ~/gone (custom) — This folder doesn't exist.", out)
        self.assertIn("· Codex: ~/.codex (automatic) — Not set up on this Mac.", out)
        self.assertIn("· OpenRouter: ~/.hermes/.env (automatic, Hermes home) — No OPENROUTER_API_KEY found.", out)

    def test_json_passes_payload_through(self):
        self.payload_helper("hermes-desktop-quotas")
        result = self.run_cli("--source", "hermes", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), self.payload)

    def test_json_all_sources_keys_each_source_and_reports_failures(self):
        self.payload_helper("hermes-desktop-quotas")
        self.helper("hermes-local-quotas", "sys.stderr.write('boom\\n'); sys.exit(1)\n")
        result = self.run_cli("--source", "all", "--json")
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        self.assertEqual(data["hermes"], self.payload)
        self.assertEqual(data["local"], {"error": "boom", "exit": 1})

    def test_sign_in_failure_exits_two(self):
        self.helper("hermes-desktop-quotas", "sys.stderr.write('Sign in to Hermes Desktop.\\n'); sys.exit(2)\n")
        result = self.run_cli("--source", "hermes")
        self.assertEqual(result.returncode, 2)
        self.assertIn("failed (exit 2): Sign in to Hermes Desktop.", result.stdout)

    def test_invalid_json_is_reported(self):
        self.helper("hermes-desktop-quotas", "sys.stdout.write('not json')\n")
        result = self.run_cli("--source", "hermes")
        self.assertEqual(result.returncode, 1)
        self.assertIn("helper printed invalid JSON", result.stdout)

    def test_hung_helper_times_out(self):
        self.helper("hermes-desktop-quotas", "time.sleep(30)\n")
        result = self.run_cli("--source", "hermes", "--timeout", "0.5")
        self.assertEqual(result.returncode, 1)
        self.assertIn("timed out after 0.5s", result.stdout)

    def test_missing_helper_is_reported(self):
        result = self.run_cli("--source", "local")
        self.assertEqual(result.returncode, 1)
        self.assertIn("hermes-local-quotas is not installed", result.stdout)

    def state_files(self):
        return sorted(
            p.relative_to(self.home) for p in self.home.rglob("*")
            if p.relative_to(self.home).parts[:2] not in (("Library",), ("Library", "Caches"))
        )

    def test_doctor_reports_helper_results_without_touching_hermes_state(self):
        self.payload_helper("hermes-desktop-quotas")
        self.helper("hermes-local-quotas", "sys.stderr.write('Sign in first.\\n'); sys.exit(2)\n")
        logs = self.home / ".hermes/logs"
        logs.mkdir(parents=True)
        (logs / "provider-quotas-error.log").write_text("first\nlast line\n")
        before = self.state_files()
        result = self.run_cli("doctor", "--all-sources", "--timeout", "10")
        out = result.stdout
        self.assertEqual(result.returncode, 1, out)
        self.assertIn(f"hermes-desktop-quotas: {self.bin / 'hermes-desktop-quotas'}", out)
        self.assertIn("Hermes Desktop connection: not configured", out)
        self.assertRegex(out, r"hermes read: [\d.]+s — Claude ok, OpenRouter ok, Codex authentication_required")
        self.assertIn("Codex: Sign in to Codex.", out)
        self.assertRegex(out, r"local read: sign-in required \(exit 2, [\d.]+s\): Sign in first\.")
        self.assertIn("last line", out)
        self.assertRegex(out, r"\d+ problem\(s\), \d+ warning\(s\)")
        self.assertEqual(self.state_files(), before)

    def test_doctor_checks_the_gateway_not_this_device(self):
        self.payload_helper("hermes-desktop-quotas")
        support = self.home / "Library/Application Support/Hermes"
        support.mkdir(parents=True)
        (support / "connections.json").write_text(json.dumps({
            "version": 2, "primary": "gateway", "lastUsed": "local", "launchMode": "last-used",
            "connections": [{"id": "local", "kind": "local"},
                            {"id": "gateway", "kind": "remote", "url": "http://127.0.0.1:9/hermes/"}]}))
        result = self.run_cli("doctor", "--timeout", "2")
        self.assertIn("Hermes Desktop connection: remote http://127.0.0.1:9/hermes", result.stdout)
        self.assertNotIn("local backend", result.stdout)

    def test_doctor_reads_the_migrated_desktop_store(self):
        self.payload_helper("hermes-desktop-quotas")
        support = self.home / "Library/Application Support/Hermes"
        support.mkdir(parents=True)
        (support / "connections.json").write_text(json.dumps({
            "version": 2, "primary": "old", "connections": [{"id": "old", "kind": "remote", "url": "http://127.0.0.1:8/old"}]}))
        (support / "native-oauth-tokens.json").write_text(json.dumps({"http://127.0.0.1:9/hermes": {"encoding": "plain", "value": "{}"}}))
        store = self.home / ".hermes"
        store.mkdir()
        (store / "gateway-connections.json").write_text(json.dumps({
            "version": 2, "primary": "gateway", "lastUsed": "local",
            "connections": [{"id": "local", "kind": "local"},
                            {"id": "gateway", "kind": "remote", "url": "http://127.0.0.1:9/hermes"}]}))
        tokens = store / "gateway-connection-tokens.json"

        def doctor(expires_in):
            expires = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).timestamp() * 1000
            tokens.write_text(json.dumps({"version": 1, "tokens": {"gateway": {
                "accessToken": "x", "expiresAt": int(expires), "baseUrl": "http://127.0.0.1:9/hermes"}}}))
            return self.run_cli("doctor", "--timeout", "2").stdout

        out = doctor(2 * 3600 + 120)
        self.assertIn("Hermes Desktop connection: remote http://127.0.0.1:9/hermes", out)
        self.assertRegex(out, r"Hermes Desktop session: saved for this gateway in ~/\.hermes/gateway-connection-tokens\.json, "
                              r"access token valid 2h [12]m")
        self.assertRegex(doctor(-45 * 60), r"Hermes Desktop session: saved in ~/\.hermes/gateway-connection-tokens\.json, "
                                           r"but its access token expired 4[56]m ago")
        tokens.unlink()
        self.assertIn("Hermes Desktop session: missing from ~/.hermes/gateway-connection-tokens.json — sign in to Hermes Desktop",
                      self.run_cli("doctor", "--timeout", "2").stdout)

    def test_logs_prints_tail(self):
        logs = self.home / ".hermes/logs"
        logs.mkdir(parents=True)
        (logs / "provider-quotas-error.log").write_text("".join(f"line {i}\n" for i in range(10)))
        result = self.run_cli("logs", "-n", "3")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("line 7\nline 8\nline 9", result.stdout)
        self.assertNotIn("line 6", result.stdout)


if __name__ == "__main__":
    unittest.main()
