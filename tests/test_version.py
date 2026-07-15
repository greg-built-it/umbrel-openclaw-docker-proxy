import pytest
from openclaw_docker_proxy import _version_to_tuple, _version_in_range


def test_version_to_tuple_numeric():
    assert _version_to_tuple("1.47") == (1, 47)
    assert _version_to_tuple("v1.47") == (1, 47)


def test_version_to_tuple_none():
    assert _version_to_tuple(None) is None


def test_version_in_range_normal():
    assert _version_in_range("1.47", "1.24", "1.51") is True
    assert _version_in_range("v1.47", "1.24", "1.51") is True
    assert _version_in_range("1.23", "1.24", "1.51") is False
    assert _version_in_range("1.52", "1.24", "1.51") is False


def test_version_in_range_none():
    assert _version_in_range("1.47", None, "1.51") is True
    assert _version_in_range("1.47", "1.24", None) is True
    assert _version_in_range(None, "1.24", "1.51") is False
