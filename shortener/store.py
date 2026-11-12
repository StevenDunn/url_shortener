"""SQLite storage of short code -> URL mappings.

One table, `urls`, with a row per mapping. The database enforces that every
code and every URL appears at most once, so callers don't have to check
before inserting.

Locking: the database runs in WAL (write-ahead log) mode, which allows many
readers and one writer at a time, and readers and the writer never block
each other. Writers queue for up to BUSY_TIMEOUT_MS; past that the store
raises StoreBusyError.
"""

import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from shortener.codes import DEFAULT_LENGTH, generate_code
from shortener.urls import validate_url

DEFAULT_STORE_PATH = "urls.db"
MAX_ATTEMPTS = 10
BUSY_TIMEOUT_MS = 5000

StorePath = str | os.PathLike[str]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    code       TEXT PRIMARY KEY,
    url        TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


class StoreBusyError(Exception):
    """Raised when the database stays locked by another writer for longer than BUSY_TIMEOUT_MS."""


def _is_busy(e: sqlite3.OperationalError) -> bool:
    return e.sqlite_errorcode in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)


@contextmanager
def _busy_as_error() -> Iterator[None]:
    """Turn SQLite's "database is locked" errors into StoreBusyError."""
    try:
        yield
    except sqlite3.OperationalError as e:
        if _is_busy(e):
            raise StoreBusyError(f"store is busy: {e}") from e
        raise


def _initialise(conn: sqlite3.Connection) -> None:
    """Enable WAL and create the table, retrying while other connections hold locks.

    Needed on a brand-new database: switching to WAL can fail with "locked"
    straight away, without waiting for the busy timeout, when several
    connections open the file at once. journal_mode is stored in the database
    file, so once it's WAL this is just two cheap no-op statements.
    """
    deadline = time.monotonic() + BUSY_TIMEOUT_MS / 1000
    while True:
        try:
            if conn.execute("PRAGMA journal_mode").fetchone()[0] != "wal":
                conn.execute("PRAGMA journal_mode = WAL")
            conn.execute(_SCHEMA)
            return
        except sqlite3.OperationalError as e:
            if not _is_busy(e) or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _connect(path: StorePath) -> sqlite3.Connection:
    """Open the database for writing, creating it and its table and enabling WAL if needed."""
    conn = sqlite3.connect(path, autocommit=True, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        _initialise(conn)
    except BaseException:
        conn.close()
        raise
    return conn


def load_mappings(path: StorePath) -> dict[str, str]:
    """Return all stored mappings as {code: url}. A missing database means no mappings."""
    path = Path(path)
    if not path.exists():
        return {}
    # Read-only, so reading never creates the database file.
    uri = f"{path.absolute().as_uri()}?mode=ro"
    with _busy_as_error(), closing(sqlite3.connect(uri, uri=True, autocommit=True, timeout=BUSY_TIMEOUT_MS / 1000)) as conn:
        try:
            return dict(conn.execute("SELECT code, url FROM urls"))
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return {}
            raise


def save_mapping(path: StorePath, code: str, url: str) -> None:
    """Store one code -> url mapping. Raises sqlite3.IntegrityError if either is already stored."""
    with _busy_as_error(), closing(_connect(path)) as conn:
        conn.execute("INSERT INTO urls (code, url) VALUES (?, ?)", (code, url))


def shorten_and_save(url: object, path: StorePath = DEFAULT_STORE_PATH, length: int = DEFAULT_LENGTH) -> str:
    """Validate `url`, store it under a new short code, and return the code.

    A URL that is already stored gets its existing code back. Everything runs
    in one BEGIN IMMEDIATE transaction, which takes the write lock up front
    (waiting up to BUSY_TIMEOUT_MS for another writer), so the insert and the
    read-back of the code see a consistent database. The UNIQUE constraints
    do the rest: an already-stored URL makes the insert a no-op, and a code
    that's already taken raises IntegrityError, so another code is tried.
    """
    url = validate_url(url)
    with _busy_as_error(), closing(_connect(path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            for _ in range(MAX_ATTEMPTS):
                try:
                    conn.execute(
                        "INSERT INTO urls (code, url) VALUES (?, ?) ON CONFLICT (url) DO NOTHING",
                        (generate_code(length), url),
                    )
                except sqlite3.IntegrityError:
                    continue
                (code,) = conn.execute("SELECT code FROM urls WHERE url = ?", (url,)).fetchone()
                conn.execute("COMMIT")
                return code
        finally:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
    raise RuntimeError(f"could not find an unused code after {MAX_ATTEMPTS} attempts")
