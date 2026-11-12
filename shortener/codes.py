"""Short code generation and validation."""

import secrets
import string

ALPHABET = string.ascii_letters + string.digits
DEFAULT_LENGTH = 7


def generate_code(length: int = DEFAULT_LENGTH) -> str:
    """Return a random short code of `length` characters drawn from ALPHABET."""
    if length < 1:
        raise ValueError(f"length must be at least 1, got {length}")
    return "".join(secrets.choice(ALPHABET) for _ in range(length))


def is_valid_code(code: str) -> bool:
    """Return True if `code` is non-empty and uses only ALPHABET characters."""
    return bool(code) and all(c in ALPHABET for c in code)
