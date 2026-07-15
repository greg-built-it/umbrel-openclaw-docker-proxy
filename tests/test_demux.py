import struct
import pytest
from openclaw_docker_proxy import _demultiplex_logs, _version_to_tuple


def _make_frame(stream, payload):
    header = bytes([stream, 0, 0, 0]) + struct.pack(">I", len(payload))
    return header + payload


def test_demux_multiplex_stdout_stderr():
    out = b"stdout line" + bytes([10])
    err = b"stderr line" + bytes([10])
    frame = _make_frame(1, out) + _make_frame(2, err)
    payloads = _demultiplex_logs(frame)
    assert payloads == [out, err]


def test_demux_empty():
    assert _demultiplex_logs(b"") == []


def test_demux_truncated_header():
    assert _demultiplex_logs(bytes([1, 0, 0, 0])) == []


def test_demux_truncated_payload():
    frame = bytes([1, 0, 0, 0]) + struct.pack(">I", 100) + b"short"
    result = _demultiplex_logs(frame)
    assert result == []


def test_demux_multiple_frames():
    a = _make_frame(1, b"first")
    b = _make_frame(2, b"second")
    c = _make_frame(1, b"third")
    assert _demultiplex_logs(a + b + c) == [b"first", b"second", b"third"]


def test_version_to_tuple():
    assert _version_to_tuple("1.47") == (1, 47)
    assert _version_to_tuple("v1.47") == (1, 47)
    assert _version_to_tuple("25.0") == (25, 0)
    assert _version_to_tuple(None) is None
