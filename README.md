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

## Install

**Homebrew** (easiest):

```bash
brew tap gabrielahn/quota https://github.com/gabrielAHN/quota-viewer-ios
brew install --HEAD gabrielahn/quota/provider-quotas
brew services start provider-quotas        # menu bar + run at login
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
