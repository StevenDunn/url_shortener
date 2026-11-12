# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A URL shortener in Python: POST a URL, get back a short code; requesting the short code redirects to the original URL. No external services.

HTTP API (JSON in/out), bound to `127.0.0.1:8000` by default:

- `POST /` with `{"url": "..."}` → `201 {"code", "url", "short_url"}`; same URL → same code. Bad input → `400 {"error"}`; no `Content-Length` → `411`; body > 8 KB → `413`.
- `GET /<code>` → `302` with `Location: <url>` (non-ASCII percent-encoded); unknown → `404`.
- Store locked by another writer past the busy timeout → `503` with `Retry-After: 1`.

## Environment

- Python 3.14 (Homebrew): `python` / `python3`. Use the latest language features freely.
- Stick to the standard library (e.g. `http.server`, `urllib.parse`, `secrets`, `sqlite3`, `json`, `unittest`).
- Only if the standard library genuinely can't do something, a currently maintained pip package is acceptable — but **ask before adding any dependency**.

## Commands

Run from the repo root (no install step; there is no `pyproject.toml`):

```
python -m shortener [--host 127.0.0.1] [--port 8000] [--store urls.db]    # run; Ctrl-C stops
python -m unittest                                         # full suite
python -m unittest tests.test_cli                          # one file
python -m unittest tests.test_cli.ParseArgsTest.test_defaults   # one test

curl -X POST localhost:8000/ -d '{"url": "https://www.google.com"}'
curl -i localhost:8000/<code>
```

## Structure

- `shortener/` is the package. `__main__.py` only calls `shortener.cli.main()` and exits with its return code; keep logic out of it.
Request flow: `cli.main` → `server.serve` → `ShortenerHandler` → `store.shorten_and_save` (POST: `urls.validate_url` + `codes.generate_code`) or `store.load_mappings` (GET).

- `shortener/cli.py`: `parse_args(argv)` and `main(argv) -> int`. Both take an optional `argv` list so tests can call them directly instead of patching `sys.argv`.
- `shortener/server.py`: `ThreadingHTTPServer` (one thread per request). `make_server(host, port, store_path, log)` builds it without starting; `serve()` runs it until Ctrl-C.
- `shortener/store.py`: SQLite via stdlib `sqlite3` (default `urls.db`), one table `urls(code TEXT PRIMARY KEY, url TEXT NOT NULL UNIQUE, created_at)`. Each call opens its own connection (autocommit), so it's safe from any thread or process. Readers open the DB read-only, so a GET never creates the file.
- **Uniqueness is the DB's job**: `shorten_and_save` does `INSERT ... ON CONFLICT (url) DO NOTHING` then `SELECT code WHERE url = ?`. An existing URL makes the insert a no-op (same code back); a taken code raises `IntegrityError` → retry with a new code. Don't replace this with a read-then-insert check — that reintroduces a race.
- **Locking**: WAL mode (many readers + one writer; readers and the writer never block each other, readers see only committed data). `shorten_and_save` runs in one `BEGIN IMMEDIATE` transaction, taking the write lock up front. Writers wait up to `BUSY_TIMEOUT_MS` (5 s), then `StoreBusyError` (server → 503). `_initialise` retries the WAL switch + `CREATE TABLE` while locked, because on a brand-new DB the WAL switch can fail instantly without honouring the busy timeout when several connections open it at once.
- **Migration in progress** on branch `sqlite-storage`: (1) storage — done; (2) locking — done; (3) interface — indexed `lookup(path, code)` for GET instead of `load_mappings`; (4) `import_tsv` for old flat files (`tests/fixtures/sample_urls.tsv` was written by the pre-migration flat-file store).
- `shortener/urls.py`: `validate_url` (allowed schemes: http, https, ftp, gopher, mailto; credentials only allowed for ftp). `shorten()` is a pure validate-and-generate helper that stores nothing.
- `tests/` mirrors the package: one `tests/test_<module>.py` per `shortener/<module>.py`. Tests capture output with `contextlib.redirect_stdout`/`redirect_stderr`. Server tests run a real server on port 0 in a thread with a temp store and use `http.client` (it doesn't follow redirects). Store tests use a temp `urls.db` and check rows with their own `sqlite3` connection; lock tests hold locks through a second connection (as another process would) and patch `shortener.store.BUSY_TIMEOUT_MS` to keep timeouts short.

## Working conventions

- Keep each change small and focused.
- Add tests for every function you add or change.
- Run the full test suite after every change and report the result.
- Don't add dependencies without asking first.
