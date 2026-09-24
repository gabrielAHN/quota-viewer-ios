#!/usr/bin/env python3
"""Seed the provider-quota last-good disk cache from a FRESH process.

Why this exists
---------------
The dashboard plugin keeps a disk-backed last-good snapshot
(``$TMPDIR/provider-quota-lastgood.json``) so a TRANSIENT in-process failure —
an expired-token refresh gap or a rate-limit condition that wedges the
long-lived dashboard's usage reads for a while — never blanks a provider that
actually has quota. Instead of collapsing to "sign in", the dashboard falls
back to the last good reading.

The gap this closes
-------------------
That disk mirror is only written when the dashboard's OWN in-process read
succeeds — which is exactly what fails when the process has rotted (the class
of failure the fallback was meant to survive). So a long-lived dashboard whose
per-provider usage reads are 401/429-ing in-process has nothing refreshing its
fallback, and the menu stays blank until the process is restarted. The
persistence machinery was designed for "a fresh short-lived reader hands a good
reading to the wedged long-lived one" — but nothing ever spawned that reader.

What this does
--------------
Run periodically as a SHORT-LIVED process (launchd ``StartInterval`` on the
gateway host), it re-reads every configured provider through the plugin's own
``_provider`` — which, on success, records the snapshot to the same disk path
the dashboard reads. A fresh process never inherits the wedged credential
state, so it succeeds where the dashboard is failing and hands it a current
reading with no restart. It reuses the plugin's exact provider set, snapshot
shape, Anthropic file-token fallback and disk path — no logic is duplicated
here. Best-effort and quiet: any failure is a no-op, so the launchd job never
flaps and the dashboard simply keeps whatever it already had.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_PLUGIN_API = Path.home() / ".hermes/plugins/provider-quota/dashboard/plugin_api.py"


def _load_plugin_module():
    """Load the installed plugin by file path (matches verify.sh), so this works
    regardless of sys.path — the plugin dir is not an importable package."""
    spec = importlib.util.spec_from_file_location("provider_quota_refresh", _PLUGIN_API)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load plugin_api from {_PLUGIN_API}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main() -> int:
    try:
        mod = _load_plugin_module()
    except Exception as exc:  # best-effort: never fail the launchd job
        print(f"provider-quota refresh: cannot load plugin ({exc})", file=sys.stderr)
        return 0
    seeded = 0
    # Since the official-quota-plugin adoption, _provider reads the quota plugin's
    # cache rather than fetching itself — so refresh that cache first (this IS a
    # fresh short-lived process, exactly what the sweep wants), then adapt each
    # provider through _provider, which records the last-good disk snapshot on a
    # good reading.
    try:
        mod._refresh_official_cache()
    except Exception:
        pass
    cache = mod._read_official_cache()
    for slug, label in mod._configured_providers():
        try:
            result = mod._provider(slug, label, cache)
            if result.get("status") == "ok" and result.get("windows"):
                seeded += 1
        except Exception:
            # One provider failing must never stop the others.
            pass
    print(f"provider-quota refresh: seeded {seeded} provider(s) to last-good cache")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
