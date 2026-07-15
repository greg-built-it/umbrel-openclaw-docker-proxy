import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
import httpx

from openclaw_docker_proxy import (
    _docker_error_response,
    _docker_path,
    _stream_with_limit,
    _read_limited_json,
    DOCKER_API_VERSION,
    DOCKER_TOTAL_TIMEOUT,
    DOCKER_SOCKET_PATH,
)


def test_docker_error_response_structure():
    resp = _docker_error_response("test_error", "test detail")
    body = resp.body.decode("utf-8")
    assert '"code":"test_error"' in body or '"code": "test_error"' in body
    assert '"message":"test detail"' in body or '"message": "test detail"' in body


def test_docker_path_builds_correctly():
    assert _docker_path("containers", "openclaw_gateway_1", "logs") == "/v1.47/containers/openclaw_gateway_1/logs"


def test_total_timeout_is_15s():
    import openclaw_docker_proxy as mod
    assert mod.DOCKER_TOTAL_TIMEOUT == 15.0


@pytest.mark.asyncio
async def test_read_limited_json_within_limit():
    class FakeResp:
        async def aiter_bytes(self, chunk_size=None):
            yield b'{"key": "value"}'

    data = await _read_limited_json(FakeResp(), limit=100)
    assert data == {"key": "value"}


@pytest.mark.asyncio
async def test_read_limited_json_exceeds_limit():
    class FakeResp:
        async def aiter_bytes(self, chunk_size=None):
            yield b'{"key": "' + b"x" * 200 + b'"}'

    with pytest.raises(RuntimeError):
        await _read_limited_json(FakeResp(), limit=100)


@pytest.mark.asyncio
async def test_stream_with_limit_respects_total_size():
    sem = asyncio.Semaphore(1)

    class FakeResponse:
        def __init__(self):
            self._chunks = [b"x" * 50, b"x" * 100]
            self._idx = 0
        async def aiter_bytes(self, chunk_size=None):
            while self._idx < len(self._chunks):
                chunk = self._chunks[self._idx]
                self._idx += 1
                yield chunk
        def raise_for_status(self):
            pass
        async def aclose(self):
            pass

    class FakeClient:
        def stream(self, method, path):
            class Ctx:
                async def __aenter__(self):
                    return FakeResponse()
                async def __aexit__(self, *args):
                    pass
            return Ctx()

    chunks = []
    with pytest.raises(RuntimeError):
        async for chunk in _stream_with_limit(FakeClient(), sem, "GET", "/x", 80):
            chunks.append(chunk)
    assert sum(len(c) for c in chunks) <= 80
