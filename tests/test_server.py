import contextlib
import http.client
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from shortener.codes import is_valid_code
from shortener.server import MAX_BODY_BYTES, ShortenerServer, _location, make_server, serve
from shortener.store import StoreBusyError, load_mappings, save_mapping


class ServerTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "urls.db"

        self.server = make_server("127.0.0.1", 0, self.path, log=False)
        self.addCleanup(self.server.server_close)
        thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        self.addCleanup(thread.join, 5)
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request(self, method, path, body=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=headers or {})
            resp = conn.getresponse()
            return resp.status, resp.headers, resp.read()
        finally:
            conn.close()

    def post_url(self, url):
        status, _, body = self.request("POST", "/", json.dumps({"url": url}))
        return status, json.loads(body)


class PostTest(ServerTestCase):
    def test_shortens_google(self):
        status, body = self.post_url("https://www.google.com")
        self.assertEqual(status, 201)
        self.assertTrue(is_valid_code(body["code"]))
        self.assertEqual(body["url"], "https://www.google.com")
        self.assertEqual(body["short_url"], f"http://127.0.0.1:{self.port}/{body['code']}")
        self.assertEqual(load_mappings(self.path), {body["code"]: "https://www.google.com"})

    def test_same_url_returns_same_code(self):
        _, first = self.post_url("https://www.google.com")
        _, second = self.post_url("https://www.google.com")
        self.assertEqual(first["code"], second["code"])

    def test_bad_requests_return_400_and_store_nothing(self):
        bodies = {
            "invalid URL": json.dumps({"url": "javascript:alert(1)"}),
            "not JSON": "https://www.google.com",
            "not UTF-8": b"\xff\xfe",
            "JSON list": json.dumps(["https://www.google.com"]),
            "missing url": json.dumps({"link": "https://www.google.com"}),
            "url not a string": json.dumps({"url": 123}),
        }
        for name, body in bodies.items():
            with self.subTest(name):
                status, _, resp = self.request("POST", "/", body)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(resp))
        self.assertFalse(self.path.exists())

    def test_oversized_body_returns_413(self):
        status, _, _ = self.request("POST", "/", headers={"Content-Length": str(MAX_BODY_BYTES + 1)})
        self.assertEqual(status, 413)

    def test_missing_content_length_returns_411(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.putrequest("POST", "/")
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 411)

    def test_post_to_other_path_returns_404(self):
        status, _, _ = self.request("POST", "/other", json.dumps({"url": "https://www.google.com"}))
        self.assertEqual(status, 404)

    def test_concurrent_posts_get_distinct_codes(self):
        urls = [f"https://example.com/{i}" for i in range(20)]
        codes = [None] * len(urls)

        def post(i):
            codes[i] = self.post_url(urls[i])[1]["code"]

        threads = [threading.Thread(target=post, args=(i,)) for i in range(len(urls))]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(set(codes)), 20)
        self.assertEqual(load_mappings(self.path), dict(zip(codes, urls)))


class GetTest(ServerTestCase):
    def test_redirects_to_shortened_url(self):
        _, body = self.post_url("https://www.google.com")
        status, headers, _ = self.request("GET", f"/{body['code']}")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://www.google.com")

    def test_ignores_query_string(self):
        _, body = self.post_url("https://www.google.com")
        status, _, _ = self.request("GET", f"/{body['code']}?utm=x")
        self.assertEqual(status, 302)

    def test_serves_mappings_written_to_the_file(self):
        save_mapping(self.path, "AAAAAAA", "https://example.com/from-file")
        status, headers, _ = self.request("GET", "/AAAAAAA")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://example.com/from-file")

    def test_non_ascii_location_is_percent_encoded(self):
        save_mapping(self.path, "AAAAAAA", "https://bücher.de/straße")
        status, headers, _ = self.request("GET", "/AAAAAAA")
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://b%C3%BCcher.de/stra%C3%9Fe")

    def test_unknown_paths_return_404(self):
        save_mapping(self.path, "AAAAAAA", "https://example.com")
        for path in ("/BBBBBBB", "/bad-code!", "/", "/AAAAAAA/extra"):
            with self.subTest(path=path):
                status, _, body = self.request("GET", path)
                self.assertEqual(status, 404)
                self.assertIn("error", json.loads(body))

    def test_unknown_code_with_no_file_returns_404(self):
        status, _, _ = self.request("GET", "/AAAAAAA")
        self.assertEqual(status, 404)


class BusyStoreTest(ServerTestCase):
    def test_post_returns_503_while_another_writer_holds_the_lock(self):
        save_mapping(self.path, "AAAAAAA", "https://a.com")
        other = sqlite3.connect(self.path, autocommit=True, timeout=0)
        self.addCleanup(other.close)
        other.execute("BEGIN IMMEDIATE")
        with mock.patch("shortener.store.BUSY_TIMEOUT_MS", 100):
            status, headers, body = self.request("POST", "/", json.dumps({"url": "https://b.com"}))
        other.execute("ROLLBACK")
        self.assertEqual(status, 503)
        self.assertEqual(headers["Retry-After"], "1")
        self.assertIn("busy", json.loads(body)["error"])

    def test_get_returns_503_when_store_is_busy(self):
        with mock.patch("shortener.server.load_mappings", side_effect=StoreBusyError("store is busy")):
            status, headers, _ = self.request("GET", "/AAAAAAA")
        self.assertEqual(status, 503)
        self.assertEqual(headers["Retry-After"], "1")


class LocationTest(unittest.TestCase):
    def test_leaves_ascii_urls_unchanged(self):
        url = "https://example.com/a%20b?q=1&r=[2]#frag"
        self.assertEqual(_location(url), url)

    def test_encodes_non_ascii(self):
        self.assertEqual(_location("https://bücher.de"), "https://b%C3%BCcher.de")


class MakeServerTest(unittest.TestCase):
    def test_configures_server(self):
        with make_server("127.0.0.1", 0, "m.tsv", log=False) as server:
            self.assertIsInstance(server, ShortenerServer)
            self.assertEqual(server.server_address[0], "127.0.0.1")
            self.assertEqual(server.store_path, "m.tsv")
            self.assertFalse(server.log)


class ServeTest(unittest.TestCase):
    def test_prints_address_and_stops_on_ctrl_c(self):
        out = io.StringIO()
        with (
            mock.patch.object(ShortenerServer, "serve_forever", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(out),
        ):
            serve("127.0.0.1", 0, "m.tsv")
        self.assertRegex(out.getvalue(), r"Serving on http://127\.0\.0\.1:\d+ \(store: m\.tsv\)")


if __name__ == "__main__":
    unittest.main()
