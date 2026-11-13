# Provider Quotas 👀

A macOS **menu-bar app** (+ a **Hermes gateway plugin**) showing live provider
quota — **OpenRouter**, **Claude**, **Codex**, **opencode** — with usage bars,
reset times, and animated per-session **pets**.

![Quota Viewer menu bar](docs/menubar.png)

Two sources, both **off by default**, toggled from the `Sources` row:

- **Hermes** — piggybacks on the Hermes Desktop app's gateway session (local or
  remote), so it shows exactly what your Desktop can see.
- **Local** — reads the providers signed in on this Mac directly (their own usage
  APIs), no gateway needed.

Enable either or both; each source's providers list under its own header, and a
provider only appears if it's actually signed in.

### Shared Desktop authentication

The Hermes reader follows this Mac's Hermes Desktop gateway connection — a
configured remote gateway always wins over "This device" (the one Desktop is on,
then its primary, then any other), so a Desktop window on the local backend never
turns the gateway's quotas into a local-instance check — and reads
its saved access token. Current Desktop builds keep that registry and sign-in in
the Hermes home (`~/.hermes` unless you change it under **Settings**):
`gateway-connections.json` and `gateway-connection-tokens.json`. Once those exist
the reader uses only them, and falls back to the older
`~/Library/Application Support/Hermes` files otherwise.
**Hermes Desktop owns token renewal**: the menu never
rotates refresh tokens or writes Desktop's credential store. Keep Desktop running
and connected so it can renew the session. If Desktop is closed, the menu can
continue using the saved access token until the gateway rejects it.

If Desktop updates credentials during a poll, the reader reloads them and retries
with the new access token. Brief file-write gaps are retried too. An unavailable
gateway or HTTP 403 is not presented as an expired login; only missing credentials
or a confirmed HTTP 401 requests Desktop sign-in.

Transient quota-fetch failures retain the last successful reading for up to two
minutes, explicitly marked as stale. Longer outages show a retrying/unavailable
state rather than asking you to sign in again.

Normal polls use the gateway's quota cache instead of forcing provider refreshes.
A silent quota connection times out after five seconds; activity requests time out
after two seconds and stop contacting a failed gateway for the rest of that scan.
The menu also bounds helper process execution so a stuck reader cannot hang a poll.
No automatic sign-in, credential repair, or Hermes restart is attempted.

### Settings

The gear button in the menu's bottom bar opens **Settings**. It lists only what
is active, grouped by source:

- **Hermes**: a switch to read Hermes at all, its home folder, and a switch for
  each provider Hermes serves.
- **This Mac**: a switch to read this Mac's own logins, then one row per provider
  found here, with its own switch.

Switching a provider off hides its activity from the menu bar and the pet; its
row shrinks to one line, and switching it back on restores it. Switching a whole
source off stops reading it, and its section shrinks to one line naming what was
found. Providers that aren't set up on this Mac stay out of the list.
**Add Provider** names them and adds one from a folder (or, for OpenRouter, a key
file) you choose. A provider you sign in to later, in its standard location,
appears on its own.

Each location starts as **Automatic**, so most installs never change anything:

| Provider | Automatic location | Read from it |
|---|---|---|
| Hermes | `HERMES_HOME`, else `~/.hermes` | Hermes Desktop's gateway connection and sign-in, and pets |
| Claude | `CLAUDE_CONFIG_DIR`, else `~/.claude` | the Claude Code login (Keychain or `.credentials.json`, whichever is newer), sessions and plan |
| Codex | `CODEX_HOME`, else `~/.codex`, plus Goose's Codex login | `auth.json` and sessions |
| opencode | `$XDG_DATA_HOME/opencode`, else `~/.local/share/opencode` | `auth.json` (its OpenRouter key) and sessions |
| Antigravity | `~/.gemini/antigravity-cli`, plus the `agy` Keychain login | its sign-in token |
| OpenRouter | `OPENROUTER_API_KEY`, else the Hermes home's `.env` | `OPENROUTER_API_KEY` |

Switch a row to **Custom** to read another folder instead, such as a second
account or a copied login (a key file for OpenRouter). Pick it with **Choose…**
or paste a path. Each row's status line says what it found, such as "Signed in
(auth.json)." or "This folder doesn't exist.", and never shows a secret. A custom
Claude folder uses the Keychain item Claude Code creates for that folder
(`Claude Code-credentials-<hash>`, as with `CLAUDE_CONFIG_DIR`). A custom Codex
home is read on its own, without Goose's login. A Hermes profile folder
(`…/profiles/<name>`) maps to its Hermes home, as it does in Hermes Desktop.

The same settings work from the terminal, and a change there reaches the running
menu (and an open Settings window) right away:

