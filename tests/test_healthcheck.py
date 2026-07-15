import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from openclaw_docker_proxy import healthcheck


def test_healthcheck_uses_uds_transport():
    src = Path("src/openclaw_docker_proxy/healthcheck.py").read_text()
    assert "httpx.HTTPTransport(uds=" in src
    assert 'r.text != "ok"' in src
    assert "r.status_code != 200" in src


def test_healthcheck_main_fails_when_unavailable():
    with patch("httpx.Client") as mock_client:
        instance = MagicMock()
        instance.request.return_value.status_code = 503
        mock_client.return_value = instance
        assert healthcheck.main() == 1
