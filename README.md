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

The Hermes reader follows this Mac's primary Hermes Desktop connection and reads
its saved access token. **Hermes Desktop owns token renewal**: the menu never
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
provider-quotas doctor           # install, service, sources, gateway and recent logs
provider-quotas logs             # recent menu-bar app logs
```

`status` exits `0` when every source reads, `2` when a source needs sign-in, and
`1` for other failures. `doctor` exits `1` when it finds a problem. The CLI only
reads: it never signs in, refreshes credentials, restarts services or forces a
provider refresh.

## Configuring providers (gateway)

The plugin reports a configurable set via `PROVIDER_QUOTA_PROVIDERS`
(comma-separated slugs, optionally `slug=Label`); unset defaults to
`openrouter,anthropic,openai-codex`. Every quota is read through the gateway's own
credentials, so the plugin stays tied to the gateway it runs in.

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
