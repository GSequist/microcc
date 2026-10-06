import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

from micro_cc.models import catalog_, registry  # noqa: E402

REPO_JSON = os.path.join(os.path.dirname(__file__), "..", "models.json")
NEW = {"anthropic": "claude-x", "thinking": True, "summarized_display": False, "context_window": 200_000, "max_output": 8_000}


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
        self.write(registry.remote_cache_path(), doc)

    def user(self, doc):
        self.write(registry.user_catalog_path(), doc)

    def build(self):
        return registry.build_catalog()


class TestMerge(CatalogCase):
    def test_builtin_only(self):
        models, options, default, status = self.build()
        self.assertEqual(options, registry._BUILTIN_OPTIONS)
        self.assertEqual(default, "sonnet-5")
        self.assertEqual(status["skipped"], [])

    def test_order_user_beats_remote_beats_builtin(self):
        self.remote({"schema": 1, "models": {"sonnet-5": {"context_window": 500_000}, "opus-5": {"max_output": 1000}}})
        self.user({"models": {"sonnet-5": {"context_window": 300_000}}})
        models = self.build()[0]
        self.assertEqual(models["sonnet-5"]["context_window"], 300_000)
        self.assertEqual(models["opus-5"]["max_output"], 1000)
        self.assertEqual(models["opus-4.8"]["max_output"], 128_000)

    def test_field_level_override_keeps_other_fields(self):
        self.user({"models": {"sonnet-5": {"context_window": 300_000}}})
        e = self.build()[0]["sonnet-5"]
        self.assertEqual(e["anthropic"], "claude-sonnet-5")
        self.assertTrue(e["thinking"])

    def test_new_alias_via_user_and_remote_appended_in_order(self):
        self.remote({"schema": 1, "models": {"r-new": NEW}})
        self.user({"models": {"u-new": NEW}})
        models, options, _, _ = self.build()
        self.assertEqual(options[-2:], ["r-new", "u-new"])
        self.assertEqual(options[:len(registry._BUILTIN_OPTIONS)], registry._BUILTIN_OPTIONS)
        self.assertIn("u-new", models)

    def test_new_alias_needs_full_entry(self):
        self.user({"models": {"partial": {"anthropic": "claude-x"}}})
        models, _, _, status = self.build()
        self.assertNotIn("partial", models)
        self.assertTrue(any("partial" in s for s in status["skipped"]))

    def test_builtin_not_mutated(self):
        before = json.dumps(registry._BUILTIN_MODELS, sort_keys=True)
        self.user({"models": {"sonnet-5": {"context_window": 1}}})
        self.build()
        self.assertEqual(json.dumps(registry._BUILTIN_MODELS, sort_keys=True), before)

    def test_default_model(self):
        self.remote({"schema": 1, "default_model": "opus-5", "models": {}})
        self.assertEqual(self.build()[2], "opus-5")
        self.user({"default_model": "missing-alias"})
        self.assertEqual(self.build()[2], "opus-5")
        self.user({"default_model": "sonnet-4.6"})
        self.assertEqual(self.build()[2], "sonnet-4.6")


class TestValidation(CatalogCase):
    def test_invalid_entries_skipped_not_file(self):
        self.user({"models": {
            "bad-type": {**NEW, "thinking": "yes"},
            "bad-int": {**NEW, "context_window": -5},
            "bad-bool-int": {**NEW, "max_output": True},
            "empty-id": {**NEW, "anthropic": ""},
            "good": NEW,
        }})
        models, _, _, status = self.build()
        self.assertIn("good", models)
        for alias in ("bad-type", "bad-int", "bad-bool-int", "empty-id"):
            self.assertNotIn(alias, models)
        self.assertEqual(len(status["skipped"]), 4)

    def test_unknown_backend_dropped(self):
        self.user({"models": {"m": {**NEW, "mystery": "id-1"}, "only-unknown": {**NEW, "anthropic": None, "mystery": "x"}}})
        models = self.build()[0]
        self.assertNotIn("mystery", models["m"])
        self.assertNotIn("only-unknown", models)
        self.user({"models": {"no-backend": {k: v for k, v in NEW.items() if k != "anthropic"} | {"mystery": "x"}}})
        self.assertNotIn("no-backend", self.build()[0])

    def test_min_version_skip(self):
        with mock.patch.object(registry, "_running_version", return_value=(0, 2, 102)):
            self.remote({"schema": 1, "models": {"future": {**NEW, "min_version": "9.0.0"}, "ok": {**NEW, "min_version": "0.2.100"}}})
            models, _, _, status = self.build()
        self.assertNotIn("future", models)
        self.assertIn("ok", models)
        self.assertTrue(any("future" in s for s in status["skipped"]))

    def test_bad_min_version_skipped(self):
        self.user({"models": {"m": {**NEW, "min_version": "banana"}}})
        self.assertNotIn("m", self.build()[0])

    def test_corrupt_files_ignored(self):
        self.remote("{not json")
        self.user("[1, 2]")
        models, options, default, status = self.build()
        self.assertEqual(options, registry._BUILTIN_OPTIONS)
        self.assertEqual(default, "sonnet-5")
        self.assertTrue(status["remote"].startswith("unreadable"))
        self.assertTrue(status["user"].startswith("unreadable"))

    def test_models_not_a_dict(self):
        self.user({"models": [1, 2]})
        self.assertEqual(self.build()[1], registry._BUILTIN_OPTIONS)

    def test_shorthand_points_at_same_entry(self):
        self.assertIs(registry.MODELS["haiku"], registry.MODELS["haiku-4.5"])


