"""Exercise reader transformations without credentials, network or cache writes."""
import ast
import copy
from datetime import datetime, timedelta, timezone
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

    def test_local_openrouter_shows_key_credits_left_and_account_balance(self):
        def get(url, *args, **kwargs):
            return {"data": {"total_credits": 100, "total_usage": 25} if url.endswith("credits")
                    else {"limit": 20, "limit_remaining": 15, "limit_reset": "monthly", "usage": 50,
                          "usage_daily": 1.5, "usage_weekly": 3, "usage_monthly": 5}}
        with patch.dict(self.reader, {"_get": get, "UA": "test"}):
            result = self.reader["_openrouter_credits_provider"](
                "openrouter", "OpenRouter", "test-key", "credits_api", "rejected")
        key, credits = result["windows"]
        self.assertEqual(key["label"], "Key credits")
        self.assertEqual(key["remaining_amount"], 15)
        self.assertEqual(key["remaining_percent"], 75)
        self.assertEqual(key["detail"], "$15.00 of $20.00 key limit")
        reset = datetime.fromisoformat(key["resets_at"])
        self.assertEqual((reset.day, reset.hour, reset.minute, reset.utcoffset()), (1, 0, 0, timedelta(0)))
        self.assertGreater(reset, datetime.now(timezone.utc))
        self.assertEqual(credits["remaining_amount"], 75)
        self.assertEqual(credits["currency"], "USD")
        self.assertEqual((credits["used_percent"], credits["remaining_percent"]), (25, 75))
        self.assertEqual(credits["detail"], "$75.00 of $100.00 purchased")
        self.assertEqual(result["details"], ["Spent today $1.50 · week $3.00 · month $5.00"])
        self.assertEqual([(w.get("scope"), w.get("window_id")) for w in result["windows"]],
                         [("api-key", "api_key_limit"), ("account", "account_credits")])
        self.assertTrue(all(w.get("window_seconds") is None for w in result["windows"]))

    def test_local_openrouter_legacy_key_without_remaining_uses_lifetime_cap(self):
        def get(url, *args, **kwargs):
            return {"data": {"total_credits": 100, "total_usage": 25} if url.endswith("credits")
                    else {"limit": 20, "usage": 5, "limit_reset": None}}
        with patch.dict(self.reader, {"_get": get, "UA": "test"}):
            key, _ = self.reader["_openrouter_credits_provider"](
                "openrouter", "OpenRouter", "test-key", "credits_api", "rejected")["windows"]
        self.assertEqual(key["remaining_amount"], 15)
        self.assertIsNone(key["resets_at"])

    def test_local_openrouter_tighter_account_balance_leads(self):
        def get(url, *args, **kwargs):
            return {"data": {"total_credits": 10, "total_usage": 8} if url.endswith("credits")
                    else {"limit": 20, "limit_remaining": 15, "limit_reset": "daily"}}
        with patch.dict(self.reader, {"_get": get, "UA": "test"}):
            result = self.reader["_openrouter_credits_provider"](
                "openrouter", "OpenRouter", "test-key", "credits_api", "rejected")
        self.assertEqual([w["label"] for w in result["windows"]], ["Account credits", "Key credits"])

    def test_opencode_uses_the_same_credit_metadata_without_changing_overage_math(self):
        def get(url, *args, **kwargs):
            return {"data": {"total_credits": 20, "total_usage": 25} if url.endswith("credits")
                    else {"limit": 20, "limit_remaining": -5, "usage": 25}}
        with patch.dict(self.reader, {"_get": get, "UA": "test", "_opencode_key": lambda: "test"}):
            result = self.reader["opencode_provider"]()
        quota, credits = result["windows"]
        self.assertEqual(result["provider"], "opencode")
        self.assertEqual(credits["remaining_amount"], 0)
        self.assertEqual(credits["used_percent"], 100)
        self.assertEqual(quota["used_percent"], 100)
        self.assertEqual(quota["remaining_percent"], 0)
        self.assertEqual(quota["remaining_amount"], 0)
        self.assertTrue(all(w["warning"] for w in result["windows"]))
        self.assertEqual([(w.get("scope"), w.get("window_id")) for w in result["windows"]],
                         [("api-key", "api_key_limit"), ("account", "account_credits")])


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


PLUGIN_NAMES = {"_adapt_record", "_openrouter_view", "_next_utc_reset", "_AUTH_REASONS", "_OR_KEY", "_OR_USD",
                "_OR_NOISE", "_OR_SPEND", "_KNOWN_LABELS", "_DEFAULT_PROVIDERS", "_label_for",
                "_hermes_providers", "_configured_providers", "_read_quota_cache", "_provider"}


