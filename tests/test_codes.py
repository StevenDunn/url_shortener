import unittest

from shortener.codes import ALPHABET, DEFAULT_LENGTH, generate_code, is_valid_code


class GenerateCodeTest(unittest.TestCase):
    def test_default_length(self):
        self.assertEqual(len(generate_code()), DEFAULT_LENGTH)

    def test_custom_length(self):
        self.assertEqual(len(generate_code(12)), 12)

    def test_uses_only_alphabet(self):
        code = generate_code(200)
        self.assertTrue(set(code) <= set(ALPHABET))

    def test_codes_differ(self):
        codes = {generate_code() for _ in range(1000)}
        self.assertEqual(len(codes), 1000)

    def test_rejects_non_positive_length(self):
        for length in (0, -1):
            with self.subTest(length=length), self.assertRaises(ValueError):
                generate_code(length)


class IsValidCodeTest(unittest.TestCase):
    def test_accepts_generated_code(self):
        self.assertTrue(is_valid_code(generate_code()))

    def test_accepts_letters_and_digits(self):
        self.assertTrue(is_valid_code("aZ09"))

    def test_rejects_empty(self):
        self.assertFalse(is_valid_code(""))

    def test_rejects_other_characters(self):
        for code in ("abc-def", "abc_def", "abc/def", "abc def", "ab%20", "é"):
            with self.subTest(code=code):
                self.assertFalse(is_valid_code(code))


if __name__ == "__main__":
    unittest.main()
