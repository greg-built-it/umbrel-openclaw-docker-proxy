import json
from unittest.mock import AsyncMock, patch
from urllib.parse import unquote

import pytest
from starlette.routing import Route

import openclaw_docker_proxy as proxy


PREFIX = "ghcr.io/getumbrel/openclaw-umbrel"


def _image(
    index: int,
    *,
    allowed: bool = True,
    repo_tags: list[str] | None = None,
    repo_digests: list[str] | None = None,
) -> dict:
    repository = PREFIX if allowed else "example.invalid/foreign"
    digest = f"{index:064x}"[-64:]
    return {
        "Id": f"sha256:{digest}",
        "RepoTags": [f"{repository}:{index}"] if repo_tags is None else repo_tags,
        "RepoDigests": (
            [f"{repository}@sha256:{digest}"]
            if repo_digests is None
            else repo_digests
        ),
        "Size": index,
        "Created": index,
    }


def test_local_images_filters_foreign_references_and_preserves_digest():
    result = proxy._filter_local_images([_image(1), _image(2, allowed=False)])

    assert result["count"] == 1
    assert result["truncated"] is False
    assert result["images"][0]["id"] == f"{1:064x}"
    serialized = json.dumps(result)
    assert "example.invalid" not in serialized


def test_local_images_accepts_digest_only_with_null_or_empty_repo_tags():
    digest_only_null = _image(3, repo_tags=None)
    digest_only_null["RepoTags"] = None
    digest_only_empty = _image(4, repo_tags=[])

    result = proxy._filter_local_images([digest_only_null, digest_only_empty])

    assert result["count"] == 2
    assert result["images"][0]["repo_tags"] == []
    assert result["images"][1]["repo_tags"] == []
    assert result["images"][0]["repo_digests"] == [
        f"{PREFIX}@sha256:{3:064x}"
    ]


def test_local_images_accepts_tag_only_and_preserves_multiple_allowed_references():
    tag_only = _image(5, repo_digests=[])
    multiple = _image(
        6,
        repo_tags=[f"{PREFIX}:stable", f"{PREFIX}:2026.7.1-1"],
        repo_digests=[f"{PREFIX}@sha256:{6:064x}"],
    )

    result = proxy._filter_local_images([tag_only, multiple])

    assert result["count"] == 2
    assert result["images"][0]["repo_digests"] == []
    assert result["images"][1]["repo_tags"] == [
        f"{PREFIX}:stable",
        f"{PREFIX}:2026.7.1-1",
    ]


def test_repository_boundary_rejects_similarly_named_foreign_repository():
    similar = _image(
        7,
        repo_tags=[f"{PREFIX}-evil:latest"],
        repo_digests=[f"{PREFIX}-evil@sha256:{7:064x}"],
    )

    assert proxy._filter_local_images([similar])["count"] == 0


def test_allowed_references_include_tag_digest_and_full_local_id():
    image = _image(8)
    tag = image["RepoTags"][0]
    digest = image["RepoDigests"][0].split("@", 1)[1]

    references = proxy._allowed_image_references([image])

    assert tag in references
    assert image["RepoDigests"][0] in references
    assert f"{tag}@{digest}" in references
    assert image["Id"] in references
    assert image["Id"].removeprefix("sha256:") in references


def test_foreign_image_id_is_not_authorized():
    foreign = _image(9, allowed=False)

    references = proxy._allowed_image_references([foreign])

    assert foreign["Id"] not in references
    assert foreign["Id"].removeprefix("sha256:") not in references


def test_local_images_exact_limit_is_not_truncated_but_lookahead_is():
    exact = proxy._filter_local_images([_image(i) for i in range(100)])
    overflow = proxy._filter_local_images([_image(i) for i in range(101)])

    assert exact["count"] == 100
    assert exact["truncated"] is False
    assert overflow["count"] == 100
    assert overflow["truncated"] is True
    assert overflow["truncation_reasons"] == ["max_images"]


def test_image_config_exposes_env_keys_never_values_or_commands():
    secret_value = "unit-test-value-that-must-not-leak"
    result = proxy._filter_image_config(
        {
            "Config": {
                "User": "1000:1000",
                "WorkingDir": "/app",
                "Entrypoint": ["/bin/entry", secret_value],
                "Cmd": ["run", secret_value],
                "Env": [f"TOKEN={secret_value}", "PATH=/usr/bin"],
            }
        }
    )

    assert result["env_keys"] == ["TOKEN", "PATH"]
    assert result["env_key_count"] == 2
    assert result["entrypoint_present"] is True
    assert result["cmd_present"] is True
    assert secret_value not in json.dumps(result)


