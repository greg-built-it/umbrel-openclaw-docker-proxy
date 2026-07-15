import pytest
from openclaw_docker_proxy import _version_to_tuple, _version_in_range


def test_version_to_tuple_numeric():
    assert _version_to_tuple("1.47") == (1, 47)
    assert _version_to_tuple("v1.47") == (1, 47)
    assert _version_to_tuple("25.0.1") == (25, 0, 1)


def test_version_to_tuple_none():
    assert _version_to_tuple(None) is None


def test_version_in_range_normal():
    assert _version_in_range("v1.47", "1.45", "1.48") is True
    assert _version_in_range("v1.47", "1.47", "1.47") is True
    assert _version_in_range("v1.47", "1.48", "1.50") is False
    assert _version_in_range("v1.47", "1.40", "1.46") is False


def test_version_in_range_none():
    assert _version_in_range("v1.47", None, "1.48") is True
    assert _version_in_range("v1.47", "1.40", None) is True
    assert _version_in_range("v1.47", None, None) is True
