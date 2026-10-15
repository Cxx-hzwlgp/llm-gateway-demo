"""Gateway API key generation, hashing and display hints."""

import hashlib
import re
import secrets

KEY_PREFIX = "lgw-"
SUB_MAX_LENGTH = 512
KEY_MAX_LENGTH = 128
_HINT_EDGE = 4
_SECRET_RE = re.compile(r"[A-Za-z0-9_-]+")
_SUB_RE = re.compile(r"[a-zA-Z0-9_-]+")


def generate_key():
    """Return a new gateway API key.

    The key is ``lgw-`` followed by :func:`secrets.token_urlsafe` of
    32 bytes. Callers show this value once and store only its hash.

    Returns
    -------
    str
        Raw API key.
    """
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(api_key):
    """Return the hex SHA-256 digest of ``api_key``.

    Parameters
    ----------
    api_key : str
        Raw gateway API key.

    Returns
    -------
    str
        64-character hex digest.
    """
    digest = hashlib.sha256(api_key.encode("utf-8"))
    return digest.hexdigest()


def key_hint(api_key):
    """Return a masked form such as ``lgw-ab12…yz89``.

    Parameters
    ----------
    api_key : str
        Raw gateway API key that passed :func:`is_key_format`.

    Returns
    -------
    str
        Prefix, the first four secret characters, an ellipsis and the
        last four secret characters.
    """
    secret = api_key[len(KEY_PREFIX) :]
    return f"{KEY_PREFIX}{secret[:_HINT_EDGE]}\u2026{secret[-_HINT_EDGE:]}"


def is_key_format(api_key):
    """Return whether ``api_key`` looks like a gateway key.

    Parameters
    ----------
    api_key : str
        Candidate raw key.

    Returns
    -------
    bool
        True when the key has the ``lgw-`` prefix, a URL-safe secret
        long enough to build a hint, and is at most 128 characters.
    """
    if (
        not isinstance(api_key, str)
        or len(api_key) > KEY_MAX_LENGTH
        or not api_key.startswith(KEY_PREFIX)
    ):
        return False
    secret = api_key[len(KEY_PREFIX) :]
    if len(secret) < _HINT_EDGE * 2:
        return False
    return _SECRET_RE.fullmatch(secret) is not None


def validate_user_sub(sub):
    """Return an error message when ``sub`` cannot be a DeepSeek user id.

    DeepSeek requires ``user_id`` to match ``[a-zA-Z0-9_-]+`` and to be
    at most 512 characters.

    Parameters
    ----------
    sub : str
        Identity provider subject.

    Returns
    -------
    str or None
        Error message, or None when ``sub`` is acceptable.
    """
    if (
        not isinstance(sub, str)
        or len(sub) > SUB_MAX_LENGTH
        or _SUB_RE.fullmatch(sub) is None
    ):
        return (
            "user sub must match [a-zA-Z0-9-_]+ and be at most 512 characters"
        )
    return None
