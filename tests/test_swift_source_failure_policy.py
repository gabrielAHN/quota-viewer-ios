import pathlib
import re
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "menubar/ProviderQuotaMenuBar.swift"
SCRATCH = pathlib.Path(tempfile.gettempdir())


class SwiftSourceFailurePolicyTests(unittest.TestCase):
    def test_transient_failure_preserves_recent_reading(self):
        source = SOURCE.read_text()
        if "struct SourceFailurePolicy {" in source:
            policy = source[source.index("struct SourceFailurePolicy {"):source.index("final class AppDelegate:")]
            program = "import Foundation\n" + policy + '''
var policy = SourceFailurePolicy()
policy.succeeded(at: 100)
policy.failed(NSError(domain: "ProviderQuotaMenuBar", code: 1), isHermes: true, at: 110)
guard policy.state == .stale else { fatalError("Transient failure discarded recent reading") }
print("PASS: transient failure preserves recent reading")
'''
        else:
            match = re.search(r"case \.failure:\s*(self\.upsertSource\(SourceQuota\([^\n]+)", source)
            assert match is not None
            body = match.group(1)
            program = '''import Foundation
enum GatewayKind { case hermes }
struct SourceQuota {
    let kind: GatewayKind
    let providers: [String]
    let connected: Bool
    let generatedAt: String?
}
final class Harness {
    var source = SourceQuota(kind: .hermes, providers: ["reading"], connected: true, generatedAt: "now")
    func disconnectedProviders(for kind: GatewayKind) -> [String] { [] }
    func upsertSource(_ value: SourceQuota) { source = value }
    func fail() {
        let kind = GatewayKind.hermes
''' + body + '''
    }
}
let harness = Harness()
harness.fail()
guard harness.source.connected && harness.source.providers == ["reading"] else {
    fatalError("Transient failure discarded recent reading")
}
'''
        self.run_swift(program)

    def test_policy_deadline_authentication_and_recovery(self):
        source = SOURCE.read_text()
        policy = source[source.index("struct SourceFailurePolicy {"):source.index("final class AppDelegate:")]
        self.run_swift("import Foundation\n" + policy + '''
func expect(_ condition: Bool, _ message: String) {
    guard condition else { fatalError(message) }
}
let transient = NSError(domain: "ProviderQuotaMenuBar", code: 1)
let auth = NSError(domain: "ProviderQuotaMenuBar", code: 2)
var policy = SourceFailurePolicy()
policy.failed(transient, isHermes: true, at: 100)
expect(policy.state == .unavailable, "Cold transient must not request sign-in")
expect(policy.message == "Gateway unavailable — retrying", "Unavailable message")
policy.succeeded(at: 100)
expect(policy.state == .healthy && policy.message == nil, "Success clears failure")
for time in [101.0, 130, 190, 219.999] {
    policy.failed(transient, isHermes: true, at: time)
    expect(policy.state == .stale, "Recent reading retained")
    expect(policy.lastSuccess == 100, "Failure must not slide deadline")
    expect(policy.message == "Updating… · showing last reading", "Stale warning visible")
}
policy.failed(transient, isHermes: true, at: 220)
expect(policy.state == .unavailable, "Exact 120-second boundary expires")
policy.failed(transient, isHermes: true, at: 10000)
expect(policy.state == .unavailable, "Repeated errors cannot revive grace")
policy.succeeded(at: 20000)
policy.failed(transient, isHermes: true, at: 20001)
policy.expire(at: 20119.999)
expect(policy.state == .stale, "Timer must not expire early")
policy.expire(at: 20120)
expect(policy.state == .unavailable, "Expire without another fetch")
policy.succeeded(at: 30000)
policy.failed(auth, isHermes: true, at: 30001)
expect(policy.state == .authenticationRequired && policy.lastSuccess == nil, "Auth immediately clears grace")
policy.failed(transient, isHermes: true, at: 30002)
expect(policy.state == .authenticationRequired, "Transient cannot resurrect revoked reading")
policy.expire(at: 40000)
expect(policy.state == .authenticationRequired, "Expiry cannot hide authentication failure")
policy.succeeded(at: 50000)
expect(policy.state == .healthy && policy.lastSuccess == 50000, "Success resets auth")
policy.failed(NSError(domain: NSCocoaErrorDomain, code: 2), isHermes: true, at: 50001)
expect(policy.state == .stale, "Unrelated domain code 2 is not auth")
for code in [1, 127, 60] {
    policy.failed(NSError(domain: "ProviderQuotaMenuBar", code: code), isHermes: true, at: 50002)
    expect(policy.state == .stale, "Transport/config failures retain recent reading")
}
policy.failed(transient, isHermes: false, at: 50003)
expect(policy.state == .authenticationRequired && policy.lastSuccess == nil, "Local failure behavior preserved")
print("PASS: fixed deadline, timer expiry, auth classification, local behavior, success recovery")
''')

    def test_both_refresh_paths_use_shared_policy(self):
        source = SOURCE.read_text()
        manual = source[source.index("@objc private func refreshSourceAction"):source.index("private func sourceQuota(")]
        periodic = source[source.index("private func refresh() {"):source.index("private static func fetchPayload(")]
        self.assertIn("self.sourceQuota(for: kind, result: result)", manual)
        self.assertIn("self.sourceQuota(for: kind, result: result)", periodic)
        self.assertIn("RunLoop.main.add(timer, forMode: .common)", source)
        self.assertIn('addView(messageView("Updating… · showing last reading", color: .hermesOrange))', source)
        self.assertIn('return messageView("Gateway unavailable — retrying", color: .hermesOrange)', source)

    def run_swift(self, program):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="source-policy-", dir=SCRATCH) as temp:
            path = pathlib.Path(temp) / "main.swift"
            path.write_text(program)
            result = subprocess.run(["xcrun", "swift", str(path)], text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            print(result.stdout.strip())


if __name__ == "__main__":
    unittest.main()
