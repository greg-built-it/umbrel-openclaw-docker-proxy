import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path

from openclaw_docker_proxy import healthcheck


def test_healthcheck_uses_uds_transport():
    src = Path("src/openclaw_docker_proxy/healthcheck.py").read_text()
    assert "HTTPTransport(uds=" in src


def test_healthcheck_main_fails_when_unavailable():
    with patch("httpx.Client") as mock_client_cls:
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value.status_code = 503
        mock_client_cls.return_value = mock_client
        assert healthcheck.main() == 1
