import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "menubar/ProviderQuotaMenuBar.swift"
SCRATCH = pathlib.Path(tempfile.gettempdir())


class SwiftHelperTimeoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(dir=SCRATCH)
        cls.work = pathlib.Path(cls.directory.name)
        source = SOURCE.read_text()
        start = source.index("    private static func runHelper(_ path:")
        end = source.index("    // Rebuild the floating pets:", start)
        methods = source[start:end].replace("private static func", "static func")
        timeout = ", timeout: Double(CommandLine.arguments[3])!" if "timeout: TimeInterval" in methods else ""
        program = "import Foundation\nimport Darwin\nenum Harness {\n" + methods + "}\n" + '''
let result = Harness.runHelper(CommandLine.arguments[1], [CommandLine.arguments[2]]''' + timeout + ''')
if let result {
    print("DATA:\\(result.count):\\(result.first ?? 0):\\(result.last ?? 0)")
} else {
    print("NIL")
}
'''
        swift = cls.work / "Harness.swift"
        swift.write_text(program)
        cls.binary = cls.work / "harness"
        subprocess.run(["xcrun", "swiftc", str(swift), "-o", str(cls.binary)], check=True, capture_output=True, text=True, timeout=60)

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def run_helper(self, body, timeout=0.3):
        pidfile = self.work / "helper.pid"
        pidfile.unlink(missing_ok=True)
        helper = self.work / "helper.py"
        helper.write_text("import os, signal, time\n" + f"open({str(pidfile)!r}, 'w').write(str(os.getpid()))\n" + body)
        wrapper = self.work / "helper.sh"
        wrapper.write_text("#!/bin/bash\nexec " + json.dumps(sys.executable) + " " + json.dumps(str(helper)) + ' "$@"\n')
        wrapper.chmod(0o755)
        started = time.monotonic()
        try:
            try:
                result = subprocess.run([str(self.binary), str(wrapper), "--activity", str(timeout)], capture_output=True, text=True, timeout=3)
            except subprocess.TimeoutExpired:
                self.fail("Swift helper exceeded the bounded completion budget")
            elapsed = time.monotonic() - started
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(pidfile.exists(), "The real helper must start")
            pid = int(pidfile.read_text())
            with self.assertRaises(ProcessLookupError, msg="Helper must be terminated and reaped before returning"):
                os.kill(pid, 0)
            return result.stdout.strip(), elapsed
        finally:
            if pidfile.exists():
                try:
                    os.kill(int(pidfile.read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_hanging_helper_ignoring_termination_is_killed_and_reaped(self):
        output, elapsed = self.run_helper("signal.signal(signal.SIGTERM, signal.SIG_IGN)\ntime.sleep(60)\n")
        self.assertEqual(output, "NIL")
        self.assertLess(elapsed, 2)

    def test_both_output_pipes_are_drained_without_deadlock(self):
        output, elapsed = self.run_helper("os.write(2, b'e' * 1048576)\nos.write(1, b'x' * 1048576)\n", timeout=2)
        self.assertEqual(output, "DATA:1048576:120:120")
        self.assertLess(elapsed, 2)

    def test_nonzero_exit_discards_partial_output(self):
        output, _ = self.run_helper("os.write(1, b'partial')\nos.write(2, b'error')\nraise SystemExit(2)\n")
        self.assertEqual(output, "NIL")


if __name__ == "__main__":
    unittest.main()
