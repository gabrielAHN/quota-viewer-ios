"""Exercise reader transformations without credentials, network or cache writes."""
import ast
import copy
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_local_reader():
    script = (ROOT / "local-quotas.sh").read_text()
    source = script.split("<<'PY'\n", 1)[1].rsplit("\nPY", 1)[0]
    tree = ast.parse(source)
    # Omit only the executable provider sweep; execute the real reader helpers.
    tree.body = [node for node in tree.body if isinstance(
        node, (ast.Import, ast.ImportFrom, ast.FunctionDef))]
    namespace = {}
    exec(compile(tree, str(ROOT / "local-quotas.sh"), "exec"), namespace)
    return namespace


class LocalCodexWindowTests(unittest.TestCase):
    def setUp(self):
        self.reader = load_local_reader()

    def fetch(self, payload):
        with patch.dict(self.reader, {
            "_codex_creds": lambda: ("test-token", None),
            "_get": lambda *args, **kwargs: copy.deepcopy(payload),
        }):
            return self.reader["codex_provider"]()

    def test_weekly_only_primary_carries_explicit_period_and_identity(self):
        result = self.fetch({"plan_type": "prolite", "rate_limit": {
            "primary_window": {"used_percent": 42, "limit_window_seconds": 604800,
                               "reset_after_seconds": 60}}})
        window, = result["windows"]
        self.assertEqual(window["label"], "Weekly")
        self.assertEqual(window.get("window_seconds"), 604800)
        self.assertEqual(window.get("scope"), "account")
        self.assertEqual(window.get("window_id"), "primary")
        self.assertEqual(window["remaining_percent"], 58)
        self.assertIsNotNone(window["resets_at"])

    def test_duration_alone_determines_labels_not_plan_or_near_reset(self):
        for duration, expected in [(18000, "Session"), (604800, "Weekly"),
                                   (86400, "24h quota"), (259200, "3d quota"),
                                   (5400, "90m quota"), (45, "45s quota"),
                                   (None, "Primary quota"), (True, "Primary quota"),
                                   (0, "Primary quota"), (-1, "Primary quota")]:
            for plan in ("prolite", "pro", "unknown"):
                with self.subTest(duration=duration, plan=plan):
                    window, = self.fetch({"plan_type": plan, "rate_limit": {
                        "primary_window": {"used_percent": 5,
                                           "limit_window_seconds": duration,
                                           "reset_after_seconds": 60}}})["windows"]
                    self.assertEqual(window["label"], expected)
                    self.assertEqual(window.get("window_seconds"),
                                     duration if expected != "Primary quota" else None)

    def test_five_hour_and_weekly_keep_separate_window_ids(self):
        windows = self.fetch({"rate_limit": {
            "primary_window": {"used_percent": 100, "limit_window_seconds": 18000},
            "secondary_window": {"used_percent": 40, "limit_window_seconds": 604800},
        }})["windows"]
        self.assertEqual([(w["label"], w.get("window_id"), w.get("scope")) for w in windows],
                         [("Session", "primary", "account"), ("Weekly", "secondary", "account")])

    def test_missing_secondary_duration_near_reset_stays_neutral(self):
        window, = self.fetch({"rate_limit": {"secondary_window": {
            "used_percent": 99, "reset_after_seconds": 60}}})["windows"]
        self.assertEqual(window["label"], "Secondary quota")
        self.assertIsNone(window.get("window_seconds"))

    def test_additional_reserve_weekly_is_not_account_session(self):
        windows = self.fetch({"rate_limit": {
            "primary_window": {"used_percent": 20, "limit_window_seconds": 18000}},
            "additional_rate_limits": [{"limit_name": "GPT reserve", "metered_feature": "gpt-reserve",
                "rate_limit": {"primary_window": {"used_percent": 100, "limit_window_seconds": 604800},
                               "secondary_window": {"used_percent": 10, "limit_window_seconds": 18000}}},
                {"limit_name": "Other bucket", "rate_limit": {
                    "primary_window": {"used_percent": 5}}},
                {"rate_limit": {"primary_window": {"used_percent": 6}}}]
        })["windows"]
        self.assertEqual(len(windows), 5)
        self.assertEqual([(w["scope"], w["window_id"]) for w in windows],
                         [("account", "primary"), ("gpt-reserve", "primary"),
                          ("gpt-reserve", "secondary"), ("Other bucket", "primary"),
                          ("additional:2", "primary")])
        self.assertEqual(windows[1]["label"], "GPT reserve Weekly")
        self.assertEqual(windows[1]["window_seconds"], 604800)
        self.assertEqual(windows[2]["label"], "GPT reserve Session")
        self.assertEqual(windows[3]["label"], "Other bucket Primary quota")

    def test_malformed_additional_codex_buckets_preserve_valid_windows(self):
        valid_extra = {"metered_feature": "reserve", "rate_limit": {
            "primary_window": {"used_percent": 30, "limit_window_seconds": 604800}}}
        cases = [
            ([{"rate_limit": "unexpected"}, valid_extra], [("reserve", "primary", 70)]),
            (42, []),
            ("unexpected", []),
            ({"rate_limit": "unexpected"}, []),
            ([{"metered_feature": "partial", "rate_limit": {
                "primary_window": "unexpected", "secondary_window": {"used_percent": 40}}},
              valid_extra], [("partial", "secondary", 60), ("reserve", "primary", 70)]),
        ]
        for additional, expected in cases:
            with self.subTest(additional=additional):
                result = self.fetch({"plan_type": "pro", "rate_limit": {
                    "primary_window": {"used_percent": 20, "limit_window_seconds": 18000}},
                    "additional_rate_limits": additional})
                self.assertEqual(result["status"], "ok")
                self.assertEqual([(w["scope"], w["window_id"], w["remaining_percent"])
                                  for w in result["windows"]],
                                 [("account", "primary", 80)] + expected)

    def test_overflowing_optional_duration_preserves_account_and_usage(self):
        for duration in (10 ** 400, float("inf"), float("nan")):
            with self.subTest(duration=duration):
                result = self.fetch({"rate_limit": {"primary_window": {"used_percent": 20}},
                    "additional_rate_limits": [{"limit_name": "reserve", "rate_limit": {
                        "primary_window": {"used_percent": 30, "limit_window_seconds": duration}}}]})
                account, extra = result["windows"]
                self.assertEqual(account["remaining_percent"], 80)
                self.assertEqual(extra["remaining_percent"], 70)
                self.assertIsNone(extra["window_seconds"])
                self.assertEqual(extra["label"], "reserve Primary quota")

    def test_invalid_optional_codex_resets_preserve_usage_and_account(self):
        for field in ("reset_at", "reset_after_seconds"):
            for value in (True, False, float("inf"), float("-inf"), float("nan"),
                          100000000000000000000, 10 ** 400, 1e300):
                with self.subTest(field=field, value=value):
                    result = self.fetch({"rate_limit": {
                        "primary_window": {"used_percent": 20}},
                        "additional_rate_limits": [{"metered_feature": "reserve", "rate_limit": {
                            "primary_window": {"used_percent": 30, field: value}}}]})
                    self.assertEqual(result["status"], "ok")
                    account, extra = result["windows"]
                    self.assertEqual(account["remaining_percent"], 80)
                    self.assertEqual(extra["remaining_percent"], 70)
                    self.assertIsNone(extra["resets_at"])

    def test_invalid_optional_codex_usage_skips_only_invalid_window(self):
        for used in ("invalid", {}, [], float("inf"), float("-inf"), float("nan"),
                     "NaN", "Infinity", 10 ** 400):
            with self.subTest(used=used):
                result = self.fetch({"rate_limit": {
                    "primary_window": {"used_percent": 20}},
                    "additional_rate_limits": [{"metered_feature": "reserve", "rate_limit": {
                        "primary_window": {"used_percent": used},
                        "secondary_window": {"used_percent": 30}}}]})
                self.assertEqual(result["status"], "ok")
                self.assertEqual([(w["scope"], w["window_id"], w["remaining_percent"])
                                  for w in result["windows"]],
                                 [("account", "primary", 80), ("reserve", "secondary", 70)])

    def test_local_claude_percentages_and_labels_are_unchanged(self):
        with patch.dict(self.reader, {
            "_anthropic_token": lambda: "sk-ant-oat-test",
            "_get": lambda *args, **kwargs: {
                "five_hour": {"utilization": 1}, "seven_day": {"utilization": 20},
                "seven_day_opus": {"utilization": 30}, "seven_day_sonnet": {"utilization": 40}},
        }):
            windows = self.reader["anthropic_provider"]()["windows"]
        self.assertEqual([w["label"] for w in windows],
                         ["Current session", "Current week", "Opus week", "Sonnet week"])
        self.assertEqual([w["remaining_percent"] for w in windows], [99, 80, 70, 60])
        self.assertEqual([(w.get("scope"), w.get("window_seconds"), w.get("window_id"))
                          for w in windows], [
            ("account", 18000, "five_hour"), ("account", 604800, "seven_day"),
            ("opus", 604800, "seven_day_opus"), ("sonnet", 604800, "seven_day_sonnet")])

    def claude(self, payload):
        with patch.dict(self.reader, {
            "_anthropic_token": lambda: "sk-ant-oat-fixture",
            "_get": lambda *args, **kwargs: copy.deepcopy(payload),
        }):
            return self.reader["anthropic_provider"]()

    def test_local_claude_adds_model_scoped_weekly_limits(self):
        result = self.claude({
            "five_hour": {"utilization": 31, "resets_at": "2099-10-04T05:00:00Z"},
            "seven_day": {"utilization": 20, "resets_at": "2099-10-08T00:00:00Z"},
            "seven_day_opus": {"utilization": 5}, "seven_day_sonnet": None,
            "extra_usage": {"is_enabled": True, "used_credits": 242, "monthly_limit": 200, "currency": "USD"},
            "limits": [
                {"kind": "session", "group": "session", "percent": 31, "scope": None},
                {"kind": "weekly_all", "group": "weekly", "percent": 20, "scope": None},
                {"kind": "weekly_scoped", "group": "weekly", "percent": 0, "resets_at": "2099-10-08T00:00:00Z",
                 "scope": {"model": {"display_name": "Fable", "id": None}, "surface": None}},
                {"kind": "weekly_scoped", "group": "weekly", "percent": 5,
                 "scope": {"model": {"display_name": "Opus", "id": "claude-opus"}}},
                {"kind": "weekly_scoped", "group": "monthly", "percent": 7, "resets_at": "not-a-date",
                 "scope": {"model": {"display_name": None, "id": "model-x"}}},
                {"kind": "weekly_scoped", "group": "weekly", "percent": "bad", "scope": {"model": {"display_name": "Bad"}}},
                {"kind": "weekly_scoped", "percent": 3, "scope": {"model": {}}},
                "unexpected"],
        })
        self.assertEqual(result["status"], "ok")
        self.assertEqual([(w["label"], w["used_percent"], w.get("window_seconds"), w.get("scope"), w.get("window_id"))
                          for w in result["windows"]], [
            ("Current session", 31, 18000, "account", "five_hour"),
            ("Current week", 20, 604800, "account", "seven_day"),
            ("Opus week", 5, 604800, "opus", "seven_day_opus"),
            ("Fable week", 0, 604800, "model:Fable", "weekly_scoped:Fable"),
            ("model-x week", 7, None, "model:model-x", "weekly_scoped:model-x")])
        self.assertIsNotNone(result["windows"][3]["resets_at"])
        self.assertIsNone(result["windows"][4]["resets_at"])
        self.assertEqual(result["details"], ["Extra usage: 242.00 / 200.00 USD"])

    def test_local_claude_unsupported_limits_keep_account_windows(self):
        for limits in ({"kind": "weekly_scoped"}, "bad", None):
            with self.subTest(limits=limits):
                result = self.claude({"five_hour": {"utilization": 1}, "limits": limits})
                self.assertEqual([w["label"] for w in result["windows"]], ["Current session"])
                self.assertEqual(result["windows"][0]["used_percent"], 1)

    def test_local_openrouter_balances_and_key_cap_are_unchanged(self):
        def get(url, *args, **kwargs):
            return {"data": {"total_credits": 100, "total_usage": 25} if url.endswith("credits")
                    else {"limit": 20, "usage": 5}}
        with patch.dict(self.reader, {"_get": get, "UA": "test"}):
            result = self.reader["_openrouter_credits_provider"](
                "openrouter", "OpenRouter", "test-key", "credits_api", "rejected")
        credits, quota = result["windows"]
        self.assertEqual(credits["remaining_amount"], 75)
        self.assertEqual(credits["currency"], "USD")
        self.assertEqual(quota["label"], "API key quota")
        self.assertEqual(quota["remaining_percent"], 75)
        self.assertEqual([(w.get("scope"), w.get("window_id")) for w in result["windows"]],
                         [("account", "account_credits"), ("api-key", "api_key_limit")])
        self.assertTrue(all(w.get("window_seconds") is None for w in result["windows"]))

    def test_opencode_uses_the_same_credit_metadata_without_changing_overage_math(self):
        def get(url, *args, **kwargs):
            return {"data": {"total_credits": 20, "total_usage": 25} if url.endswith("credits")
                    else {"limit": 20, "usage": 25}}
        with patch.dict(self.reader, {"_get": get, "UA": "test", "_opencode_key": lambda: "test"}):
            result = self.reader["opencode_provider"]()
        credits, quota = result["windows"]
        self.assertEqual(result["provider"], "opencode")
        self.assertEqual(credits["remaining_amount"], 0)
        self.assertIsNone(credits["used_percent"])
        self.assertEqual(quota["used_percent"], 100)
        self.assertEqual(quota["remaining_percent"], 0)
        self.assertTrue(all(w["warning"] for w in result["windows"]))
        self.assertEqual([(w.get("scope"), w.get("window_id")) for w in result["windows"]],
                         [("account", "account_credits"), ("api-key", "api_key_limit")])


