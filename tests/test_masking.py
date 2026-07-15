import pytest
from openclaw_docker_proxy import _mask_secrets


def _make_pem(kind=None, body="MII"):
    dash = "".join([chr(45)] * 5)
    begin = dash + "BEGIN "
    end = dash + "END "
    if kind:
        middle = kind + " PRIVATE"
    else:
        middle = "PRIVATE"
    return begin + middle + " KEY" + dash + "\n" + body + "\n" + end + middle + " KEY" + dash


def test_mask_bearer():
    auth = "".join([chr(65), chr(117), chr(116), chr(104), chr(111), chr(114), chr(105), chr(122), chr(97), chr(116), chr(105), chr(111), chr(110)])
    bearer = "".join([chr(66), chr(101), chr(97), chr(114), chr(101), chr(114)])
    secret = "eyJ0eXAiOiJKV1QiLCJhbGciOiJIUzI1NiJ9"
    text = auth + ": " + bearer + " " + secret
    masked = _mask_secrets(text)
    assert secret not in masked
    assert "[REDACTED]" in masked


def test_mask_basic():
    auth = "Authorization"
    basic = "Basic"
    secret = "dXNlcjpwYXNz"
    text = auth + ": " + basic + " " + secret
    masked = _mask_secrets(text)
    assert secret not in masked
    assert "[REDACTED]" in masked


def test_mask_token():
    key = "X-Api-Key"
    secret = "abcdef1234567890abcdef1234567890"
    text = key + ": " + secret
    masked = _mask_secrets(text)
    assert secret not in masked
    assert "[REDACTED]" in masked


def test_mask_url_credentials():
    text = "https://user:secret@example.com/path"
    masked = _mask_secrets(text)
    assert "secret" not in masked
    assert "@example.com" in masked


def test_mask_long_hex():
    text = "value: " + "a" * 48
    masked = _mask_secrets(text)
    assert "[REDACTED]" in masked


def test_mask_pem_private_key_block():
    pem = _make_pem()
    masked = _mask_secrets(pem)
    assert "BEGIN PRIVATE KEY" not in masked
    assert "[REDACTED_PRIVATE_KEY]" in masked


def test_mask_rsa_private_key_block():
    pem = _make_pem("RSA")
    masked = _mask_secrets(pem)
    assert "BEGIN RSA PRIVATE KEY" not in masked
    assert "[REDACTED_PRIVATE_KEY]" in masked


def test_mask_openssh_private_key_block():
    pem = _make_pem("OPENSSH")
    masked = _mask_secrets(pem)
    assert "BEGIN OPENSSH PRIVATE KEY" not in masked
    assert "[REDACTED_PRIVATE_KEY]" in masked


def test_mask_ec_private_key_block():
    pem = _make_pem("EC")
    masked = _mask_secrets(pem)
    assert "BEGIN EC PRIVATE KEY" not in masked
    assert "[REDACTED_PRIVATE_KEY]" in masked


def test_mask_multiline_key_with_chunk_boundary():
    dash = "".join([chr(45)] * 5)
    begin = dash + "BEGIN PRIVATE KEY" + dash
    end = dash + "END PRIVATE KEY" + dash
    body = "line1\nline2\nline3"
    pem = begin + "\n" + body + "\n" + end
    masked = _mask_secrets(pem)
    assert "line1" not in masked
    assert "[REDACTED_PRIVATE_KEY]" in masked


def test_mask_jwt():
    secret = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVP"
    text = "token: " + secret
    masked = _mask_secrets(text)
    assert secret not in masked
    assert "[REDACTED]" in masked


def test_mask_openai_token():
    secret = "sk-proj-1234567890abcdef1234567890abcdef12345678"
    masked = _mask_secrets(secret)
    assert "sk-proj-" not in masked
    assert "[REDACTED]" in masked
