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

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("HERMES_HOME", "PROVIDER_QUOTAS_HERMES_HOME")}
        return {**env, "HOME": self.home.name, **extra}

    def run_reader(self, *args, **env):
        return subprocess.run(["/bin/bash", str(READER), *args], env=self.env(**env),
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
                env=self.env(), capture_output=True, text=True, timeout=7)
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
            "/api/plugins/provider-quota/activity", "/api/sessions?order=recent", "/api/sessions",
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

    def write_connections(self, **registry):
        (self.support / "connections.json").write_text(json.dumps({
            "version": 2, "launchMode": "primary",
            "connections": [{"id": "local", "kind": "local", "label": "This device"},
                            {"id": "remote", "kind": "remote", "url": self.url, "authMode": "oauth"}],
            **registry}))

    def test_reads_the_gateway_even_when_this_device_is_primary_or_active(self):
        for registry in ({"primary": "local", "lastUsed": "remote"},
                         {"primary": "remote", "lastUsed": "local", "launchMode": "last-used"},
                         {"primary": "local", "lastUsed": "local"}):
            with self.subTest(registry=registry):
                self.requests.clear()
                self.write_connections(**registry)
                result = self.run_reader()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotIn("local", result.stderr)
                self.assertEqual([r[:2] for r in self.requests], [("GET", "/api/plugins/provider-quota/quotas")])

    def test_local_backend_is_only_read_without_a_gateway(self):
        (self.support / "connections.json").write_text(json.dumps({
            "version": 2, "primary": "local", "lastUsed": "local",
            "connections": [{"id": "local", "kind": "local"}]}))
        result = self.run_reader()
        self.assertEqual(result.returncode, 1)
        self.assertIn("local mode", result.stderr)
        self.assertEqual(self.requests, [])
        (self.support / "backend-ownership.json").write_text(json.dumps({"backends": [{"url": self.url}]}))
        result = self.run_reader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r[:3] for r in self.requests], [("GET", "/api/plugins/provider-quota/quotas", None)])

    def test_activity_finds_long_running_and_other_profile_gateway_sessions(self):
        now = time.time()
        store = {
            "created": [{"id": "old-cron", "source": "cron", "is_active": False, "ended_at": now - 50,
                         "last_active": now - 50, "model": "gpt-6.1-sol"}],
            "recent": [{"id": "long-run", "source": "desktop", "is_active": True, "ended_at": None,
                        "last_active": now - 5, "model": "gpt-6.1-sol"},
                       {"id": "finished", "source": "desktop", "is_active": False, "ended_at": None,
                        "last_active": now - 400, "model": "deepseek/deepseek-v4"}],
        }
        plugin = [{"session_id": "carto-turn", "surface": "tui", "model": "claude-opus-5-5", "is_active": True},
                  {"session_id": "finished", "surface": "desktop", "model": "deepseek/deepseek-v4", "is_active": True}]

        def respond(handler):
            if handler.path == "/api/sessions?order=recent":
                return 200, {"sessions": store["recent"]}
            if handler.path == "/api/sessions":
                return 200, {"sessions": store["created"]}
            if handler.path == "/api/plugins/provider-quota/activity":
                return 200, {"sessions": plugin}
            return 404, {}
        self.on_get = respond
        result = self.run_reader("--activity")
        self.assertEqual(result.returncode, 0, result.stderr)
        sessions = json.loads(result.stdout)["sessions"]
        self.assertEqual([s["providers"] for s in sessions], [["openai-codex"], ["anthropic"]])
        self.assertTrue(json.loads(result.stdout)["status_busy"])

    def test_activity_falls_back_when_gateway_rejects_recent_order(self):
        now = time.time()

        def respond(handler):
            if handler.path == "/api/sessions?order=recent":
                return 422, {}
            if handler.path == "/api/sessions":
                return 200, {"sessions": [{"id": "live", "is_active": True, "ended_at": None,
                                           "last_active": now, "model": "claude-opus-5-5"}]}
            return 404, {}
        self.on_get = respond
        result = self.run_reader("--activity")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([s["providers"] for s in json.loads(result.stdout)["sessions"]], [["anthropic"]])

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

    def write_store(self, root=None, **sections):
        root = Path(root or Path(self.home.name) / ".hermes")
        root.mkdir(parents=True, exist_ok=True)
        (root / "gateway-connections.json").write_text(json.dumps({
            "version": 2, "primary": "gateway", "lastUsed": "local", "launchMode": "last-used", "revision": 3,
            "connections": [{"id": "local", "kind": "local", "label": "This device"},
                            {"id": "gateway", "kind": "remote", "url": self.url, "authMode": "oauth"}]}))
        tokens = root / "gateway-connection-tokens.json"
        if sections:
            tokens.write_text(json.dumps({"version": 1, "revision": 2, "tokens": {}, "unbound": {},
                                          "undecrypted": {}, **sections}))
        elif tokens.exists():
            tokens.unlink()
        return root

    def store_set(self, access, base=None):
        return {"accessToken": access, "refreshToken": "desktop-owned-refresh", "provider": "self-hosted",
                "expiresAt": int((time.time() + 3600) * 1000), "baseUrl": base or self.url}

    def test_migrated_desktop_store_wins_over_stale_legacy_files(self):
        self.save_session("stale-legacy-access", time.time() - 60)
        (self.support / "connections.json").write_text(json.dumps({
            "primary": "old", "connections": [{"id": "old", "kind": "remote", "url": "http://127.0.0.1:9"}]}))
        store = self.write_store(tokens={"gateway": self.store_set("store-access")})
        before = {p.name: p.read_bytes() for p in store.iterdir()}
        result = self.run_reader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r[1:] for r in self.requests], [("/api/plugins/provider-quota/quotas", "Bearer store-access")])
        self.assertEqual({p.name: p.read_bytes() for p in store.iterdir()}, before)

    def test_migrated_store_without_this_gateway_needs_sign_in_despite_legacy_tokens(self):
        for sections in ({}, {"tokens": {"gateway": self.store_set("elsewhere", base="https://other.test/hermes")}}):
            with self.subTest(sections=sections):
                self.write_store(**sections)
                result = self.run_reader()
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("Sign in to Hermes Desktop", result.stderr)
                self.assertEqual(self.requests, [])

    def test_migrated_store_reads_unbound_and_undecrypted_sign_ins(self):
        cases = [("unbound", {self.url + "/": self.store_set("unbound-access")}, "unbound-access"),
                 ("undecrypted", {self.url: {"encoding": "plain", "value": json.dumps(self.store_set("envelope-access"))}},
                  "envelope-access")]
        for section, entries, access in cases:
            with self.subTest(section=section):
                self.requests.clear()
                self.write_store(**{section: entries})
                result = self.run_reader()
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.requests[0][2], "Bearer " + access)

    def test_profile_hermes_home_reads_the_root_store(self):
        root = Path(self.home.name) / "custom-hermes"
        self.write_store(root=root, tokens={"gateway": self.store_set("root-access")})
        result = self.run_reader(HERMES_HOME=str(root / "profiles" / "carto"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.requests[0][2], "Bearer root-access")

    def test_hermes_home_chosen_in_the_app_wins_over_hermes_home(self):
        chosen = self.write_store(root=Path(self.home.name) / "chosen", tokens={"gateway": self.store_set("chosen-access")})
        self.write_store(tokens={"gateway": self.store_set("env-access")})
        result = self.run_reader(PROVIDER_QUOTAS_HERMES_HOME=str(chosen),
                                 HERMES_HOME=str(Path(self.home.name) / ".hermes"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r[2] for r in self.requests], ["Bearer chosen-access"])

    def test_adopts_desktop_rotation_in_the_store_after_unauthorized(self):
        self.write_store(tokens={"gateway": self.store_set("store-access")})

        def respond(handler):
            if handler.headers.get("Authorization") == "Bearer store-access":
                self.write_store(tokens={"gateway": self.store_set("rotated-in-store")})
                return 401, {}
            return 200, {"providers": [{"provider": "test", "status": "ok"}]}
        self.on_get = respond
        result = self.run_reader()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([r[2] for r in self.requests], ["Bearer store-access", "Bearer rotated-in-store"])


if __name__ == "__main__":
    unittest.main()
