import json
import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class CodexAuxiliaryRenderingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "menubar/ProviderQuotaMenuBar.swift").read_text()
        source = source[:source.index("@main\nstruct ProviderQuotaMenuBarApp")]
        source += r'''
extension AppDelegate {
    func renderedWindows(_ provider: QuotaProvider) -> [[String: Any]] {
        displayWindows(provider).map { window in
            let view = windowView(window, provider: provider)
            let header = providerView(provider, kind: .hermes, connected: true)
            return [
                "header": header.subviews.compactMap { child -> [String: Any]? in
                    guard let field = child as? NSTextField else { return nil }
                    return ["text": field.stringValue, "fits": field.attributedStringValue.size().width <= field.frame.width]
                },
                "bars": view.subviews.filter { $0.frame.height == 5 && !$0.subviews.isEmpty }.count,
                "text": view.subviews.compactMap { ($0 as? NSTextField)?.stringValue },
                "fields": view.subviews.compactMap { child -> [String: Any]? in
                    guard let field = child as? NSTextField else { return nil }
                    return ["text": field.stringValue, "x": field.frame.minX, "y": field.frame.minY,
                            "width": field.frame.width, "height": field.frame.height,
                            "fits": field.attributedStringValue.size().width <= field.frame.width]
                }
            ]
        }
    }
}
@main
struct RenderingTest {
    static func main() throws {
        _ = NSApplication.shared
        let provider = try JSONDecoder().decode(QuotaProvider.self, from: FileHandle.standardInput.readDataToEndOfFile())
        let result = AppDelegate().renderedWindows(provider)
        print(String(data: try JSONSerialization.data(withJSONObject: result), encoding: .utf8)!)
    }
}
'''
        cls.temp = tempfile.TemporaryDirectory(prefix="codex-auxiliary-rendering-")
        path = pathlib.Path(cls.temp.name)
        swift = path / "RenderingTest.swift"
        cls.binary = path / "rendering-tests"
        swift.write_text(source)
        build = subprocess.run(["xcrun", "swiftc", "-parse-as-library", "-framework", "AppKit", str(swift), "-o", str(cls.binary)], capture_output=True, text=True, timeout=120)
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def render(self, windows, provider="openai-codex"):
        payload = dict(provider=provider, label=provider, status="ok", windows=windows, details=[])
        result = subprocess.run([str(self.binary)], input=json.dumps(payload), capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def window(self, label="Weekly", percent: float | None = 25, scope="account", seconds=604800) -> dict:
        return dict(label=label, remaining_percent=percent, scope=scope, window_seconds=seconds,
                    window_id="primary", resets_at="2099-10-04T23:45:14Z", warning=False)

    def test_exhausted_percentage_keeps_limit_notice_and_usage(self):
        row = self.render([self.window(percent=0)])[0]
        self.assertIn("Limit reached", row["text"])
        self.assertIn("100% used · 0% left", row["text"])
        self.assertEqual(row["bars"], 0)

    def test_collapsed_percentages_and_reset_are_readable(self):
        row = self.render([self.window(percent=13)])[0]
        usage = [f for f in row["header"] if "87% used" in f["text"]]
        self.assertEqual(len(usage), 1)
        self.assertTrue(usage[0]["fits"], usage)
        self.assertEqual(usage[0]["text"], "Weekly · 87% used · 13% left")
        self.assertTrue(any(f["text"].startswith("Resets in ") and f["fits"] for f in row["header"]))

    def test_percentage_text_fits_without_overlapping_period_label(self):
        for provider in ("openai-codex", "anthropic", "openrouter", "opencode", "antigravity", "future-provider"):
            with self.subTest(provider=provider):
                fields = self.render([self.window(percent=13)], provider)[0]["fields"]
                period, usage = fields[:2]
                self.assertEqual(usage["text"], "87% used · 13% left")
                self.assertTrue(usage["fits"], usage)
                self.assertTrue(period["y"] >= usage["y"] + usage["height"] or
                                period["x"] + period["width"] <= usage["x"], fields)

    def test_weekly_account_has_one_bar_and_text_only_reserve(self):
        rows = self.render([self.window(), self.window("gpt-reserve · Weekly", 100, "base_model_inference")])
        self.assertEqual([row["bars"] for row in rows], [1, 0])
        self.assertEqual(rows[0]["text"][:2], ["Weekly", "75% used · 25% left"])
        self.assertEqual(rows[1]["text"][:2], ["gpt-reserve · Weekly", "0% used · 100% left"])
        self.assertTrue(rows[1]["text"][2].startswith("Resets "))

    def test_account_session_and_weekly_keep_both_bars(self):
        rows = self.render([self.window("Session", 70, seconds=18000), self.window(),
                            self.window("gpt-reserve · Weekly", 100, "base_model_inference")])
        self.assertEqual([row["bars"] for row in rows], [1, 1, 0])
        self.assertEqual(rows[0]["text"][:2], ["5h", "30% used · 70% left"])

    def test_other_providers_keep_independent_window_bars(self):
        for provider in ("anthropic", "openrouter", "opencode", "future-provider"):
            with self.subTest(provider=provider):
                rows = self.render([self.window(), self.window("Model", 80, "model")], provider)
                self.assertEqual([row["bars"] for row in rows], [1, 1])

    def test_reserve_only_fallback_keeps_bar_and_details(self):
        rows = self.render([self.window("gpt-reserve · Weekly", 80, "base_model_inference")])
        self.assertEqual(rows[0]["bars"], 1)
        self.assertEqual(rows[0]["text"][:2], ["gpt-reserve · Weekly", "20% used · 80% left"])
        self.assertTrue(rows[0]["text"][2].startswith("Resets "))

    def test_missing_metadata_does_not_invent_duration_or_scope(self):
        account = self.window()
        reserve = self.window("Model", 80, "model")
        account.pop("window_seconds")
        reserve.pop("window_seconds")
        rows = self.render([account, reserve])
        self.assertEqual([row["bars"] for row in rows], [1, 0])
        self.assertEqual(rows[1]["text"][0], "Model")
        reserve.pop("scope")
        rows = self.render([account, reserve])
        self.assertEqual([row["bars"] for row in rows], [1, 1])

    def test_exhausted_reserve_remains_text_without_exhausting_account(self):
        rows = self.render([self.window(), self.window("gpt-reserve · Weekly", 0, "base_model_inference")])
        self.assertEqual([row["bars"] for row in rows], [1, 0])
        self.assertEqual(rows[1]["text"][:2], ["gpt-reserve · Weekly", "Limit reached"])
        self.assertTrue(rows[1]["text"][2].startswith("Resets in "))

    def test_claude_model_scoped_weekly_renders_readable_label(self):
        session = dict(self.window("Current session", 69, seconds=18000), window_id="five_hour")
        week = dict(self.window("Current week", 80), window_id="seven_day")
        fable = dict(self.window("Fable week", 100, "model:Fable"), window_id="weekly_scoped:Fable")
        rows = self.render([session, week, fable], "anthropic")
        self.assertEqual([row["text"][:2] for row in rows], [
            ["5h", "31% used · 69% left"], ["Weekly", "20% used · 80% left"], ["Fable · Weekly", "0% used · 100% left"]])
        self.assertTrue(all(f["fits"] for row in rows for f in row["fields"]))
        self.assertTrue(any("31% used" in f["text"] for f in rows[0]["header"]))

    def test_unknown_and_credit_windows_keep_text_only_rendering(self):
        credit = self.window("Account credits", 100)
        credit.update(remaining_amount=12.5, currency="USD")
        for window in (credit, self.window(percent=None)):
            with self.subTest(window=window):
                self.assertEqual(self.render([window], "openrouter")[0]["bars"], 0)


if __name__ == "__main__":
    unittest.main()
