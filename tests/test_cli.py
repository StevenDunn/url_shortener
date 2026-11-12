import contextlib
import io
import unittest
from unittest import mock

from shortener.cli import DEFAULT_HOST, DEFAULT_PORT, main, parse_args
from shortener.store import DEFAULT_STORE_PATH


class ParseArgsTest(unittest.TestCase):
    def test_defaults(self):
        args = parse_args([])
        self.assertEqual(args.host, DEFAULT_HOST)
        self.assertEqual(args.port, DEFAULT_PORT)
        self.assertEqual(args.store, DEFAULT_STORE_PATH)

    def test_default_host_is_localhost(self):
        self.assertEqual(DEFAULT_HOST, "127.0.0.1")

    def test_custom_host_port_and_store(self):
        args = parse_args(["--host", "0.0.0.0", "--port", "9000", "--store", "/tmp/m.tsv"])
        self.assertEqual(args.host, "0.0.0.0")
        self.assertEqual(args.port, 9000)
        self.assertEqual(args.store, "/tmp/m.tsv")

    def test_rejects_non_integer_port(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--port", "abc"])


class MainTest(unittest.TestCase):
    def test_starts_server_with_parsed_options(self):
        with mock.patch("shortener.cli.serve") as serve:
            code = main(["--port", "9001", "--store", "m.tsv"])
        self.assertEqual(code, 0)
        serve.assert_called_once_with(DEFAULT_HOST, 9001, "m.tsv")


if __name__ == "__main__":
    unittest.main()