```bash
provider-quotas config                             # sources and providers: on/off, location and status
provider-quotas config claude ~/work/.claude       # read another folder
provider-quotas config openrouter ~/keys/openrouter.env
provider-quotas config claude --automatic          # back to the automatic location
provider-quotas config codex --off                 # hide Codex (this Mac) from the menu bar and pet
provider-quotas config hermes:copilot --off        # hide a provider Hermes serves
provider-quotas config local --on                  # read this Mac (hermes switches Hermes)
provider-quotas config --json
```

Setting a location also switches that provider back on.

## Install

**Homebrew** (easiest):

```bash
brew tap gabrielahn/quota https://github.com/gabrielAHN/quota-viewer-ios
brew install --HEAD gabrielahn/quota/provider-quotas
brew services start provider-quotas        # menu bar + run at login
provider-quotas doctor                     # check the install from the terminal
```

Update with `brew upgrade provider-quotas` (or the app's **Check for Updates**).

**From a git clone:**

```bash
./install-menubar.sh   # build + install the app + helpers (also runs install.sh
                       # when a local gateway is present)
```

On a Hermes **gateway host**, install the plugin with `./install.sh` (then
`./verify.sh`). For a remote gateway, run `install.sh` there and the app on your Mac.

## Features

- **Provider rows** — a usage bar in the provider's colour; click to expand for
  per-window bars, reset times, and details. The **eye** hides a provider
  (collapsing its row and dropping its dots); click the hidden row to restore it.
- **Menu-bar dots** — one coloured dot per provider, grouped by source. A dot
  **glows** in its colour while that provider has a session running now; an
  offline / out-of-quota provider gets a red ring. A transient error keeps the
  last-known quota (cached ~15 min), so a blip doesn't read as an outage.
- **Session pets** — one animated pet per source, with a **glowing point per
  running session** in the session's provider colour (a multi-model session shows
  one glowing point per model); points clear the instant a session ends. Hermes
  reads the gateway's live sessions (incl. subagent / background turns); Local
  reads `claude` / `codex` / `opencode` sessions on this Mac. Resize / switch pets
  from the source header; art comes from the [petdex](https://petdex.dev) catalog.
- **Both sources at once** — no fallback; a disconnected source's providers still
  list as **Disconnected**.

## Plan-aware quota windows

Bars represent individual API-reported allowances, not a fixed quota inferred
from the subscription name. Codex Pro, Pro (More), and Pro (Max) share the same
data-driven rendering; a weekly-only plan does not acquire a fictional five-hour
session window.

Window metadata is optional and additive:

- `window_seconds`: the allowance duration, separate from its reset timestamp.
- `scope`: `account` for an account-wide limit, `api-key` for the selected key's
  cap, or a model/bucket identifier.
- `window_id`: the identity within that scope, such as `primary` or `secondary`.

Percentage allowances show **used and left** together (for example, `87% used ·
13% left`); bars fill according to the amount **left**. The collapsed row gives
its period and percentages a full-width line, with the reset countdown below.
Expanded rows keep a compact period/value line; exhausted rows retain the limit
notice and show both percentages. Very small positive allowances use `<1% left`
(and `>99% used`) rather than appearing exhausted.

Periods come from source metadata, never the plan name or distance to reset.
Explicit `Monthly` / `Current month` labels display as **Monthly**; `Calendar
month` stays explicit. A fixed `2592000`-second window displays as **30d**, not a
calendar month. Duration metadata takes precedence over legacy period labels.
No monthly allowance is manufactured for a provider that reports only weekly or
session limits. The current payload has no separate calendar/billing-period field;
monthly support relies on an explicit source label, not a guessed billing cycle.

The collapsed bar and summary refer to the same allowance. Expanded rows keep
independent quotas separate. A depleted model/reserve bucket does not by itself
mark the whole account exhausted. When Codex supplies an account allowance,
independent reserve/model allowances stay text-only; a reserve-only payload can
still show its own bar. Credit balances remain amounts rather than
invented percentages, and unknown usage does not produce a full bar. Older
payloads still work; missing duration is never inferred from time until reset.

For Hermes sources, update the gateway plugin as well as the Mac app. The gateway
compatibility helper preserves Codex duration and scope during the existing
quota-cache refresh, without extra usage requests or changes to authentication.
It runs only inside the fresh refresh subprocess, not as a permanent upstream
plugin modification. Refreshes initiated outside this gateway plugin may still
produce older metadata-free records; those receive the legacy display fallback.

## Debugging from the terminal

Both install paths put a `provider-quotas` CLI on your `PATH`. It runs the same
helpers the menu runs, so what it prints is what the menu shows.

```bash
provider-quotas                  # quotas from the sources enabled in the menu
provider-quotas --source all -v  # both sources, with scopes, fetch age and details
provider-quotas --json           # raw helper payload
provider-quotas doctor           # install, service, sources, gateway, locations and recent logs
provider-quotas logs             # recent menu-bar app logs
provider-quotas config           # what is read and shown, and where from (see Settings)
```

`status` exits `0` when every source reads, `2` when a source needs sign-in, and
`1` for other failures. `doctor` exits `1` when it finds a problem. Apart from
`config`, which saves the app's settings, the CLI only reads: it never
signs in, refreshes credentials, restarts services or forces a provider refresh.

The CLI uses the locations set in the app's Settings or with `config`, and
`doctor` lists each one with where it came from and what it found. The helpers
receive custom locations as `PROVIDER_QUOTAS_HERMES_HOME`,
`PROVIDER_QUOTAS_CLAUDE_HOME`, `PROVIDER_QUOTAS_CODEX_HOME`,
`PROVIDER_QUOTAS_OPENCODE_HOME`, `PROVIDER_QUOTAS_ANTIGRAVITY_HOME` and
`PROVIDER_QUOTAS_OPENROUTER_ENV`. Setting one in your shell overrides the app's
setting for that run, and `config` says so. To run a helper by hand against
another folder, use `PROVIDER_QUOTAS_HERMES_HOME=/path/to/hermes hermes-desktop-quotas`.

## Configuring providers (gateway)

By default the plugin reports the providers Hermes on the gateway actually has
credentials for: every provider in the official quota plugin's cache that is not
`no-credentials`, `not-logged-in` or `opt-in-disabled` (a provider with a recent
good reading stays listed through a brief credential blip). Set
`PROVIDER_QUOTA_PROVIDERS` (comma-separated slugs, optionally `slug=Label`) to pin
an explicit set; with no quota cache yet it falls back to
`openrouter,anthropic,openai-codex`. Every quota is read through the gateway's own
credentials, so the plugin stays tied to the gateway it runs in.

**One Claude row per account.** Hermes can list several Claude subscriptions as
separate named providers: a `providers.<slug>` entry on `api.anthropic.com` whose
key comes from `key_cmd` (say a personal plan and a team seat). The official quota
plugin only reads the gateway's single built-in `anthropic` login, so the plugin
reads each named account itself in the same fresh refresh subprocess. It gets the
token from the account's own `key_cmd`, through Hermes's runner. It then calls
Anthropic's OAuth usage endpoint, plus the profile endpoint for the plan badge at
most every 6 h, and writes `$HERMES_HOME/provider_quota_accounts.json`. Tokens are
never written or returned.

Each account appears under the provider's name, where Claude sits in the list,
with the same windows and metadata as the Local Claude reader. The windows are the
5h session, the week, and the model weeks. When the gateway hides the built-in
`anthropic` provider from its picker (`model_catalog.excluded_providers`), its row
would duplicate one of the accounts, so it is dropped too. The app colours these
rows as Claude, using the payload's `family` field, or the `claude-*` slug as a
fallback.

OpenRouter is shown as credits: **Key credits** is what the connected key can
still spend under its cap (resetting on OpenRouter's UTC daily/weekly/monthly
schedule), **Account credits** is the shared wallet, and whichever is tighter is
listed first, with today/week/month spend underneath.

## Self-healing quota (last-good refresher)

A provider's live usage read can fail *in-process* for a while inside the
long-lived gateway dashboard — an expired-token refresh gap, or a rate-limit
(HTTP 429) condition — even though the credentials are fine and a fresh process
reads the quota without a hitch. When that happens the dashboard would otherwise
blank the provider back to "sign in" despite it having quota.

The plugin keeps a disk-backed **last-good** snapshot
(`$TMPDIR/provider-quota-lastgood.json`) to ride out those blips, and `install.sh`
registers a small launchd job (`io.github.gabrielahn.provider-quota-refresh`,
every 120s) that re-reads every provider in a **fresh** process and writes that
cache. A fresh process never inherits the wedged credential state, so it hands
the failing dashboard a current reading and the menu self-heals **without a
dashboard restart**. The dashboard prefers whichever last-good reading (its own
in-process one or the refresher's on-disk one) is newer by wall clock, so a
rotted process can't keep serving a stale value. It's the gateway host that runs
this job; `verify.sh` confirms it's registered.

## Requirements

macOS 13+ · Homebrew **or** the Xcode command-line tools (`xcode-select
--install`, for the build) · `python3` + `openssl` (preinstalled). It's a
**macOS** app despite the repo name. Plus **a source**: Hermes Desktop signed in
to a gateway running the `provider-quota` plugin, and/or local logins (`claude
login`, `~/.codex`, `OPENROUTER_API_KEY`, `opencode auth login`).

## License

MIT — see [LICENSE](LICENSE).
