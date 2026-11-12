import sqlite3
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from shortener.codes import is_valid_code
from shortener.store import MAX_ATTEMPTS, StoreBusyError, load_mappings, lookup, save_mapping, shorten_and_save
from shortener.urls import InvalidURLError
from tests.test_urls import VALID_URLS


class TempStoreTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "urls.db"

    def row_count(self):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute("SELECT COUNT(*) FROM urls").fetchone()[0]


class LoadMappingsTest(TempStoreTestCase):
    def test_missing_database_is_empty_and_not_created(self):
        self.assertEqual(load_mappings(self.path), {})
        self.assertFalse(self.path.exists())

    def test_database_without_table_is_empty(self):
        sqlite3.connect(self.path).close()
        self.assertEqual(load_mappings(self.path), {})

    def test_reads_saved_mappings(self):
        save_mapping(self.path, "abc1234", "https://a.com")
        save_mapping(self.path, "XYZ9876", "https://b.com")
        self.assertEqual(load_mappings(self.path), {"abc1234": "https://a.com", "XYZ9876": "https://b.com"})


class LookupTest(TempStoreTestCase):
    def test_finds_stored_url(self):
        save_mapping(self.path, "abc1234", "https://a.com")
        save_mapping(self.path, "XYZ9876", "https://bücher.de/straße")
        self.assertEqual(lookup(self.path, "abc1234"), "https://a.com")
        self.assertEqual(lookup(self.path, "XYZ9876"), "https://bücher.de/straße")

    def test_unknown_code_is_none(self):
        save_mapping(self.path, "abc1234", "https://a.com")
        self.assertIsNone(lookup(self.path, "XYZ9876"))

    def test_codes_are_case_sensitive(self):
        save_mapping(self.path, "abc1234", "https://a.com")
        self.assertIsNone(lookup(self.path, "ABC1234"))

    def test_missing_database_is_none_and_not_created(self):
        self.assertIsNone(lookup(self.path, "abc1234"))
        self.assertFalse(self.path.exists())

    def test_database_without_table_is_none(self):
        sqlite3.connect(self.path).close()
        self.assertIsNone(lookup(self.path, "abc1234"))

    def test_uses_primary_key_index(self):
        save_mapping(self.path, "abc1234", "https://a.com")
        with closing(sqlite3.connect(self.path)) as conn:
            plan = conn.execute("EXPLAIN QUERY PLAN SELECT url FROM urls WHERE code = ?", ("abc1234",)).fetchall()
        detail = " ".join(row[-1] for row in plan)
        self.assertIn("USING INDEX", detail)
        self.assertNotIn("SCAN", detail)


class SaveMappingTest(TempStoreTestCase):
    def test_creates_database(self):
        save_mapping(self.path, "abc1234", "https://a.com")
        self.assertTrue(self.path.exists())
        self.assertEqual(load_mappings(self.path), {"abc1234": "https://a.com"})

    def test_round_trips_non_ascii(self):
        save_mapping(self.path, "XYZ9876", "https://bücher.de/straße")
        self.assertEqual(load_mappings(self.path), {"XYZ9876": "https://bücher.de/straße"})

    def test_rejects_duplicate_code_or_url(self):
        save_mapping(self.path, "abc1234", "https://a.com")
        for code, url in (("abc1234", "https://b.com"), ("XYZ9876", "https://a.com")):
            with self.subTest(code=code, url=url), self.assertRaises(sqlite3.IntegrityError):
                save_mapping(self.path, code, url)
        self.assertEqual(load_mappings(self.path), {"abc1234": "https://a.com"})


class ShortenAndSaveTest(TempStoreTestCase):
    def test_stores_google(self):
        code = shorten_and_save("https://www.google.com", self.path)
        self.assertTrue(is_valid_code(code))
        self.assertEqual(load_mappings(self.path), {code: "https://www.google.com"})

    def test_stores_all_valid_urls(self):
        codes = {url: shorten_and_save(url, self.path) for url in VALID_URLS}
        mappings = load_mappings(self.path)
        for url, code in codes.items():
            with self.subTest(url=url):
                self.assertEqual(mappings[code], url.strip())

    def test_same_url_returns_existing_code(self):
        first = shorten_and_save("https://www.google.com", self.path)
        second = shorten_and_save("  https://www.google.com ", self.path)
        self.assertEqual(first, second)
        self.assertEqual(self.row_count(), 1)

    def test_invalid_url_writes_nothing(self):
        with self.assertRaises(InvalidURLError):
            shorten_and_save("javascript:alert(1)", self.path)
        self.assertFalse(self.path.exists())

    def test_retries_on_code_collision(self):
        save_mapping(self.path, "AAAAAAA", "https://a.com")
        with mock.patch("shortener.store.generate_code", side_effect=["AAAAAAA", "BBBBBBB"]):
            code = shorten_and_save("https://b.com", self.path)
        self.assertEqual(code, "BBBBBBB")
        self.assertEqual(load_mappings(self.path), {"AAAAAAA": "https://a.com", "BBBBBBB": "https://b.com"})

    def test_gives_up_after_max_attempts(self):
        save_mapping(self.path, "AAAAAAA", "https://a.com")
        with mock.patch("shortener.store.generate_code", return_value="AAAAAAA") as gen:
            with self.assertRaises(RuntimeError):
                shorten_and_save("https://b.com", self.path)
        self.assertEqual(gen.call_count, MAX_ATTEMPTS)
        self.assertEqual(load_mappings(self.path), {"AAAAAAA": "https://a.com"})


