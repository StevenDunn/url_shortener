# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

A URL shortener in Python: POST a URL, get back a short code; requesting the short code redirects to the original URL. No external services.

HTTP API (JSON in/out), bound to `127.0.0.1:8000` by default:

- `POST /` with `{"url": "..."}` → `201 {"code", "url", "short_url"}`; same URL → same code. Bad input → `400 {"error"}`; no `Content-Length` → `411`; body > 8 KB → `413`.
- `GET /<code>` → `302` with `Location: <url>` (non-ASCII percent-encoded); unknown → `404`.

## Environment

- Python 3.14 (Homebrew): `python` / `python3`. Use the latest language features freely.
- Stick to the standard library (e.g. `http.server`, `urllib.parse`, `secrets`, `sqlite3`, `json`, `unittest`).
- Only if the standard library genuinely can't do something, a currently maintained pip package is acceptable — but **ask before adding any dependency**.

## Commands

Run from the repo root (no install step; there is no `pyproject.toml`):

```
python -m shortener [--host 127.0.0.1] [--port 8000] [--store urls.tsv]   # run; Ctrl-C stops
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
- `shortener/store.py`: the mapping file, one `code<TAB>url` line per mapping (UTF-8, append-only). No escaping is needed because codes are base62 and `validate_url` rejects whitespace/control characters — keep both of those true. The server re-reads the file on every GET, so mappings written by other processes show up immediately.
- **Locking**: `fcntl.flock` on the mapping file itself — `LOCK_SH` for readers, `LOCK_EX` for writers (many readers or one writer, across threads and processes; Unix-only). `shorten_and_save` does its duplicate check, code choice and append under a single exclusive lock; don't split that into separate `load_mappings` + `save_mapping` calls or concurrent requests can race.
- `shortener/urls.py`: `validate_url` (allowed schemes: http, https, ftp, gopher, mailto; credentials only allowed for ftp). `shorten()` is a pure validate-and-generate helper that stores nothing.
- `tests/` mirrors the package: one `tests/test_<module>.py` per `shortener/<module>.py`. Tests capture output with `contextlib.redirect_stdout`/`redirect_stderr`. Server tests run a real server on port 0 in a thread with a temp store and use `http.client` (it doesn't follow redirects); lock tests hold `flock` on a separate file handle, as another process would.

## Working conventions

- Keep each change small and focused.
- Add tests for every function you add or change.
- Run the full test suite after every change and report the result.
- Don't add dependencies without asking first.
