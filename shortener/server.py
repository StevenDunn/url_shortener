"""HTTP server: POST a URL to get a short code, GET /<code> to be redirected."""

import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit

from shortener.codes import is_valid_code
from shortener.store import StoreBusyError, StorePath, lookup, shorten_and_save
from shortener.urls import InvalidURLError, validate_url

MAX_BODY_BYTES = 8192
# How long a client may go without sending anything before its connection is
# closed. Without it, a client that never finishes its request holds a
# server thread forever.
REQUEST_TIMEOUT_SECONDS = 10

# Every printable ASCII character except space: quote() then only escapes
# non-ASCII characters, leaving the URL's existing structure and escapes alone.
_LOCATION_SAFE = "".join(chr(i) for i in range(33, 127))

# A Host header we're willing to echo back in short_url: a hostname or
# bracketed IPv6 address, optionally with a port.
_HOST_HEADER = re.compile(r"(?:[A-Za-z0-9.-]+|\[[0-9A-Fa-f:.]+\])(?::\d{1,5})?")


def _location(url: str) -> str:
    """Make `url` safe for a Location header by percent-encoding non-ASCII characters."""
    return quote(url, safe=_LOCATION_SAFE)


def normalize_base_url(value: str) -> str:
    """Check a --base-url value and return it without a trailing slash.

    It must be an http(s) URL with a host and may include a path prefix
    (e.g. https://sho.rt/s), but no credentials, query or fragment.
    Raises ValueError otherwise.
    """
    url = validate_url(value)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise ValueError("base URL must start with http:// or https://")
    if parts.query or parts.fragment or url.endswith(("?", "#")):
        raise ValueError("base URL must not have a query or fragment")
    return url.rstrip("/")


class ShortenerHandler(BaseHTTPRequestHandler):
    server: "ShortenerServer"

    # Socket timeout for every read and write on the connection (set by StreamRequestHandler).
    # An idle connection, or one stuck sending its request line or headers, is closed by
    # http.server; a stalled body read is answered with 408 in do_POST.
    timeout = REQUEST_TIMEOUT_SECONDS

    def do_POST(self) -> None:
        if urlsplit(self.path).path != "/":
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return

        length_header = self.headers.get("Content-Length")
        if length_header is None:
            self._send_json(HTTPStatus.LENGTH_REQUIRED, {"error": "Content-Length is required"})
            return
        try:
            length = int(length_header)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid Content-Length"})
            return
        if length > MAX_BODY_BYTES:
            self._send_json(HTTPStatus.CONTENT_TOO_LARGE, {"error": f"body is larger than {MAX_BODY_BYTES} bytes"})
            return

        try:
            body = self.rfile.read(length)
        except TimeoutError:
            self.close_connection = True
            self._send_json(HTTPStatus.REQUEST_TIMEOUT, {"error": "timed out waiting for the request body"})
            return
        try:
            payload = json.loads(body)
        except ValueError:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "body must be JSON"})
            return
        if not isinstance(payload, dict) or "url" not in payload:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": 'body must be a JSON object with a "url" field'})
            return

        try:
            code = shorten_and_save(payload["url"], self.server.store_path)
        except InvalidURLError as e:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(e)})
            return
        except StoreBusyError as e:
            self._send_busy(e)
            return
        except RuntimeError as e:
            self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(e)})
            return

        self._send_json(
            HTTPStatus.CREATED,
            {"code": code, "url": payload["url"].strip(), "short_url": f"{self._base_url()}/{code}"},
        )

    def _base_url(self) -> str:
        """Where short links point: --base-url if set, else the Host the client used, else the bound address."""
        if self.server.base_url:
            return self.server.base_url
        host = self.headers.get("Host", "")
        if _HOST_HEADER.fullmatch(host):
            return f"http://{host}"
        bound_host, bound_port = self.server.server_address[:2]
        return f"http://{bound_host}:{bound_port}"

    def do_GET(self) -> None:
        code = urlsplit(self.path).path.removeprefix("/")
        try:
            url = lookup(self.server.store_path, code) if is_valid_code(code) else None
        except StoreBusyError as e:
            self._send_busy(e)
            return
        if url is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "unknown short code"})
            return
        self.send_response(HTTPStatus.FOUND)
        self.send_header("Location", _location(url))
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _send_busy(self, error: StoreBusyError) -> None:
        self._send_json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": str(error)}, {"Retry-After": "1"})

    def _send_json(self, status: HTTPStatus, body: dict, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(status)
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        if self.server.log:
            super().log_message(format, *args)


class ShortenerServer(ThreadingHTTPServer):
    # socketserver's default listen backlog of 5 resets connections in a burst.
    request_queue_size = 128

    store_path: StorePath
    log: bool
    base_url: str | None


def make_server(
    host: str, port: int, store_path: StorePath, log: bool = True, base_url: str | None = None
) -> ShortenerServer:
    """Create (but don't start) a server bound to host:port that uses `store_path`.

    `base_url` (already checked by normalize_base_url) is the prefix for short
    links in POST responses; without it, the request's Host header is used.
    """
    server = ShortenerServer((host, port), ShortenerHandler)
    server.store_path = store_path
    server.log = log
    server.base_url = base_url
    return server


def serve(host: str, port: int, store_path: StorePath, base_url: str | None = None) -> None:
    """Run the server until interrupted with Ctrl-C."""
    with make_server(host, port, store_path, base_url=base_url) as server:
        bound_host, bound_port = server.server_address[:2]
        links = base_url or "from the request's Host header"
        print(f"Serving on http://{bound_host}:{bound_port} (store: {store_path}; short links: {links})", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
