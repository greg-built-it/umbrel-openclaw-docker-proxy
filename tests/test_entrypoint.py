import runpy
import tomllib
from pathlib import Path

import openclaw_docker_proxy


ROOT = Path(__file__).parent.parent


def test_python_m_package_invokes_proxy_main(monkeypatch):
    calls = []
    monkeypatch.setattr(openclaw_docker_proxy, "main", lambda: calls.append("called"))

    runpy.run_module("openclaw_docker_proxy", run_name="__main__")

    assert calls == ["called"]


def test_project_version_is_1_0_4():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert project["project"]["version"] == "1.0.4"
