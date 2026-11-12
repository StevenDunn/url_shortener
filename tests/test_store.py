import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing
from pathlib import Path
from unittest import mock

from shortener.codes import is_valid_code
from shortener.store import MAX_ATTEMPTS, load_mappings, save_mapping, shorten_and_save
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

    def test_concurrent_same_url_is_stored_once(self):
        codes = self.run_concurrently(["https://example.com/same"] * 10)
        self.assertEqual(len(set(codes)), 1)
        self.assertEqual(self.row_count(), 1)


if __name__ == "__main__":
    unittest.main()