def load_plugin(**stubs):
    path = ROOT / "dashboard" / "plugin_api.py"
    tree = ast.parse(path.read_text())
    # The adapter is pure; avoid importing optional FastAPI/server dependencies.
    tree.body = [node for node in tree.body if (
        isinstance(node, ast.FunctionDef) and node.name in PLUGIN_NAMES) or (
        isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and
            t.id in PLUGIN_NAMES for t in node.targets))]
    import os
    import re
    from typing import Any
    namespace = {"os": os, "re": re, "Any": Any, "datetime": datetime, "timedelta": timedelta,
                 "timezone": timezone, **stubs}
    exec(compile(tree, str(path), "exec"), namespace)
    return namespace


OPENROUTER_RECORD = {
    "label": "openrouter", "plan": None, "unavailable_reason": None,
    "details": [
        "Locally configured credentials: 1 (deduplicated); not an inventory of all account keys",
        "Management API key listing not queried; credentials from Hermes native auth and saved pool only",
        "Account wallet via Key 1 (native): USD 4.01 remaining (shared account credits, shown once; not summed across keys)",
        "Account membership of other keys is unverified; wallet is not a total across accounts",
        "Key 1 (native): key cap USD 40.00; remaining USD 3.61; resets monthly",
        "Key 1 (native): usage total USD 55.5056",
        "Key 1 (native): usage today UTC USD 9.2485",
        "Key 1 (native): usage this week UTC USD 35.6715",
        "Key 1 (native): usage this month UTC USD 36.3936",
    ],
    "windows": [{"label": "Key 1 (native) quota", "reset_at": None, "used_percent": 90.98404401}],
}


class OfficialCacheMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.adapt = staticmethod(load_plugin()["_adapt_record"])

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

    def test_openrouter_plugin_record_becomes_key_credits_left_and_account_balance(self):
        record = copy.deepcopy(OPENROUTER_RECORD)
        result = self.adapt("openrouter", "OpenRouter", record, None)
        self.assertEqual(record, OPENROUTER_RECORD)
        self.assertEqual(result["status"], "ok")
        key, account = result["windows"]
        self.assertEqual((key["label"], key["remaining_amount"], key["currency"]), ("Key credits", 3.61, "USD"))
        self.assertAlmostEqual(key["remaining_percent"], 9.025, places=3)
        self.assertEqual(key["detail"], "$3.61 of $40.00 key limit")
        self.assertEqual((key["scope"], key["window_id"]), ("api-key", "api_key_limit"))
        self.assertTrue(key["warning"])
        reset = datetime.fromisoformat(key["resets_at"])
        self.assertEqual((reset.day, reset.hour, reset.utcoffset()), (1, 0, timedelta(0)))
        self.assertGreater(reset, datetime.now(timezone.utc))
        self.assertEqual((account["label"], account["remaining_amount"], account["used_percent"]),
                         ("Account credits", 4.01, None))
        self.assertEqual(account["detail"], "Account balance shared by all keys")
        self.assertEqual((account["scope"], account["window_id"]), ("account", "account_credits"))
        self.assertEqual(result["details"], ["Spent today $9.25 · week $35.67 · month $36.39"])

    def test_openrouter_purchased_total_gives_the_wallet_a_real_bar(self):
        record = copy.deepcopy(OPENROUTER_RECORD)
        record["details"].insert(3, "Account credits purchased: USD 80.00")
        result = self.adapt("openrouter", "OpenRouter", record, None)
        key, account = result["windows"]
        self.assertEqual(account["remaining_amount"], 4.01)
        self.assertAlmostEqual(account["remaining_percent"], 5.0125)
        self.assertAlmostEqual(account["used_percent"], 94.9875)
        self.assertEqual(account["detail"], "$4.01 of $80.00 purchased")
        self.assertTrue(account["warning"])
        self.assertNotIn("Account credits purchased: USD 80.00", result["details"])
        record["details"][3] = "Account credits purchased: USD 0.00"
        account = self.adapt("openrouter", "OpenRouter", record, None)["windows"][1]
        self.assertIsNone(account["used_percent"])

    def test_openrouter_tighter_wallet_leads_and_exhausted_cap_reads_zero(self):
        record = copy.deepcopy(OPENROUTER_RECORD)
        record["details"][2] = "Account wallet via Key 1 (native): USD 1.50 remaining (shared account credits)"
        result = self.adapt("openrouter", "OpenRouter", record, None)
        self.assertEqual([w["label"] for w in result["windows"]], ["Account credits", "Key credits"])
        record["details"][4] = ("Key 1 (native): key cap USD 40.00; remaining USD -0.20 "
                                "(key cap exhausted; account wallet is separate); resets weekly")
        key = next(w for w in self.adapt("openrouter", "OpenRouter", record, None)["windows"] if w["label"] == "Key credits")
        self.assertEqual((key["remaining_amount"], key["remaining_percent"]), (0.0, 0.0))
        self.assertEqual(datetime.fromisoformat(key["resets_at"]).weekday(), 0)

    def test_openrouter_uncapped_and_multiple_keys(self):
        record = copy.deepcopy(OPENROUTER_RECORD)
        record["details"][4] = "Key 1 (native): no key-specific cap (account wallet still applies)"
        result = self.adapt("openrouter", "OpenRouter", dict(record, windows=[]), None)
        account, = result["windows"]
        self.assertEqual(account["detail"], "Account balance · key has no spending limit")
        record = copy.deepcopy(OPENROUTER_RECORD)
        record["details"] += ["Key 2 (saved): key cap USD 10.00; remaining USD 7.50; resets daily",
                              "Key 2 (saved): unavailable (http-401)"]
        result = self.adapt("openrouter", "OpenRouter", record, None)
        self.assertEqual([(w["label"], w["window_id"]) for w in result["windows"]],
                         [("Key 1 credits", "api_key_limit:1"), ("Key 2 credits", "api_key_limit:2"),
                          ("Account credits", "account_credits")])
        self.assertEqual(result["details"], ["Key 1 spent today $9.25 · week $35.67 · month $36.39",
                                             "Key 2 (saved): unavailable (http-401)"])

    def test_openrouter_unrecognised_details_are_left_alone(self):
        record = {"details": ["Something new"], "windows": [{"label": "Key 1 (native) quota", "used_percent": 50}]}
        result = self.adapt("openrouter", "OpenRouter", record, None)
        self.assertEqual([w["label"] for w in result["windows"]], ["Key 1 (native) quota"])
        self.assertEqual(result["details"], ["Something new"])


