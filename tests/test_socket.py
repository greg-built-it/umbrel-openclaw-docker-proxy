import os
import socket as socklib
import pytest
from pathlib import Path
from unittest.mock import patch

from openclaw_docker_proxy import (
    _is_unix_socket,
    _prepare_socket_path,
    create_unix_socket,
    SOCK_DIR,
    SOCK_PATH,
    SocketLifecycleError,
)


def test_is_unix_socket_true(tmp_path):
    sock = tmp_path / "sock"
    s = socklib.socket(socklib.AF_UNIX, socklib.SOCK_STREAM)
    s.bind(str(sock))
    s.close()
    assert _is_unix_socket(sock) is True


def test_is_unix_socket_false(tmp_path):
    regular = tmp_path / "file"
    regular.write_text("not a socket")
    assert _is_unix_socket(regular) is False


def test_prepare_socket_path_removes_stale_socket(tmp_path):
    stale = tmp_path / "sock"
    s = socklib.socket(socklib.AF_UNIX, socklib.SOCK_STREAM)
    s.bind(str(stale))
    s.close()
    with patch("openclaw_docker_proxy.SOCK_PATH", stale):
        _prepare_socket_path()
        assert not stale.exists()


def test_prepare_socket_path_rejects_regular_file(tmp_path):
    regular = tmp_path / "sock"
    regular.write_text("not a socket")
    with patch("openclaw_docker_proxy.SOCK_PATH", regular):
        with pytest.raises(SocketLifecycleError):
            _prepare_socket_path()


def test_prepare_socket_path_missing_is_noop(tmp_path):
    missing = tmp_path / "nosuchsock"
    with patch("openclaw_docker_proxy.SOCK_PATH", missing):
        _prepare_socket_path()
        assert not missing.exists()


@pytest.mark.asyncio
async def test_create_unix_socket_bind_unbind(tmp_path):
    sock_dir = tmp_path / "proxy-run"
    sock_path = sock_dir / "proxy.sock"
    with patch("openclaw_docker_proxy.SOCK_DIR", sock_dir), patch("openclaw_docker_proxy.SOCK_PATH", sock_path):
        s = create_unix_socket()
        assert _is_unix_socket(sock_path) is True
        s.close()
        if sock_path.exists():
            os.unlink(sock_path)
