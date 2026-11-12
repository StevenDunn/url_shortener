import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from shortener.cli import DEFAULT_HOST, DEFAULT_PORT, main, parse_args
from shortener.store import DEFAULT_STORE_PATH, load_mappings
from tests.test_store import SAMPLE_MAPPINGS, SAMPLE_TSV


class ParseArgsTest(unittest.TestCase):
    def test_defaults(self):
        args = parse_args([])
        self.assertEqual(args.host, DEFAULT_HOST)
        self.assertEqual(args.port, DEFAULT_PORT)
        self.assertEqual(args.store, DEFAULT_STORE_PATH)
        self.assertIsNone(args.import_file)
        self.assertIsNone(args.base_url)

    def test_base_url_is_normalized(self):
        self.assertEqual(parse_args(["--base-url", "https://sho.rt/"]).base_url, "https://sho.rt")

    def test_rejects_invalid_base_url(self):
        for value in ("sho.rt", "ftp://sho.rt", "https://sho.rt?x=1"):
            with self.subTest(value=value):
                err = io.StringIO()
                with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
                    parse_args(["--base-url", value])
                self.assertIn("--base-url", err.getvalue())

    def test_default_host_is_localhost(self):
        self.assertEqual(DEFAULT_HOST, "127.0.0.1")

    def test_custom_host_port_and_store(self):
        args = parse_args(["--host", "0.0.0.0", "--port", "9000", "--store", "/tmp/m.db"])
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 9000)
        self.assertEqual(args.store, "/tmp/m.db")

    def test_import_file(self):
        self.assertEqual(parse_args(["--import", "old.tsv"]).import_file, "old.tsv")

    def test_rejects_non_integer_port(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--port", "abc"])


class MainTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.db = str(Path(tmp.name) / "urls.db")

    def run_main(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("shortener.cli.serve") as serve, contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue(), serve

    def test_starts_server_with_parsed_options(self):
        code, _, _, serve = self.run_main(["--port", "9001", "--store", "m.db"])
        self.assertEqual(code, 0)
        serve.assert_called_once_with(DEFAULT_HOST, 9001, "m.db", None)

    def test_passes_base_url_to_server(self):
        _, _, _, serve = self.run_main(["--base-url", "https://sho.rt/"])
        serve.assert_called_once_with(DEFAULT_HOST, DEFAULT_PORT, DEFAULT_STORE_PATH, "https://sho.rt")

    def test_import_loads_file_and_does_not_serve(self):
        code, out, _, serve = self.run_main(["--import", str(SAMPLE_TSV), "--store", self.db])
        self.assertEqual(code, 0)
        self.assertIn("Imported 10 new mapping(s)", out)
        self.assertEqual(load_mappings(self.db), SAMPLE_MAPPINGS)
        serve.assert_not_called()

    def test_import_twice_reports_nothing_new(self):
        self.run_main(["--import", str(SAMPLE_TSV), "--store", self.db])
        code, out, _, _ = self.run_main(["--import", str(SAMPLE_TSV), "--store", self.db])
        self.assertEqual(code, 0)
        self.assertIn("Imported 0 new mapping(s)", out)

    def test_import_failure_returns_1_with_message(self):
        bad = Path(self.db).with_name("bad.tsv")
        bad.write_text("AAAAAAA\tjavascript:alert(1)\n", encoding="utf-8")
        for name, path in {"bad line": bad, "missing file": bad.with_name("missing.tsv")}.items():
            with self.subTest(name):
                code, _, err, serve = self.run_main(["--import", str(path), "--store", self.db])
                self.assertEqual(code, 1)
                self.assertIn("import failed", err)
                serve.assert_not_called()
        self.assertFalse(Path(self.db).exists())


if __name__ == "__main__":
    unittest.main()
