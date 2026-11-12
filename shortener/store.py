"""SQLite storage of short code -> URL mappings.

One table, `urls`, with a row per mapping. The database enforces that every
code and every URL appears at most once, so callers don't have to check
before inserting.
"""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

from shortener.codes import DEFAULT_LENGTH, generate_code
from shortener.urls import validate_url

DEFAULT_STORE_PATH = "urls.db"
MAX_ATTEMPTS = 10

StorePath = str | os.PathLike[str]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS urls (
    code       TEXT PRIMARY KEY,
    url        TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


def _connect(path: StorePath) -> sqlite3.Connection:
    """Open the database for writing, creating it and its table if needed."""
    conn = sqlite3.connect(path, autocommit=True)
    conn.execute(_SCHEMA)
    return conn


def load_mappings(path: StorePath) -> dict[str, str]:
    """Return all stored mappings as {code: url}. A missing database means no mappings."""
    path = Path(path)
    if not path.exists():
        return {}
    # Read-only, so reading never creates the database file.
    with closing(sqlite3.connect(f"{path.absolute().as_uri()}?mode=ro", uri=True, autocommit=True)) as conn:
        try:
            return dict(conn.execute("SELECT code, url FROM urls"))
        except sqlite3.OperationalError as e:
            if "no such table" in str(e):
                return {}
            raise


def save_mapping(path: StorePath, code: str, url: str) -> None:
    """Store one code -> url mapping. Raises sqlite3.IntegrityError if either is already stored."""
    with closing(_connect(path)) as conn:
        conn.execute("INSERT INTO urls (code, url) VALUES (?, ?)", (code, url))


def shorten_and_save(url: object, path: StorePath = DEFAULT_STORE_PATH, length: int = DEFAULT_LENGTH) -> str:
    """Validate `url`, store it under a new short code, and return the code.

    A URL that is already stored gets its existing code back. The UNIQUE
    constraints make this safe under concurrency: an already-stored URL makes
    the insert a no-op, and a code that's already taken raises IntegrityError,
    so another code is tried.
    """
    url = validate_url(url)
    with closing(_connect(path)) as conn:
        for _ in range(MAX_ATTEMPTS):
            try:
                conn.execute(
                    "INSERT INTO urls (code, url) VALUES (?, ?) ON CONFLICT (url) DO NOTHING",
                    (generate_code(length), url),
                )
            except sqlite3.IntegrityError:
                continue
            (code,) = conn.execute("SELECT code FROM urls WHERE url = ?", (url,)).fetchone()
            return code
    raise RuntimeError(f"could not find an unused code after {MAX_ATTEMPTS} attempts")
