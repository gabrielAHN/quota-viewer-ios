import pathlib
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "menubar/ProviderQuotaMenuBar.swift"


class CodexPlanDisplayTests(unittest.TestCase):
    def test_plan_names_from_both_readers(self):
        source = SOURCE.read_text()
        start = source.index("    private func displayPlan(")
        end = source.index("\n    }", start) + len("\n    }")
        method = source[start:end].replace("private func", "func")
        program = '''import Foundation
struct QuotaProvider {
    let provider: String
    let plan: String?
}
final class Harness {
    static let claudeSubscriptionLabel: String? = "Team"
    static func normalizedProvider(_ value: String) -> String { value }
''' + method + '''
}
let harness = Harness()
let cases: [(String, String?, String?)] = [
    ("openai-codex", "Prolite", "Pro"),
    ("openai-codex", "prolite", "Pro"),
    ("openai-codex", " PROLITE ", "Pro"),
    ("openai-codex", "pro", "Pro (More)"),
    ("openai-codex", "Pro", "Pro (More)"),
    ("openai-codex", "promax", "Pro (Max)"),
    ("openai-codex", "Promax", "Pro (Max)"),
    ("openai-codex", "Plus", "Plus"),
    ("openai-codex", "Business", "Business"),
    ("openai-codex", "Pro (More)", "Pro (More)"),
    ("openai-codex", "Pro (Max)", "Pro (Max)"),
    ("openai-codex", "Future Tier", "Future Tier"),
    ("openai-codex", nil, nil),
    ("openai-codex", "", nil),
    ("anthropic", "Pro", "Pro"),
    ("anthropic", nil, "Team"),
    ("openrouter", "Pro", "Pro"),
]
for (provider, raw, expected) in cases {
    let actual = harness.displayPlan(QuotaProvider(provider: provider, plan: raw))
    guard actual == expected else {
        print("FAIL: \\(provider) \\(raw ?? "nil") -> \\(actual ?? "nil"), expected \\(expected ?? "nil")")
        exit(1)
    }
}
print("PASS: \\(cases.count) plan display cases")
'''
        with tempfile.TemporaryDirectory(prefix="quota-plan-") as temp:
            path = pathlib.Path(temp)
            swift = path / "main.swift"
            binary = path / "plan-tests"
            swift.write_text(program)
            build = subprocess.run(
                ["xcrun", "swiftc", str(swift), "-o", str(binary)],
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(build.returncode, 0, build.stdout + build.stderr)
            result = subprocess.run([str(binary)], capture_output=True, text=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