class TestImport(unittest.TestCase):
    def test_import_does_no_network(self):
        import importlib
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("network at import")):
            importlib.reload(registry)
        self.assertTrue(registry.MODELS)


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
    DOC = {"schema": 1, "models": {"fetched": NEW}}

    def fetch(self, **kw):
        return catalog_.refresh_remote_catalog(**kw)

    def test_writes_cache_with_etag_and_registry_reads_it(self):
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(json.dumps(self.DOC).encode(), '"abc"')) as m:
            self.assertEqual(self.fetch(), "updated")
        self.assertEqual(m.call_args.kwargs["timeout"], 2)
        self.assertIn("fetched", self.build()[0])
        self.assertEqual(catalog_._cached_etag(registry.remote_cache_path()), '"abc"')
        self.assertEqual([f for f in os.listdir(os.path.dirname(registry.remote_cache_path())) if f.endswith(".tmp")], [])

    def test_throttle_skips_fresh_cache(self):
        self.remote(self.DOC)
        with mock.patch("urllib.request.urlopen", side_effect=AssertionError("hit network")):
            self.assertEqual(self.fetch(), "fresh")

    def test_stale_cache_refetches(self):
        self.remote(self.DOC)
        old = time.time() - catalog_.MIN_REFRESH_SECONDS - 10
        os.utime(registry.remote_cache_path(), (old, old))
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(json.dumps(self.DOC).encode())):
            self.assertEqual(self.fetch(), "updated")

    def test_304_keeps_cache_and_refreshes_mtime(self):
        self.remote({**self.DOC, "_etag": '"abc"'})
        path = registry.remote_cache_path()
        old = time.time() - catalog_.MIN_REFRESH_SECONDS - 10
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
        self.assertFalse(os.path.exists(registry.remote_cache_path()))

    def test_invalid_payload_not_written(self):
        for body in (b"[]", json.dumps({"schema": 2, "models": {}}).encode(), json.dumps({"schema": 1, "models": []}).encode()):
            with mock.patch("urllib.request.urlopen", return_value=FakeResp(body)):
                self.assertEqual(self.fetch(), "invalid")
        with mock.patch("urllib.request.urlopen", return_value=FakeResp(b"<html>")):
            self.assertTrue(self.fetch().startswith("error"))
        self.assertFalse(os.path.exists(registry.remote_cache_path()))

    def test_atomic_write_failure_keeps_old_cache(self):
        self.remote(self.DOC)
        path = registry.remote_cache_path()
        before = read(path)
        with mock.patch("os.replace", side_effect=OSError("boom")):
            with self.assertRaises(OSError):
                catalog_._write_atomic(path, {"schema": 1, "models": {}})
        self.assertEqual(read(path), before)
        self.assertEqual([f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")], [])


class TestConcurrentRefresh(CatalogCase):
    def test_readers_never_see_partial_cache_while_writers_race(self):
        import threading
        path = registry.remote_cache_path()
        docs = [{"schema": 1, "models": {f"m{i}": NEW}, "pad": "x" * 200_000} for i in range(3)]
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
                catalog_._write_atomic(path, doc)

        r = threading.Thread(target=reader)
        ws = [threading.Thread(target=writer, args=(d,)) for d in docs]
        r.start()
        [w.start() for w in ws]
        [w.join() for w in ws]
        stop.set()
        r.join()
        self.assertEqual(bad, [])
        self.assertEqual([f for f in os.listdir(os.path.dirname(path)) if f.endswith(".tmp")], [])


class TestRepoFile(unittest.TestCase):
    def test_repo_models_json_valid_and_matches_builtin(self):
        with open(REPO_JSON) as f:
            doc = json.load(f)
        self.assertEqual(doc["schema"], 1)
        entries, default, skipped = registry.validate_catalog(doc)
        self.assertEqual(skipped, [])
        self.assertEqual(set(entries), set(registry._BUILTIN_MODELS))
        self.assertEqual(entries, registry._BUILTIN_MODELS)
        self.assertIn(default, entries)


if __name__ == "__main__":
    unittest.main()
