# url_shortener

[![Tests](https://github.com/StevenDunn/url_shortener/actions/workflows/tests.yml/badge.svg)](https://github.com/StevenDunn/url_shortener/actions/workflows/tests.yml)

A small URL shortener written in Python using only the standard library. POST a URL and get back a short code; visiting `/<code>` redirects to the original URL. Mappings are stored in a local SQLite database.

It is meant for running on your own machine: it listens on `127.0.0.1` by default and has no authentication or rate limiting.

## Requirements

- Python 3.14 (what CI tests on Ubuntu and macOS). Python 3.13 should also work but isn't tested; 3.12 and older won't.
- Nothing to install — no third-party packages.

## Quick start

From the repository root:

```sh
python -m shortener
```

```
Serving on http://127.0.0.1:8000 (store: urls.db; short links: from the request's Host header)
```

In another terminal:

```sh
curl -X POST http://127.0.0.1:8000/ -d '{"url": "https://www.python.org/downloads/"}'
```

```json
{"code": "6nnZaCO", "url": "https://www.python.org/downloads/", "short_url": "http://127.0.0.1:8000/6nnZaCO"}
```

Open the `short_url` in a browser, or:

```sh
curl -i http://127.0.0.1:8000/6nnZaCO
```

```
HTTP/1.0 302 Found
Location: https://www.python.org/downloads/
```

Press Ctrl-C to stop the server. Mappings stay in `urls.db` for next time.

## Options

| Option | Default | Description |
|---|---|---|
| `--host HOST` | `127.0.0.1` | Address to listen on. |
| `--port PORT` | `8000` | Port to listen on. |
| `--store FILE` | `urls.db` | SQLite database to read and write. Created on first write. |
| `--base-url URL` | from the request's `Host` header | Prefix for the `short_url` in responses, e.g. `https://sho.rt`. Must be `http(s)`; may include a path; no query or fragment. |
| `--import FILE` | — | Import a file from the old flat-file store into `--store`, then exit (see [Importing old flat files](#importing-old-flat-files)). |

## API

All responses other than redirects are JSON.

### `POST /` — shorten a URL

Request body: `{"url": "<url>"}`.

| Response | When |
|---|---|
| `201 {"code", "url", "short_url"}` | Shortened. Submitting a URL that's already stored returns its existing code. |
| `400 {"error"}` | Body isn't a JSON object with a `url` string, or the URL is invalid. |
| `408` | The client stopped sending the body (10 s timeout). |
| `411` / `413` | No `Content-Length` / body larger than 8 KB. |
| `503` + `Retry-After: 1` | The database stayed locked by another writer for more than 5 s. |
| `500 {"error": "internal server error"}` | Unexpected error; details go to the server log. |

Accepted URLs:

- Schemes `http`, `https`, `ftp`, `gopher` and `mailto` (so `javascript:`, `data:`, `file:` and others are rejected).
- `http`/`https`/`ftp`/`gopher` need a host; a port, if given, must be valid. Credentials (`user:pass@`) are only allowed for `ftp`, which blocks lookalikes such as `https://www.google.com@evil.com`.
- `mailto` needs exactly one `name@domain` address.
- At most 2048 characters, with no spaces or control characters. Surrounding whitespace is stripped.

Short codes are 7 random letters and digits (about 3.5 trillion possibilities), generated with `secrets`.

### `GET /<code>` — follow a short link

| Response | When |
|---|---|
| `302` + `Location: <url>` | Known code. Non-ASCII characters in the URL are percent-encoded. |
| `404 {"error"}` | Unknown or malformed code. |
| `503` / `500` | As for POST. |

`HEAD /<code>` returns the same status and headers without a body.

## Storage

Mappings live in one SQLite table, `urls(code PRIMARY KEY, url UNIQUE, created_at)`:

- The database enforces that each code and each URL appears once, so concurrent requests can't create duplicates.
- It runs in WAL mode: many readers and one writer at a time, and reads never wait for a write in progress.
- Redirects look up a single code by its primary key.

Inspect it with the `sqlite3` command-line tool:

```sh
sqlite3 urls.db 'SELECT code, url, created_at FROM urls'
```

### Importing old flat files

Before SQLite, mappings were stored one per line as `code<TAB>url` in `urls.tsv`. To bring such a file into the database:

```sh
python -m shortener --import urls.tsv [--store urls.db]
```

```
Imported 10 new mapping(s) from urls.tsv into urls.db
```

- Original codes are kept, so short links that were already shared keep working.
- Mappings that are already stored exactly are skipped, so running an import twice is safe.
- Anything else is all-or-nothing: a malformed line, an invalid code or URL, or a code or URL already stored with a different partner fails the import with the line number (exit code 1), and nothing is imported.

## Development

```sh
python -m unittest                                              # full suite
python -m unittest tests.test_store                             # one file
python -m unittest tests.test_cli.ParseArgsTest.test_defaults   # one test
```

GitHub Actions runs the full suite on every pull request and every push to `main` (`.github/workflows/tests.yml`).

Project layout:

| Path | Contents |
|---|---|
| `shortener/cli.py` | Command-line options; starts the server or runs an import. |
| `shortener/server.py` | HTTP server (`http.server.ThreadingHTTPServer`, one thread per request). |
| `shortener/store.py` | SQLite storage, locking and the flat-file import. |
| `shortener/urls.py` | Input URL validation. |
| `shortener/codes.py` | Short code generation and validation. |
| `tests/` | One `test_<module>.py` per module; `tests/fixtures/sample_urls.tsv` is sample data in the old flat-file format. |

`CLAUDE.md` has more detail on the design and conventions for working in this repository.
