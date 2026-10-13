import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
sys.path.insert(0, os.path.join(ROOT, "src"))

from micro_cc.models import catalog, catalog_sync, providers  # noqa: E402

PACKAGED = catalog.PACKAGED_PATH
NEW = {"ids": {"anthropic": "claude-x"}, "context_window": 200_000, "max_output": 8_000}
ENV_KEYS = providers.routing_env_keys()


def read(path):
    with open(path) as f:
        return f.read()


class CatalogCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        p = mock.patch.dict(os.environ, {"HOME": self.home})
        p.start()
        self.addCleanup(p.stop)

    def write(self, path, doc):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(doc if isinstance(doc, str) else json.dumps(doc))

    def remote(self, doc):
        self.write(catalog.remote_cache_path(), doc)

    def user(self, doc):
        self.write(catalog.user_catalog_path(), doc)

    def legacy(self, doc):
        self.write(catalog.legacy_user_path(), doc)

    def build(self):
        return catalog.build_catalog()


class TestMerge(CatalogCase):
    def test_builtin_only(self):
        c = self.build()
        self.assertEqual(c["options"], catalog._BUILTIN_OPTIONS)
        self.assertEqual(c["default"], catalog._BUILTIN_DEFAULT)
        self.assertEqual(c["status"]["skipped"], [])

    def test_order_user_beats_legacy_beats_remote_beats_builtin(self):
        self.remote({"schema": 2, "models": {"sonnet-5": {"context_window": 500_000}, "opus-5": {"max_output": 1000}}})
        self.legacy({"models": {"sonnet-5": {"context_window": 400_000}, "opus-4.8": {"max_output": 2000}}})
        self.user({"models": {"sonnet-5": {"context_window": 300_000}}})
        models = self.build()["models"]
        self.assertEqual(models["sonnet-5"]["context_window"], 300_000)
        self.assertEqual(models["opus-5"]["max_output"], 1000)
        self.assertEqual(models["opus-4.8"]["max_output"], 2000)
        self.assertEqual(models["opus-4.7"]["max_output"], 128_000)

    def test_nested_fields_merge_one_level(self):
        self.user({"models": {"sonnet-5": {"ids": {"openrouter": "anthropic/claude-sonnet-5"}, "reasoning": {"default": "low"}}}})
        e = self.build()["models"]["sonnet-5"]
        self.assertEqual(e["ids"]["anthropic"], {"id": "claude-sonnet-5"})
        self.assertEqual(e["ids"]["openrouter"], {"id": "anthropic/claude-sonnet-5"})
        self.assertEqual(e["reasoning"]["default"], "low")
        self.assertIn("xhigh", e["reasoning"]["levels"])

    def test_new_alias_via_user_and_remote_appended_in_order(self):
        self.remote({"schema": 2, "models": {"r-new": NEW}})
        self.user({"models": {"u-new": NEW}})
        c = self.build()
        self.assertEqual(c["options"][-2:], ["r-new", "u-new"])
        self.assertEqual(c["options"][:len(catalog._BUILTIN_OPTIONS)], catalog._BUILTIN_OPTIONS)
        self.assertIsNone(c["models"]["u-new"]["reasoning"])

    def test_new_alias_needs_full_entry(self):
        self.user({"models": {"partial": {"ids": {"anthropic": "claude-x"}}}})
        c = self.build()
        self.assertNotIn("partial", c["models"])
        self.assertTrue(any("partial" in s for s in c["status"]["skipped"]))

    def test_builtin_not_mutated(self):
        before = json.dumps(catalog._BUILTIN_MODELS, sort_keys=True)
        self.user({"models": {"sonnet-5": {"context_window": 1, "ids": {"x": "y"}}}})
        self.build()
        self.assertEqual(json.dumps(catalog._BUILTIN_MODELS, sort_keys=True), before)

    def test_default_model(self):
        self.remote({"schema": 2, "default_model": "opus-5", "models": {}})
        self.assertEqual(self.build()["default"], "opus-5")
        self.user({"default_model": "missing-alias"})
        self.assertEqual(self.build()["default"], "opus-5")
        self.user({"default_model": "sonnet-4.6"})
        self.assertEqual(self.build()["default"], "sonnet-4.6")

    def test_user_adds_openai_compatible_provider(self):
        self.user({
            "providers": {"groq": {"name": "Groq", "api": "openai-chat", "requires": ["GROQ_API_KEY"],
                                   "base_url": "https://api.groq.com/openai/v1", "api_key_env": "GROQ_API_KEY"}},
            "routing": ["groq"],
            "models": {"kimi": {"ids": {"groq": "moonshotai/kimi-k2"}, "context_window": 131_072, "max_output": 16_384}},
        })
        c = self.build()
        self.assertIn("groq", c["providers"])
        self.assertEqual(c["routing"][0], "groq")
        self.assertEqual(set(c["routing"]), set(c["providers"]))  # unlisted providers still route, last

    def test_provider_needs_name_and_known_api(self):
        self.user({"providers": {"bad": {"name": "Bad", "api": "carrier-pigeon"}, "anthropic": {"first_event_timeout": 30}}})
        c = self.build()
        self.assertNotIn("bad", c["providers"])
        self.assertEqual(c["providers"]["anthropic"]["first_event_timeout"], 30)
        self.assertEqual(c["providers"]["anthropic"]["api"], "anthropic-messages")