def test_container_inspect_strips_env_and_label_values_and_bounds_collections():
    secret_value = "unit-test-value-that-must-not-leak"
    result = proxy._filter_container_inspect(
        {
            "Name": "/openclaw_gateway_1",
            "Image": "sha256:" + "a" * 64,
            "Config": {
                "Env": [f"TOKEN={secret_value}", "PATH=/usr/bin"],
                "Labels": {"safe.key": secret_value},
                "Cmd": [secret_value],
                "Entrypoint": [secret_value],
                "Image": f"{PREFIX}:latest",
            },
            "HostConfig": {"Privileged": False, "ReadonlyRootfs": True},
            "Mounts": [],
            "NetworkSettings": {"Networks": {}},
        }
    )

    serialized = json.dumps(result)
    assert result["config"]["EnvKeys"] == ["TOKEN", "PATH"]
    assert result["config"]["Labels"] == ["safe.key"]
    assert result["config"]["CmdPresent"] is True
    assert result["config"]["EntrypointPresent"] is True
    assert secret_value not in serialized


@pytest.mark.asyncio
async def test_local_images_uses_exact_repository_docker_filters():
    getter = AsyncMock(return_value=[])
    with patch.object(proxy, "_get_docker_json", getter):
        await proxy._get_local_images(object(), object())

    path = getter.await_args.args[2]
    assert path.startswith("/v1.47/images/json?filters=")
    decoded = json.loads(unquote(path.split("=", 1)[1]))
    assert decoded == {"reference": [f"{PREFIX}:*", f"{PREFIX}@*"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("reference_kind", ["tag", "digest", "tag_digest", "id", "bare_id"])
async def test_image_config_authorizes_each_local_reference_and_inspects_by_local_id(
    reference_kind,
):
    image = _image(10)
    tag = image["RepoTags"][0]
    digest = image["RepoDigests"][0]
    references = {
        "tag": tag,
        "digest": digest,
        "tag_digest": f"{tag}@{digest.split('@', 1)[1]}",
        "id": image["Id"],
        "bare_id": image["Id"].removeprefix("sha256:"),
    }
    getter = AsyncMock(side_effect=[[image], {"Config": {"Env": []}}])

    with patch.object(proxy, "_get_docker_json", getter):
        await proxy._get_image_config(
            object(), object(), references[reference_kind]
        )

    listing_path = getter.await_args_list[0].args[2]
    decoded = json.loads(unquote(listing_path.split("=", 1)[1]))
    assert decoded == {"reference": [f"{PREFIX}:*", f"{PREFIX}@*"]}
    assert getter.await_args_list[1].args[2] == (
        f"/v1.47/images/{image['Id'].replace(':', '%3A')}/json"
    )


@pytest.mark.asyncio
async def test_image_config_rejects_foreign_image_before_inspect():
    foreign = "example.invalid/foreign:latest"
    getter = AsyncMock(return_value=[_image(1, allowed=False)])

    with patch.object(proxy, "_get_docker_json", getter):
        with pytest.raises(RuntimeError, match="image_not_allowed"):
            await proxy._get_image_config(object(), object(), foreign)

    assert getter.await_count == 1


@pytest.mark.asyncio
async def test_image_config_uses_one_total_deadline_for_both_docker_gets():
    image_ref = f"{PREFIX}:latest"
    getter = AsyncMock(
        side_effect=[
            [_image(1)],
            {"Config": {"Env": []}},
        ]
    )
    first_image = _image(1)
    first_image["RepoTags"] = [image_ref]
    getter.side_effect = [[first_image], {"Config": {"Env": []}}]

    with patch.object(proxy, "_get_docker_json", getter):
        await proxy._get_image_config(object(), object(), image_ref)

    first_deadline = getter.await_args_list[0].kwargs["deadline"]
    second_deadline = getter.await_args_list[1].kwargs["deadline"]
    assert first_deadline == second_deadline


def test_bounded_response_fails_closed_with_structured_reason(monkeypatch):
    monkeypatch.setattr(proxy, "MAX_RESPONSE_BYTES", 64)
    response = proxy._bounded_json_response({"value": "x" * 500})
    payload = json.loads(response.body)

    assert payload["truncated"] is True
    assert payload["truncation_reasons"] == ["response_budget"]
    assert payload["error"]["code"] == "response_budget"
    assert "x" * 100 not in response.body.decode()


def test_new_routes_are_post_only_and_no_mutating_route_exists():
    contract = {
        route.path: route.methods
        for route in proxy.routes
        if isinstance(route, Route)
    }

    for path in (
        "/v1/docker_info",
        "/v1/local_images",
        "/v1/image_config",
        "/v1/container_inspect",
    ):
        assert contract[path] == {"POST"}
    assert not any(
        word in path.lower()
        for path in contract
        for word in ("start", "stop", "create", "delete", "pull", "exec")
    )


def test_proxy_budget_is_below_bridge_total_budget():
    assert proxy.DOCKER_TOTAL_TIMEOUT == 12.0
    assert proxy.MAX_RESPONSE_BYTES == 256 * 1024
