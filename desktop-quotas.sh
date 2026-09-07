#!/usr/bin/env bash
# Provider quotas for the menu-bar, through the Hermes Desktop app's own gateway
# session. Works with whatever gateway the Desktop is bound to — a LOCAL backend
# or any REMOTE gateway (OAuth/Authelia) — exactly like Hermes Desktop's own
# Provider Quotas page (/api/plugins/provider-quota/quotas).
#
# Portable: uses only the system python3 + openssl + security (no Hermes venv,
# httpx, or cryptography), so it runs on a client Mac that has Hermes Desktop but
# no local gateway install. Prints the QuotaPayload JSON to stdout on success;
# on any failure prints a one-line reason to stderr and exits non-zero.
set -euo pipefail
exec /usr/bin/env python3 - "$@" <<'PY'
import base64, hashlib, json, subprocess, sys, urllib.request, urllib.error
from pathlib import Path

SUPPORT = Path.home() / "Library/Application Support/Hermes"
# Optional args: $1 = gateway endpoint path (default: the quotas endpoint),
# $2 = output file for binary bodies (e.g. a pet spritesheet); otherwise the
# body is written to stdout.
ENDPOINT = sys.argv[1] if len(sys.argv) > 1 else "/api/plugins/provider-quota/quotas?refresh=true"
OUT = sys.argv[2] if len(sys.argv) > 2 else None
# Activity polls run on the menu-bar's 1s timer and only read session lists, so a
# slow gateway call must fail FAST — a 20s hang would stall the in-flight guard
# and make a session's dot appear (or clear) many seconds late. Quota fetches hit
# slow provider APIs, so they keep the generous timeout.
TIMEOUT = 6 if ENDPOINT == "--activity" else 20


def fail(msg):
    sys.stderr.write(msg.rstrip() + "\n")
    sys.exit(1)


def emit(data):
    if OUT:
        with open(OUT, "wb") as fh:
            fh.write(data)
    else:
        sys.stdout.buffer.write(data)


def read_json(name):
    try:
        return json.loads((SUPPORT / name).read_text())
    except Exception:
        return None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # surface 3xx as an error instead of following to a login page


_opener = urllib.request.build_opener(_NoRedirect)

# Gateways are often fronted by Cloudflare, which 403s the default
# "Python-urllib/x.y" agent. Send a browser-like UA so the request is allowed.
_USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")


def http_get(url, headers):
    hdrs = dict(headers)
    hdrs.setdefault("User-Agent", _USER_AGENT)
    req = urllib.request.Request(url, headers=hdrs)
    try:
        with _opener.open(req, timeout=TIMEOUT) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    except Exception as exc:
        fail("Cannot reach Hermes gateway: %s" % exc)


# --- Which gateway is the Desktop bound to? Prefer the primary entry in the v2
# connections.json, fall back to the legacy connection.json. ---
conns = read_json("connections.json") or {}
primary = None
if isinstance(conns.get("connections"), list) and conns["connections"]:
    by_id = {c.get("id"): c for c in conns["connections"]}
    primary = by_id.get(conns.get("primary")) or conns["connections"][0]

if primary is not None:
    kind = primary.get("kind")
    url = (primary.get("url") or "").rstrip("/")
else:
    legacy = read_json("connection.json") or {}
    kind = legacy.get("mode")
    url = ""
    if kind and kind != "local":
        url = ((legacy.get(kind) or legacy.get("remote") or {}).get("url") or "").rstrip("/")

if not kind:
    fail("Hermes Desktop is not set up.")

def _norm(value):
    return value.strip().strip("/")