class LockingTest(TempStoreTestCase):
    """Locks are held through a second sqlite3 connection, as another process would."""

    BLOCKED_SECONDS = 0.2

    def setUp(self):
        super().setUp()
        save_mapping(self.path, "AAAAAAA", "https://a.com")
        self.other = sqlite3.connect(self.path, autocommit=True, timeout=0)
        self.addCleanup(self.other.close)

    def test_database_uses_wal(self):
        self.assertEqual(self.other.execute("PRAGMA journal_mode").fetchone()[0], "wal")

    def test_reader_not_blocked_by_writer(self):
        # EXCLUSIVE would block readers in SQLite's default journal mode; in WAL it doesn't.
        self.other.execute("BEGIN EXCLUSIVE")
        self.other.execute("INSERT INTO urls (code, url) VALUES ('BBBBBBB', 'https://b.com')")
        with mock.patch("shortener.store.BUSY_TIMEOUT_MS", 2000):
            start = time.monotonic()
            mappings = load_mappings(self.path)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(mappings, {"AAAAAAA": "https://a.com"}, "uncommitted rows must not be visible")

    def test_writer_not_blocked_by_reader(self):
        self.other.execute("BEGIN")
        self.other.execute("SELECT COUNT(*) FROM urls").fetchone()
        with mock.patch("shortener.store.BUSY_TIMEOUT_MS", 2000):
            start = time.monotonic()
            code = shorten_and_save("https://b.com", self.path)
        self.assertLess(time.monotonic() - start, 0.5)
        self.assertEqual(load_mappings(self.path)[code], "https://b.com")
        # The open read transaction keeps seeing its original snapshot.
        self.assertEqual(self.other.execute("SELECT COUNT(*) FROM urls").fetchone()[0], 1)

    def test_writer_waits_for_writer(self):
        self.other.execute("BEGIN IMMEDIATE")
        result = {}
        thread = threading.Thread(target=lambda: result.update(code=shorten_and_save("https://b.com", self.path)))
        thread.start()
        self.addCleanup(thread.join, 5)

        thread.join(self.BLOCKED_SECONDS)
        self.assertTrue(thread.is_alive(), "should wait for the other writer")
        self.other.execute("COMMIT")
        thread.join(5)
        self.assertFalse(thread.is_alive(), "should finish once the other writer commits")
        self.assertEqual(load_mappings(self.path)[result["code"]], "https://b.com")

    def test_writer_gives_up_after_busy_timeout(self):
        self.other.execute("BEGIN IMMEDIATE")
        with mock.patch("shortener.store.BUSY_TIMEOUT_MS", 100):
            with self.assertRaises(StoreBusyError):
                shorten_and_save("https://b.com", self.path)
            with self.assertRaises(StoreBusyError):
                save_mapping(self.path, "BBBBBBB", "https://b.com")
        self.other.execute("ROLLBACK")
        self.assertEqual(load_mappings(self.path), {"AAAAAAA": "https://a.com"})


class ConcurrencyTest(TempStoreTestCase):
    def run_concurrently(self, urls):
        barrier = threading.Barrier(len(urls))
        codes = [None] * len(urls)

        def work(i):
            barrier.wait()
            codes[i] = shorten_and_save(urls[i], self.path)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(len(urls))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        return codes

    def test_concurrent_distinct_urls_get_distinct_codes(self):
        urls = [f"https://example.com/{i}" for i in range(20)]
        codes = self.run_concurrently(urls)
        self.assertEqual(len(set(codes)), 20)
        self.assertEqual(load_mappings(self.path), dict(zip(codes, urls)))

    def test_concurrent_first_use_of_new_database(self):
        # Switching a new database to WAL can fail with "locked" without waiting
        # for the busy timeout, so many threads opening it at once must still work.
        for round_ in range(20):
            with self.subTest(round=round_):
                self.path = self.path.with_name(f"fresh{round_}.db")
                codes = self.run_concurrently([f"https://example.com/{i}" for i in range(10)])
                self.assertNotIn(None, codes)
                self.assertEqual(self.row_count(), 10)

    def test_concurrent_same_url_is_stored_once(self):
        codes = self.run_concurrently(["https://example.com/same"] * 10)
        self.assertEqual(len(set(codes)), 1)
        self.assertEqual(self.row_count(), 1)


if __name__ == "__main__":
    unittest.main()
