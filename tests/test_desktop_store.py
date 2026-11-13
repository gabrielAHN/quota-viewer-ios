import ast
import base64
import hashlib
import json
import os
import pathlib
import plistlib
import re
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
URLS = ["https://gw.example/hermes", "https://GW.Example/hermes/", " https://gw.example/hermes// ",
        "http://127.0.0.1:9/hermes/", "https://user@gw.test:8443/a/B/?q=1#f", "gw.test/hermes", ""]
KEYS = ["hermes", "claude", "codex", "opencode", "antigravity", "openrouter"]
TOOL_VARIABLES = ("HERMES_HOME", "CLAUDE_CONFIG_DIR", "CODEX_HOME", "XDG_DATA_HOME", "OPENROUTER_API_KEY")
FAKE_SECURITY = """#!/bin/bash
service="" account=""
while [ $# -gt 0 ]; do
    case "$1" in
        -s) service=$2; shift 2 ;;
        -a) account=$2; shift 2 ;;
        *) shift ;;
    esac
done
IFS=, read -ra entries <<< "$FAKE_KEYCHAIN"
for entry in "${entries[@]}"; do
    name=${entry%%@*}
    owner=""
    case "$entry" in *@*) owner=${entry#*@} ;; esac
    if [ "$name" = "$service" ] && { [ -z "$account" ] || [ "$owner" = "$account" ]; }; then
        exit 0
    fi
done
exit 44
"""


def swift_method(source, name):
    match = re.search(r"^    (?:private )?static func %s\(" % name, source, re.M)
    if match is None:
        raise AssertionError(name + " not found")
    return source[match.start():source.index("\n    }", match.start()) + len("\n    }")]


def python_function(script, name, **names):
    source = (ROOT / script).read_text()
    match = re.search(r"^def %s\(.*\):\n(?:    .*\n)+" % name, source, re.M)
    if match is None:
        raise AssertionError(name + " not found in " + script)
    namespace = dict(names)
    exec("import hashlib, os, unicodedata, urllib.parse\nfrom pathlib import Path\n" + match.group(0), namespace)
    return namespace[name]


def load_script(script):
    source = (ROOT / script).read_text().split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom, ast.FunctionDef))]
    namespace = {}
    exec(compile(tree, script, "exec"), namespace)
    return namespace


def clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in TOOL_VARIABLES and not k.startswith("PROVIDER_QUOTAS_")}
    env.update(extra)
    return env


def service_for(folder):
    return "Claude Code-credentials-" + hashlib.sha256(str(folder).encode()).hexdigest()[:8]


def fake_jwt(expires):
    payload = base64.urlsafe_b64encode(json.dumps({"exp": expires}).encode()).decode().rstrip("=")
    return "header." + payload + ".signature"


class DesktopStoreTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = (ROOT / "menubar/ProviderQuotaMenuBar.swift").read_text()
        methods = "\n".join(swift_method(source, name) for name in (
            "resolveLocation", "gatewayConnection", "gatewayURLKey", "nativeOAuthSession", "hermesHomeStatus",
            "claudeKeychainServices", "envFileHasKey", "locationStatus"))
        program = "import CryptoKit\nimport Foundation\nenum Harness {\n" + methods + r'''
}
let args = CommandLine.arguments
let environment = ProcessInfo.processInfo.environment
let fakeKeychain = (environment["FAKE_KEYCHAIN"] ?? "").split(separator: ",").map(String.init)
func keychain(_ service: String, _ account: String?) -> Bool {
    fakeKeychain.contains { entry in
        let parts = entry.split(separator: "@", maxSplits: 1).map(String.init)
        return parts[0] == service && (account == nil || (parts.count > 1 && parts[1] == account))
    }
}
func json(_ value: Any) -> String {
    String(data: try! JSONSerialization.data(withJSONObject: value), encoding: .utf8)!
}
switch args[1] {
case "root":
    let settings = try! JSONSerialization.jsonObject(with: Data(args[3].utf8)) as! [String: String]
    let home = Harness.resolveLocation(args[2], setting: { settings[$0] }, environment: environment,
                                       home: URL(fileURLWithPath: args[4]))
    print("\(home.url.path)|\(home.origin)|\(home.custom)")
case "keys":
    let urls = try JSONSerialization.jsonObject(with: FileHandle.standardInput.readDataToEndOfFile()) as! [String]
    print(json(urls.map { Harness.gatewayURLKey($0) }))
case "status":
    let status = Harness.hermesHomeStatus(URL(fileURLWithPath: args[2]), support: URL(fileURLWithPath: args[3]))
    print("\(status.ok)|\(status.text)")
case "services":
    print(json(Harness.claudeKeychainServices(URL(fileURLWithPath: args[2]), home: URL(fileURLWithPath: args[3]))))
case "location":
    let status = Harness.locationStatus(args[2], url: URL(fileURLWithPath: args[3]), custom: args[4] == "1",
                                        environment: environment, home: URL(fileURLWithPath: args[5]),
                                        support: URL(fileURLWithPath: args[6]), keychain: keychain)
    print("\(status.state)|\(status.text)")
default:
    let store = args[4] == "-" ? nil : URL(fileURLWithPath: args[4])
    print(Harness.nativeOAuthSession(for: args[2], support: URL(fileURLWithPath: args[3]), store: store) != nil)
}
'''
        cls.temp = tempfile.TemporaryDirectory(prefix="desktop-store-")
        path = pathlib.Path(cls.temp.name)
        (path / "main.swift").write_text(program)
        cls.binary = path / "desktop-store"
        build = subprocess.run(["xcrun", "swiftc", str(path / "main.swift"), "-o", str(cls.binary)],
                               capture_output=True, text=True, timeout=300)
        if build.returncode:
            raise AssertionError(build.stdout + build.stderr)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.support = pathlib.Path(self.tmp.name) / "support"
        self.store = pathlib.Path(self.tmp.name) / "hermes"
        self.support.mkdir()
        self.store.mkdir()

    def run_harness(self, *args, env=None, stdin=None):
        result = subprocess.run([str(self.binary), *args], input=stdin, capture_output=True, text=True,
                                timeout=10, env=env or clean_env())
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def signed_in(self, url, store=True):
        return self.run_harness("session", url, str(self.support), str(self.store) if store else "-") == "true"

    def status(self, home=None):
        ok, text = self.run_harness("status", str(home or self.store), str(self.support)).split("|", 1)
        return ok == "true", text

    def root(self, key, settings=None, home="/Users/someone", env=None):
        path, origin, custom = self.run_harness("root", key, json.dumps(settings or {}), home, env=env).split("|")
        return path, origin, custom == "true"

    def write_tokens(self, **sections):
        (self.store / "gateway-connection-tokens.json").write_text(json.dumps(
            {"version": 1, "revision": 1, "tokens": {}, "unbound": {}, "undecrypted": {}, **sections}))

    def write_registry(self, url="https://gw.test/hermes", store=None):
        (store or self.store).mkdir(parents=True, exist_ok=True)
        ((store or self.store) / "gateway-connections.json").write_text(json.dumps({
            "version": 2, "primary": "gateway", "lastUsed": "local",
            "connections": [{"id": "local", "kind": "local"}, {"id": "gateway", "kind": "remote", "url": url}]}))

    def run_cli_config(self, home, settings, keychain=""):
        prefs = pathlib.Path(self.tmp.name) / "prefs.plist"
        with open(prefs, "wb") as handle:
            plistlib.dump({f"location.{key}": value for key, value in settings.items()}, handle)
        fake_bin = pathlib.Path(self.tmp.name) / "fake-bin"
        fake_bin.mkdir(exist_ok=True)
        (fake_bin / "security").write_text(FAKE_SECURITY)
        (fake_bin / "security").chmod(0o755)
        env = clean_env(HOME=str(home), PROVIDER_QUOTAS_DEFAULTS=str(prefs), FAKE_KEYCHAIN=keychain, NO_COLOR="1",
                        PATH=f"{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin")
        result = subprocess.run(["bash", str(ROOT / "provider-quotas.sh"), "config", "--json"], capture_output=True,
                                text=True, env=env, timeout=60)
        self.assertEqual(result.returncode, 0, result.stderr)
        return {record["provider"]: record for record in json.loads(result.stdout) if "path" in record}

    def assert_app_matches_cli(self, home, settings, keychain=""):
        records = self.run_cli_config(home, settings, keychain)
        self.assertEqual(sorted(records), sorted(KEYS))
        support = home / "Library/Application Support/Hermes"
        for key in KEYS:
            record = records[key]
            with self.subTest(provider=key):
                self.assertEqual(self.root(key, settings, str(home)), (record["path"], record["origin"], record["mode"] == "custom"))
                state, text = self.run_harness("location", key, record["path"], "1" if record["mode"] == "custom" else "0",
                                               str(home), str(support), env=clean_env(FAKE_KEYCHAIN=keychain)).split("|", 1)
                self.assertEqual((state, text), (record["state"], record["status"]))
        return {key: (records[key]["state"], records[key]["status"]) for key in KEYS}

    def test_custom_settings_beat_tool_variables_and_hermes_profiles_map_to_the_root(self):
        env = clean_env(HERMES_HOME="/tmp/h/profiles/carto", CLAUDE_CONFIG_DIR="/tmp/cc/", CODEX_HOME="/tmp/cx",
                        XDG_DATA_HOME="/tmp/data")
        self.assertEqual(self.root("hermes"), ("/Users/someone/.hermes", "default", False))
        self.assertEqual(self.root("hermes", env=env), ("/tmp/h", "HERMES_HOME", False))
        self.assertEqual(self.root("hermes", {"hermes": " /tmp/custom/ "}, env=env), ("/tmp/custom", "custom", True))
        self.assertEqual(self.root("hermes", {"hermes": "/tmp/x/profiles/work"}, env=env), ("/tmp/x", "custom", True))
        self.assertEqual(self.root("hermes", {"hermes": "  "}, env=env), ("/tmp/h", "HERMES_HOME", False))
        self.assertEqual(self.root("claude"), ("/Users/someone/.claude", "default", False))
        self.assertEqual(self.root("claude", env=env), ("/tmp/cc", "CLAUDE_CONFIG_DIR", False))
        self.assertEqual(self.root("claude", {"claude": "/tmp/work/.claude"}, env=env), ("/tmp/work/.claude", "custom", True))
        self.assertEqual(self.root("codex", env=env), ("/tmp/cx", "CODEX_HOME", False))
        self.assertEqual(self.root("opencode"), ("/Users/someone/.local/share/opencode", "default", False))
        self.assertEqual(self.root("opencode", env=env), ("/tmp/data/opencode", "XDG_DATA_HOME", False))
        self.assertEqual(self.root("antigravity"), ("/Users/someone/.gemini/antigravity-cli", "default", False))
        self.assertEqual(self.root("openrouter", {"hermes": "/tmp/x"}), ("/tmp/x/.env", "Hermes home", False))
        self.assertEqual(self.root("openrouter", {"openrouter": "/tmp/keys.env"}), ("/tmp/keys.env", "custom", True))

    def test_app_and_cli_agree_on_automatic_locations(self):
        home = pathlib.Path(self.tmp.name) / "home"
        self.write_registry("https://gw.example/hermes", home / ".hermes")
        (home / ".hermes/gateway-connection-tokens.json").write_text(json.dumps({"version": 1, "tokens": {"gateway": {
            "accessToken": "a", "baseUrl": "https://gw.example/hermes", "expiresAt": int((time.time() + 3600) * 1000)}}}))
        (home / ".hermes/.env").write_text("OPENROUTER_API_KEY='placeholder'\n")
        (home / ".claude").mkdir()
        (home / ".config/goose/chatgpt_codex").mkdir(parents=True)
        (home / ".config/goose/chatgpt_codex/tokens.json").write_text("{}")
        (home / ".local/share/opencode").mkdir(parents=True)
        (home / ".local/share/opencode/auth.json").write_text(json.dumps({"openrouter": {"key": "placeholder"}}))
        (home / ".gemini/antigravity-cli").mkdir(parents=True)
        self.assertEqual(self.assert_app_matches_cli(home, {}, "Claude Code-credentials,gemini@antigravity"), {
            "hermes": ("ok", "gw.example/hermes: signed in."),
            "claude": ("ok", "Signed in (Keychain: Claude Code-credentials)."),
            "codex": ("ok", "Signed in (Goose)."),
            "opencode": ("ok", "Signed in (auth.json)."),
            "antigravity": ("ok", "Signed in (Keychain)."),
            "openrouter": ("ok", "OPENROUTER_API_KEY found."),
        })
        empty = pathlib.Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertEqual(set(self.assert_app_matches_cli(empty, {}).values()), {
            ("info", "Not set up on this Mac."), ("info", "No OPENROUTER_API_KEY found.")})

    def test_app_and_cli_agree_on_custom_locations(self):
        home = pathlib.Path(self.tmp.name) / "home"
        base = pathlib.Path(self.tmp.name) / "custom"
        self.write_registry("https://gw.example/work", base / "Work Hermes")
        (base / "work claude").mkdir(parents=True)
        (base / "file claude").mkdir()
        (base / "file claude/.credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": "a"}}))
        (base / "opencode").mkdir()
        (base / "agy").mkdir()
        (base / "agy/antigravity-oauth-token").write_text("{}")
        (base / "keys.env").write_text("# OPENROUTER_API_KEY=commented\nOTHER=1\n")
        home.mkdir()
        settings = {"hermes": str(base / "Work Hermes/profiles/coder"), "claude": str(base / "work claude"),
                    "codex": str(base / "missing"), "opencode": str(base / "opencode"),
                    "antigravity": str(base / "agy"), "openrouter": str(base / "keys.env")}
        self.assertEqual(self.assert_app_matches_cli(home, settings, service_for(base / "work claude") + ",gemini@antigravity"), {
            "hermes": ("warn", "gw.example/work: not signed in. Sign in from Hermes Desktop."),
            "claude": ("ok", f"Signed in (Keychain: {service_for(base / 'work claude')})."),
            "codex": ("warn", "This folder doesn't exist."),
            "opencode": ("info", "Not signed in. Run opencode auth login."),
            "antigravity": ("ok", "Signed in (antigravity-oauth-token)."),
            "openrouter": ("warn", "No OPENROUTER_API_KEY in this file."),
        })
        settings.update(claude=str(base / "file claude"), openrouter=str(base / "missing.env"), antigravity=str(base / "opencode"))
        statuses = self.assert_app_matches_cli(home, settings, "Claude Code-credentials,gemini@antigravity")
        self.assertEqual(statuses["claude"], ("ok", "Signed in (.credentials.json)."))
        self.assertEqual(statuses["openrouter"], ("warn", "This file doesn't exist."))
        self.assertEqual(statuses["antigravity"], ("info", "Not signed in. Run agy to sign in."))

    def test_claude_keychain_names_follow_claude_code_and_match_everywhere(self):
        home = pathlib.Path("/Users/someone")
        cli = python_function("provider-quotas.sh", "claude_services", HOME=home)
        reader = python_function("local-quotas.sh", "_claude_services", HOME=home)
        for folder in (home / ".claude", pathlib.Path("/tmp/x"), pathlib.Path("/Users/someone/Work Claude")):
            with self.subTest(folder=str(folder)):
                swift = json.loads(self.run_harness("services", str(folder), str(home)))
                self.assertEqual(swift, cli(folder))
                self.assertEqual(swift, reader(folder))
        self.assertEqual(cli(home / ".claude"), ["Claude Code-credentials", service_for(home / ".claude")])
        self.assertEqual(cli(pathlib.Path("/tmp/x"))[0], "Claude Code-credentials-2e56aa36")

    def test_readers_resolve_the_hermes_home_the_app_passes(self):
        cases = [({}, pathlib.Path.home() / ".hermes"), ({"HERMES_HOME": "/tmp/h/profiles/carto"}, "/tmp/h"),
                 ({"HERMES_HOME": "/tmp/h", "PROVIDER_QUOTAS_HERMES_HOME": "/tmp/app/"}, "/tmp/app"),
                 ({"PROVIDER_QUOTAS_HERMES_HOME": "/tmp/app/profiles/x"}, "/tmp/app")]
        for script in ("desktop-quotas.sh", "local-quotas.sh"):
            resolve = python_function(script, "_hermes_root")
            for env, expected in cases:
                with self.subTest(script=script, env=env), patch.dict(os.environ, clean_env(**env), clear=True):
                    self.assertEqual(str(resolve()), str(expected))

    def test_local_reader_uses_the_freshest_claude_login_and_the_folders_keychain_item(self):
        reader = load_script("local-quotas.sh")
        home = pathlib.Path(self.tmp.name) / "home"
        (home / ".claude").mkdir(parents=True)
        now = int(time.time() * 1000)
        keychain, asked = {}, []

        def check_output(args, **kwargs):
            asked.append(args[args.index("-s") + 1])
            if asked[-1] not in keychain:
                raise subprocess.CalledProcessError(44, args)
            return json.dumps({"claudeAiOauth": keychain[asked[-1]]}).encode()

        def credentials(folder, token, expires):
            (folder / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {"accessToken": token, "expiresAt": expires}}))

        fake = types.SimpleNamespace(check_output=check_output, DEVNULL=subprocess.DEVNULL)
        with patch.dict(reader, {"HOME": home, "subprocess": fake}), patch.dict(os.environ, clean_env(), clear=True):
            credentials(home / ".claude", "stale-file-token", now - 3600_000)
            keychain["Claude Code-credentials"] = {"accessToken": "live-keychain-token", "expiresAt": now + 3600_000}
            self.assertEqual(reader["_anthropic_token"](), "live-keychain-token")
            credentials(home / ".claude", "newer-file-token", now + 7200_000)
            self.assertEqual(reader["_anthropic_token"](), "newer-file-token")
            custom = home / "Work Claude"
            custom.mkdir()
            keychain[service_for(custom)] = {"accessToken": "work-token", "expiresAt": now + 60_000}
            asked.clear()
            os.environ["PROVIDER_QUOTAS_CLAUDE_HOME"] = str(custom)
            self.assertEqual(reader["_anthropic_token"](), "work-token")
            self.assertNotIn("Claude Code-credentials", asked)
            del keychain[service_for(custom)]
            self.assertIsNone(reader["_anthropic_token"]())

    def test_local_reader_reads_custom_codex_opencode_antigravity_and_openrouter_locations(self):
        reader = load_script("local-quotas.sh")
        home = pathlib.Path(self.tmp.name) / "home"
        (home / ".config/goose/chatgpt_codex").mkdir(parents=True)
        (home / ".config/goose/chatgpt_codex/tokens.json").write_text(json.dumps({"access_token": fake_jwt(4_000_000_000)}))
        (home / ".codex").mkdir()
        (home / ".codex/auth.json").write_text(json.dumps({"tokens": {"access_token": fake_jwt(3_000_000_000)}}))
        work = home / "work-codex"
        work.mkdir()
        (work / "auth.json").write_text(json.dumps({"tokens": {"access_token": fake_jwt(2_000_000_000), "account_id": "acct"}}))
        (home / "oc").mkdir()
        (home / "oc/auth.json").write_text(json.dumps({"openrouter": {"key": "custom-opencode-key"}}))
        (home / "agy").mkdir()
        (home / "agy/antigravity-oauth-token").write_text(json.dumps({"token": {"access_token": "custom-agy"}}))
        (home / "keys.env").write_text("OPENROUTER_API_KEY=custom-file-key\n")
        (home / "empty.env").write_text("# nothing\n")
        keychain_calls = []

        def check_output(args, **kwargs):
            keychain_calls.append(args)
            raise subprocess.CalledProcessError(44, args)

        fake = types.SimpleNamespace(check_output=check_output, DEVNULL=subprocess.DEVNULL)
        with patch.dict(reader, {"HOME": home, "subprocess": fake}), \
                patch.dict(os.environ, clean_env(OPENROUTER_API_KEY="environment-key"), clear=True):
            self.assertEqual(reader["_codex_creds"](), (fake_jwt(4_000_000_000), None))
            self.assertEqual(reader["_openrouter_key"](), "environment-key")
            os.environ.update(PROVIDER_QUOTAS_CODEX_HOME=str(work), PROVIDER_QUOTAS_OPENCODE_HOME=str(home / "oc"),
                              PROVIDER_QUOTAS_ANTIGRAVITY_HOME=str(home / "agy"),
                              PROVIDER_QUOTAS_OPENROUTER_ENV=str(home / "keys.env"))
            self.assertEqual(reader["_codex_creds"](), (fake_jwt(2_000_000_000), "acct"))
            self.assertEqual(reader["_opencode_key"](), "custom-opencode-key")
            self.assertEqual(reader["_antigravity_credentials"](), {"token": {"access_token": "custom-agy"}})
            self.assertEqual(keychain_calls, [])
            self.assertEqual(reader["_openrouter_key"](), "custom-file-key")
            os.environ["PROVIDER_QUOTAS_OPENROUTER_ENV"] = str(home / "empty.env")
            self.assertIsNone(reader["_openrouter_key"]())

    def test_local_reader_reads_openrouter_key_from_the_chosen_hermes_home(self):
        (self.store / ".env").write_text("# keys\nOPENROUTER_API_KEY='chosen-home-key'\n")
        reader = load_script("local-quotas.sh")
        with patch.dict(os.environ, clean_env(PROVIDER_QUOTAS_HERMES_HOME=str(self.store)), clear=True):
            self.assertEqual(reader["_openrouter_key"](), "chosen-home-key")
        with patch.dict(os.environ, clean_env(PROVIDER_QUOTAS_HERMES_HOME=str(self.support)), clear=True):
            self.assertIsNone(reader["_openrouter_key"]())

    def test_url_keys_match_the_reader_and_cli(self):
        swift = json.loads(self.run_harness("keys", stdin=json.dumps(URLS)))
        for script in ("desktop-quotas.sh", "provider-quotas.sh"):
            with self.subTest(script=script):
                self.assertEqual([python_function(script, "url_key")(url) for url in URLS], swift)

    def test_bound_unbound_and_undecrypted_sign_ins_count(self):
        url = "https://gw.test/hermes"
        self.write_tokens(tokens={"gateway": {"accessToken": "a", "baseUrl": url}})
        self.assertTrue(self.signed_in("https://GW.test/hermes/"))
        self.assertFalse(self.signed_in("https://other.test/hermes"))
        self.write_tokens(unbound={url + "/": {"accessToken": "a"}})
        self.assertTrue(self.signed_in(url))
        self.write_tokens(undecrypted={url: {"encoding": "safeStorage", "value": "djEw"}})
        self.assertTrue(self.signed_in(url))

    def test_migrated_store_is_authoritative_over_legacy_tokens(self):
        url = "https://gw.test/hermes"
        (self.support / "native-oauth-tokens.json").write_text(json.dumps({url + "/": {"encoding": "plain", "value": "{}"}}))
        self.assertTrue(self.signed_in(url, store=False))
        self.assertFalse(self.signed_in(url))
        self.write_tokens(tokens={"gateway": {"accessToken": "a", "baseUrl": "https://elsewhere.test"}})
        self.assertFalse(self.signed_in(url))

    def test_settings_status_explains_what_the_folder_holds(self):
        self.assertEqual(self.status(self.store / "missing"), (False, "This folder doesn't exist."))
        self.assertEqual(self.status(), (False, "No Hermes Desktop connection found here."))
        self.write_registry()
        self.assertEqual(self.status(), (False, "gw.test/hermes: not signed in. Sign in from Hermes Desktop."))
        session = {"accessToken": "a", "baseUrl": "https://gw.test/hermes"}
        self.write_tokens(tokens={"gateway": {**session, "expiresAt": int((time.time() + 3600) * 1000)}})
        self.assertEqual(self.status(), (True, "gw.test/hermes: signed in."))
        self.write_tokens(tokens={"gateway": {**session, "expiresAt": int((time.time() - 60) * 1000)}})
        self.assertEqual(self.status(), (False, "gw.test/hermes: sign-in expired. Open Hermes Desktop to renew it."))

    def test_settings_status_reports_older_desktop_files(self):
        (self.support / "connections.json").write_text(json.dumps({
            "primary": "old", "connections": [{"id": "old", "kind": "remote", "url": "https://old.test/hermes"}]}))
        (self.support / "native-oauth-tokens.json").write_text(json.dumps({
            "https://old.test/hermes": {"encoding": "safeStorage", "value": "djEw"}}))
        self.assertEqual(self.status(), (True, "old.test/hermes: signed in (older Hermes Desktop files)."))
        expired = {"accessToken": "a", "expiresAt": int((time.time() - 60) * 1000)}
        (self.support / "native-oauth-tokens.json").write_text(json.dumps({
            "https://old.test/hermes": {"encoding": "plain", "value": json.dumps(expired)}}))
        self.assertEqual(self.status(), (False, "old.test/hermes: sign-in expired. Open Hermes Desktop to renew it "
                                                "(older Hermes Desktop files)."))


if __name__ == "__main__":
    unittest.main()
