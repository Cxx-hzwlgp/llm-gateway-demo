"""Key format, hash and hint."""

import hashlib
import re

from lgw.keys import (
    generate_key,
    hash_key,
    is_key_format,
    key_hint,
    validate_user_sub,
)


def test_generate_key_format_and_hash():
    api_key = generate_key()
    assert is_key_format(api_key)
    assert api_key.startswith("lgw-")
    assert re.fullmatch(r"lgw-[A-Za-z0-9_-]{8,}", api_key)
    digest = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
    assert hash_key(api_key) == digest
    assert len(digest) == 64


def test_key_hint_keeps_edges():
    api_key = "lgw-" + ("a" * 20) + "wxyz"
    assert key_hint(api_key) == "lgw-aaaa\u2026wxyz"


def test_key_format_rejects_short_or_foreign_keys():
    assert not is_key_format("sk-12345678")
    assert not is_key_format("lgw-short")
    assert not is_key_format("lgw-has space-xxxx")
    assert not is_key_format("lgw-" + ("a" * 200))
    assert not is_key_format(None)


def test_user_sub_rules():
    assert validate_user_sub("user_1-a") is None
    assert validate_user_sub("a" * 512) is None
    assert validate_user_sub("a" * 513) is not None
    assert validate_user_sub("has space") is not None
    assert validate_user_sub("") is not None
