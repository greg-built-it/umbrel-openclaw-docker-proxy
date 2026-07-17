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


def test_project_version_is_1_0_7():
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())

    assert project["project"]["version"] == "1.0.7"


def test_release_metadata_explicitly_disables_latest_tag():
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text()
    assert workflow.count("docker/metadata-action@") == 1
    metadata_block = workflow.split("docker/metadata-action@", 1)[1].split(
        "- name: Build and push", 1
    )[0]

    assert metadata_block.count("flavor: latest=false") == 1
    configured_tags = [
        line.strip()
        for line in metadata_block.splitlines()
        if line.strip().startswith("type=")
    ]
    assert configured_tags == ["type=semver,pattern={{version}}", "type=sha"]
    latest_lines = [
        line.strip()
        for line in workflow.splitlines()
        if "latest" in line.lower() and "ubuntu-latest" not in line.lower()
    ]
    assert latest_lines == ["flavor: latest=false"]
