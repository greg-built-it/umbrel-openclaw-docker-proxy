import asyncio
import pytest
from unittest.mock import AsyncMock, patch, MagicMock
import httpx

from openclaw_docker_proxy import (
    _docker_error_response,
    _docker_path,
    _stream_with_limit,
    _read_limited_json,
    DOCKER_API_VERSION,
    DOCKER_TOTAL_TIMEOUT,
)


def test_docker_error_response_structure():
    resp = _docker_error_response("docker_timeout", "timeout reached")
    assert resp.status_code == 500
    body = resp.body.decode()
    assert '"code":"docker_timeout"' in body
    assert '"message":"timeout reached"' in body
    assert '"raw"' not in body


def test_docker_path_builds_correctly():
    assert _docker_path("containers", "openclaw_gateway_1", "json") == "/v1.47/containers/openclaw_gateway_1/json"


def test_total_timeout_is_15s():
    assert DOCKER_TOTAL_TIMEOUT == 15.0


def aiter_mock(chunks):
    async def _gen(chunk_size=None):
        for chunk in chunks:
            yield chunk
    return _gen()


@pytest.mark.asyncio
async def test_read_limited_json_within_limit():
    response = MagicMock()
    response.aiter_bytes = MagicMock(return_value=aiter_mock([b'{"ok": true}']))
    result = await _read_limited_json(response, limit=1024)
    assert result == {"ok": True}


@pytest.mark.asyncio
async def test_read_limited_json_exceeds_limit():
    response = MagicMock()
    response.aiter_bytes = MagicMock(return_value=aiter_mock([b"x" * 2048]))
    with pytest.raises(RuntimeError, match="docker_response_too_large"):
        await _read_limited_json(response, limit=1024)


@pytest.mark.asyncio
async def test_stream_with_limit_holds_semaphore():
    sem = asyncio.Semaphore(2)
    client = AsyncMock()
    stream_cm = AsyncMock()
    response = AsyncMock()
    response.aiter_bytes = MagicMock(return_value=aiter_mock([b"payload"]))
    response.raise_for_status = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=response)
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    client.stream = MagicMock(return_value=stream_cm)

    chunks = []
    async for chunk in _stream_with_limit(client, sem, "GET", "/test", limit=1024):
        chunks.append(chunk)
    assert b"".join(chunks) == b"payload"
    assert sem.locked() is False


@pytest.mark.asyncio
async def test_stream_with_limit_respects_total_size():
    sem = asyncio.Semaphore(2)
    client = AsyncMock()
    stream_cm = AsyncMock()
    response = AsyncMock()
    response.aiter_bytes = MagicMock(return_value=aiter_mock([b"x" * 1024, b"x" * 1024]))
    response.raise_for_status = MagicMock()
    stream_cm.__aenter__ = AsyncMock(return_value=response)
    stream_cm.__aexit__ = AsyncMock(return_value=False)
    client.stream = MagicMock(return_value=stream_cm)

    chunks = []
    with pytest.raises(RuntimeError, match="docker_response_too_large"):
        async for chunk in _stream_with_limit(client, sem, "GET", "/test", limit=1536):
            chunks.append(chunk)
