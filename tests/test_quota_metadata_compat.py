"""Exercise the installed upstream fetch/serializer, not a reimplementation."""
import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class CompatTests(unittest.TestCase):
    def setUp(self):
        self.up = load('codex_upstream_fixture', ROOT / 'tests/fixtures/codex_upstream.py')
        self.up.PROVIDER_FETCHERS = {'openai-codex': self.up._fetch_codex_with_models}
        self.requests = []
        self.payload = {'plan_type': 'prolite', 'rate_limit': {
            'primary_window': {'used_percent': 31, 'reset_at': 1800000000, 'limit_window_seconds': 604800},
            'secondary_window': {'used_percent': 42, 'reset_at': 1800000100, 'limit_window_seconds': 604800}}}
        owner = self
        class Response:
            request = types.SimpleNamespace(url='https://chatgpt.com/backend-api/wham/usage')
            def json(self):
                return owner.payload
            def raise_for_status(self):
                pass
        class Client:
            def __init__(self, **kwargs):
                pass
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
            def get(self, url, headers):
                owner.requests.append(url)
                return Response()
        self.httpx = types.SimpleNamespace(Response=Response, Client=Client)
        usage = types.ModuleType('agent.account_usage')
        usage._resolve_codex_usage_credentials = lambda *a: ('fixture-token', 'https://chatgpt.com', None)
        usage._codex_backend_urls = lambda base: ('https://chatgpt.com/backend-api/wham/usage',)
        self.modules = patch.dict(sys.modules, {'httpx': self.httpx, 'agent': types.ModuleType('agent'), 'agent.account_usage': usage})
        self.modules.start()
        self.addCleanup(self.modules.stop)

    def install(self):
        path = ROOT / 'dashboard/quota_metadata_compat.py'
        self.assertTrue(path.exists(), 'subprocess metadata compatibility helper is missing')
        self.compat = load('quota_metadata_compat_test', path)
        return self.compat.install(self.up, self.httpx)

    def fetch(self):
        return self.up.PROVIDER_FETCHERS['openai-codex']()

    def test_weekly_primary_and_secondary_one_request_and_serialization(self):
        self.assertTrue(self.install())
        result = self.fetch()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual([w.label for w in result.windows], ['Weekly', 'Weekly'])
        record = self.up._result_to_record(result)
        for index, window in enumerate(record['windows']):
            self.assertEqual(window['window_seconds'], 604800)
            self.assertEqual(window['scope'], 'account')
            self.assertEqual(window['window_id'], ['primary', 'secondary'][index])
        self.assertEqual(record['plan'], 'Prolite')

    def test_standard_missing_and_custom_durations(self):
        self.install()
        primary = self.payload['rate_limit']['primary_window']
        for seconds, label in [(18000, 'Session'), (86400, '1d'), (7200, '2h'), (90, '90s'), (None, 'Primary'), (True, 'Primary'), (-1, 'Primary')]:
            primary['limit_window_seconds'] = seconds
            self.assertEqual(self.fetch().windows[0].label, label)
        self.payload['rate_limit']['secondary_window'].pop('limit_window_seconds')
        self.assertEqual(self.fetch().windows[1].label, 'Secondary')

    def test_extra_scopes_and_ignored_windows_align(self):
        self.payload['additional_rate_limits'] = [None, {'rate_limit': self.payload['rate_limit']},
            {'limit_name': 'GPT-5-Codex-Spark', 'metered_feature': 'spark', 'rate_limit': self.payload['rate_limit']},
            {'limit_name': 'Other', 'rate_limit': {'secondary_window': self.payload['rate_limit']['secondary_window']}}]
        self.payload['rate_limit']['unknown_window'] = {'used_percent': 8}
        self.install()
        result = self.fetch()
        self.assertEqual(len(result.windows), 5)
        self.assertEqual([w.scope for w in result.windows], ['account', 'account', 'spark', 'spark', 'Other'])
        self.assertEqual(result.windows[2].label, '5 Codex Spark · Weekly')

    def test_duplicate_signatures_fail_closed(self):
        extra = {'limit_name': 'same', 'rate_limit': self.payload['rate_limit']}
        self.payload['additional_rate_limits'] = [extra, extra]
        self.install()
        result = self.fetch()
        self.assertEqual(result.windows[0].label, 'Session')
        self.assertFalse(hasattr(result.windows[0], 'window_seconds'))

    def test_schema_alignment_changes_fail_closed(self):
        for mutation in ('count', 'label', 'usage', 'reset'):
            with self.subTest(mutation=mutation):
                self.setUp()
                original = self.up.PROVIDER_FETCHERS['openai-codex']
                def changed():
                    result = original()
                    if mutation == 'count':
                        result.windows.pop()
                    else:
                        setattr(result.windows[0], {'label': 'label', 'usage': 'used_percent', 'reset': 'reset_at'}[mutation], 'changed')
                    return result
                self.up.PROVIDER_FETCHERS['openai-codex'] = changed
                self.install()
                self.assertFalse(hasattr(self.fetch().windows[0], 'window_seconds'))

    def test_frozen_slotted_dataclasses(self):
        from dataclasses import make_dataclass
        self.up.QuotaWindow = make_dataclass('FrozenWindow', [('label', str), ('used_percent', float), ('reset_at', str)], frozen=True, slots=True)
        self.up.QuotaResult = make_dataclass('FrozenResult', [('label', str), ('windows', list), ('plan', object), ('unavailable_reason', object), ('details', list)], frozen=True, slots=True)
        self.install()
        self.assertEqual(self.fetch().windows[0].window_seconds, 604800)

    def test_native_metadata_and_serializer_fields_preserved(self):
        from dataclasses import make_dataclass
        self.up.QuotaWindow = make_dataclass('NativeWindow', [('window_seconds', int, 123), ('scope', str, 'native'), ('window_id', str, 'native-id')], bases=(self.up.QuotaWindow,))
        original = self.up._result_to_record
        def native_serializer(result):
            record = original(result)
            for row in record['windows']:
                row['scope'] = 'serialized-native'
            return record
        self.up._result_to_record = native_serializer
        self.install()
        result = self.fetch()
        self.assertEqual(result.windows[0].label, 'Session')
        self.assertEqual(result.windows[0].window_seconds, 123)
        record = self.up._result_to_record(result)
        self.assertEqual(record['windows'][0]['scope'], 'serialized-native')
        self.assertEqual(record['windows'][0]['window_id'], 'native-id')

    def test_concurrent_non_codex_not_contaminated_and_atomic_write(self):
        import threading
        import tempfile
        import json
        barrier = threading.Barrier(2)
        original = self.httpx.Response.json
        def interleaved(response):
            barrier.wait(timeout=2)
            return original(response)
        self.httpx.Response.json = interleaved
        def other():
            self.httpx.Response().json()
            return self.up.QuotaResult('other', [self.up.QuotaWindow('Session', 31, None)])
        self.up.PROVIDER_FETCHERS['other'] = other
        self.up.REFRESH_BUDGET_S = 3
        self.up._CACHE_LOCK = threading.Lock()
        self.install()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'quota_cache.json'
            self.up._cache_path = lambda: str(target)
            self.up.refresh_quota_cache()
            cache = json.loads(target.read_text())
        self.assertEqual(cache['providers']['openai-codex']['windows'][0]['window_seconds'], 604800)
        self.assertNotIn('scope', cache['providers']['other']['windows'][0])
        self.assertEqual(len(self.requests), 1)

    def test_idempotent_and_unsupported_seams(self):
        self.install()
        fetch = self.up.PROVIDER_FETCHERS['openai-codex']
        json_method = self.httpx.Response.json
        self.assertTrue(self.compat.install(self.up, self.httpx))
        self.assertIs(fetch, self.up.PROVIDER_FETCHERS['openai-codex'])
        self.assertIs(json_method, self.httpx.Response.json)
        self.assertFalse(self.compat.install(types.SimpleNamespace(), self.httpx))
        self.assertIs(json_method, self.httpx.Response.json)

    def test_native_dynamic_attributes_preserved(self):
        original = self.up.PROVIDER_FETCHERS['openai-codex']
        def native():
            result = original()
            result.windows[0].scope = 'native-dynamic'
            return result
        self.up.PROVIDER_FETCHERS['openai-codex'] = native
        self.install()
        self.assertEqual(self.fetch().windows[0].scope, 'native-dynamic')

    def test_multiple_json_captures_and_invalid_payload_fail_closed(self):
        original = self.up.PROVIDER_FETCHERS['openai-codex']
        def twice():
            result = original()
            self.httpx.Response().json()
            return result
        self.up.PROVIDER_FETCHERS['openai-codex'] = twice
        self.install()
        self.assertFalse(hasattr(self.fetch().windows[0], 'scope'))
        self.assertIsNone(self.compat._project({'rate_limit': None}))

    def test_unrelated_endpoint_fail_closed(self):
        self.install()
        self.httpx.Response.request = types.SimpleNamespace(url='https://unrelated.example/backend-api/wham/usage')
        self.assertFalse(hasattr(self.fetch().windows[0], 'scope'))

    def test_refresh_subprocess_loads_helper_and_installer_copies_it(self):
        import ast
        import os
        import subprocess
        source = (ROOT / 'dashboard/plugin_api.py').read_text()
        tree = ast.parse(source)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == '_refresh_official_cache')
        namespace = {'Path': Path, 'os': os, '__file__': str(ROOT / 'dashboard/plugin_api.py'), '_hermes_home': lambda: ROOT}
        exec(compile(ast.Module(body=[node], type_ignores=[]), '<refresh>', 'exec'), namespace)
        with patch.object(Path, 'is_file', return_value=True), patch.object(subprocess, 'run') as run:
            namespace['_refresh_official_cache']()
        args, kwargs = run.call_args
        self.assertIn('quota_metadata_compat', args[0][2])
        self.assertLess(args[0][2].index('install()'), args[0][2].index('refresh_quota_cache()'))
        self.assertEqual(kwargs['timeout'], 60)
        self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
        self.assertIn('dashboard/quota_metadata_compat.py', (ROOT / 'install.sh').read_text())


