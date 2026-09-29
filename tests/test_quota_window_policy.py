import json
import pathlib
import re
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "menubar/ProviderQuotaMenuBar.swift"


class QuotaWindowPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = SOURCE.read_text()
        models = source[source.index("struct QuotaProvider:"):source.index("struct DesktopStatus")]
        names = ["displayWindows", "collapsedRemainingPercent", "providerIsExhausted",
                 "providerSummary", "formattedAmount", "normalizedProvider", "preciseCountdown",
                 "parsedDate", "applicableWindows", "collapsedWindow", "windowLabel", "quotaPercentText"]
        methods = []
        for name in names:
            match = re.search(r"^    (?:private )?(?:static )?func " + name + r"\(", source, re.M)
            if match:
                end = source.index("\n    }", match.start()) + len("\n    }")
                methods.append(source[match.start():end].replace("private func", "func"))
        program = "import Foundation\n" + models + "\nfinal class Harness {\n" + "\n".join(methods) + r'''
}
let provider = try JSONDecoder().decode(QuotaProvider.self, from: FileHandle.standardInput.readDataToEndOfFile())
let h = Harness()
let result: [String: Any] = [
    "exhausted": h.providerIsExhausted(provider),
    "visible": h.displayWindows(provider).map { $0.label },
    "percent": h.collapsedRemainingPercent(provider) as Any? ?? NSNull(),
    "summary": h.providerSummary(provider, connected: true),
    "encoded": String(data: try JSONEncoder().encode(provider), encoding: .utf8)!
]
print(String(data: try JSONSerialization.data(withJSONObject: result), encoding: .utf8)!)
'''
        cls.temp = tempfile.TemporaryDirectory(prefix="quota-window-policy-")
        path = pathlib.Path(cls.temp.name)
        swift = path / "main.swift"
        cls.binary = path / "policy-tests"
        swift.write_text(program)
        build = subprocess.run(["xcrun", "swiftc", str(swift), "-o", str(cls.binary)], capture_output=True, text=True, timeout=90)
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def policy(self, windows, provider="openai-codex"):
        payload = dict(provider=provider, label=provider, status="ok", windows=windows, details=[])
        result = subprocess.run([str(self.binary)], input=json.dumps(payload), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def window(self, label, percent=None, **metadata):
        return dict(label=label, remaining_percent=percent, warning=False, **metadata)

    def test_percent_summary_identifies_used_and_remaining_for_all_providers(self):
        for provider in ("openai-codex", "anthropic", "openrouter", "opencode", "antigravity", "future-provider"):
            with self.subTest(provider=provider):
                result = self.policy([self.window("Primary", 13, scope="account", window_seconds=604800)], provider)
                self.assertEqual(result["summary"], "Weekly · 87% used · 13% left")

    def test_source_monthly_labels_are_not_fixed_thirty_day_windows(self):
        for provider in ("openai-codex", "anthropic", "openrouter", "opencode", "antigravity", "future-provider"):
            with self.subTest(provider=provider):
                monthly = self.window("Current month", 13)
                model = self.window("Model", 0, scope="model")
                result = self.policy([model, monthly], provider)
                self.assertEqual(result["summary"], "Monthly · 87% used · 13% left")
                self.assertFalse(result["exhausted"])
                for label in ("Primary", "Monthly", "Current month"):
                    fixed = self.window(label, 13, window_seconds=2592000, scope="account")
                    self.assertEqual(self.policy([fixed], provider)["summary"], "30d · 87% used · 13% left")
                calendar = self.window("Calendar month", 13, scope="account")
                self.assertEqual(self.policy([calendar], provider)["summary"], "Calendar month · 87% used · 13% left")

    def test_metadata_roundtrip_and_legacy_decode(self):
        window = self.window("Primary", 80, window_seconds=604800.0, scope="account", window_id="primary")
        result = self.policy([window, self.window("Weekly", 30)])
        encoded = json.loads(result["encoded"])["windows"]
        for key in ("window_seconds", "scope", "window_id"):
            self.assertEqual(encoded[0].get(key), window[key])
        self.assertNotIn("window_seconds", encoded[1])

    def test_account_exhaustion_ignores_model_and_reserve_caps(self):
        for provider in ("openai-codex", "anthropic", "future-provider"):
            with self.subTest(provider=provider):
                account = self.window("Primary", 70, scope="account")
                reserve = self.window("Weekly", 0, scope="gpt-reserve")
                self.assertFalse(self.policy([account, reserve], provider)["exhausted"])
                self.assertTrue(self.policy([dict(account, remaining_percent=0), reserve], provider)["exhausted"])
        for label in ("Opus week", "Sonnet weekly", "model-session", "model-week"):
            self.assertFalse(self.policy([self.window("Current week", 50), self.window(label, 0)], "anthropic")["exhausted"])
        self.assertFalse(self.policy([self.window("Model A", 0), self.window("Model B", 20)])["exhausted"])
        self.assertTrue(self.policy([self.window("Model A", 0), self.window("Model B", 0)])["exhausted"])
        self.assertFalse(self.policy([])["exhausted"])

    def test_independent_and_sole_weekly_windows_stay_visible(self):
        for windows in ([self.window("Weekly", 0)],
                        [self.window("Weekly", 0, scope="account", window_seconds=604800), self.window("Weekly", 20, scope="gpt-reserve", window_seconds=604800)],
                        [self.window("Session", 50), self.window("Weekly", 0)]):
            with self.subTest(windows=windows):
                self.assertEqual(self.policy(windows)["visible"], [w["label"] for w in windows])

    def test_collapsed_summary_and_bar_use_same_account_window(self):
        windows = [self.window("Model cap", 0, scope="model", resets_at="2099-01-01T00:00:00Z"),
                   self.window("Session", 75, scope="account", window_seconds=18000, window_id="primary"),
                   self.window("Weekly", 40, scope="account", window_seconds=604800, window_id="secondary")]
        result = self.policy(windows, "anthropic")
        self.assertEqual(result["percent"], 75)
        self.assertEqual(result["summary"], "5h · 25% used · 75% left")
        windows[2]["remaining_percent"] = 0
        windows[2]["resets_at"] = "2098-01-01T00:00:00Z"
        result = self.policy(windows, "anthropic")
        self.assertEqual(result["percent"], 0)
        self.assertTrue(result["summary"].startswith("Weekly · Limit reached · resets in "))
        self.assertEqual(result["summary"], self.policy([windows[2]], "anthropic")["summary"])

    def test_duration_labels_use_metadata_not_reset_or_plan(self):
        cases = [(18000, "5h"), (604800, "Weekly"), (172800, "2d"), (5400, "90m"), (45.5, "45.5s")]
        for seconds, label in cases:
            with self.subTest(seconds=seconds):
                result = self.policy([self.window("Primary", 62, scope="account", window_id="primary", window_seconds=seconds)])
                self.assertEqual(result["summary"], f"{label} · 38% used · 62% left")
        for label, expected in (("Session", "Primary"), ("Current session", "Primary"), ("Weekly", "Secondary")):
            self.assertEqual(self.policy([self.window(label, 62)])["summary"], f"{expected} · 38% used · 62% left")
        reserve = self.window("Primary", 62, scope="gpt-reserve", window_id="primary", window_seconds=604800)
        self.assertEqual(self.policy([reserve])["summary"], "gpt-reserve · Weekly · 38% used · 62% left")
        reserve.pop("window_seconds")
        self.assertEqual(self.policy([reserve])["summary"], "gpt-reserve · Primary · 38% used · 62% left")
        self.assertEqual(self.policy([self.window("Current week", 62)], "anthropic")["summary"], "Current week · 38% used · 62% left")
        self.assertEqual(self.policy([self.window("Budget", 62)], "unknown")["summary"], "Budget · 38% used · 62% left")

    def test_credit_and_unknown_usage_never_get_percentage_bars(self):
        for provider in ("openrouter", "opencode", "future-provider"):
            result = self.policy([self.window("Account credits", 100, remaining_amount=12.5, currency="USD")], provider)
            self.assertIsNone(result["percent"])
            self.assertIn("12.50", result["summary"])
            self.assertNotIn("%", result["summary"])
        unknown = self.policy([self.window("Primary", scope="account"), self.window("Model cap", 80)])
        self.assertIsNone(unknown["percent"])
        self.assertEqual(unknown["summary"], "Primary · Usage unavailable")
        self.assertFalse(unknown["exhausted"])

    def test_legacy_api_key_limit_gates_credit_account(self):
        result = self.policy([self.window("Account credits", remaining_amount=10, currency="USD"),
                              self.window("API key limit", remaining_amount=0, currency="USD")], "openrouter")
        self.assertTrue(result["exhausted"])
        self.assertIsNone(result["percent"])
        self.assertTrue(result["summary"].startswith("API key limit · "))
        self.assertIn("0.00", result["summary"])

    def test_reader_reserve_labels_remain_distinct_without_duplicate_duration(self):
        for label in ("gpt-reserve Weekly", "gpt-reserve · Weekly"):
            result = self.policy([self.window(label, 45, scope="gpt-reserve", window_seconds=604800, window_id="primary")])
            self.assertEqual(result["summary"], "gpt-reserve · Weekly · 55% used · 45% left")
        result = self.policy([self.window("gpt-reserve Session", 45, scope="gpt-reserve", window_id="primary")])
        self.assertEqual(result["summary"], "gpt-reserve · Primary · 55% used · 45% left")

    def test_reader_duration_labels_do_not_repeat_duration(self):
        for label, seconds, expected in (("2d quota", 172800, "2d"), ("24h quota", 86400, "1d"), ("Primary quota", 604800, "Weekly")):
            result = self.policy([self.window(label, 60, scope="account", window_seconds=seconds, window_id="primary")])
            self.assertEqual(result["summary"], f"{expected} · 40% used · 60% left")

    def test_known_short_weekly_and_weekly_only_selection(self):
        short = self.window("Session", 71, scope="account", window_seconds=18000, window_id="primary")
        weekly = self.window("Weekly", 20, scope="account", window_seconds=604800, window_id="secondary")
        self.assertEqual(self.policy([weekly, short])["percent"], 71)
        self.assertEqual(self.policy([weekly, dict(short, remaining_percent=0)])["summary"], "5h · Limit reached")
        self.assertEqual(self.policy([dict(weekly, remaining_percent=0), short])["summary"], "Weekly · Limit reached")
        reserve = self.window("gpt-reserve Weekly", 0, scope="gpt-reserve", window_seconds=604800, window_id="primary")
        primary_weekly = dict(weekly, window_id="primary")
        result = self.policy([reserve, primary_weekly])
        self.assertEqual(result["percent"], 20)
        self.assertEqual(result["summary"], "Weekly · 80% used · 20% left")
        self.assertFalse(result["exhausted"])
        result = self.policy([self.window("Model A", 0), self.window("Model B", 40)])
        self.assertEqual(result["summary"], "Model A · Limit reached")
        self.assertEqual(result["percent"], 0)
        self.assertFalse(result["exhausted"])
        shortest = self.window("Budget", 67, scope="account", window_seconds=60)
        self.assertEqual(self.policy([weekly, shortest], "future")["percent"], 67)

    def test_legacy_codex_reserve_labels_are_neutral_without_metadata(self):
        for raw, expected in (("gpt-reserve Session", "gpt-reserve · Primary"), ("gpt-reserve Weekly", "gpt-reserve · Secondary")):
            result = self.policy([self.window(raw, 65)])
            self.assertEqual(result["summary"], f"{expected} · 35% used · 65% left")

    def test_api_key_scope_blocks_current_credential_without_becoming_account_scope(self):
        account = self.window("Account credits", remaining_amount=10, currency="USD", scope="account")
        for label in ("API key quota", "API key limit"):
            for scope in (None, "api-key"):
                cap = self.window(label, remaining_amount=0, currency="USD", window_id="api_key_limit")
                if scope:
                    cap["scope"] = scope
                result = self.policy([account, cap], "openrouter")
                self.assertTrue(result["exhausted"])
                self.assertIsNone(result["percent"])
                self.assertIn(label, result["summary"])
                self.assertIn("0.00", result["summary"])
                self.assertEqual(json.loads(result["encoded"])["windows"][1].get("scope"), scope)

    def test_live_human_bucket_labels_override_opaque_scope_names(self):
        reserve = self.window("gpt-reserve · Weekly", 42, scope="base_model_inference", window_id="primary", window_seconds=604800)
        self.assertEqual(self.policy([reserve])["summary"], "gpt-reserve · Weekly · 58% used · 42% left")
        opus = self.window("Opus week", 42, scope="opus", window_seconds=604800)
        self.assertEqual(self.policy([opus], "anthropic")["summary"], "Opus · Weekly · 58% used · 42% left")

    def claude_windows(self, fable=100, session=69, week=80):
        return [self.window("Current session", session, scope="account", window_seconds=18000, window_id="five_hour"),
                self.window("Current week", week, scope="account", window_seconds=604800, window_id="seven_day"),
                self.window("Fable week", fable, scope="model:Fable", window_seconds=604800, window_id="weekly_scoped:Fable")]

    def test_claude_model_scoped_weekly_stays_out_of_collapsed_account_summary(self):
        for fable in (100, 0):
            with self.subTest(fable=fable):
                result = self.policy(self.claude_windows(fable), "anthropic")
                self.assertFalse(result["exhausted"])
                self.assertEqual(result["percent"], 69)
                self.assertEqual(result["summary"], "5h · 31% used · 69% left")
                self.assertEqual(result["visible"], ["Current session", "Current week", "Fable week"])
        self.assertTrue(self.policy(self.claude_windows(100, session=0), "anthropic")["exhausted"])

    def test_positive_fraction_is_not_exhausted(self):
        result = self.policy([self.window("Session", 0.4)])
        self.assertFalse(result["exhausted"])
        self.assertEqual(result["summary"], "Primary · >99% used · <1% left")


if __name__ == "__main__":
    unittest.main()
