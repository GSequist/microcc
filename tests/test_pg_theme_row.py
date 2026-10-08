"""Theme lives in micro_cc_settings as id='theme'; a legacy micro_cc_theme row is copied once. Fake cursor, no database."""
import json
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from micro_cc.postgres_store import pg_store_  # noqa: E402


class FakeDB:
    """Tables as {name: {id: data}}; answers exactly the statements load_theme/save_theme send."""
    def __init__(self, tables):
        self.tables, self.sql, self._row = tables, [], None

    @contextmanager
    def connect(self):
        yield self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        self.sql.append(" ".join(sql.split()))
        q = self.sql[-1]
        if q.startswith("SELECT data FROM micro_cc_settings WHERE id = 'theme'"):
            d = self.tables["micro_cc_settings"].get("theme")
            self._row = (d,) if d is not None else None
        elif q.startswith("SELECT to_regclass('micro_cc_theme')"):
            self._row = ("micro_cc_theme" in self.tables,)
        elif q.startswith("SELECT data FROM micro_cc_theme"):
            d = self.tables["micro_cc_theme"].get("global")
            self._row = (d,) if d is not None else None
        elif q.startswith("INSERT INTO micro_cc_settings"):
            if "DO NOTHING" in q and "theme" in self.tables["micro_cc_settings"]:
                return
            self.tables["micro_cc_settings"]["theme"] = json.loads(params[0])
        else:
            raise AssertionError(f"unexpected SQL: {q}")

    def fetchone(self):
        return self._row


class TestThemeRow(unittest.TestCase):
    def setUp(self):
        self._connect = pg_store_._connect

    def tearDown(self):
        pg_store_._connect = self._connect

    def _db(self, tables):
        db = FakeDB(tables)
        pg_store_._connect = db.connect
        return db

    def test_fresh_db_has_no_theme(self):
        self._db({"micro_cc_settings": {"global": {"model": "x"}}})
        self.assertIsNone(pg_store_.load_theme())

    def test_legacy_row_copied_once_settings_untouched(self):
        legacy = {"name": "pitch", "colors": {"bg": "#000"}}
        db = self._db({"micro_cc_settings": {"global": {"model": "x"}}, "micro_cc_theme": {"global": legacy}})
        self.assertEqual(pg_store_.load_theme(), legacy)
        self.assertEqual(db.tables["micro_cc_settings"]["theme"], legacy)
        self.assertEqual(db.tables["micro_cc_settings"]["global"], {"model": "x"})
        self.assertEqual(db.tables["micro_cc_theme"]["global"], legacy)  # legacy table left in place
        n = len(db.sql)
        self.assertEqual(pg_store_.load_theme(), legacy)
        self.assertEqual(len(db.sql) - n, 1)  # second read hits the new row only

    def test_save_writes_theme_row_not_settings_row(self):
        db = self._db({"micro_cc_settings": {"global": {"model": "x"}}})
        pg_store_.save_theme({"name": "white", "colors": {}})
        self.assertEqual(db.tables["micro_cc_settings"]["theme"], {"name": "white", "colors": {}})
        self.assertEqual(db.tables["micro_cc_settings"]["global"], {"model": "x"})


if __name__ == "__main__":
    unittest.main()
