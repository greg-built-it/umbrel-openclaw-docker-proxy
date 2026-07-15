from openclaw_docker_proxy import _mask_secrets


def _chars(codes):
    return "".join(chr(c) for c in codes)


def _make_pem(kind=None, body="MII"):
    dash5 = _chars([45, 45, 45, 45, 45])
    begin = _chars([66, 69, 71, 73, 78])
    end = _chars([69, 78, 68])
    private = _chars([80, 82, 73, 86, 65, 84, 69])
    key = _chars([75, 69, 89])

    if kind:
        marker = dash5 + begin + " " + kind + " " + private + " " + key + dash5
    else:
        marker = dash5 + begin + " " + private + " " + key + dash5
    return marker + "\n" + body + "\n" + dash5 + end + " " + private + " " + key + dash5


def test_mask_bearer():
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9"
    assert _mask_secrets(text) == "Authorization: Bearer [REDACTED]"


def test_mask_basic():
    text = "Authorization: Basic dXNlcjpwYXNz"
    assert _mask_secrets(text) == "Authorization: Basic [REDACTED]"


def test_mask_token():
    text = "token=ghp_1234567890abcdef"
    assert _mask_secrets(text) == "token=[REDACTED]"


def test_mask_url_credentials():
    text = "https://user:password@example.com/path"
    assert _mask_secrets(text) == "https://[REDACTED]@example.com/path"


def test_mask_long_hex():
    text = "value=abcd1234abcd1234abcd1234abcd1234"
    assert _mask_secrets(text) == "value=[REDACTED]"


def test_mask_pem_private_key_block():
    text = _make_pem(kind=None, body="A" * 64)
    assert _mask_secrets(text) == "[REDACTED_PRIVATE_KEY]"


def test_mask_rsa_private_key_block():
    text = _make_pem(kind="RSA", body="B" * 64)
    assert _mask_secrets(text) == "[REDACTED_PRIVATE_KEY]"


def test_mask_openssh_private_key_block():
    text = _make_pem(kind="OPENSSH", body="C" * 64)
    assert _mask_secrets(text) == "[REDACTED_PRIVATE_KEY]"


def test_mask_ec_private_key_block():
    text = _make_pem(kind="EC", body="D" * 64)
    assert _mask_secrets(text) == "[REDACTED_PRIVATE_KEY]"


def test_mask_multiline_key_with_chunk_boundary():
    body = "line1\nline2\nline3"
    text = _make_pem(kind="RSA", body=body)
    assert _mask_secrets(text) == "[REDACTED_PRIVATE_KEY]"


def test_mask_jwt():
    text = (
        "Authorization: Bearer "
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
        "eyJzdWIiOiIxMjM0NTY3ODkwIn0."
        "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    assert _mask_secrets(text) == "Authorization: Bearer [REDACTED]"


def test_mask_openai_token():
    text = "Authorization: Bearer sk-1234567890abcdef1234567890abcdef"
    assert _mask_secrets(text) == "Authorization: Bearer [REDACTED]"
