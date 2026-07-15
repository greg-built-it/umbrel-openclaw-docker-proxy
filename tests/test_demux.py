import struct
import pytest
from openclaw_docker_proxy import _demultiplex_logs, _version_to_tuple


def _make_frame(stream, payload):
    header = bytes([stream, 0, 0, 0]) + struct.pack(">I", len(payload))
    return header + payload


def test_demux_multiplex_stdout_stderr():
    frames = _make_frame(1, b"stdout1") + _make_frame(2, b"stderr1") + _make_frame(1, b"stdout2")
    lines = _demultiplex_logs(frames)
    assert lines == [b"stdout1", b"stderr1", b"stdout2"]


def test_demux_empty():
    assert _demultiplex_logs(b"") == []


def test_demux_truncated_header():
    assert _demultiplex_logs(b"\x01\x00\x00") == []


def test_demux_truncated_payload():
    assert _demultiplex_logs(b"\x01\x00\x00\x00\x00\x00\x00\x05ab") == []


def test_demux_multiple_frames():
    payload = b"line1\nline2"
    raw = _make_frame(1, payload)
    assert _demultiplex_logs(raw) == [payload]


def test_version_to_tuple():
    assert _version_to_tuple("v1.47") == (1, 47)
    assert _version_to_tuple("1.47") == (1, 47)