class TestLegacyLayer(CatalogCase):
    def test_v1_user_file_translates(self):
        self.legacy({"default_model": "mine", "models": {
            "mine": {"litellm": "openai.x", "openai": "x", "responses_api": True, "thinking": True,
                     "summarized_display": False, "context_window": 400_000, "max_output": 64_000,
                     "effort_levels": ["low", "high"], "default_effort": "high"},
            "plain": {"anthropic": "claude-p", "thinking": False, "summarized_display": False,
                      "context_window": 200_000, "max_output": 8_000},
        }})
        c = self.build()
        m = c["models"]["mine"]
        self.assertEqual(m["ids"]["litellm"], {"id": "openai.x", "api": "openai-responses"})
        self.assertEqual(m["reasoning"], {"levels": ["low", "high"], "default": "high", "summarized": False})
        self.assertIsNone(c["models"]["plain"]["reasoning"])
        self.assertEqual(c["default"], "mine")

    def test_v1_partial_override(self):
        self.legacy({"models": {"opus-4.6": {"default_effort": "low"}}})
        r = self.build()["models"]["opus-4.6"]["reasoning"]
        self.assertEqual(r["default"], "low")
        self.assertEqual(r["levels"], ["low", "medium", "high", "max"])


class TestValidation(CatalogCase):
    def test_invalid_entries_skipped_not_file(self):
        self.user({"models": {
            "bad-reasoning": {**NEW, "reasoning": {"levels": "high"}},
            "bad-int": {**NEW, "context_window": -5},
            "bad-bool-int": {**NEW, "max_output": True},
            "empty-id": {**NEW, "ids": {"anthropic": ""}},
            "bad-api": {**NEW, "ids": {"x": {"id": "y", "api": "smoke-signals"}}},
            "good": NEW,
        }})
        c = self.build()
        self.assertIn("good", c["models"])
        for alias in ("bad-reasoning", "bad-int", "bad-bool-int", "empty-id", "bad-api"):
            self.assertNotIn(alias, c["models"])
        self.assertEqual(len(c["status"]["skipped"]), 5)

    def test_min_version_skip(self):
        with mock.patch.object(catalog, "_running_version", return_value=(0, 2, 102)):
            self.remote({"schema": 2, "models": {"future": {**NEW, "min_version": "9.0.0"}, "ok": {**NEW, "min_version": "0.2.100"}}})
            c = self.build()
        self.assertNotIn("future", c["models"])
        self.assertIn("ok", c["models"])
        self.assertTrue(any("future" in s for s in c["status"]["skipped"]))

    def test_bad_min_version_skipped(self):
        self.user({"models": {"m": {**NEW, "min_version": "banana"}}})
        self.assertNotIn("m", self.build()["models"])

    def test_corrupt_files_ignored(self):
        self.remote("{not json")
        self.user("[1, 2]")
        self.legacy("nope")
        c = self.build()
        self.assertEqual(c["options"], catalog._BUILTIN_OPTIONS)
        self.assertEqual(c["default"], catalog._BUILTIN_DEFAULT)
        for layer in ("remote", "user", "legacy"):
            self.assertTrue(c["status"][layer].startswith("unreadable"), layer)

    def test_models_not_a_dict(self):
        self.user({"models": [1, 2]})
        self.assertEqual(self.build()["options"], catalog._BUILTIN_OPTIONS)