class HermesProviderSetTests(unittest.TestCase):
    def providers(self, cache, last_good=(), env=""):
        plugin = load_plugin(_fresh_last_good=lambda slug: {} if slug in last_good else None,
                             _read_official_cache=lambda: cache,
                             _read_accounts_cache=lambda: {"fetched_at": None, "hide": [], "providers": {}})
        with patch.dict("os.environ", {"PROVIDER_QUOTA_PROVIDERS": env}):
            return plugin["_configured_providers"](cache), plugin["_configured_providers"]()

    def test_providers_follow_the_gateways_hermes_credentials(self):
        cache = {"providers": {
            "anthropic": {"unavailable_reason": None}, "copilot": {"unavailable_reason": None},
            "cursor": {"unavailable_reason": "no-credentials"}, "grok": {"unavailable_reason": "opt-in-disabled"},
            "nous": {"unavailable_reason": "not-logged-in"}, "openai-codex": {"unavailable_reason": None},
            "openrouter": {"unavailable_reason": "fetch-error"}, "future-ai": {"unavailable_reason": None}}}
        explicit, implicit = self.providers(cache)
        self.assertEqual(explicit, implicit)
        self.assertEqual(explicit, (("openrouter", "OpenRouter"), ("anthropic", "Claude"), ("openai-codex", "Codex"),
                                    ("copilot", "Copilot"), ("future-ai", "Future Ai")))

    def test_recent_good_reading_keeps_a_provider_through_a_credential_blip(self):
        cache = {"providers": {"anthropic": {"unavailable_reason": "no-credentials"},
                               "copilot": {"unavailable_reason": "no-credentials"}}}
        self.assertEqual(self.providers(cache, last_good={"anthropic"})[0], (("anthropic", "Claude"),))

    def test_missing_cache_falls_back_and_env_overrides(self):
        default = (("openrouter", "OpenRouter"), ("anthropic", "Claude"), ("openai-codex", "Codex"))
        self.assertEqual(self.providers({"fetched_at": None, "providers": {}})[0], default)
        cache = {"providers": {"copilot": {"unavailable_reason": None}}}
        self.assertEqual(self.providers(cache, env="anthropic=Claude Max, zai")[0],
                         (("anthropic", "Claude Max"), ("zai", "Z.ai")))


if __name__ == "__main__":
    unittest.main()
