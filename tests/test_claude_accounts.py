"""Per-account Claude quota: named providers read through their own key_cmd tokens."""
import copy
import importlib.util
import json
import pathlib
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from test_quota_window_metadata import load_local_reader, load_plugin  # noqa: E402

spec = importlib.util.spec_from_file_location("claude_accounts", ROOT / "dashboard" / "claude_accounts.py")
accounts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(accounts)

FUTURE = "2026-10-24T03:00:00.194200+00:00"
USAGE = {
    "five_hour": {"utilization": 100.0, "resets_at": "2026-10-22T04:30:00Z"},
    "seven_day": {"utilization": 35.0, "resets_at": FUTURE},
    "seven_day_opus": None,
    "seven_day_sonnet": {"utilization": 1.0, "resets_at": FUTURE},
    "limits": [{"kind": "weekly_scoped", "group": "weekly", "percent": 3, "resets_at": FUTURE,
                "scope": {"model": {"id": None, "display_name": "Fable"}, "surface": None}},
               {"kind": "weekly_scoped", "percent": 9, "scope": {"model": {"display_name": "sonnet"}}},
               {"kind": "session", "percent": 50}],
    "extra_usage": {"is_enabled": True, "used_credits": 1.5, "monthly_limit": 20, "currency": "USD"},
}
CONFIG = {
    "providers": {
        "claude-personal": {"name": "Claude (Personal)", "api": "https://api.anthropic.com", "key_cmd": "tok a"},
        "local-llm": {"name": "Local", "api": "http://localhost:8080/v1", "key_cmd": "tok b"},
        "claude-team": {"name": "Claude (Team)", "base_url": "https://api.anthropic.com/", "key_cmd": "tok c"},
        "no-key": {"name": "No key", "api": "https://api.anthropic.com"},
        "off": {"name": "Off", "api": "https://api.anthropic.com", "key_cmd": "tok d", "enabled": False},
        "proxy": {"name": "Proxy", "api": "https://api.anthropic.com.evil.example", "key_cmd": "tok e"},
    },
    "model_catalog": {"excluded_providers": ["copilot", "anthropic"]},
}


def http_error(code):
    return urllib.error.HTTPError("https://api.anthropic.com", code, "x", {}, None)


class NamedAccountConfigTests(unittest.TestCase):
    def test_only_enabled_anthropic_hosts_with_a_key_cmd_are_accounts(self):
        self.assertEqual(accounts.named_accounts(CONFIG), [
            ("claude-personal", "Claude (Personal)", "tok a"), ("claude-team", "Claude (Team)", "tok c")])
        self.assertEqual(accounts.named_accounts({}), [])
        self.assertEqual(accounts.named_accounts({"providers": ["not", "a", "mapping"]}), [])

    def test_the_builtin_row_is_hidden_only_when_the_gateway_hides_it(self):
        found = accounts.named_accounts(CONFIG)
        self.assertEqual(accounts.hidden_builtins(CONFIG, found), ["anthropic"])
        self.assertEqual(accounts.hidden_builtins(CONFIG, []), [])
        self.assertEqual(accounts.hidden_builtins({"model_catalog": {"excluded_providers": ["copilot"]}}, found), [])
        self.assertEqual(accounts.hidden_builtins({"model_catalog": None}, found), [])

    def test_oauth_shape_matches_hermes(self):
        for token, expected in (("sk-ant-oat01-x", True), ("eyJhbGciOi", True), ("cc-x", True),
                                ("sk-ant-api03-x", False), ("sk-ant-usr01-x", False), ("", False), ("abc", False)):
            with self.subTest(token=token):
                self.assertIs(accounts.is_oauth_token(token), expected)


class UsageParsingTests(unittest.TestCase):
    def test_windows_match_the_local_claude_reader(self):
        reader = load_local_reader()
        with patch.dict(reader, {"_anthropic_token": lambda: "sk-ant-oat-fixture",
                                 "_get": lambda *a, **k: copy.deepcopy(USAGE)}):
            local = reader["anthropic_provider"]()
        windows, details = accounts.parse_usage(copy.deepcopy(USAGE))
        self.assertEqual([(w["label"], w["used_percent"], w["window_seconds"], w["scope"], w["window_id"])
                          for w in windows],
                         [(w["label"], 100.0 - w["remaining_percent"], w["window_seconds"], w["scope"], w["window_id"])
                          for w in local["windows"]])
        self.assertEqual(details, local["details"])
        self.assertEqual(windows[0]["reset_at"], "2026-10-22T04:30:00+00:00")
        self.assertEqual(windows[2]["used_percent"], 1.0)  # a 1% window is not used up

    def test_malformed_payloads_keep_valid_windows(self):
        windows, details = accounts.parse_usage({"five_hour": {"utilization": "x"}, "seven_day": {"utilization": 120},
                                                 "limits": "nope", "extra_usage": {"is_enabled": True}})
        self.assertEqual([(w["label"], w["used_percent"]) for w in windows], [("Current week", 100.0)])
        self.assertEqual(details, [])
        self.assertEqual(accounts.parse_usage(None), ([], []))

    def test_plan_badges(self):
        for org, expected in (({"organization_type": "claude_team", "rate_limit_tier": "default_claude_max_5x"}, "Team"),
                              ({"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_5x"}, "Max 5x"),
                              ({"organization_type": "claude_max", "rate_limit_tier": "default_claude_max_20x"}, "Max 20x"),
                              ({"organization_type": "claude_max"}, "Max"),
                              ({"organization_type": "claude_pro"}, "Pro"),
                              ({"organization_type": "claude_enterprise"}, "Enterprise"),
                              ({}, None)):
            with self.subTest(org=org):
                self.assertEqual(accounts.plan_label({"organization": org}), expected)
        self.assertIsNone(accounts.plan_label(None))


