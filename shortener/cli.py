"""Command-line entry point."""

import argparse
import sys

from shortener.server import serve
from shortener.store import DEFAULT_STORE_PATH, StoreBusyError, import_tsv

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line options for the shortener server."""
    parser = argparse.ArgumentParser(prog="shortener", description="Run the URL shortener.")
    parser.add_argument("--host", default=DEFAULT_HOST, help=f"address to bind (default: {DEFAULT_HOST})")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"port to listen on (default: {DEFAULT_PORT})")
    parser.add_argument(
        "--store", default=DEFAULT_STORE_PATH, help=f"SQLite database to read and write (default: {DEFAULT_STORE_PATH})"
    )
    parser.add_argument(
        "--import",
        dest="import_file",
        metavar="FILE",
        help="import a code<TAB>url file from the old flat-file store into --store, then exit",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Run the shortener server, or import a flat file with --import. Returns a process exit code."""
    args = parse_args(argv)
    if args.import_file is not None:
        try:
            added = import_tsv(args.import_file, args.store)
        except (OSError, ValueError, StoreBusyError) as e:
            print(f"shortener: import failed: {e}", file=sys.stderr)
            return 1
        print(f"Imported {added} new mapping(s) from {args.import_file} into {args.store}")
        return 0
    serve(args.host, args.port, args.store)
    return 0
