"""Command-line entry point."""

import argparse

from shortener.server import serve
from shortener.store import DEFAULT_STORE_PATH

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options for the shortener server."""
    parser = argparse.ArgumentParser(prog="shortener", description="Run the URL shortener.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"address to bind (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument(
        "--store", default=DEFAULT_STORE_PATH, help=f"mapping file to read and write (default: {DEFAULT_STORE_PATH})"
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the shortener server. Returns a process exit code."""
    args = parse_args(argv)
    serve(args.host, args.port, args.store)
    return 0