class LocalAntigravityWindowTests(unittest.TestCase):
    def test_bucket_names_and_family_scope_do_not_invent_periods(self):
        reader = load_local_reader()
        groups = [
            {"displayName": "Gemini", "buckets": [
                {"bucketId": "gemini-2.5", "remainingFraction": 0.75,
                 "resetTime": "2026-09-29T01:00:00Z"},
                {"id": "weekly-5", "displayName": "Daily allowance", "remaining_fraction": "0.25"},
                {"name": "Burst", "remaining": {"remainingFraction": 0}},
                {"window": "Monthly", "remaining": {"case": "remainingFraction", "value": "1"}},
                {"disabled": True, "remainingFraction": 0},
                {"remainingFraction": None}]},
            {"name": "Claude/GPT", "buckets": [
                {"bucketId": "week-5", "name": "Weekly", "remainingFraction": 0.5}]},
        ]
        for wrapper in (lambda g: {"groups": g}, lambda g: {"response": {"groups": g}},
                        lambda g: {"summary": {"groups": g}}):
            with self.subTest(wrapper=wrapper):
                payload = wrapper(copy.deepcopy(groups))
                original = copy.deepcopy(payload)
                windows = reader["_antigravity_windows"](payload)
                self.assertEqual([w["remaining_percent"] for w in windows], [75, 25, 0, 100, 50])
                self.assertEqual([w["warning"] for w in windows], [False, False, True, False, False])
                self.assertEqual([w["label"] for w in windows],
                                 ["Gemini quota", "Gemini daily allowance", "Gemini burst",
                                  "Gemini monthly", "Claude/GPT weekly"])
                self.assertEqual([w.get("scope") for w in windows],
                                 ["model-family:gemini"] * 4 + ["model-family:claude/gpt"])
                self.assertEqual([w.get("window_id") for w in windows],
                                 ["gemini-2.5", "weekly-5", "Burst", "Monthly", "week-5"])
                self.assertTrue(all(w.get("window_seconds") is None for w in windows))
                self.assertEqual(windows[0]["resets_at"], "2026-09-29T01:00:00Z")
                self.assertEqual(payload, original)


class OfficialCacheMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        path = ROOT / "dashboard" / "plugin_api.py"
        tree = ast.parse(path.read_text())
        # The adapter is pure; avoid importing optional FastAPI/server dependencies.
        tree.body = [node for node in tree.body if (
            isinstance(node, ast.FunctionDef) and node.name == "_adapt_record") or (
            isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
                t.id == "_AUTH_REASONS" for t in node.targets))]
        import re
        from typing import Any
        namespace = {"re": re, "Any": Any}
        exec(compile(tree, str(path), "exec"), namespace)
        cls.adapt = staticmethod(namespace["_adapt_record"])

    def test_provider_agnostic_metadata_passes_through_without_mutation(self):
        for provider in ("openai-codex", "anthropic", "openrouter", "custom-provider"):
            with self.subTest(provider=provider):
                record = {"plan": "test", "windows": [{
                    "label": "Reserve weekly", "used_percent": 92,
                    "reset_at": "2026-09-29T01:00:00+00:00", "window_seconds": 604800,
                    "scope": "reserve-model", "window_id": "primary"}]}
                original = copy.deepcopy(record)
                result = self.adapt(provider, "Provider", record, "test-timestamp")
                window, = result["windows"]
                for field in ("label", "window_seconds", "scope", "window_id"):
                    self.assertEqual(window.get(field), record["windows"][0][field])
                self.assertEqual(window["remaining_percent"], 8)
                self.assertTrue(window["warning"])
                self.assertEqual(window["resets_at"], record["windows"][0]["reset_at"])
                self.assertEqual(record, original)

    def test_legacy_records_without_metadata_remain_supported(self):
        for provider, label in (("anthropic", "Current week"), ("openai-codex", "Session"),
                                ("openrouter", "API key quota"), ("custom", "Tokens")):
            with self.subTest(provider=provider):
                window, = self.adapt(provider, provider, {"windows": [
                    {"label": label, "used_percent": 30}]}, None)["windows"]
                self.assertEqual(window["label"], label)
                self.assertEqual(window["remaining_percent"], 70)
                self.assertIsNone(window.get("window_seconds"))
                self.assertIsNone(window.get("scope"))
                self.assertIsNone(window.get("window_id"))

    def test_openrouter_credit_lift_and_quota_metadata_coexist(self):
        result = self.adapt("openrouter", "OpenRouter", {
            "details": ["Credits balance: $12.50"], "windows": [{
                "label": "API key quota", "used_percent": 25,
                "scope": "api-key", "window_id": "spend", "window_seconds": 86400}]}, None)
        credits, quota = result["windows"]
        self.assertEqual(credits["remaining_amount"], 12.5)
        self.assertEqual(credits["currency"], "USD")
        self.assertEqual(quota.get("scope"), "api-key")
        self.assertEqual(quota.get("window_seconds"), 86400)
        self.assertEqual(result["details"], [])


if __name__ == "__main__":
    unittest.main()