class FetchAccountTests(unittest.TestCase):
    def fetch(self, token="sk-ant-oat01-secret", usage=USAGE, profile=None, previous=None, fail=None):
        calls = []

        def get_json(url, tok):
            calls.append(url.rsplit("/", 1)[-1])
            self.assertEqual(tok, token)
            if fail and url.endswith(fail[0]):
                raise fail[1]
            return copy.deepcopy(usage if url.endswith("usage") else
                                 profile or {"organization": {"organization_type": "claude_team"}})
        record = accounts.fetch_account("Claude (Team)", "tok", previous, mint=lambda cmd, label: token,
                                        get_json=get_json)
        self.assertNotIn("secret", json.dumps(record))
        return record, calls

    def test_a_good_read_is_an_official_shape_claude_account_record(self):
        record, calls = self.fetch()
        self.assertEqual(calls, ["usage", "profile"])
        self.assertEqual((record["label"], record["plan"], record["unavailable_reason"], record["family"], record["account"]),
                         ("Claude (Team)", "Team", None, "anthropic", True))
        self.assertEqual(record["windows"][0]["label"], "Current session")
        self.assertTrue(record["fetched_at"])

    def test_missing_login_and_console_keys_send_no_request(self):
        for token, reason in (("", "no-credentials"), ("sk-ant-api03-key", "Account limits are only available")):
            with self.subTest(token=token):
                record, calls = self.fetch(token=token)
                self.assertTrue(record["unavailable_reason"].startswith(reason))
                self.assertEqual(calls, [])

    def test_a_failing_key_cmd_is_a_missing_login(self):
        def mint(cmd, label):
            raise subprocess.CalledProcessError(1, cmd)
        record = accounts.fetch_account("Claude", "tok", mint=mint, get_json=lambda *a: self.fail("no request"))
        self.assertEqual(record["unavailable_reason"], "no-credentials")

    def test_usage_errors_are_classified(self):
        for exc, reason in ((http_error(401), "auth-failed"), (http_error(403), "auth-failed"),
                            (http_error(429), "http-429"), (TimeoutError(), "fetch-error:TimeoutError")):
            with self.subTest(reason=reason):
                record, _ = self.fetch(fail=("usage", exc))
                self.assertEqual(record["unavailable_reason"], reason)
                self.assertEqual(record["windows"], [])
            if isinstance(exc, urllib.error.HTTPError):
                exc.close()

    def test_the_plan_is_reused_within_its_ttl_and_kept_through_a_profile_error(self):
        fresh = {"plan": "Max 5x", "plan_checked_at": time.time() - 60}
        record, calls = self.fetch(previous=fresh)
        self.assertEqual((record["plan"], calls), ("Max 5x", ["usage"]))
        stale = {"plan": "Max 5x", "plan_checked_at": time.time() - 7 * 60 * 60}
        error = http_error(500)
        self.addCleanup(error.close)
        record, calls = self.fetch(previous=stale, fail=("profile", error))
        self.assertEqual((record["plan"], calls), ("Max 5x", ["usage", "profile"]))


class RefreshCacheTests(unittest.TestCase):
    def test_refresh_writes_every_account_in_config_order_without_tokens(self):
        with tempfile.TemporaryDirectory() as home:
            def mint(cmd, label):
                time.sleep(0.05)  # read in parallel, written in config order
                return "sk-ant-oat01-" + cmd.split()[-1] + "-secret"
            data = accounts.refresh_accounts_cache(
                pathlib.Path(home), CONFIG, mint=mint,
                get_json=lambda url, tok: copy.deepcopy(USAGE if url.endswith("usage") else {}))
            on_disk = (pathlib.Path(home) / accounts.CACHE_NAME).read_text()
        self.assertEqual(json.loads(on_disk), data)
        self.assertNotIn("secret", on_disk)
        self.assertEqual(list(data["providers"]), ["claude-personal", "claude-team"])
        self.assertEqual(data["hide"], ["anthropic"])
        self.assertEqual(accounts.read_cache(pathlib.Path("/nonexistent")),
                         {"fetched_at": None, "hide": [], "providers": {}})


ACCOUNT = {"label": "Claude (Team)", "plan": "Team", "unavailable_reason": None, "details": [],
           "family": "anthropic", "account": True, "fetched_at": "2026-10-22T03:00:00+00:00",
           "windows": [{"label": "Current session", "used_percent": 34.0, "reset_at": FUTURE,
                        "window_seconds": 18000, "scope": "account", "window_id": "five_hour"}]}


