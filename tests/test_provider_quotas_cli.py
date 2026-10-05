import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest
from datetime import datetime, timedelta, timezone


CLI = Path(__file__).resolve().parents[1] / "provider-quotas.sh"


def iso(delta):
    return (datetime.now(timezone.utc) + delta).isoformat()


PAYLOAD = {
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

    def helper(self, name, body):
        path = self.bin / name
        path.write_text("#!/usr/bin/env python3\n" + textwrap.dedent(f"""
            import json, os, sys, time
            with open({str(self.calls)!r}, "a") as handle:
                handle.write(json.dumps({{"helper": {name!r}, "argv": sys.argv[1:]}}) + "\\n")
        """) + textwrap.dedent(body))
        path.chmod(0o755)

    def payload_helper(self, name, payload=PAYLOAD):
        self.helper(name, f"sys.stdout.write({json.dumps(json.dumps(payload))})\n")

    def run_cli(self, *args, timeout=60):
        env = {
            "HOME": str(self.home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PROVIDER_QUOTAS_BIN_DIR": str(self.bin),
            "NO_COLOR": "1",
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
        self.assertEqual(self.run_cli("--source", "all").returncode, 0)
        self.assertEqual(self.helper_calls(), [
            {"helper": "hermes-desktop-quotas", "argv": []},
            {"helper": "hermes-local-quotas", "argv": []},
        ])

    def test_json_passes_payload_through(self):
        self.payload_helper("hermes-desktop-quotas")
        result = self.run_cli("--source", "hermes", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), PAYLOAD)

    def test_json_all_sources_keys_each_source_and_reports_failures(self):
        self.payload_helper("hermes-desktop-quotas")
        self.helper("hermes-local-quotas", "sys.stderr.write('boom\\n'); sys.exit(1)\n")
        result = self.run_cli("--source", "all", "--json")
        self.assertEqual(result.returncode, 1)
        data = json.loads(result.stdout)
        self.assertEqual(data["hermes"], PAYLOAD)
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
