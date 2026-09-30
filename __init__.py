"""provider-quota — dashboard-only plugin; routes live in ``dashboard/plugin_api.py``.

Hermes's agent-side plugin loader imports every enabled plugin's package and
logs "Failed to load plugin 'provider-quota': No __init__.py" without one.
There are no agent hooks, so ``register`` is a no-op.
"""


def register(ctx):
    return None
