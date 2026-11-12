"""Input URL validation and shortening."""

from urllib.parse import urlsplit

from shortener.codes import DEFAULT_LENGTH, generate_code

MAX_URL_LENGTH = 2048
HOST_SCHEMES = {"http", "https", "ftp", "gopher"}
CREDENTIAL_SCHEMES = {"ftp"}
ALLOWED_SCHEMES = HOST_SCHEMES | {"mailto"}


class InvalidURLError(ValueError):
    """Raised when a URL is not acceptable for shortening."""


def validate_url(url: object) -> str:
    """Return `url` with surrounding whitespace stripped, or raise InvalidURLError."""
    if not isinstance(url, str):
        raise InvalidURLError(f"URL must be a string, got {type(url).__name__}")
    url = url.strip()
    if not url:
        raise InvalidURLError("URL is empty")
    if len(url) > MAX_URL_LENGTH:
        raise InvalidURLError(f"URL is longer than {MAX_URL_LENGTH} characters")
    if any(c.isspace() or not c.isprintable() for c in url):
        raise InvalidURLError("URL contains whitespace or control characters")

    try:
        parts = urlsplit(url)
    except ValueError as e:
        raise InvalidURLError(f"URL could not be parsed: {e}") from None

    if parts.scheme not in ALLOWED_SCHEMES:
        allowed = ", ".join(sorted(ALLOWED_SCHEMES))
        raise InvalidURLError(f"URL scheme must be one of {allowed}")

    if parts.scheme == "mailto":
        _validate_mailto(parts.netloc, parts.path)
    else:
        _validate_host(parts)
    return url


def _validate_host(parts) -> None:
    if not parts.hostname:
        raise InvalidURLError("URL has no host")
    if parts.scheme not in CREDENTIAL_SCHEMES and (parts.username is not None or parts.password is not None):
        raise InvalidURLError("URL must not contain credentials")
    try:
        parts.port
    except ValueError:
        raise InvalidURLError("URL has an invalid port") from None


def _validate_mailto(netloc: str, address: str) -> None:
    if netloc:
        raise InvalidURLError("mailto URL must not contain '//'")
    local, at, domain = address.partition("@")
    if not (local and at and domain) or "@" in domain:
        raise InvalidURLError("mailto URL must contain one address like name@example.com")


def shorten(url: object, length: int = DEFAULT_LENGTH) -> str:
    """Validate `url` and return a new short code for it. Nothing is stored yet."""
    validate_url(url)
    return generate_code(length)
