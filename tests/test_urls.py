import unittest

from shortener.codes import DEFAULT_LENGTH, is_valid_code
from shortener.urls import MAX_URL_LENGTH, InvalidURLError, shorten, validate_url

VALID_URLS = [
    "https://www.google.com",
    "http://example.com",
    "HTTPS://EXAMPLE.COM",
    "gopher://gopher.floodgap.com/1/world",
    "gopher://example.com:70",
    "https://example.com/path?q=1&r=2#frag",
    "ftp://example.com/file.txt",
    "ftp://user:pass@ftp.example.com",
    "mailto:a@b.com",
    "MAILTO:a@b.com?subject=Hi",
    "https://example.com:8080/",
    "http://localhost:8000",
    "http://127.0.0.1",
    "http://[::1]:8080/",
    "https://bücher.de",
    "https://example.com/a%20b",
    "  https://example.com  ",
    "https://example.com/" + "a" * (MAX_URL_LENGTH - len("https://example.com/")),
]

INVALID_URLS = [
    "",
    "   ",
    "www.google.com",
    "ssh://example.com",
    "file:///etc/passwd",
    "gopher://",
    "gopher://u:p@example.com",
    "javascript:alert(1)",
    "data:text/html,hi",
    "ftp://",
    "mailto:",
    "mailto:ab.com",
    "mailto:@b.com",
    "mailto:a@",
    "mailto:a@b@c.com",
    "mailto://a@b.com",
    "https://",
    "https:///path",
    "http://exa mple.com",
    "https://example.com/\nx",
    "https://example.com:abc",
    "https://example.com:99999",
    "https://user:pass@example.com",
    "https://www.google.com@evil.com",
    "http://[::1",
    "https://example.com/" + "a" * MAX_URL_LENGTH,
    None,
    123,
]


class ValidateUrlTest(unittest.TestCase):
    def test_accepts_valid_urls(self):
        for url in VALID_URLS:
            with self.subTest(url=url):
                self.assertEqual(validate_url(url), url.strip())

    def test_strips_surrounding_whitespace(self):
        self.assertEqual(validate_url("  https://example.com\t"), "https://example.com")

    def test_rejects_invalid_urls(self):
        for url in INVALID_URLS:
            with self.subTest(url=url), self.assertRaises(InvalidURLError):
                validate_url(url)

    def test_error_is_a_value_error(self):
        self.assertTrue(issubclass(InvalidURLError, ValueError))


class ShortenTest(unittest.TestCase):
    def test_shortens_google(self):
        code = shorten("https://www.google.com")
        self.assertTrue(is_valid_code(code))
        self.assertEqual(len(code), DEFAULT_LENGTH)

    def test_custom_length(self):
        self.assertEqual(len(shorten("https://www.google.com", length=10)), 10)

    def test_shortens_valid_urls(self):
        for url in VALID_URLS:
            with self.subTest(url=url):
                self.assertTrue(is_valid_code(shorten(url)))

    def test_rejects_invalid_urls(self):
        for url in INVALID_URLS:
            with self.subTest(url=url), self.assertRaises(InvalidURLError):
                shorten(url)


if __name__ == "__main__":
    unittest.main()
