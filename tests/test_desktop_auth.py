import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


READER = Path(__file__).resolve().parents[1] / "desktop-quotas.sh"


class DesktopAuthTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.support = Path(self.home.name) / "Library/Application Support/Hermes"
        self.support.mkdir(parents=True)
        self.requests = []
        self.on_get = lambda handler: (200, {"providers": [{"provider": "test", "status": "ok"}]})
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                owner.requests.append(("GET", self.path, self.headers.get("Authorization")))
                status, body = owner.on_get(self)
                self.send_response(status)
                self.end_headers()
                try:
                    self.wfile.write(json.dumps(body).encode())
                except BrokenPipeError:
                    pass

            def do_POST(self):
                owner.requests.append(("POST", self.path, None))
                self.send_response(401)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = "http://127.0.0.1:%s" % self.server.server_port
        (self.support / "connections.json").write_text(json.dumps({
            "primary": "remote", "connections": [{"id": "remote", "kind": "remote", "url": self.url}]
        }))
        self.save_session("desktop-access", time.time() + 60)

    def save_session(self, access, expires):
        self.token_file = self.support / "native-oauth-tokens.json"
        self.token_file.write_text(json.dumps({self.url: {"encoding": "plain", "value": json.dumps({
            "accessToken": access, "refreshToken": "desktop-owned-refresh", "expiresAt": expires
        })}}))

    def run_reader(self, *args):
        return subprocess.run(["/bin/bash", str(READER), *args], env={**os.environ, "HOME": self.home.name},
                              capture_output=True, text=True, timeout=15)

    def test_valid_desktop_access_is_used_without_rotating_shared_refresh(self):
        original = self.token_file.read_bytes()
        result = self.run_reader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r[0] for r in self.requests], ["GET"])
        self.assertEqual(self.requests[0][2], "Bearer desktop-access")
        self.assertEqual(self.token_file.read_bytes(), original)


    def test_adopts_desktop_rotation_after_unauthorized_without_writing_tokens(self):
        def respond(handler):
            if handler.headers.get("Authorization") == "Bearer desktop-access":
                self.save_session("rotated-by-desktop", time.time() + 3600)
                return 401, {}
            return 200, {"providers": [{"provider": "test", "status": "ok"}]}
        self.on_get = respond
        self.save_session("desktop-access", time.time() + 3600)
        result = self.run_reader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r[2] for r in self.requests], ["Bearer desktop-access", "Bearer rotated-by-desktop"])

    def test_waits_for_desktop_in_place_token_write(self):
        def respond(handler):
            if handler.headers.get("Authorization") == "Bearer desktop-access":
                self.token_file.write_text("{")
                timer = threading.Timer(0.25, self.save_session, args=("rotated-by-desktop", time.time() + 3600))
                timer.start()
                self.addCleanup(timer.join)
                return 401, {}
            return 200, {"providers": [{"provider": "test", "status": "ok"}]}
        self.on_get = respond
        result = self.run_reader()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_session_has_auth_exit_code(self):
        self.token_file.unlink()
        result = self.run_reader()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(self.requests, [])
        self.assertFalse(self.token_file.exists())

    def test_malformed_store_is_transient_not_sign_out(self):
        self.token_file.write_text("{")
        result = self.run_reader()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertNotIn("Sign in", result.stderr)

    def test_default_poll_does_not_force_gateway_refresh(self):
        result = self.run_reader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.requests[0][1], "/api/plugins/provider-quota/quotas")

    def test_unresponsive_gateway_fails_within_seven_seconds(self):
        release = threading.Event()
        self.addCleanup(release.set)
        def stall(handler):
            release.wait(12)
            return 503, {}
        self.on_get = stall
        before = self.token_file.read_bytes()
        started = time.monotonic()
        try:
            result = subprocess.run(["/bin/bash", str(READER)],
                env={**os.environ, "HOME": self.home.name}, capture_output=True, text=True, timeout=7)
        except subprocess.TimeoutExpired:
            self.fail("Unresponsive gateway kept the quota reader running beyond seven seconds")
        finally:
            release.set()
        self.assertLess(time.monotonic() - started, 7)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(self.token_file.read_bytes(), before)
        self.assertEqual([r[0] for r in self.requests], ["GET"])

    def test_unavailable_gateway_is_not_auth_failure(self):
        self.on_get = lambda handler: (503, {})
        result = self.run_reader()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("503", result.stderr)

    def test_activity_missing_plugin_preserves_core_sessions_fallback(self):
        def respond(handler):
            if handler.path == "/api/plugins/provider-quota/activity":
                return 404, {}
            if handler.path == "/api/sessions":
                return 200, {"sessions": [{
                    "session_id": "active-core-session", "is_active": True,
                    "ended_at": None, "model": "gpt-5", "last_active": time.time(),
                }]}
            return 404, {}
        self.on_get = respond
        before = self.token_file.read_bytes()
        result = self.run_reader("--activity")
        self.assertEqual(result.returncode, 0, result.stderr)
        activity = json.loads(result.stdout)
        self.assertEqual(len(activity["sessions"]), 1)
        self.assertTrue(activity["sessions"][0]["is_active"])
        self.assertEqual(activity["sessions"][0]["providers"], ["openai-codex"])
        self.assertTrue(activity["status_busy"])
        self.assertEqual([r[1] for r in self.requests], [
            "/api/plugins/provider-quota/activity", "/api/sessions",
        ])
        self.assertEqual(self.token_file.read_bytes(), before)

    def test_activity_stops_contacting_failed_gateway(self):
        self.on_get = lambda handler: (503, {})
        before = self.token_file.read_bytes()
        result = self.run_reader("--activity")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["sessions"], [])
        self.assertEqual(len(self.requests), 1)
        self.assertEqual(self.token_file.read_bytes(), before)

    def test_activity_survives_missing_primary_session(self):
        self.token_file.unlink()
        result = self.run_reader("--activity")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["sessions"], [])

    def test_confirmed_unauthorized_has_distinct_auth_exit_code(self):
        self.on_get = lambda handler: (401, {})
        self.save_session("desktop-access", time.time() + 3600)
        result = self.run_reader()
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_forbidden_is_not_claimed_to_be_an_expired_session(self):
        self.on_get = lambda handler: (403, {})
        self.save_session("desktop-access", time.time() + 3600)
        result = self.run_reader()
        self.assertEqual(result.returncode, 1)
        self.assertNotIn("expired", result.stderr)


if __name__ == "__main__":
    unittest.main()
