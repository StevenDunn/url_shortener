import contextlib
import http.client
import io
import json
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock
from urllib.parse import urlsplit

from shortener.codes import is_valid_code
from shortener.server import (
    MAX_BODY_BYTES,
    REQUEST_TIMEOUT_SECONDS,
    ShortenerHandler,
    ShortenerServer,
    _location,
    make_server,
    normalize_base_url,
    serve,
)
from shortener.store import StoreBusyError, import_tsv, load_mappings, save_mapping
from tests.test_store import SAMPLE_MAPPINGS, SAMPLE_TSV


class ServerTestCase(unittest.TestCase):
    BASE_URL = None

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / "urls.db"

        self.server = make_server("127.0.0.1", 0, self.path, log=False, base_url=self.BASE_URL)
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

    def test_redirects_imported_codes(self):
        import_tsv(SAMPLE_TSV, self.path)
        for code, url in SAMPLE_MAPPINGS.items():
            with self.subTest(code=code):
                status, headers, _ = self.request("GET", f"/{code}")
                self.assertEqual(status, 302)
                self.assertEqual(headers["Location"], _location(url))

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


class ShortUrlFromHostTest(ServerTestCase):
    def short_url(self, headers=None):
        status, _, body = self.request("POST", "/", json.dumps({"url": "https://www.google.com"}), headers)
        self.assertEqual(status, 201)
        body = json.loads(body)
        return body["short_url"].removesuffix(body["code"])

    def test_uses_host_header(self):
        for host in ("sho.rt", "sho.rt:8443", "localhost:8000", "192.168.1.5:8000", "[::1]:8000"):
            with self.subTest(host=host):
                self.assertEqual(self.short_url({"Host": host}), f"http://{host}/")

    def test_falls_back_to_bound_address_for_bad_host_header(self):
        for host in ("evil.com/path", "a b", "user@evil.com", "evil.com:99999999", ""):
            with self.subTest(host=host):
                self.assertEqual(self.short_url({"Host": host}), f"http://127.0.0.1:{self.port}/")

    def test_falls_back_to_bound_address_without_host_header(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        body = json.dumps({"url": "https://www.google.com"}).encode()
        conn.putrequest("POST", "/", skip_host=True)
        conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        result = json.loads(conn.getresponse().read())
        self.assertEqual(result["short_url"], f"http://127.0.0.1:{self.port}/{result['code']}")


class ShortUrlFromBaseUrlTest(ShortUrlFromHostTest):
    BASE_URL = "https://sho.rt/s"

    def test_uses_host_header(self):
        self.assertEqual(self.short_url({"Host": "other.example"}), "https://sho.rt/s/")

    def test_falls_back_to_bound_address_for_bad_host_header(self):
        self.assertEqual(self.short_url({"Host": "evil.com/path"}), "https://sho.rt/s/")

    def test_falls_back_to_bound_address_without_host_header(self):
        self.assertEqual(self.short_url(), "https://sho.rt/s/")

    def test_short_url_redirects(self):
        _, body = self.post_url("https://www.google.com")
        path = urlsplit(body["short_url"]).path.removeprefix("/s")
        status, headers, _ = self.request("GET", path)
        self.assertEqual(status, 302)
        self.assertEqual(headers["Location"], "https://www.google.com")


class NormalizeBaseUrlTest(unittest.TestCase):
    def test_accepts_and_strips_trailing_slash(self):
        for value, expected in {
            "https://sho.rt": "https://sho.rt",
            "https://sho.rt/": "https://sho.rt",
            "http://localhost:8000": "http://localhost:8000",
            "https://example.com/s/": "https://example.com/s",
            "  HTTPS://sho.rt  ": "HTTPS://sho.rt",
        }.items():
            with self.subTest(value=value):
                self.assertEqual(normalize_base_url(value), expected)

    def test_rejects(self):
        for value in (
            "",
            "sho.rt",
            "ftp://sho.rt",
            "mailto:a@b.com",
            "https://",
            "https://sho.rt?x=1",
            "https://sho.rt/?",
            "https://sho.rt#frag",
            "https://user:pass@sho.rt",
            "https://sho.rt:99999",
            "https://sho rt",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                normalize_base_url(value)


class TimeoutTest(ServerTestCase):
    TIMEOUT = 0.2

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(ShortenerHandler, "timeout", self.TIMEOUT)
        patcher.start()
        self.addCleanup(patcher.stop)

    def send_raw(self, data):
        """Send `data`, then read until the server closes the connection. Returns (reply, seconds)."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=5)
        self.addCleanup(sock.close)
        start = time.monotonic()
        sock.sendall(data)
        reply = b""
        while chunk := sock.recv(4096):
            reply += chunk
        return reply, time.monotonic() - start

    def test_stalled_body_gets_408_and_is_closed(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            reply, seconds = self.send_raw(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n{")
        self.assertTrue(reply.startswith(b"HTTP/1.0 408"), reply[:40])
        self.assertIn(b"timed out", reply)
        self.assertLess(seconds, 2)
        self.assertEqual(err.getvalue(), "", "a timeout must not print a traceback")
        self.assertFalse(self.path.exists())

    def test_idle_connection_is_closed(self):
        reply, seconds = self.send_raw(b"")
        self.assertEqual(reply, b"")
        self.assertLess(seconds, 2)

    def test_stalled_headers_are_closed(self):
        reply, seconds = self.send_raw(b"GET /AAAAAAA HTTP/1.1\r\nHost: x\r\n")
        self.assertEqual(reply, b"")
        self.assertLess(seconds, 2)

    def test_keeps_serving_after_timeouts(self):
        self.send_raw(b"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: 100\r\n\r\n")
        self.send_raw(b"")
        status, body = self.post_url("https://www.google.com")
        self.assertEqual(status, 201)
        self.assertTrue(is_valid_code(body["code"]))


class UnexpectedErrorTest(ServerTestCase):
    def capture(self, method, path, body=None, log=False):
        """Make a request while capturing the server's stderr. Returns (status, body, stderr)."""
        self.server.log = log
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status, _, resp = self.request(method, path, body)
        return status, resp, err.getvalue()

    def test_post_error_returns_generic_500(self):
        failure = sqlite3.DatabaseError("database disk image is malformed")
        with mock.patch("shortener.server.shorten_and_save", side_effect=failure):
            status, body, _ = self.capture("POST", "/", json.dumps({"url": "https://www.google.com"}))
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "internal server error"}, "internal details must not leak")

    def test_get_error_returns_500(self):
        with mock.patch("shortener.server.lookup", side_effect=ValueError("boom")):
            status, body, _ = self.capture("GET", "/AAAAAAA")
        self.assertEqual(status, 500)
        self.assertEqual(json.loads(body), {"error": "internal server error"})

    def test_logs_traceback_when_logging_is_on(self):
        with mock.patch("shortener.server.lookup", side_effect=ValueError("boom")):
            _, _, stderr = self.capture("GET", "/AAAAAAA", log=True)
        self.assertIn("unexpected error handling GET /AAAAAAA", stderr)
        self.assertIn("Traceback", stderr)
        self.assertIn("ValueError: boom", stderr)

    def test_silent_when_logging_is_off(self):
        with mock.patch("shortener.server.lookup", side_effect=ValueError("boom")):
            _, _, stderr = self.capture("GET", "/AAAAAAA")
        self.assertEqual(stderr, "")

    def test_error_after_response_started_closes_connection(self):
        save_mapping(self.path, "AAAAAAA", "https://a.com")
        original = ShortenerHandler.send_header

        def failing_send_header(handler, name, value):
            if name == "Location":
                raise RuntimeError("fails mid-response")
            original(handler, name, value)

        with mock.patch.object(ShortenerHandler, "send_header", failing_send_header):
            with self.assertRaises(http.client.RemoteDisconnected):
                self.capture("GET", "/AAAAAAA")

    def test_keeps_serving_after_an_error(self):
        with mock.patch("shortener.server.lookup", side_effect=ValueError("boom")):
            self.capture("GET", "/AAAAAAA")
        status, body = self.post_url("https://www.google.com")
        self.assertEqual(status, 201)
        status, headers, _ = self.request("GET", f"/{body['code']}")
        self.assertEqual(status, 302)


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
        with mock.patch("shortener.server.lookup", side_effect=StoreBusyError("store is busy")):
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
        with make_server("127.0.0.1", 0, "m.db", log=False, base_url="https://sho.rt") as server:
            self.assertIsInstance(server, ShortenerServer)
            self.assertEqual(server.server_address[0], "127.0.0.1")
            self.assertEqual(server.store_path, "m.db")
            self.assertFalse(server.log)
            self.assertEqual(server.base_url, "https://sho.rt")

    def test_skips_reverse_dns_lookup(self):
        with mock.patch("socket.getfqdn") as getfqdn, make_server("127.0.0.1", 0, "m.db", log=False) as server:
            getfqdn.assert_not_called()
            self.assertEqual(server.server_name, "127.0.0.1")
            self.assertEqual(server.server_port, server.server_address[1])

    def test_handler_uses_request_timeout(self):
        self.assertEqual(REQUEST_TIMEOUT_SECONDS, 10)
        self.assertEqual(ShortenerHandler.timeout, REQUEST_TIMEOUT_SECONDS)

    def test_base_url_defaults_to_none(self):
        with make_server("127.0.0.1", 0, "m.db", log=False) as server:
            self.assertIsNone(server.base_url)


class ServeTest(unittest.TestCase):
    def run_serve(self, *args):
        out = io.StringIO()
        with (
            mock.patch.object(ShortenerServer, "serve_forever", side_effect=KeyboardInterrupt),
            contextlib.redirect_stdout(out),
        ):
            serve("127.0.0.1", 0, "m.db", *args)
        return out.getvalue()

    def test_prints_address_and_stops_on_ctrl_c(self):
        self.assertRegex(
            self.run_serve(),
            r"Serving on http://127\.0\.0\.1:\d+ \(store: m\.db; short links: from the request's Host header\)",
        )

    def test_prints_base_url(self):
        self.assertIn("short links: https://sho.rt)", self.run_serve("https://sho.rt"))


if __name__ == "__main__":
    unittest.main()