class PluginAccountMergeTests(unittest.TestCase):
    def plugin(self, official, account_cache, env=""):
        plugin = load_plugin(_read_official_cache=lambda: copy.deepcopy(official),
                             _read_accounts_cache=lambda: copy.deepcopy(account_cache),
                             _fresh_last_good=lambda slug: None, _record_last_good=lambda *a: None)
        patcher = patch.dict("os.environ", {"PROVIDER_QUOTA_PROVIDERS": env})
        patcher.start()
        self.addCleanup(patcher.stop)
        return plugin

    OFFICIAL = {"fetched_at": "2026-10-22T03:42:17+00:00", "providers": {
        "openrouter": {"unavailable_reason": None}, "anthropic": {"unavailable_reason": "no-credentials"},
        "openai-codex": {"unavailable_reason": None}, "copilot": {"unavailable_reason": None}}}

    def test_accounts_replace_the_hidden_builtin_row_where_claude_sits(self):
        cache = {"fetched_at": "x", "hide": ["anthropic"], "providers": {
            "claude-personal": dict(ACCOUNT, label="Claude (Personal)"), "claude-team": ACCOUNT}}
        plugin = self.plugin(self.OFFICIAL, cache)
        self.assertEqual(plugin["_configured_providers"](), (
            ("openrouter", "OpenRouter"), ("claude-personal", "Claude (Personal)"), ("claude-team", "Claude (Team)"),
            ("openai-codex", "Codex"), ("copilot", "Copilot")))

    def test_a_shown_builtin_row_stays_first_and_env_still_pins(self):
        official = copy.deepcopy(self.OFFICIAL)
        official["providers"]["anthropic"]["unavailable_reason"] = None
        cache = {"fetched_at": "x", "hide": [], "providers": {"claude-team": ACCOUNT}}
        self.assertEqual([slug for slug, _ in self.plugin(official, cache)["_configured_providers"]()][:3],
                         ["openrouter", "anthropic", "claude-team"])
        pinned = self.plugin(official, cache, env="claude-team, anthropic=Claude Max")["_configured_providers"]()
        self.assertEqual(pinned, (("claude-team", "Claude (Team)"), ("anthropic", "Claude Max")))

    def test_account_rows_carry_their_own_freshness_source_and_family(self):
        cache = {"fetched_at": "x", "hide": ["anthropic"], "providers": {"claude-team": ACCOUNT}}
        plugin = self.plugin(self.OFFICIAL, cache)
        merged = plugin["_read_quota_cache"]()
        self.assertEqual(merged["hide"], ["anthropic"])
        row = plugin["_provider"]("claude-team", "Claude (Team)", merged)
        self.assertEqual((row["status"], row["plan"], row["fetched_at"], row["source"], row["family"]),
                         ("ok", "Team", ACCOUNT["fetched_at"], "account usage", "anthropic"))
        window, = row["windows"]
        self.assertEqual((window["remaining_percent"], window["window_seconds"], window["scope"]), (66.0, 18000, "account"))
        signed_out = plugin["_provider"]("claude-x", "Claude", {"providers": {"claude-x": dict(
            ACCOUNT, windows=[], unavailable_reason="no-credentials")}})
        self.assertEqual(signed_out["status"], "authentication_required")


class SwiftAccountColourTests(unittest.TestCase):
    def test_named_accounts_take_their_vendor_colour_but_keep_their_identity(self):
        source = (ROOT / "menubar/ProviderQuotaMenuBar.swift").read_text()
        start = source.index("    static func normalizedProvider(")
        end = source.index("    static func providerLabel(")
        program = "import Foundation\nfinal class Harness {\n" + source[start:end] + "}\n" + r'''
let claude = Harness.providerHex("anthropic")
var failures: [String] = []
func check(_ ok: Bool, _ what: String) { if !ok { failures.append(what) } }
check(Harness.providerHex("claude-personal") == claude, "claude-* slug is Claude")
check(Harness.normalizedProvider("claude-personal") == "claude-personal", "identity kept")
check(Harness.providerHex("work-seat") == "#8e8e93", "unknown stays grey")
Harness.providerFamilies["work-seat"] = "anthropic"
check(Harness.providerHex("work-seat") == claude, "gateway family wins")
check(Harness.providerHex("openrouter") == "#8e1afe" && Harness.providerHex("claude") == claude, "built-ins unchanged")
if failures.isEmpty { print("PASS") } else { print("FAIL: \(failures)"); exit(1) }
'''
        with tempfile.TemporaryDirectory(prefix="quota-family-") as temp:
            swift, binary = pathlib.Path(temp) / "main.swift", pathlib.Path(temp) / "family"
            swift.write_text(program)
            build = subprocess.run(["xcrun", "swiftc", str(swift), "-o", str(binary)], capture_output=True, text=True)
            self.assertEqual(build.returncode, 0, build.stderr)
            run = subprocess.run([str(binary)], capture_output=True, text=True)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)


if __name__ == "__main__":
    unittest.main()
