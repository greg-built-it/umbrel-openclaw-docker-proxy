import os
import socket as socklib
import stat
import pytest
from pathlib import Path

from openclaw_docker_proxy import (
    _is_unix_socket,
    _prepare_socket_path,
    create_unix_socket,
    SOCK_PATH,
)


def test_is_unix_socket_true(tmp_path):
    p = tmp_path / "sock"
    s = socklib.socket(socklib.AF_UNIX, socklib.SOCK_STREAM)
    s.bind(str(p))
    s.close()
    assert _is_unix_socket(p) is True


def test_is_unix_socket_false(tmp_path):
    p = tmp_path / "file"
    p.write_text("x")
    assert _is_unix_socket(p) is False


# Socket lifecycle functions operate on global SOCK_PATH; monkeypatch it for tests

def test_prepare_socket_path_removes_stale_socket(tmp_path, monkeypatch):
    p = tmp_path / "sock"
    s = socklib.socket(socklib.AF_UNIX, socklib.SOCK_STREAM)
    s.bind(str(p))
    s.close()
    monkeypatch.setattr("openclaw_docker_proxy.SOCK_PATH", p)
    monkeypatch.setattr("openclaw_docker_proxy.SOCK_DIR", tmp_path)
    _prepare_socket_path()
    assert not p.exists()


def test_prepare_socket_path_rejects_regular_file(tmp_path, monkeypatch):
    p = tmp_path / "file"
    p.write_text("x")
    monkeypatch.setattr("openclaw_docker_proxy.SOCK_PATH", p)
    monkeypatch.setattr("openclaw_docker_proxy.SOCK_DIR", tmp_path)
    from openclaw_docker_proxy import SocketLifecycleError
    with pytest.raises(SocketLifecycleError):
        _prepare_socket_path()


def test_prepare_socket_path_missing_is_noop(tmp_path, monkeypatch):
    p = tmp_path / "sock"
    monkeypatch.setattr("openclaw_docker_proxy.SOCK_PATH", p)
    monkeypatch.setattr("openclaw_docker_proxy.SOCK_DIR", tmp_path)
    _prepare_socket_path()
    assert not p.exists()


def test_create_unix_socket_bind_unbind(tmp_path, monkeypatch):
    p = tmp_path / "sock"
    monkeypatch.setattr("openclaw_docker_proxy.SOCK_PATH", p)
    monkeypatch.setattr("openclaw_docker_proxy.SOCK_DIR", tmp_path)
    sock = create_unix_socket()
    assert p.exists()
    assert stat.S_ISSOCK(os.lstat(str(p)).st_mode)
    sock.close()
    os.unlink(str(p))
