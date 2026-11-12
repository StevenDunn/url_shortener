"""Flat-file storage of short code -> URL mappings.

The file holds one `code<TAB>url` line per mapping. Tabs and newlines can't
appear in either field: codes are letters and digits only, and validate_url
rejects whitespace and control characters.

Access is locked with flock on the file itself: readers take a shared lock,
writers an exclusive one, so many readers or one writer at a time, across
threads and processes. flock is Unix-only.
"""

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

from shortener.codes import DEFAULT_LENGTH, generate_code
from shortener.urls import validate_url

DEFAULT_STORE_PATH = "urls.tsv"
MAX_ATTEMPTS = 10

StorePath = str | os.PathLike[str]


@contextmanager
def _locked(path: StorePath, mode: str, lock: int) -> Iterator[TextIO]:
    """Open `path` and hold `lock` (fcntl.LOCK_SH or LOCK_EX) until the file is closed."""
    with open(path, mode, encoding="utf-8") as f:
        fcntl.flock(f, lock)
        yield f


def _parse(text: str) -> dict[str, str]:
    mappings = {}
    for line in text.splitlines():
        if line:
            code, url = line.split("\t", 1)
            mappings[code] = url
    return mappings


def load_mappings(path: StorePath) -> dict[str, str]:
    """Return all stored mappings as {code: url}. A missing file means no mappings."""
    try:
        with _locked(path, "r", fcntl.LOCK_SH) as f:
            return _parse(f.read())
    except FileNotFoundError:
        return {}


def save_mapping(path: StorePath, code: str, url: str) -> None:
    """Append one code -> url mapping, creating the file if needed."""
    with _locked(path, "a", fcntl.LOCK_EX) as f:
        f.write(f"{code}\t{url}\n")


def shorten_and_save(url: object, path: StorePath = DEFAULT_STORE_PATH, length: int = DEFAULT_LENGTH) -> str:
    """Validate `url`, store it under a new short code, and return the code.

    A URL that is already stored gets its existing code back. The lookup and
    the append happen under one exclusive lock, so concurrent calls can't
    store the same URL twice or reuse a code.
    """
    url = validate_url(url)
    with _locked(path, "a+", fcntl.LOCK_EX) as f:
        f.seek(0)
        mappings = _parse(f.read())
        for code, stored_url in mappings.items():
            if stored_url == url:
                return code

        for _ in range(MAX_ATTEMPTS):
            code = generate_code(length)
            if code not in mappings:
                f.write(f"{code}\t{url}\n")
                return code
    raise RuntimeError(f"could not find an unused code after {MAX_ATTEMPTS} attempts")
