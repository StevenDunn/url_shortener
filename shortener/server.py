"""HTTP server: POST a URL to get a short code, GET /<code> to be redirected."""

import json
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import quote, urlsplit

from shortener.codes import is_valid_code
from shortener.store import StoreBusyError, StorePath, lookup, shorten_and_save
from shortener.urls import InvalidURLError

MAX_BODY_BYTES = 8192

# Every printable ASCII character except space: quote() then only escapes
# non-ASCII characters, leaving the URL's existing structure and escapes alone.
_LOCATION_SAFE = "".join(chr(i) for i in range(33, 127))


def _location(url: str) -> str:
    """Make `url` safe for a Location header by percent-encoding non-ASCII characters."""
    return quote(url, safe=_LOCATION_SAFE)


class ShortenerHandler(BaseHTTPRequestHandler):
    server: "ShortenerServer"

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
            payload = json.loads(self.rfile.read(length))
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

        host, port = self.server.server_address[:2]
        self._send_json(
            HTTPStatus.CREATED,
            {"code": code, "url": payload["url"].strip(), "short_url": f"http://{host}:{port}/{code}"},
        )

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


def make_server(host: str, port: int, store_path: StorePath, log: bool = True) -> ShortenerServer:
    """Create (but don't start) a server bound to host:port that uses `store_path`."""
    server = ShortenerServer((host, port), ShortenerHandler)
    server.store_path = store_path
    server.log = log
    return server


def serve(host: str, port: int, store_path: StorePath) -> None:
    """Run the server until interrupted with Ctrl-C."""
    with make_server(host, port, store_path) as server:
        bound_host, bound_port = server.server_address[:2]
        print(f"Serving on http://{bound_host}:{bound_port} (store: {store_path})", flush=True)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
