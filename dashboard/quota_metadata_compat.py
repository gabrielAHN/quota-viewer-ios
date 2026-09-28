"""Process-local bridge for the official quota plugin's legacy Codex windows.

Install only inside the existing short-lived refresh subprocess. Observe JSON
already fetched by Codex; never fetch, renew credentials, or patch upstream files.
Unsupported response/model/serializer shapes leave the official result unchanged.
"""
from dataclasses import fields, is_dataclass, make_dataclass, replace
from datetime import datetime, timezone
from functools import wraps
import math
import threading
from urllib.parse import urlsplit

_KEYS = ('window_seconds', 'scope', 'window_id')


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _signature(window):
    return (window.label, window.used_percent, window.reset_at)


def _project(payload):
    """Retain only aligned window facts, never raw JSON/headers/tokens/account IDs."""
    if not isinstance(payload, dict) or not isinstance(payload.get('rate_limit'), dict):
        return None
    rows = []

    def add(rate, scope, prefix='', extra=False):
        if not isinstance(rate, dict):
            raise ValueError('unsupported rate limit')
        for key, old_label in (('primary', '5h' if extra else 'Session'), ('secondary', 'Weekly')):
            raw = rate.get(key + '_window') or {}
            if not isinstance(raw, dict):
                raise ValueError('unsupported window')
            used = raw.get('used_percent')
            if not _number(used):
                continue
            reset = raw.get('reset_at')
            reset = datetime.fromtimestamp(float(reset), timezone.utc).isoformat() if isinstance(reset, (int, float)) else None
            seconds = raw.get('limit_window_seconds')
            seconds = int(seconds) if _number(seconds) and seconds > 0 and int(seconds) == seconds else None
            label = {'primary': 'Primary', 'secondary': 'Secondary'}[key]
            if seconds == 18000:
                label = 'Session'
            elif seconds == 604800:
                label = 'Weekly'
            elif seconds is not None:
                label = f'{seconds // 86400}d' if seconds % 86400 == 0 else f'{seconds // 3600}h' if seconds % 3600 == 0 else f'{seconds // 60}m' if seconds % 60 == 0 else f'{seconds}s'
            rows.append(((prefix + old_label, float(used), reset), prefix + label,
                         {'window_seconds': seconds, 'scope': scope, 'window_id': key}))

    add(payload['rate_limit'], 'account')
    extras = payload.get('additional_rate_limits') or []
    if not isinstance(extras, list):
        return None
    for index, extra in enumerate(extras):
        if not isinstance(extra, dict):
            continue
        name = str(extra.get('limit_name') or '').strip()
        if not name:
            continue
        feature = extra.get('metered_feature')
        scope = feature.strip() if isinstance(feature, str) and feature.strip() else name or f'additional:{index}'
        prefix = name.replace('GPT-', '').replace('-Codex-', ' Codex ') + ' · '
        add(extra.get('rate_limit') or {}, scope, prefix, True)
    return rows


def _enrich(result, rows):
    if result is None or result.label != 'openai-codex' or result.unavailable_reason or rows is None:
        return result
    signatures = [_signature(w) for w in result.windows]
    if signatures != [row[0] for row in rows] or len(set(signatures)) != len(signatures):
        return result
    updated = []
    for window, (_, label, metadata) in zip(result.windows, rows):
        if not is_dataclass(window):
            return result
        native = any(getattr(window, key, None) is not None for key in _KEYS)
        additions = {key: getattr(window, key, None) if getattr(window, key, None) is not None else value
                     for key, value in metadata.items()}
        field_names = {field.name for field in fields(window)}
        missing = [key for key in additions if key not in field_names]
        if missing:
            cls = make_dataclass('MetadataWindow', [(key, object, None) for key in missing],
                                 bases=(type(window),), frozen=window.__dataclass_params__.frozen)
            values = {field.name: getattr(window, field.name) for field in fields(window) if field.init}
            values.update(additions)
            values['label'] = window.label if native else label
            cloned = cls(**values)
        else:
            cloned = replace(window, label=window.label if native else label, **additions)
        updated.append(cloned)
    return replace(result, windows=updated)


def install(cache=None, httpx_module=None):
    """Install once; False means an unsupported seam (official refresh still runs)."""
    try:
        if cache is None:
            from quota import quota_cache as cache
        if httpx_module is None:
            import httpx as httpx_module
        if getattr(cache, '_codex_metadata_compat_installed', False):
            return True
        registry = cache.PROVIDER_FETCHERS
        fetch = registry['openai-codex']
        serialize = cache._result_to_record
        response_type = httpx_module.Response
        original_json = response_type.json
        if not all(callable(fn) for fn in (fetch, serialize, original_json)):
            return False
    except Exception:
        return False
    local = threading.local()

    @wraps(original_json)
    def observed_json(response, *args, **kwargs):
        payload = original_json(response, *args, **kwargs)
        if getattr(local, 'active', False):
            try:
                url = urlsplit(str(response.request.url))
                if url.scheme == 'https' and url.hostname in ('chatgpt.com', 'chat.openai.com') and url.path in ('/backend-api/wham/usage', '/wham/usage'):
                    local.count += 1
                    local.rows = _project(payload) if local.count == 1 else None
            except Exception:
                local.rows = None
        return payload

    @wraps(fetch)
    def fetch_with_metadata(*args, **kwargs):
        if getattr(local, 'active', False):
            local.rows = None
            return fetch(*args, **kwargs)
        local.active, local.rows, local.count = True, None, 0
        try:
            result = fetch(*args, **kwargs)
            try:
                return _enrich(result, local.rows)
            except Exception:
                return result
        finally:
            local.__dict__.clear()

    @wraps(serialize)
    def serialize_with_metadata(result, *args, **kwargs):
        record = serialize(result, *args, **kwargs)
        try:
            if result.label != 'openai-codex':
                return record
            windows = record['windows']
            if len(windows) != len(result.windows) or any(
                (row.get('label'), row.get('used_percent'), row.get('reset_at')) != _signature(window)
                for row, window in zip(windows, result.windows)
            ):
                return record
            enriched = []
            for row, window in zip(windows, result.windows):
                row = dict(row)
                for key in _KEYS:
                    value = getattr(window, key, None)
                    if key not in row and value is not None:
                        row[key] = value
                enriched.append(row)
            return dict(record, windows=enriched)
        except Exception:
            return record

    try:
        response_type.json = observed_json
        registry['openai-codex'] = fetch_with_metadata
        cache._result_to_record = serialize_with_metadata
        cache._codex_metadata_compat_installed = True
        return True
    except Exception:
        response_type.json = original_json
        registry['openai-codex'] = fetch
        cache._result_to_record = serialize
        return False