class TestImport(unittest.TestCase):
    def test_import_does_no_network(self):
        import importlib
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network at import")):
            importlib.reload(catalog)
        self.assertTrue(catalog.MODELS)


class FakeResp:
    def __init__(self, body, etag=None):
        self.body, self.headers = body, ({"ETag": etag} if etag else {})

    def read(self, n=-1):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestRefresh(CatalogCase):
    DOC = {"schema": 2, "models": {"fetched": NEW}}

    def fetch(self, **kw):
        return catalog_sync.refresh_remote_catalog(**kw)

    def test_fetches_catalog_json(self):
        self.assertTrue(catalog_sync.CATALOG_URL.endswith("/src/micro_cc/models/catalog.json"))

    def test_writes_cache_with_etag_and_catalog_reads_it(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(json.dumps(self.DOC).encode(), '"abc"')) as m:
            self.assertEqual(self.fetch(), "updated")
        self.assertEqual(m.call_args.kwargs["timeout"], 2)
        self.assertIn("fetched", self.build()["models"])
        self.assertEqual(catalog_sync._cached_etag(catalog.remote_cache_path()), '"abc"')
        self.assertEqual([f for f in os.listdir(os.path.dirname(catalog.remote_cache_path())) if f.endswith(".tmp")], [])

    def test_throttle_skips_fresh_cache(self):
        self.remote(self.DOC)
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("hit network")):
            self.assertEqual(self.fetch(), "fresh")

    def test_stale_cache_refetches(self):
        self.remote(self.DOC)
        old = time.time() - catalog_sync.MIN_REFRESH_SECONDS - 10
        os.utime(catalog.remote_cache_path(), (old, old))
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(json.dumps(self.DOC).encode())):
            self.assertEqual(self.fetch(), "updated")

    def test_304_keeps_cache_and_refreshes_mtime(self):
        self.remote({**self.DOC, "_etag": '"abc"'})
        path = catalog.remote_cache_path()
        old = time.time() - catalog_sync.MIN_REFRESH_SECONDS - 10
        os.utime(path, (old, old))
        before = read(path)

        def urlopen(req, timeout):
            self.assertEqual(req.get_header("If-none-match"), '"abc"')
            raise urllib.error.HTTPError(req.full_url, 304, "Not Modified", {}, None)

        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            self.assertEqual(self.fetch(), "not-modified")
        self.assertEqual(read(path), before)
        self.assertGreater(os.path.getmtime(path), old + 100)

    def test_failures_degrade_silently(self):
        for exc in (urllib.error.HTTPError("u", 404, "nf", {}, None), TimeoutError(), OSError("down")):
            with mock.patch("urllib.request.urlopen", side_effect=exc):
                self.assertIsInstance(self.fetch(), str)
        self.assertFalse(os.path.exists(catalog.remote_cache_path()))

    def test_invalid_payload_not_written(self):
        for body in (b"[]", json.dumps({"schema": 1, "models": {}}).encode(), json.dumps({"schema": 2, "models": []}).encode()):
            with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
                self.assertEqual(self.fetch(), "invalid")
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(b"<html>")):
            self.assertTrue(self.fetch().startswith("error"))
        self.assertFalse(os.path.exists(catalog.remote_cache_path()))

    def test_atomic_write_failure_keeps_old_cache(self):
        self.remote(self.DOC)
        path = catalog.remote_cache_path()
        before = read(path)
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                catalog_sync._write_atomic(path, {"schema": 2, "models": {}})
        self.assertEqual(read(path), before)
        self.assertEqual([f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")], [])


class TestConcurrentRefresh(CatalogCase):
    def test_readers_never_see_partial_cache_while_writers_race(self):
        import threading
        path = catalog.remote_cache_path()
        docs = [{"schema": 2, "models": {f"m{i}": NEW}, "pad": "x" * 200_000} for i in range(3)]
        stop, bad = threading.Event(), []

        def reader():
            while not stop.is_set():
                try:
                    with open(path) as f:
                        json.load(f)
                except FileNotFoundError:
                    pass
                except Exception as e:
                    bad.append(e)

        def writer(doc):
            for _ in range(30):
                catalog_sync._write_atomic(path, doc)

        r = threading.Thread(target=reader)
        ws = [threading.Thread(target=writer, args=(d,)) for d in docs]
        r.start()
        [w.start() for w in ws]
        [w.join() for w in ws]
        stop.set()
        r.join()
        self.assertEqual(bad, [])
        self.assertEqual([f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")], [])


class TestShippedFiles(unittest.TestCase):
    def setUp(self):
        with open(PACKAGED) as f:
            self.doc = json.load(f)

    def test_catalog_valid(self):
        self.assertEqual(self.doc["schema"], catalog.SCHEMA)
        entries, default, skipped = catalog.validate_catalog(self.doc)
        self.assertEqual(skipped, [])
        self.assertEqual(entries, catalog._BUILTIN_MODELS)
        self.assertIn(default, entries)
        for alias, e in self.doc["models"].items():
            self.assertTrue(e.get("name"), alias)
            self.assertTrue(set(e["ids"]) <= set(self.doc["providers"]), alias)

    def test_every_provider_routes_and_can_log_in(self):
        for pid, p in catalog.PROVIDERS.items():
            self.assertIn(p["api"], catalog.APIS, pid)
            self.assertTrue(p.get("requires"), pid)
            self.assertIn(pid, catalog.ROUTING)
            if p.get("auth") != "oauth":
                self.assertTrue(set(p["requires"]) <= set(p["login"]), pid)

    def test_resolve_effort_clamps_to_model(self):
        self.assertEqual(catalog.resolve_effort("sonnet-5", "xhigh"), "xhigh")
        self.assertEqual(catalog.resolve_effort("opus-4.6", "xhigh"), "high")
        self.assertEqual(catalog.resolve_effort("gpt-6-sol", "minimal"), "minimal")
        self.assertIsNone(catalog.resolve_effort("haiku-5.5", "high"))
        self.assertIsNone(catalog.resolve_effort("unknown-model", "high"))

    def test_resolve_and_models_for(self):
        self.assertEqual(catalog.resolve("gpt-6-sol", "litellm"), ("openai.gpt-6-sol", "openai-responses"))
        self.assertEqual(catalog.resolve("sonnet-5", "litellm"), ("bedrock.anthropic.claude-sonnet-5", "openai-chat"))
        self.assertEqual(catalog.resolve("sonnet-5", "claude-code"), ("claude-sonnet-5", "anthropic-messages"))
        self.assertEqual(catalog.resolve("typo-model", "anthropic"), ("typo-model", "anthropic-messages"))
        self.assertEqual(catalog.models_for("ollama", "qwen3"), ["qwen3"])
        self.assertEqual(catalog.models_for("claude-code", None), catalog.models_for("anthropic", None))
        self.assertNotIn("gpt-6-sol", catalog.models_for("anthropic", None))
        self.assertIn("gpt-6-sol", catalog.models_for("litellm", None))

    def test_login_sequence(self):
        self.assertEqual(catalog.login_sequence("foundry"), ["FOUNDRY_BASE_URL", "FOUNDRY_API_KEY", "SERPAPI_KEY"])
        self.assertEqual(catalog.login_sequence("claude-code"), [])
        self.assertTrue(catalog.login_prompt("OLLAMA_NUM_CTX").startswith("Context window"))


class TestRouting(unittest.TestCase):
    def env(self, **values):
        p = mock.patch.dict(os.environ, {k: "" for k in ENV_KEYS})
        p.start()
        self.addCleanup(p.stop)
        os.environ.update(values)

    def test_legacy_precedence(self):
        self.env(ANTHROPIC_API_KEY="k", OLLAMA_BASE_URL="http://x")
        self.assertEqual(providers.active_provider(), "ollama")
        self.env(ANTHROPIC_API_KEY="k", ANTHROPIC_OAUTH_TOKEN="t")
        self.assertEqual(providers.active_provider(), "claude-code")
        self.env(OPENAI_API_KEY="k", OPENROUTER_API_KEY="k")
        self.assertEqual(providers.active_provider(), "openrouter")
        self.env(FOUNDRY_BASE_URL="u")  # half-configured
        self.assertEqual(providers.active_provider(), "anthropic")
        self.assertFalse(providers.any_configured())

    def test_pinned_provider_wins_only_when_configured(self):
        self.env(OLLAMA_BASE_URL="http://x", LITELLM_BASE_URL="u", LITELLM_API_KEY="k", MICROCC_PROVIDER="litellm")
        self.assertEqual(providers.active_provider(), "litellm")
        self.env(OLLAMA_BASE_URL="http://x", MICROCC_PROVIDER="litellm")
        self.assertEqual(providers.active_provider(), "ollama")

    def test_credentials_and_defaults(self):
        self.env(OLLAMA_BASE_URL="", OLLAMA_MODEL="qwen3:14b")
        self.assertEqual(providers.credentials("ollama"), ("http://localhost:11434/v1", "ollama"))
        self.assertEqual(providers.default_model("ollama"), "qwen3:14b")
        self.assertEqual(providers.default_model("anthropic"), catalog.DEFAULT_MODEL)

    def test_routing_env_keys(self):
        keys = providers.routing_env_keys()
        self.assertIn("MICROCC_PROVIDER", keys)
        self.assertIn("ANTHROPIC_OAUTH_TOKEN", keys)
        self.assertNotIn("SERPAPI_KEY", keys)

    def test_apply_login_pins_provider_and_clears_others(self):
        from micro_cc.utils import helpers
        home = tempfile.mkdtemp()
        os.makedirs(os.path.join(home, ".micro-cc"))
        self.env(HOME=home, ANTHROPIC_API_KEY="old", SERPAPI_KEY="s")
        helpers.apply_login({"LITELLM_BASE_URL": "u", "LITELLM_API_KEY": "k"}, "litellm")
        self.assertEqual(os.environ.get("ANTHROPIC_API_KEY"), None)
        self.assertEqual(os.environ["SERPAPI_KEY"], "s")
        self.assertEqual(providers.active_provider(), "litellm")
        self.assertIn("MICROCC_PROVIDER=litellm", read(os.path.join(home, ".micro-cc", ".env")))

    def test_budgets(self):
        self.env(OLLAMA_NUM_CTX="16384", OLLAMA_MAX_OUTPUT="2048", MICRO_CC_TRIM_BUDGET="")
        self.assertEqual(catalog.effective_trim_budget("x", "ollama"), 16384 - 2048 - catalog.OLLAMA_PROMPT_RESERVE)
        self.assertEqual(catalog.effective_trim_budget("unknown/slug", "openrouter"), 1_000_000)
        self.assertEqual(catalog.effective_trim_budget("sonnet-5", "anthropic"), 1_000_000 - 128_000 - catalog.PROMPT_RESERVE)
        self.env(MICRO_CC_TRIM_BUDGET="1234")
        self.assertEqual(catalog.effective_trim_budget("sonnet-5", "anthropic"), 1234)


class TestEnvProtection(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.home, ".micro-cc"))
        p = mock.patch.dict(os.environ, {"HOME": self.home, **{k: "" for k in ENV_KEYS}})
        p.start()
        self.addCleanup(p.stop)

    def test_apply_login_writes_owner_only(self):
        from micro_cc.utils import helpers
        path = os.path.join(self.home, ".micro-cc", ".env")
        with open(path, "w") as f:
            f.write("OLD=1\n")
        os.chmod(path, 0o644)
        helpers.apply_login({"GROQ_API_KEY": "g"}, "groq")
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(read(path), "GROQ_API_KEY=g\nMICROCC_PROVIDER=groq\n")
        self.assertEqual([f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")], [])

    def test_existing_env_file_tightened(self):
        path = os.path.join(self.home, "k.env")
        with open(path, "w") as f:
            f.write("X=1\n")
        os.chmod(path, 0o644)
        providers._owner_only(path)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)

    def test_bash_env_hides_saved_provider_keys_and_keeps_settings(self):
        from micro_cc.tools.bash_tool import _bash_env
        saved = {"LITELLM_API_KEY": "l", "AWS_BEARER_TOKEN_BEDROCK": "b", "AZURE_OPENAI_API_KEY": "z", "GROQ_API_KEY": "g"}
        with open(os.path.join(self.home, ".micro-cc", ".env"), "w") as f:
            f.writelines(f"{k}={v}\n" for k, v in saved.items())
        os.environ.update(saved, MICRO_CC_MIRROR_POSTGRES_URL="pg", ANTHROPIC_API_KEY="a", AWS_REGION="eu-west-1", OLLAMA_MODEL="q")
        with mock.patch.object(providers, "ENV_PATH", os.path.join(self.home, ".micro-cc", ".env")):
            env = _bash_env()
        for k in [*saved, "ANTHROPIC_API_KEY", "MICRO_CC_MIRROR_POSTGRES_URL"]:
            self.assertNotIn(k, env)
        self.assertEqual(env["AWS_REGION"], "eu-west-1")
        self.assertEqual(env["OLLAMA_MODEL"], "q")

    def test_env_only_keys_reach_headless_subagents(self):
        from micro_cc.tools.bash_tool import _bash_env
        os.environ.update(LITELLM_BASE_URL="u", LITELLM_API_KEY="from-launcher")  # no ~/.micro-cc/.env entry
        with mock.patch.object(providers, "ENV_PATH", os.path.join(self.home, ".micro-cc", ".env")):
            self.assertEqual(_bash_env()["LITELLM_API_KEY"], "from-launcher")

    def test_login_never_clears_shell_settings(self):
        keys = providers.routing_env_keys()
        for k in ("AWS_REGION", "CLOUD_ML_REGION", "OLLAMA_MODEL", "SERPAPI_KEY"):
            self.assertNotIn(k, keys)
        self.assertIn("AWS_BEARER_TOKEN_BEDROCK", keys)


class TestPlatformProviders(unittest.TestCase):
    def setUp(self):
        p = mock.patch.dict(os.environ, {k: "" for k in ENV_KEYS})
        p.start()
        self.addCleanup(p.stop)

    def test_bedrock_and_vertex_ids(self):
        self.assertEqual(catalog.resolve("opus-5.5", "bedrock"), ("anthropic.claude-opus-5-5", "anthropic-messages"))
        self.assertEqual(catalog.resolve("opus-5.5", "vertex"), ("claude-opus-5-5", "anthropic-messages"))
        self.assertIn(catalog.DEFAULT_MODEL, catalog.models_for("bedrock", None))
        self.assertIn(catalog.DEFAULT_MODEL, catalog.models_for("vertex", None))
        self.assertNotIn("gpt-6-sol", catalog.models_for("bedrock", None))

    def test_clients(self):
        import anthropic
        from micro_cc.models.apis import anthropic_messages as am
        os.environ.update(AWS_BEARER_TOKEN_BEDROCK="b", ANTHROPIC_VERTEX_PROJECT_ID="proj")
        bedrock = am.build_client("bedrock", 10)
        self.assertIsInstance(bedrock, anthropic.AsyncAnthropicBedrockMantle)
        self.assertIn("us-east-1", str(bedrock.base_url))
        os.environ["AWS_REGION"] = "eu-west-1"
        self.assertIn("eu-west-1", str(am.build_client("bedrock", 10).base_url))
        self.assertIsInstance(am.build_client("vertex", 10), anthropic.AsyncAnthropicVertex)

    def test_no_betas_off_the_direct_api(self):
        for pid in ("bedrock", "vertex", "foundry"):
            self.assertFalse(catalog.compat(pid).get("betas"), pid)

    def test_ensure_package_installs_only_when_missing(self):
        with mock.patch("subprocess.run") as run, mock.patch("importlib.import_module", return_value=object()):
            self.assertIsNone(providers.ensure_package("vertex"))
            run.assert_not_called()
        with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=0, stdout="", stderr="")) as run, \
                mock.patch("importlib.import_module", side_effect=ImportError):
            self.assertIsNone(providers.ensure_package("vertex"))
            self.assertIn("google-auth[requests]>=2,<3", run.call_args.args[0])
        with mock.patch("subprocess.run", return_value=SimpleNamespace(returncode=1, stdout="", stderr="no network")), \
                mock.patch("importlib.import_module", side_effect=ImportError):
            self.assertIn("no network", providers.ensure_package("vertex"))
        self.assertIsNone(providers.ensure_package("anthropic"))

    def test_compat_providers_omit_unknown_max_tokens_and_read_reasoning(self):
        from micro_cc.models.adapters import chat
        from micro_cc.models.apis import openai_chat
        seen = {}

        class Fake:
            def __init__(self, **kw):
                self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

            async def create(self, **kw):
                seen.update(kw)
                msg = SimpleNamespace(content="hi", tool_calls=None, reasoning_content="thought")
                return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)

        os.environ["GROQ_API_KEY"] = "g"
        with mock.patch.object(openai_chat, "AsyncOpenAI", Fake):
            r = asyncio.run(openai_chat.call("groq", "some/model", "some/model", input="x", thinking=True))
            self.assertNotIn("max_tokens", seen)
            self.assertNotIn("extra_body", seen)
            self.assertEqual([b.type for b in r.content], ["thinking", "text"])
            asyncio.run(openai_chat.call("groq", "some/model", "some/model", input="x", max_tokens=500))
            self.assertEqual(seen["max_tokens"], 500)
        self.assertEqual(chat._reasoning_text(SimpleNamespace(reasoning="r"), "read"), "r")


class TestDispatch(unittest.TestCase):
    """model_call picks the api module from (provider, alias); no SDK is touched."""

    def route(self, provider, model):
        from micro_cc.models import client
        seen = {}

        async def fake_call(p, alias, model_id, **kw):
            seen.update(provider=p, alias=alias, model_id=model_id)
            return "ok"

        import importlib
        real = importlib.import_module

        def fake_import(name):
            mod = real(name)
            seen["module"] = name.rsplit(".", 1)[-1]
            return SimpleNamespace(call=fake_call) if ".apis." in name else mod

        with mock.patch.object(client, "import_module", side_effect=fake_import):
            asyncio.run(client.model_call("hi", model, provider=provider))
        return seen

    def test_routes(self):
        self.assertEqual(self.route("litellm", "gpt-6-sol")["module"], "openai_responses")
        self.assertEqual(self.route("litellm", "sonnet-5")["module"], "openai_chat")
        self.assertEqual(self.route("foundry", "opus-5")["module"], "anthropic_messages")
        s = self.route("openai", "gpt-6-luna")
        self.assertEqual((s["module"], s["model_id"]), ("openai_responses", "gpt-6-luna"))

    def test_unknown_provider_raises(self):
        from micro_cc.models import client
        with self.assertRaises(ValueError):
            asyncio.run(client.model_call("hi", "sonnet-5", provider="Anthropic"))


if __name__ == "__main__":
    unittest.main()
