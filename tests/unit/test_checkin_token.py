# tests/unit/test_checkin_token.py
import uuid

import pytest

from app.utils.checkin_token import InvalidCheckinToken, make_token, parse_token

KEY = b"k" * 32
S, U = uuid.uuid4(), uuid.uuid4()


def test_roundtrip():
    token = make_token(S, U, key=KEY)
    assert token.startswith("SR1.")
    assert len(token) < 80
    assert parse_token(token, key=KEY) == (S, U)


def test_every_char_flip_is_rejected():
    token = make_token(S, U, key=KEY)
    for i, ch in enumerate(token):
        if ch == ".":
            continue
        repl = "A" if ch != "A" else "B"
        tampered = token[:i] + repl + token[i + 1:]
        with pytest.raises(InvalidCheckinToken):
            parse_token(tampered, key=KEY)


def test_wrong_key_bad_signature():
    token = make_token(S, U, key=KEY)
    with pytest.raises(InvalidCheckinToken) as e:
        parse_token(token, key=b"x" * 32)
    assert e.value.reason == "bad_signature"


def test_other_version_unsupported():
    token = make_token(S, U, key=KEY).replace("SR1.", "SR2.", 1)
    with pytest.raises(InvalidCheckinToken) as e:
        parse_token(token, key=KEY)
    assert e.value.reason == "unsupported_version"


@pytest.mark.parametrize(
    "garbage", ["", "SR1", "SR1..", "SR1.a.b.c", "hello world", "SR1.!!!.???", "x" * 500]
)
def test_garbage_malformed(garbage):
    with pytest.raises(InvalidCheckinToken) as e:
        parse_token(garbage, key=KEY)
    assert e.value.reason == "malformed"


def test_default_key_from_settings():
    token = make_token(S, U)
    assert parse_token(token) == (S, U)
