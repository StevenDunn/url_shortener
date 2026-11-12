import fcntl
import tempfile
import threading
import unittest
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
        self.path = Path(tmp.name) / "urls.tsv"


class LoadMappingsTest(TempStoreTestCase):
    def test_missing_file_is_empty(self):
        self.assertEqual(load_mappings(self.path), {})

    def test_reads_saved_lines(self):
        self.path.write_text("abc1234\thttps://a.com\nXYZ9876\thttps://b.com\n", encoding="utf-8")
        self.assertEqual(load_mappings(self.path), {"abc1234": "https://a.com", "XYZ9876": "https://b.com"})

    def test_skips_blank_lines(self):
        self.path.write_text("\nabc1234\thttps://a.com\n\n", encoding="utf-8")
        self.assertEqual(load_mappings(self.path), {"abc1234": "https://a.com"})


class SaveMappingTest(TempStoreTestCase):
    def test_creates_file(self):
        save_mapping(self.path, "abc1234", "https://a.com")
        self.assertEqual(self.path.read_text(encoding="utf-8"), "abc1234\thttps://a.com\n")

    def test_appends_in_order(self):
        save_mapping(self.path, "abc1234", "https://a.com")
        save_mapping(self.path, "XYZ9876", "https://bücher.de")
        self.assertEqual(
            self.path.read_text(encoding="utf-8"),
            "abc1234\thttps://a.com\nXYZ9876\thttps://bücher.de\n",
        )


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
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 1)

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
    """Locks are held here through a separate file handle, as another process would."""

    BLOCKED_SECONDS = 0.2

    def setUp(self):
        super().setUp()
        save_mapping(self.path, "AAAAAAA", "https://a.com")

    def hold_lock(self, lock):
        f = open(self.path, "a")
        self.addCleanup(f.close)
        fcntl.flock(f, lock)
        return f

    def start(self, target):
        result = {}
        thread = threading.Thread(target=lambda: result.update(value=target()))
        thread.start()
        self.addCleanup(thread.join, 5)
        return thread, result

    def assert_blocked_until_released(self, held, target):
        thread, result = self.start(target)
        thread.join(self.BLOCKED_SECONDS)
        self.assertTrue(thread.is_alive(), "should wait for the lock")
        held.close()
        thread.join(5)
        self.assertFalse(thread.is_alive(), "should finish once the lock is released")
        return result.get("value")

    def test_readers_share_the_lock(self):
        self.hold_lock(fcntl.LOCK_SH)
        thread, result = self.start(lambda: load_mappings(self.path))
        thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result["value"], {"AAAAAAA": "https://a.com"})

    def test_writer_waits_for_reader(self):
        held = self.hold_lock(fcntl.LOCK_SH)
        self.assert_blocked_until_released(held, lambda: save_mapping(self.path, "BBBBBBB", "https://b.com"))
        self.assertEqual(load_mappings(self.path)["BBBBBBB"], "https://b.com")

    def test_shorten_waits_for_reader(self):
        held = self.hold_lock(fcntl.LOCK_SH)
        code = self.assert_blocked_until_released(held, lambda: shorten_and_save("https://b.com", self.path))
        self.assertEqual(load_mappings(self.path)[code], "https://b.com")

    def test_reader_waits_for_writer(self):
        held = self.hold_lock(fcntl.LOCK_EX)
        mappings = self.assert_blocked_until_released(held, lambda: load_mappings(self.path))
        self.assertEqual(mappings, {"AAAAAAA": "https://a.com"})

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
        mappings = load_mappings(self.path)
        self.assertEqual(len(mappings), 21)
        for url, code in zip(urls, codes):
            self.assertEqual(mappings[code], url)

    def test_concurrent_same_url_is_stored_once(self):
        codes = self.run_concurrently(["https://example.com/same"] * 10)
        self.assertEqual(len(set(codes)), 1)
        self.assertEqual(len(self.path.read_text(encoding="utf-8").splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