class AnthropicCompatTests(unittest.TestCase):
    URL = 'https://api.anthropic.com/api/oauth/usage'

    def setUp(self):
        self.up = load('codex_upstream_fixture', ROOT / 'tests/fixtures/codex_upstream.py')
        self.core = load('anthropic_upstream_fixture', ROOT / 'tests/fixtures/anthropic_upstream.py')
        self.requests = []
        self.payload = {
            'five_hour': {'utilization': 1.0, 'resets_at': '2099-10-04T05:00:00.123456+00:00'},
            'seven_day': {'utilization': 20.0, 'resets_at': '2099-10-08T00:00:00Z'},
            'seven_day_opus': None, 'seven_day_sonnet': None,
            'extra_usage': {'is_enabled': True, 'used_credits': 242.0, 'monthly_limit': 200.0, 'currency': 'USD'},
            'limits': [
                {'kind': 'session', 'group': 'session', 'percent': 1, 'resets_at': '2099-10-04T05:00:00Z', 'scope': None},
                {'kind': 'weekly_all', 'group': 'weekly', 'percent': 20, 'resets_at': '2099-10-08T00:00:00Z', 'scope': None},
                {'kind': 'weekly_scoped', 'group': 'weekly', 'percent': 0, 'resets_at': '2099-10-08T00:00:00Z',
                 'scope': {'model': {'display_name': 'Fable', 'id': None}, 'surface': None}},
            ]}
        owner = self

        class Response:
            request = types.SimpleNamespace(url=self.URL)

            def __init__(self, url=None):
                if url is not None:
                    self.request = types.SimpleNamespace(url=url)

            def json(self):
                return owner.payload

            def raise_for_status(self):
                pass

        class Client:
            def __init__(self, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                pass

            def get(self, url, headers):
                owner.requests.append(url)
                return Response(url)

        self.httpx = types.SimpleNamespace(Response=Response, Client=Client)
        self.core.httpx = self.httpx
        self.core.resolve_anthropic_token = lambda: 'fixture-token'
        self.core._is_oauth_token = lambda token: True
        for name in ('QuotaWindow', 'QuotaResult', 'build_unavailable'):
            setattr(self.core, name, getattr(self.up, name))
        usage = types.ModuleType('agent.account_usage')
        usage.fetch_account_usage = lambda provider: self.core._fetch_anthropic_account_usage()
        self.modules = patch.dict(sys.modules, {'httpx': self.httpx, 'agent': types.ModuleType('agent'),
                                                'agent.account_usage': usage})
        self.modules.start()
        self.addCleanup(self.modules.stop)
        self.up.PROVIDER_FETCHERS = {'anthropic': self.core._fetch_anthropic}

    def install(self):
        self.compat = load('quota_metadata_compat_test', ROOT / 'dashboard/quota_metadata_compat.py')
        return self.compat.install(self.up, self.httpx)

    def fetch(self):
        return self.up.PROVIDER_FETCHERS['anthropic']()

    def test_upstream_fraction_scaling_is_reproduced_without_compat(self):
        self.assertEqual(self.fetch().windows[0].used_percent, 100.0)

    def test_percent_utilization_is_raw_with_metadata_and_scoped_weekly(self):
        self.assertTrue(self.install())
        result = self.fetch()
        self.assertEqual(len(self.requests), 1)
        self.assertEqual([(w.label, w.used_percent) for w in result.windows],
                         [('Current session', 1.0), ('Current week', 20.0), ('Fable week', 0.0)])
        self.assertEqual([(w.window_seconds, w.scope, w.window_id) for w in result.windows], [
            (18000, 'account', 'five_hour'), (604800, 'account', 'seven_day'),
            (604800, 'model:Fable', 'weekly_scoped:Fable')])
        self.assertEqual(result.windows[0].reset_at, '2099-10-04T05:00:00.123456+00:00')
        self.assertEqual(result.windows[2].reset_at, '2099-10-08T00:00:00+00:00')
        self.assertEqual(result.details, ['Extra usage: 242.00 / 200.00 USD'])
        record = self.up._result_to_record(result)
        self.assertEqual([(w['label'], w['used_percent'], w['window_seconds'], w['scope'], w['window_id'])
                          for w in record['windows']], [
            ('Current session', 1.0, 18000, 'account', 'five_hour'),
            ('Current week', 20.0, 604800, 'account', 'seven_day'),
            ('Fable week', 0.0, 604800, 'model:Fable', 'weekly_scoped:Fable')])

    def test_sub_one_percent_is_not_scaled(self):
        self.payload['five_hour']['utilization'] = 0.5
        self.install()
        self.assertEqual(self.fetch().windows[0].used_percent, 0.5)

    def test_model_windows_and_duplicate_scoped_limits(self):
        self.payload['seven_day_opus'] = {'utilization': 0.25, 'resets_at': '2099-10-08T00:00:00Z'}
        self.payload['seven_day_sonnet'] = {'utilization': 40, 'resets_at': None}
        self.payload['limits'] += [
            {'kind': 'weekly_scoped', 'group': 'weekly', 'percent': 0.25, 'resets_at': None,
             'scope': {'model': {'display_name': 'Opus', 'id': 'claude-opus'}}},
            {'kind': 'weekly_scoped', 'group': 'monthly', 'percent': 7, 'resets_at': 'not-a-date',
             'scope': {'model': {'display_name': None, 'id': 'model-x'}}},
            {'kind': 'weekly_scoped', 'group': 'weekly', 'percent': 'bad', 'scope': {'model': {'display_name': 'Bad'}}},
            {'kind': 'weekly_scoped', 'group': 'weekly', 'percent': 3, 'scope': {'model': {}}},
            'unexpected']
        self.install()
        result = self.fetch()
        self.assertEqual([(w.label, w.used_percent, w.window_seconds, w.scope, w.window_id) for w in result.windows], [
            ('Current session', 1.0, 18000, 'account', 'five_hour'),
            ('Current week', 20.0, 604800, 'account', 'seven_day'),
            ('Opus week', 0.25, 604800, 'opus', 'seven_day_opus'),
            ('Sonnet week', 40.0, 604800, 'sonnet', 'seven_day_sonnet'),
            ('Fable week', 0.0, 604800, 'model:Fable', 'weekly_scoped:Fable'),
            ('model-x week', 7.0, None, 'model:model-x', 'weekly_scoped:model-x')])
        self.assertIsNone(result.windows[5].reset_at)

    def test_missing_limits_keeps_core_windows_with_metadata(self):
        self.payload.pop('limits')
        self.install()
        result = self.fetch()
        self.assertEqual([(w.label, w.used_percent, w.window_id) for w in result.windows],
                         [('Current session', 1.0, 'five_hour'), ('Current week', 20.0, 'seven_day')])

    def test_unsupported_shapes_leave_official_result_unchanged(self):
        for mutation in ('limits', 'label', 'reset', 'count', 'unavailable', 'url'):
            with self.subTest(mutation=mutation):
                self.setUp()
                if mutation == 'limits':
                    self.payload['limits'] = {'kind': 'weekly_scoped'}
                original = self.core._snapshot_to_result
                def changed(snapshot):
                    result = original(snapshot)
                    if mutation == 'label':
                        result.windows[0].label = 'Renamed'
                    elif mutation == 'reset':
                        result.windows[1].reset_at = None
                    elif mutation == 'count':
                        result.windows.pop()
                    elif mutation == 'unavailable':
                        result.unavailable_reason = 'fetch-error'
                    return result
                self.core._snapshot_to_result = changed
                if mutation == 'url':
                    self.httpx.Client.get = lambda client, url, headers: self.httpx.Response('https://api.anthropic.com/api/other')
                self.install()
                result = self.fetch()
                self.assertFalse(any(hasattr(w, 'window_seconds') for w in result.windows))
                self.assertEqual(result.windows[0].used_percent, 100.0)

    def test_upstream_raw_percent_fix_still_aligns(self):
        original = self.core._usage_windows
        self.core._usage_windows = lambda *args, fraction=False: original(*args)
        self.install()
        result = self.fetch()
        self.assertEqual([(w.used_percent, w.window_id) for w in result.windows],
                         [(1.0, 'five_hour'), (20.0, 'seven_day'), (0.0, 'weekly_scoped:Fable')])

    def test_codex_and_anthropic_refresh_concurrently_without_extra_requests(self):
        import json
        import tempfile
        import threading
        self.up.PROVIDER_FETCHERS['other'] = lambda: self.up.QuotaResult('other', [self.up.QuotaWindow('Current week', 1.0, None)])
        self.up.REFRESH_BUDGET_S = 3
        self.up._CACHE_LOCK = threading.Lock()
        self.install()
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / 'quota_cache.json'
            self.up._cache_path = lambda: str(target)
            self.up.refresh_quota_cache()
            cache = json.loads(target.read_text())
        self.assertEqual([w['used_percent'] for w in cache['providers']['anthropic']['windows']], [1.0, 20.0, 0.0])
        self.assertEqual(cache['providers']['anthropic']['windows'][2]['scope'], 'model:Fable')
        self.assertNotIn('scope', cache['providers']['other']['windows'][0])
        self.assertEqual(self.requests, [self.URL])

    def test_install_handles_missing_fetchers_idempotently(self):
        self.assertTrue(self.install())
        fetch = self.up.PROVIDER_FETCHERS['anthropic']
        self.assertTrue(self.compat.install(self.up, self.httpx))
        self.assertIs(fetch, self.up.PROVIDER_FETCHERS['anthropic'])
        self.assertNotIn('openai-codex', self.up.PROVIDER_FETCHERS)
        empty = types.SimpleNamespace(PROVIDER_FETCHERS={}, _result_to_record=self.up._result_to_record)
        json_method = self.httpx.Response.json
        self.assertFalse(self.compat.install(empty, self.httpx))
        self.assertIs(json_method, self.httpx.Response.json)


class RefreshBootstrapTests(unittest.TestCase):
    def test_editable_core_sibling_import_in_actual_subprocess(self):
        import ast
        import os
        import subprocess
        import tempfile
        import textwrap

        source = (ROOT / 'dashboard/plugin_api.py').read_text()
        node = next(n for n in ast.parse(source).body
                    if isinstance(n, ast.FunctionDef) and n.name == '_refresh_official_cache')
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            core = base / 'editable-core'
            agent = core / 'agent'
            plugin = base / 'home/plugins/quota'
            dashboard = base / 'dashboard'
            for path in (agent, plugin, dashboard):
                path.mkdir(parents=True)
            (core / 'hermes_constants.py').write_text('')
            (core / 'hermes_yaml.py').write_text("VALUE = 'fixture-secret-not-for-stdout'\n")
            (agent / '__init__.py').write_text('')
            (agent / 'account_usage.py').write_text('from hermes_yaml import VALUE\n')
            marker = base / 'refreshed'
            (plugin / 'quota_cache.py').write_text(
                'from agent.account_usage import VALUE\n'
                'from pathlib import Path\n'
                'def refresh_quota_cache():\n'
                f'    Path({str(marker)!r}).write_text("refreshed")\n'
            )
            (dashboard / 'quota_metadata_compat.py').write_text('def install(): pass\n')
            namespace = {'Path': Path, 'os': os,
                         '__file__': str(dashboard / 'plugin_api.py'),
                         '_hermes_home': lambda: base / 'home'}
            exec(compile(ast.Module(body=[node], type_ignores=[]), '<refresh>', 'exec'), namespace)
            with patch.object(subprocess, 'run') as run:
                namespace['_refresh_official_cache']()
            args, kwargs = run.call_args
            self.assertEqual(kwargs['stdout'], subprocess.DEVNULL)
            self.assertEqual(kwargs['stderr'], subprocess.DEVNULL)
            # Reproduce stale editable metadata: only registered modules resolve;
            # newly added root siblings remain invisible until the core path is added.
            bootstrap = textwrap.dedent(f'''\
                import importlib.abc, importlib.util, sys
                from pathlib import Path
                core = Path({str(core)!r})
                class EditableFinder(importlib.abc.MetaPathFinder):
                    def find_spec(self, fullname, path=None, target=None):
                        if fullname == 'hermes_constants':
                            return importlib.util.spec_from_file_location(fullname, core / 'hermes_constants.py')
                        if fullname == 'agent':
                            return importlib.util.spec_from_file_location(fullname, core / 'agent/__init__.py')
                sys.meta_path.insert(0, EditableFinder())
                import hermes_constants
                assert importlib.util.find_spec('hermes_yaml') is None
                try:
                    import agent.account_usage
                except ModuleNotFoundError as error:
                    assert error.name == 'hermes_yaml'
                else:
                    raise AssertionError('fixture did not reproduce the missing sibling')
            ''')
            result = subprocess.run(
                [sys.executable, '-I', '-S', '-c', bootstrap + args[0][2]],
                cwd=base, env=kwargs['env'], capture_output=True, text=True, timeout=10,
            )
            self.assertEqual(result.stdout, '')
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stderr, '')
            self.assertEqual(marker.read_text(), 'refreshed')


if __name__ == '__main__':
    unittest.main()