# --- Build a `fetch(path) -> (status, body)` bound to the gateway the Desktop is
# bound to: a loopback bind (local mode) needs no auth; a remote gateway uses the
# Desktop's OAuth session. Resolving this once lets one process serve several
# endpoints (see --activity) with a single Keychain read / token decrypt. ---
if kind == "local":
    own = read_json("backend-ownership.json") or {}
    local_url = None
    for backend in own.get("backends") or []:
        candidate = (backend.get("url") or backend.get("baseUrl") or "").rstrip("/")
        if candidate:
            local_url = candidate
            break
        port = backend.get("port")
        if port:
            local_url = "http://127.0.0.1:%s" % port
            break
    if not local_url:
        fail("Hermes Desktop is in local mode but no local gateway is running.")

    def fetch(path):
        return http_get(local_url + path, {"Accept": "*/*"})

    EXPIRED_MSG = "Local Hermes gateway returned HTTP %s."
else:
    if not url:
        fail("Hermes Desktop has no gateway configured.")
    tokens = read_json("native-oauth-tokens.json")
    if tokens is None:
        fail("Sign in to Hermes Desktop.")
    entry = tokens.get(url) or next((v for k, v in tokens.items() if _norm(k) == _norm(url)), None)
    if not entry:
        fail("Sign in to Hermes Desktop (%s)." % url)

    value = entry.get("value") or ""
    if entry.get("encoding") == "safeStorage":
        # Electron safeStorage v10: AES-128-CBC, key = PBKDF2-HMAC-SHA1(secret,
        # "saltysalt", 1003, 16), IV = 16 spaces. Decrypt with openssl to avoid a
        # python crypto dependency.
        try:
            secret = subprocess.check_output(
                ["security", "find-generic-password", "-s", "Hermes Safe Storage", "-w"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
        except Exception as exc:
            fail("Cannot read 'Hermes Safe Storage' Keychain key: %s" % exc)
        key = hashlib.pbkdf2_hmac("sha1", secret.encode(), b"saltysalt", 1003, dklen=16)
        raw = base64.b64decode(value)
        if raw[:3] != b"v10":
            fail("Unexpected Hermes Desktop token format.")
        proc = subprocess.run(
            ["openssl", "enc", "-d", "-aes-128-cbc", "-K", key.hex(),
             "-iv", "20" * 16, "-nopad"],
            input=raw[3:], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        plaintext = proc.stdout
        if not plaintext:
            fail("Cannot decrypt Hermes Desktop session token (openssl).")
        plaintext = plaintext[: -plaintext[-1]]  # strip PKCS7 padding
        session = json.loads(plaintext.decode())
    else:
        session = json.loads(value)

    access_token = session.get("accessToken") or ""
    if not access_token:
        fail("Sign in to Hermes Desktop.")

    def fetch(path):
        return http_get(url + path, {"Authorization": "Bearer " + access_token, "Accept": "*/*"})

    EXPIRED_MSG = "Hermes gateway returned HTTP %s."


def get_json(path):
    status, body = fetch(path)
    if status != 200:
        return None
    try:
        return json.loads(body)
    except Exception:
        return None


def _spawned_local_backend_get_json():
    """A get_json bound to the LOCAL `hermes serve` backend the Desktop spawned, if
    one is running — INDEPENDENT of which connection is the Desktop's PRIMARY. A
    Desktop chat runs on that local backend (source: desktop), so the Hermes pet has
    to read it even when the primary connection is a REMOTE gateway whose
    /api/sessions never lists this machine's desktop sessions. The backend's REST
    token is not on disk — Electron mints it in memory and hands it to `serve` as
    HERMES_DASHBOARD_SESSION_TOKEN — so resolve everything live: backend pid (from
    backend-ownership.json) -> its listening loopback port (lsof; serve runs with
    --port 0) -> the token from that process's OWN environment. Returns None when no
    local backend is running or its port/token can't be resolved. This is the macOS
    path (ps eww / lsof over our own uid); a client with only a remote gateway and no
    local spawn simply gets None and nothing changes."""
    import re
    own = read_json("backend-ownership.json") or {}
    for backend in own.get("backends") or []:
        pid = backend.get("pid")
        if not isinstance(pid, int) or pid <= 0:
            continue
        base = (backend.get("url") or backend.get("baseUrl") or "").rstrip("/")
        if not base:
            port = backend.get("port")
            if not port:
                try:
                    listing = subprocess.run(
                        ["/usr/sbin/lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid)],
                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3,
                    ).stdout.decode("utf-8", "replace")
                    m = re.search(r"127\.0\.0\.1:(\d+)", listing)
                    port = m.group(1) if m else None
                except Exception:
                    port = None
            if not port:
                continue
            base = "http://127.0.0.1:%s" % port
        token = None
        try:
            env = subprocess.run(
                ["ps", "eww", "-p", str(pid)],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=3,
            ).stdout.decode("utf-8", "replace")
            m = re.search(r"HERMES_DASHBOARD_SESSION_TOKEN=(\S+)", env)
            token = m.group(1) if m else None
        except Exception:
            token = None
        headers = {"Accept": "*/*"}
        if token:
            headers["X-Hermes-Session-Token"] = token
        def _get(path, _base=base, _headers=headers):
            status, body = http_get(_base + path, _headers)
            if status != 200:
                return None
            try:
                return json.loads(body)
            except Exception:
                return None
        return _get
    return None


# --- Activity mode: resolve the gateway ONCE, then fetch /api/status plus each
# profile's recent sessions in the same process, and emit a compact summary the
# menu-bar uses to light the Hermes pet. Bot turns run as `source: cli` sessions
# that DON'T show up in the aggregate active_agents/active_sessions counters, so
# we return the per-session flags (is_active / ended_at / last_active) and let the
# app decide what's "running". Never fails: a signed-out gateway just yields an
# empty summary so the pet quietly stays idle. ---
if ENDPOINT == "--activity":
    import re, time
    from concurrent.futures import ThreadPoolExecutor

    def _dot_provider(s):
        # Colour the pet's dot by the MODEL FAMILY the user reasons about — Claude
        # vs Codex vs OpenRouter — resolved from `model`, which is populated from
        # the moment a session starts. We do NOT lead with `billing_provider`: it's
        # only stamped by usage accounting AFTER the first response (null on early
        # and failed turns → grey), and for a Claude model served through Copilot it
        # reads "copilot-acp" → wrong colour. OpenRouter serves "vendor/model" slugs
        # (qwen/…, deepseek/…, anthropic/…, openai/…), so ANY "/" means OpenRouter.
        # Fall back to billing_provider / provider only when the family is unknown.
        model = (s.get("model") or "").lower()
        if model:
            if "/" in model:
                return "openrouter"
            if model.startswith("claude"):
                return "anthropic"
            if model.startswith("gpt") or "codex" in model or model.startswith(("o1", "o3", "o4")):
                return "openai-codex"
        return s.get("billing_provider") or s.get("provider") or ""

    def _soft(getter):
        # A source that's down/signed-out must NOT kill the scan — we merge whatever
        # sources ARE up. (http_get fail()s hard via sys.exit on a dead gateway.)
        def g(path):
            try:
                return getter(path)
            except SystemExit:
                return None
            except Exception:
                return None
        return g

    # Sessions come from the ONE provider-quota plugin's /activity endpoint, which
    # aggregates the session store + tui_gateway logs + active-sessions registry
    # SERVER-SIDE (per provider). The client reads sessions from that single plugin
    # over the Desktop/gateway connection instead of scanning REST itself. An active
    # session gets a fresh last_active so the client lights it; model → colour.
    out = []
    status_busy = False
    act = _soft(get_json)("/api/plugins/provider-quota/activity") or {}

    # The /activity endpoint only reports is_active (no turn state). The core session
    # store (/api/sessions) carries a human-readable `last_activity_description` per
    # session — e.g. "tool running: clarify" while an agent is asking YOU to clarify.
    # Join by session_id to detect the WAITING-FOR-YOU states without a gateway change.
    # Collect sessions from the PRIMARY connection AND — always — the local
    # `hermes serve` backend the Desktop spawned, if any. Desktop chats run on that
    # local backend (source: desktop); when the primary connection is a REMOTE
    # gateway its /api/sessions never lists them, so without this merge the Hermes
    # pet is blind to the very sessions you start from the app. Dedup by session id
    # so a backend that is BOTH the primary and the local spawn isn't counted twice.
    store_by_id = {}
    merged_sessions = []
    _seen_sids = set()

    def _ingest(container):
        if not isinstance(container, dict):
            return
        for _s in (container.get("sessions") or []):
            _sid = _s.get("session_id") or _s.get("id")
            key = str(_sid) if _sid else ("obj:%d" % id(_s))
            if key in _seen_sids:
                continue
            _seen_sids.add(key)
            merged_sessions.append(_s)
            if _sid:
                store_by_id[str(_sid)] = _s

    try:
        _ingest(_soft(get_json)("/api/sessions"))
        _local_get = _spawned_local_backend_get_json()
        if _local_get is not None:
            _ingest(_soft(_local_get)("/api/sessions"))
            # If the primary's plugin /activity came back empty (typical when the
            # primary is a remote gateway but the live sessions are local), let the
            # local backend's /activity be the fallback list too.
            if not (act.get("sessions") if isinstance(act, dict) else None):
                _local_act = _soft(_local_get)("/api/plugins/provider-quota/activity")
                if isinstance(_local_act, dict) and _local_act.get("sessions"):
                    act = _local_act
    except Exception:
        pass
    _sess = {"sessions": merged_sessions}

    def _store(sess):
        return store_by_id.get(str(sess.get("session_id") or ""), {})

    # Hermes describes a session's step as "tool running: <name>" while the tool is
    # PENDING — blocked waiting on YOU — and "executing tool: <name>" once it's been
    # approved and is actually running. So a PENDING tool means attention:
    #   - an INPUT prompt (clarify / ask / …) → input
    #   - an ACTION awaiting your APPROVAL (terminal / patch / edit / write / …) →
    #     permission — anything that isn't a plain read-only tool.
    _INPUT_TOOLS = ("clarify", "ask_user", "askuser", "ask_followup", "ask_question",
                    "request_input", "user_input", "get_input", "elicit", "prompt_user")
    # Read-only tools are auto-approved (a pending read is transient, not "permission").
    _READONLY_TOOLS = ("read", "grep", "glob", "ls", "list", "search", "find", "cat",
                       "view", "fetch", "web", "todo", "think", "plan", "notebook_read")
    _INPUT_PHRASES = ("waiting for input", "awaiting input", "needs input", "input needed",
                      "awaiting your response", "waiting for you", "awaiting user")
    _PERM_PHRASES = ("permission", "approval", "awaiting approval", "needs approval",
                     "awaiting your approval", "permission required", "waiting for approval")

    def _matches(tool, names):
        return any(tool == t or tool.startswith(t) for t in names)

    def _attention(sess):
        # Explicit gateway flags win if the gateway ever reports them.
        if any(sess.get(k) is True for k in ("needs_permission", "awaiting_approval", "permission_required")):
            return "permission"
        if any(sess.get(k) is True for k in ("needs_input", "awaiting_input", "input_required")):
            return "input"
        st = str(sess.get("state") or sess.get("status") or "").lower()
        if st in ("permission", "awaiting_approval", "approval", "needs_permission"):
            return "permission"
        if st in ("waiting", "awaiting_input", "needs_input", "input_required"):
            return "input"
        # Else derive from the activity description (the session's own, else the store's).
        d = str(sess.get("last_activity_description") or _store(sess).get("last_activity_description") or "").lower()
        if any(p in d for p in _PERM_PHRASES):
            return "permission"
        if any(p in d for p in _INPUT_PHRASES):
            return "input"
        # PENDING tool ("tool running: X") — waiting on you. "executing tool: X" is
        # already approved and just working, so it is deliberately NOT matched here.
        if "tool running:" in d:
            tool = d.split("tool running:", 1)[1].strip()
            if _matches(tool, _INPUT_TOOLS):
                return "input"
            if not _matches(tool, _READONLY_TOOLS):
                return "permission"
        return ""

    # The gateway's is_active flag is UNRELIABLE — it stays False for sessions that
    # are plainly running: a cli/tool/desktop turn mid-API-call, or one blocked
    # waiting on a slow provider ("waiting on <model> — 115s with no output yet").
    # So drive the pet from the session STORE (/api/sessions): a session is LIVE when
    # it's still OPEN (ended_at is None) AND working (is_active, or its activity
    # description shows work) or needs you. Ended sessions never dot (that honours
    # "only active"). Fall back to the /activity is_active list if the store is down.
    _WORKING = ("starting api call", "api call", "receiving stream", "streaming",
                "waiting on", "executing tool", "generating", "thinking",
                "responding", "reasoning", "running")
    # A session must have done SOMETHING within this window to count as running.
    # This is what rejects LEAKED sessions — ones left open (ended_at never set) for
    # days, frozen at "starting API call #1" (api_call_count 0), which otherwise
    # matched a working phrase and showed a phantom dot.
    _FRESH = 120.0
    now = time.time()

    def _last_activity(sess):
        for k in ("last_active", "last_activity_at", "started_at"):
            v = sess.get(k)
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
        return 0.0

    def _live(sess):
        # is_active is trustworthy when TRUE — it flips False the instant a turn ends
        # — so honour it directly. When False it's unreliable (a running turn can read
        # False), so fall back to "working per description AND recent activity". The
        # recency gate is what keeps days-old leaked sessions from lighting the pet.
        if sess.get("is_active"):
            return True
        la = _last_activity(sess)
        if not la or (now - la) > _FRESH:
            return False
        d = str(sess.get("last_activity_description")
                or _store(sess).get("last_activity_description") or "").lower()
        return any(p in d for p in _WORKING) or bool(_attention(sess))

    store_list = _sess.get("sessions") if isinstance(_sess, dict) else None
    if store_list:
        candidates = [s for s in store_list if s.get("ended_at") is None and _live(s)]
    else:
        candidates = [s for s in (act.get("sessions") or []) if s.get("is_active")]

    for s in candidates:
        att = _attention(s)
        # A session waiting for YOU counts as ATTENTION, not "busy" — don't let it set
        # the aggregate busy flag (the pet should wave, not read as working).
        if att == "":
            status_busy = True
        # Colour by the LIVE model from the store session (falling back to the joined
        # store entry, then billing/provider). Multi-model → one wedge per family.
        st = _store(s)
        src = st if (st.get("model") or st.get("models")) else s
        model_list = ([src["model"]] if src.get("model") else []) + (src.get("models") or [])
        families = []
        for m in model_list:
            fam = _dot_provider({"model": m})
            if fam and fam not in families:
                families.append(fam)
        if not families:
            fam = _dot_provider({"model": src.get("model"), "billing_provider": src.get("billing_provider")}) or _dot_provider(s)
            families = [fam] if fam else []
        out.append({
            "is_active": True,   # we already filtered to genuinely-live sessions
            "ended_at": s.get("ended_at"),
            "last_active": _last_activity(s) or now,
            "billing_provider": _dot_provider({"model": src.get("model"), "billing_provider": src.get("billing_provider")}) or _dot_provider(s),
            "provider": src.get("provider") or s.get("provider"),
            "providers": families,
            "needs_input": att == "input",
            "needs_permission": att == "permission",
        })
    emit(json.dumps({"agents": 0, "status_busy": status_busy, "sessions": out}).encode())
    sys.exit(0)

# --- Normal single-endpoint mode. ---
status, body = fetch(ENDPOINT)
if kind != "local" and status in (301, 302, 303, 307, 308, 401, 403):
    fail("Hermes Desktop session expired — open Hermes Desktop to refresh.")
if status != 200:
    fail(EXPIRED_MSG % status)
emit(body)
PY
