import json
import pathlib
import re
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class GatewayConnectionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "menubar/ProviderQuotaMenuBar.swift").read_text()
        match = re.search(r"^    static func gatewayConnection\(", source, re.M)
        if match is None:
            raise AssertionError("gatewayConnection not found")
        method = source[match.start():source.index("\n    }", match.start()) + len("\n    }")]
        program = "import Foundation\nenum Harness {\n" + method + r'''
}
let root = try JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String: Any]
print((Harness.gatewayConnection(in: root)?["id"] as? String) ?? "none")
'''
        cls.temp = tempfile.TemporaryDirectory(prefix="gateway-connection-")
        path = pathlib.Path(cls.temp.name)
        (path / "main.swift").write_text(program)
        cls.binary = path / "gateway-connection"
        build = subprocess.run(["xcrun", "swiftc", str(path / "main.swift"), "-o", str(cls.binary)],
                               capture_output=True, text=True, timeout=120)
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def chosen(self, connections=None, **registry):
        if connections is None:
            connections = [{"id": "local", "kind": "local"},
                           {"id": "gateway", "kind": "remote", "url": "https://gateway.test/hermes"},
                           {"id": "other", "kind": "remote", "url": "https://other.test"}]
        payload = {"version": 2, "connections": connections, **registry}
        result = subprocess.run([str(self.binary)], input=json.dumps(payload), capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_gateway_wins_over_this_device_whichever_is_active(self):
        self.assertEqual(self.chosen(primary="gateway", lastUsed="local", launchMode="last-used"), "gateway")
        self.assertEqual(self.chosen(primary="local", lastUsed="gateway"), "gateway")
        self.assertEqual(self.chosen(primary="local", lastUsed="local"), "gateway")

    def test_connected_gateway_beats_primary_gateway(self):
        self.assertEqual(self.chosen(primary="gateway", lastUsed="other"), "other")
        self.assertEqual(self.chosen(primary="other", lastUsed="removed"), "other")

    def test_local_only_registries_and_empty_lists(self):
        local_only = [{"id": "local", "kind": "local"}, {"id": "box", "kind": "ssh"}]
        self.assertEqual(self.chosen(local_only, primary="box", lastUsed="local"), "local")
        self.assertEqual(self.chosen(local_only), "local")
        self.assertEqual(self.chosen([]), "none")


if __name__ == "__main__":
    unittest.main()
