#
# openclaw_docker_proxy – minimal Docker-API proxy for OpenClaw diagnostics
#

import os
import socket
import stat
from pathlib import Path
from contextlib import asynccontextmanager
from urllib.parse import quote
import asyncio
import json
import re
import time
from datetime import datetime, timezone

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.routing import Route

SOCK_DIR = Path("/run/proxy")
SOCK_PATH = SOCK_DIR / "docker-proxy.sock"

MAX_BODY_SIZE = 8192
DOCKER_API_VERSION = "v1.47"
DOCKER_SOCKET_PATH = "/var/run/docker.sock"
DOCKER_TIMEOUT = httpx.Timeout(connect=2.0, read=10.0, write=5.0, pool=2.0)
DOCKER_SEMAPHORE = asyncio.Semaphore(2)
DOCKER_TOTAL_TIMEOUT = 12.0
MAX_RESPONSE_BYTES = 256 * 1024
MAX_SAFE_STRING_LENGTH = 4096
MAX_ENV_KEYS = 256
MAX_LABEL_KEYS = 256
MAX_MOUNTS = 256
MAX_NETWORKS = 128

CONTAINER_MAP = {
    "gateway": "openclaw_gateway_1",
    "app_proxy": "openclaw_app_proxy_1",
}


class SocketLifecycleError(Exception):
    pass


def _is_unix_socket(path: Path) -> bool:
    try:
        st = os.lstat(path)
        return not stat.S_ISLNK(st.st_mode) and stat.S_ISSOCK(st.st_mode)
    except FileNotFoundError:
        return False


def _ensure_directory() -> None:
    if SOCK_DIR.exists() or SOCK_DIR.is_symlink():
        st = os.lstat(SOCK_DIR)
        if stat.S_ISLNK(st.st_mode):
            raise SocketLifecycleError(f"{SOCK_DIR} is a symlink; aborting")
        if not stat.S_ISDIR(st.st_mode):
            raise SocketLifecycleError(
                f"{SOCK_DIR} is not a directory (mode {st.st_mode:o}); aborting"
            )
    else:
        SOCK_DIR.mkdir(parents=True, exist_ok=True)
        st = os.lstat(SOCK_DIR)
        if not stat.S_ISDIR(st.st_mode):
            raise SocketLifecycleError(f"{SOCK_DIR} could not be created as directory")


def _prepare_socket_path() -> None:
    try:
        st = os.lstat(SOCK_PATH)
    except FileNotFoundError:
        return

    if stat.S_ISLNK(st.st_mode):
        raise SocketLifecycleError(f"{SOCK_PATH} is a symlink; aborting")
    if stat.S_ISSOCK(st.st_mode):
        try:
            SOCK_PATH.unlink()
        except OSError as e:
            raise SocketLifecycleError(
                f"Could not remove stale socket {SOCK_PATH}: {e}"
            ) from e
        return
    if stat.S_ISREG(st.st_mode):
        raise SocketLifecycleError(f"{SOCK_PATH} is a regular file; aborting")
    if stat.S_ISDIR(st.st_mode):
        raise SocketLifecycleError(f"{SOCK_PATH} is a directory; aborting")
    raise SocketLifecycleError(
        f"{SOCK_PATH} has unexpected type (mode {st.st_mode:o}); aborting"
    )


def create_unix_socket() -> socket.socket:
    _ensure_directory()
    _prepare_socket_path()

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(SOCK_PATH))
        os.chmod(SOCK_PATH, 0o660)
        sock.listen()
    except Exception:
        sock.close()
        try:
            if _is_unix_socket(SOCK_PATH):
                SOCK_PATH.unlink()
        except OSError:
            pass
        raise
    return sock


def _version_to_tuple(version: str | None) -> tuple | None:
    if version is None:
        return None
    version = version.lstrip("v")
    return tuple(int(x) for x in version.split("."))


async def assert_docker_api_compatible(client: httpx.AsyncClient) -> None:
    async with DOCKER_SEMAPHORE:
        async with asyncio.timeout(DOCKER_TOTAL_TIMEOUT):
            async with client.stream("GET", "/version") as response:
                response.raise_for_status()
                data = await _read_limited_json(response, limit=1024 * 1024)

    api_version = data.get("ApiVersion")
    min_api_version = data.get("MinAPIVersion")
    expected = DOCKER_API_VERSION

    if not api_version or not min_api_version:
        raise RuntimeError("docker_api_incompatible")

    if not _version_in_range(expected, min_api_version, api_version):
        raise RuntimeError("docker_api_incompatible")


def _version_in_range(version: str | None, min_version: str | None, max_version: str | None) -> bool:
    """Numeric tuple comparison of Docker API versions."""
    vt = _version_to_tuple(version)
    if vt is None:
        return False
    if min_version is not None and vt < _version_to_tuple(min_version):
        return False
    if max_version is not None and vt > _version_to_tuple(max_version):
        return False
    return True


@asynccontextmanager
async def lifespan(app: Starlette):
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCKET_PATH)
    client = httpx.AsyncClient(
        base_url="http://docker",
        transport=transport,
        timeout=DOCKER_TIMEOUT,
    )
    app.state.docker = client
    app.state.semaphore = DOCKER_SEMAPHORE
    try:
        await assert_docker_api_compatible(client)
        yield
    finally:
        await client.aclose()


def _media_type(content_type: str | None) -> str:
    if not content_type:
        return ""
    return content_type.split(";", 1)[0].strip().lower()


async def _read_limited_body(request: Request) -> dict:
    if _media_type(request.headers.get("content-type")) != "application/json":
        raise HTTPException(415, "Unsupported Media Type")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_BODY_SIZE:
            raise HTTPException(413, "Payload Too Large")
        body.extend(chunk)

    try:
        text = body.decode("utf-8")
        data = json.loads(text)
    except UnicodeDecodeError:
        raise HTTPException(400, "Invalid UTF-8")
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    if not isinstance(data, dict):
        raise HTTPException(400, "JSON object expected")

    return data


def _validate_container(data: dict) -> str:
    if set(data.keys()) != {"container"}:
        raise HTTPException(400, "Invalid request fields")
    container = data["container"]
    if container not in CONTAINER_MAP:
        raise HTTPException(400, "Invalid container")
    return CONTAINER_MAP[container]


def _validate_logs_request(data: dict) -> tuple[str, int]:
    if set(data.keys()) != {"container", "tail"}:
        raise HTTPException(400, "Invalid request fields")
    container = data["container"]
    if container not in CONTAINER_MAP:
        raise HTTPException(400, "Invalid container")
    tail = data["tail"]
    if type(tail) is not int or isinstance(tail, bool):
        raise HTTPException(400, "tail must be an integer")
    if tail < 1 or tail > 500:
        raise HTTPException(400, "tail must be between 1 and 500")
    return CONTAINER_MAP[container], tail


def _validate_empty_request(data: dict) -> None:
    if data:
        raise HTTPException(400, "Request body must be empty object")


async def _read_limited_json(response: httpx.Response, limit: int) -> dict:
    total = 0
    chunks = []
    async for chunk in response.aiter_bytes(chunk_size=8192):
        total += len(chunk)
        if total > limit:
            raise RuntimeError("docker_response_too_large")
        chunks.append(chunk)
    body = b"".join(chunks)
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise RuntimeError("invalid_docker_response")


async def _stream_with_limit(
    client: httpx.AsyncClient, semaphore: asyncio.Semaphore, method: str, path: str, limit: int
):
    response = None
    try:
        async with asyncio.timeout(DOCKER_TOTAL_TIMEOUT):
            async with semaphore:
                async with client.stream(method, path) as response:
                    response.raise_for_status()
                    total = 0
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        total += len(chunk)
                        if total > limit:
                            raise RuntimeError("docker_response_too_large")
                        yield chunk
    finally:
        if response is not None:
            await response.aclose()


def _docker_error_response(code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=500)


def _docker_path(*parts: str) -> str:
    return "/" + "/".join([DOCKER_API_VERSION] + list(parts))


def _bounded_text(value: object) -> object:
    if isinstance(value, str) and len(value) > MAX_SAFE_STRING_LENGTH:
        return value[:MAX_SAFE_STRING_LENGTH]
    return value


def _bounded_json_response(payload: dict) -> JSONResponse:
    response = JSONResponse(payload)
    if len(response.body) <= MAX_RESPONSE_BYTES:
        return response
    reasons = list(payload.get("truncation_reasons") or [])
    if "response_budget" not in reasons:
        reasons.append("response_budget")
    return JSONResponse({
        "truncated": True,
        "truncation_reasons": reasons,
        "error": {
            "code": "response_budget",
            "message": "Filtered response exceeded the proxy response budget.",
        },
    })


def _new_deadline() -> float:
    return time.monotonic() + DOCKER_TOTAL_TIMEOUT


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise RuntimeError("docker_timeout")
    return remaining


def _filter_docker_info(data: dict) -> dict:
    """Expose only the fixed, non-secret Docker host facts required by preflight."""
    return {
        "architecture": data.get("Architecture"),
        "storage_driver": data.get("Driver"),
        "docker_root_dir": data.get("DockerRootDir"),
        "images_count": data.get("Images"),
        "containers_count": data.get("Containers"),
        "truncated": False,
        "truncation_reasons": [],
    }


async def _get_docker_json(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    path: str,
    *,
    limit: int = 2 * 1024 * 1024,
    deadline: float | None = None,
) -> dict | list:
    if deadline is None:
        deadline = _new_deadline()
    try:
        async with asyncio.timeout(_remaining(deadline)):
            async with semaphore:
                async with client.stream("GET", path) as response:
                    if response.status_code == 404:
                        raise RuntimeError("docker_object_not_found")
                    response.raise_for_status()
                    data = await _read_limited_json(response, limit=limit)
    except httpx.ConnectError:
        raise RuntimeError("docker_unavailable")
    except (httpx.TimeoutException, TimeoutError):
        raise RuntimeError("docker_timeout")
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("docker_unavailable")
    if not isinstance(data, (dict, list)):
        raise RuntimeError("invalid_docker_response")
    return data


OPENCLAW_REPOSITORY_PREFIXES = (
    "ghcr.io/getumbrel/openclaw",
)
MAX_IMAGE_RESULTS = 100


def _is_openclaw_reference(reference: object) -> bool:
    if not isinstance(reference, str):
        return False
    return any(
        reference.startswith(prefix + separator)
        for prefix in OPENCLAW_REPOSITORY_PREFIXES
        for separator in (":", "@")
    )


def _is_openclaw_image(image: dict) -> bool:
    references = list(image.get("RepoTags") or []) + list(image.get("RepoDigests") or [])
    return any(_is_openclaw_reference(reference) for reference in references)


def _normalize_image_id(raw: str | None) -> str | None:
    if not raw:
        return None
    raw = raw.lower()
    if raw.startswith("sha256:"):
        raw = raw[7:]
    if re.fullmatch(r"[a-f0-9]{64}", raw):
        return raw
    return raw[:32] if raw else None


def _filter_local_images(images: list[dict], max_results: int = MAX_IMAGE_RESULTS) -> dict:
    results = []
    matching_count = 0
    reasons: list[str] = []
    for image in images:
        if not _is_openclaw_image(image):
            continue
        matching_count += 1
        if matching_count > max_results:
            reasons.append("max_images")
            break
        repo_tags = [
            _bounded_text(tag) for tag in (image.get("RepoTags") or [])
            if _is_openclaw_reference(tag)
        ][:32]
        repo_digests = [
            _bounded_text(digest) for digest in (image.get("RepoDigests") or [])
            if _is_openclaw_reference(digest)
        ][:32]
        results.append(
            {
                "id": _normalize_image_id(image.get("Id")),
                "repo_tags": repo_tags,
                "repo_digests": repo_digests,
                "size": image.get("Size"),
                "created": image.get("Created"),
            }
        )
    return {
        "images": results,
        "truncated": bool(reasons),
        "truncation_reasons": reasons,
        "count": len(results),
    }


async def _get_local_images(client: httpx.AsyncClient, semaphore: asyncio.Semaphore) -> dict:
    filters = json.dumps(
        {"reference": [prefix + "*" for prefix in OPENCLAW_REPOSITORY_PREFIXES]},
        separators=(",", ":"),
        sort_keys=True,
    )
    path = _docker_path("images", "json") + "?filters=" + quote(filters, safe="")
    data = await _get_docker_json(client, semaphore, path, limit=8 * 1024 * 1024)
    if not isinstance(data, list):
        raise RuntimeError("invalid_docker_response")
    return _filter_local_images(data)


def _filter_image_config(inspect_data: dict) -> dict:
    config = inspect_data.get("Config") or {}
    env = config.get("Env") or []
    env_keys = []
    reasons: list[str] = []
    for entry in env:
        if isinstance(entry, str) and "=" in entry:
            key = entry.split("=", 1)[0]
            env_keys.append(str(_bounded_text(key)))
            if len(env_keys) >= MAX_ENV_KEYS:
                if len(env) > len(env_keys):
                    reasons.append("max_env_keys")
                break
    return {
        "user": _bounded_text(config.get("User")),
        "working_dir": _bounded_text(config.get("WorkingDir")),
        "entrypoint_present": config.get("Entrypoint") is not None,
        "cmd_present": config.get("Cmd") is not None,
        "env_key_count": len(env_keys),
        "env_keys": env_keys,
        "truncated": bool(reasons),
        "truncation_reasons": reasons,
    }


def _allowed_image_references(images: list[dict]) -> set[str]:
    references: set[str] = set()
    for image in images:
        if not _is_openclaw_image(image):
            continue
        references.update(
            ref for ref in (image.get("RepoTags") or []) if _is_openclaw_reference(ref)
        )
        references.update(
            ref for ref in (image.get("RepoDigests") or []) if _is_openclaw_reference(ref)
        )
        raw_id = image.get("Id")
        normalized_id = _normalize_image_id(raw_id)
        if isinstance(raw_id, str) and re.fullmatch(r"sha256:[a-fA-F0-9]{64}", raw_id):
            references.add(raw_id.lower())
        if normalized_id and re.fullmatch(r"[a-f0-9]{64}", normalized_id):
            references.add(normalized_id)
    return references


async def _get_image_config(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    image_ref: str,
) -> dict:
    deadline = _new_deadline()
    filters = json.dumps(
        {"reference": [image_ref]}, separators=(",", ":"), sort_keys=True
    )
    listing_path = _docker_path("images", "json") + "?filters=" + quote(
        filters, safe=""
    )
    images = await _get_docker_json(
        client,
        semaphore,
        listing_path,
        limit=2 * 1024 * 1024,
        deadline=deadline,
    )
    if not isinstance(images, list):
        raise RuntimeError("invalid_docker_response")
    if image_ref not in _allowed_image_references(images):
        raise RuntimeError("image_not_allowed")
    encoded_ref = quote(image_ref, safe="")
    path = _docker_path("images", encoded_ref, "json")
    data = await _get_docker_json(
        client,
        semaphore,
        path,
        limit=2 * 1024 * 1024,
        deadline=deadline,
    )
    if not isinstance(data, dict):
        raise RuntimeError("invalid_docker_response")
    return _filter_image_config(data)


ALLOWED_INSPECT_KEYS = frozenset({"Mounts", "HostConfig", "Config", "NetworkSettings", "State", "Name", "RestartCount", "Created", "Image"})


def _filter_container_inspect(inspect_data: dict) -> dict:
    host_config = inspect_data.get("HostConfig") or {}
    safe_host_config = {
        "NetworkMode": host_config.get("NetworkMode"),
        "PidMode": host_config.get("PidMode"),
        "RestartPolicy": host_config.get("RestartPolicy"),
        "ReadonlyRootfs": host_config.get("ReadonlyRootfs"),
        "CapAdd": host_config.get("CapAdd"),
        "CapDrop": host_config.get("CapDrop"),
        "Privileged": host_config.get("Privileged"),
    }

    config = inspect_data.get("Config") or {}
    env_entries = config.get("Env") or []
    env_keys = [
        str(_bounded_text(entry.split("=", 1)[0]))
        for entry in env_entries
        if isinstance(entry, str) and "=" in entry
    ][:MAX_ENV_KEYS]
    labels = [str(_bounded_text(key)) for key in (config.get("Labels") or {})][
        :MAX_LABEL_KEYS
    ]
    reasons: list[str] = []
    if len(env_entries) > len(env_keys):
        reasons.append("max_env_keys")
    if len(config.get("Labels") or {}) > len(labels):
        reasons.append("max_label_keys")
    safe_config = {
        "Hostname": _bounded_text(config.get("Hostname")),
        "Domainname": _bounded_text(config.get("Domainname")),
        "User": _bounded_text(config.get("User")),
        "AttachStdin": config.get("AttachStdin"),
        "AttachStdout": config.get("AttachStdout"),
        "AttachStderr": config.get("AttachStderr"),
        "Tty": config.get("Tty"),
        "OpenStdin": config.get("OpenStdin"),
        "StdinOnce": config.get("StdinOnce"),
        "EnvCount": len(env_entries),
        "EnvKeys": env_keys,
        "CmdPresent": config.get("Cmd") is not None,
        "Image": _bounded_text(config.get("Image")),
        "WorkingDir": _bounded_text(config.get("WorkingDir")),
        "EntrypointPresent": config.get("Entrypoint") is not None,
        "Labels": labels,
        "StopSignal": _bounded_text(config.get("StopSignal")),
    }

    mounts = inspect_data.get("Mounts") or []
    safe_mounts = []
    for mount in mounts[:MAX_MOUNTS]:
        if not isinstance(mount, dict):
            continue
        safe_mounts.append(
            {
                "Type": _bounded_text(mount.get("Type")),
                "Source": _bounded_text(mount.get("Source")),
                "Destination": _bounded_text(mount.get("Destination")),
                "Mode": _bounded_text(mount.get("Mode")),
                "RW": mount.get("RW"),
                "Propagation": _bounded_text(mount.get("Propagation")),
            }
        )
    if len(mounts) > MAX_MOUNTS:
        reasons.append("max_mounts")

    network_settings = inspect_data.get("NetworkSettings") or {}
    networks = network_settings.get("Networks") or {}
    safe_networks = {}
    for name, net in list(networks.items())[:MAX_NETWORKS]:
        aliases = net.get("Aliases") or []
        safe_networks[str(_bounded_text(name))] = {
            "Aliases": [str(_bounded_text(alias)) for alias in aliases[:64]],
            "NetworkID": _bounded_text(net.get("NetworkID")),
        }
    if len(networks) > MAX_NETWORKS:
        reasons.append("max_networks")

    return {
        "name": _bounded_text(inspect_data.get("Name", "").lstrip("/")),
        "image": _bounded_text(inspect_data.get("Image")),
        "created": _bounded_text(inspect_data.get("Created")),
        "restart_count": inspect_data.get("RestartCount"),
        "host_config": safe_host_config,
        "config": safe_config,
        "mounts": safe_mounts,
        "networks": safe_networks,
        "truncated": bool(reasons),
        "truncation_reasons": reasons,
    }


async def _get_container_inspect(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    container_name: str,
) -> dict:
    path = _docker_path("containers", container_name, "json")
    data = await _get_docker_json(client, semaphore, path, limit=2 * 1024 * 1024)
    if not isinstance(data, dict):
        raise RuntimeError("invalid_docker_response")
    return _filter_container_inspect(data)


async def _get_docker_info(client: httpx.AsyncClient, semaphore: asyncio.Semaphore) -> dict:
    data = await _get_docker_json(client, semaphore, _docker_path("info"), limit=1024 * 1024)
    if not isinstance(data, dict):
        raise RuntimeError("invalid_docker_response")
    return _filter_docker_info(data)


def _filter_container_status(inspect_data: dict) -> dict:
    name = inspect_data.get("Name", "")
    if name.startswith("/"):
        name = name[1:]
    state = inspect_data.get("State", {})
    health = state.get("Health", {})
    return {
        "docker_name": name,
        "image": inspect_data.get("Config", {}).get("Image", ""),
        "created_at": inspect_data.get("Created"),
        "restart_count": inspect_data.get("RestartCount", 0),
        "state": {
            "status": state.get("Status"),
            "running": state.get("Running", False),
            "exit_code": state.get("ExitCode", 0),
            "error": state.get("Error", ""),
            "oom_killed": state.get("OOMKilled", False),
            "started_at": state.get("StartedAt"),
            "finished_at": state.get("FinishedAt"),
            "health_status": health.get("Status") if health else None,
        },
    }


async def _get_container_status(
    client: httpx.AsyncClient, semaphore: asyncio.Semaphore, container_name: str
) -> dict:
    path = _docker_path("containers", container_name, "json")
    try:
        async with semaphore:
            async with asyncio.timeout(DOCKER_TOTAL_TIMEOUT):
                async with client.stream("GET", path) as response:
                    if response.status_code == 404:
                        raise RuntimeError("container_not_found")
                    response.raise_for_status()
                    data = await _read_limited_json(response, limit=2 * 1024 * 1024)
    except httpx.ConnectError:
        raise RuntimeError("docker_unavailable")
    except httpx.TimeoutException:
        raise RuntimeError("docker_timeout")
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("docker_unavailable")
    return _filter_container_status(data)


async def _get_container_tty(
    client: httpx.AsyncClient, semaphore: asyncio.Semaphore, container_name: str
) -> bool:
    path = _docker_path("containers", container_name, "json")
    try:
        async with semaphore:
            async with asyncio.timeout(DOCKER_TOTAL_TIMEOUT):
                async with client.stream("GET", path) as response:
                    if response.status_code == 404:
                        raise RuntimeError("container_not_found")
                    response.raise_for_status()
                    data = await _read_limited_json(response, limit=2 * 1024 * 1024)
    except httpx.ConnectError:
        raise RuntimeError("docker_unavailable")
    except httpx.TimeoutException:
        raise RuntimeError("docker_timeout")
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("docker_unavailable")
    return bool(data.get("Config", {}).get("Tty", False))


def _mask_secrets(text: str) -> str:
    # Bearer / Basic / Token / API key patterns
    text = re.sub(
        r"(?i)(Authorization\s*:\s*(Bearer|Basic|Token)\s+)[A-Za-z0-9_\-\.=+/]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)(\b(?:api[_-]?key|apikey|token|access[_-]?token|auth[_-]?token)\s*[=:]\s*)[A-Za-z0-9_\-\.=+/]+",
        r"\1[REDACTED]",
        text,
    )
    # URL credentials
    text = re.sub(
        r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^@\s]+@",
        r"\1[REDACTED]@",
        text,
    )
    # Long hex/base64/jwt-like strings
    text = re.sub(
        r"\b([A-Za-z0-9+/._-]{32,})\b",
        "[REDACTED]",
        text,
    )
    # Private key blocks (multiline, non-greedy)
    text = re.sub(
        r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----.+?-----END (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----",
        "[REDACTED_PRIVATE_KEY]",
        text,
        flags=re.DOTALL,
    )
    return text


async def _get_logs(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    container_name: str,
    tail: int,
) -> list[str]:
    is_tty = await _get_container_tty(client, semaphore, container_name)
    path = _docker_path(
        "containers", container_name, f"logs?stdout=1&stderr=1&timestamps=1&tail={tail}"
    )

    chunks = []
    try:
        async for chunk in _stream_with_limit(
            client, semaphore, "GET", path, limit=1 * 1024 * 1024
        ):
            chunks.append(chunk)
    except httpx.ConnectError:
        raise RuntimeError("docker_unavailable")
    except httpx.TimeoutException:
        raise RuntimeError("docker_timeout")

    raw = b"".join(chunks)

    if is_tty:
        payloads = [raw]
    else:
        payloads = _demultiplex_logs(raw)

    text = b"".join(payloads).decode("utf-8", "replace")
    text = _mask_secrets(text)

    lines = text.splitlines()
    if len(lines) > 500:
        lines = lines[:500]
        truncated = True
    else:
        truncated = False

    # Enforce per-line limit
    result = []
    for line in lines:
        if len(line) > 4096:
            line = line[:4096] + " [truncated]"
        result.append(line)

    return result, truncated, len(raw)


def _demultiplex_logs(raw: bytes) -> list[bytes]:
    payloads = []
    i = 0
    while i < len(raw):
        # Need at least 8 bytes for header
        if i + 8 > len(raw):
            break
        length = int.from_bytes(raw[i + 4 : i + 8], "big", signed=False)
        if i + 8 + length > len(raw):
            break
        payload = raw[i + 8 : i + 8 + length]
        payloads.append(payload)
        i += 8 + length
    return payloads


async def _get_stats(
    client: httpx.AsyncClient, semaphore: asyncio.Semaphore, container_name: str
) -> dict:
    path = _docker_path("containers", container_name, "stats?stream=false")
    try:
        async for chunk in _stream_with_limit(
            client, semaphore, "GET", path, limit=1 * 1024 * 1024
        ):
            # stream=false returns a single JSON object; collect first chunk
            pass
        # Re-request non-stream for JSON parsing
        async with semaphore:
            async with asyncio.timeout(DOCKER_TOTAL_TIMEOUT):
                response = await client.get(path)
                if response.status_code == 404:
                    raise RuntimeError("container_not_found")
                response.raise_for_status()
                data = response.json()
    except httpx.ConnectError:
        raise RuntimeError("docker_unavailable")
    except httpx.TimeoutException:
        raise RuntimeError("docker_timeout")
    except RuntimeError:
        raise
    except Exception:
        raise RuntimeError("docker_unavailable")
    return _filter_stats(data)


def _filter_stats(data: dict) -> dict:
    cpu_stats = data.get("cpu_stats", {})
    precpu_stats = data.get("precpu_stats", {})
    memory_stats = data.get("memory_stats", {})
    networks = data.get("networks", {})

    cpu_percent = _calculate_cpu_percent(cpu_stats, precpu_stats)

    usage = memory_stats.get("usage", 0)
    limit = memory_stats.get("limit", 1)
    stats_detail = memory_stats.get("stats", {})
    inactive_file = stats_detail.get("inactive_file", stats_detail.get("total_inactive_file", 0))
    working_set = max(0, usage - inactive_file)

    net_in = 0
    net_out = 0
    for iface in networks.values():
        net_in += iface.get("rx_bytes", 0)
        net_out += iface.get("tx_bytes", 0)

    memory_percent = (usage / limit * 100.0) if limit else 0.0
    working_set_percent = (working_set / limit * 100.0) if limit else 0.0

    return {
        "cpu_percent": cpu_percent,
        "memory_usage_bytes": usage,
        "memory_working_set_bytes": working_set,
        "memory_limit_bytes": limit,
        "memory_percent": memory_percent,
        "memory_working_set_percent": working_set_percent,
        "network_input_bytes": net_in,
        "network_output_bytes": net_out,
    }


def _calculate_cpu_percent(cpu_stats: dict, precpu_stats: dict) -> float | None:
    cpu_usage = cpu_stats.get("cpu_usage", {})
    precpu_usage = precpu_stats.get("cpu_usage", {})
    total_usage = cpu_usage.get("total_usage")
    pre_total_usage = precpu_usage.get("total_usage")
    system_usage = cpu_stats.get("system_cpu_usage")
    pre_system_usage = precpu_stats.get("system_cpu_usage")
    online_cpus = cpu_stats.get("online_cpus") or len(cpu_stats.get("percpu_usage") or [])

    if (
        total_usage is None
        or pre_total_usage is None
        or system_usage is None
        or pre_system_usage is None
        or online_cpus is None
        or online_cpus == 0
    ):
        return None

    system_delta = system_usage - pre_system_usage
    cpu_delta = total_usage - pre_total_usage

    if system_delta <= 0 or cpu_delta < 0:
        return 0.0

    return (cpu_delta / system_delta) * online_cpus * 100.0


async def health(request: Request):
    return PlainTextResponse("ok")


async def container_status(request: Request):
    data = await _read_limited_body(request)
    container_name = _validate_container(data)
    try:
        result = await _get_container_status(
            request.app.state.docker, request.app.state.semaphore, container_name
        )
    except RuntimeError as e:
        code = str(e)
        messages = {
            "container_not_found": f"Container {container_name} nicht gefunden",
            "docker_unavailable": "Docker-Daemon nicht erreichbar",
            "docker_timeout": "Docker-API-Aufruf hat das Zeitlimit überschritten",
            "docker_response_too_large": "Docker-Antwort überschreitet das Größenlimit",
            "invalid_docker_response": "Ungültige Antwort vom Docker-Daemon",
        }
        return _docker_error_response(code, messages.get(code, "Docker-Fehler"))
    return _bounded_json_response({**result, "container": data["container"]})


async def logs(request: Request):
    data = await _read_limited_body(request)
    container_name, tail = _validate_logs_request(data)
    try:
        lines, truncated, size_bytes = await _get_logs(
            request.app.state.docker, request.app.state.semaphore, container_name, tail
        )
    except RuntimeError as e:
        code = str(e)
        messages = {
            "container_not_found": f"Container {container_name} nicht gefunden",
            "docker_unavailable": "Docker-Daemon nicht erreichbar",
            "docker_timeout": "Docker-API-Aufruf hat das Zeitlimit überschritten",
            "docker_response_too_large": "Docker-Antwort überschreitet das Größenlimit",
            "invalid_docker_response": "Ungültige Antwort vom Docker-Daemon",
        }
        return _docker_error_response(code, messages.get(code, "Docker-Fehler"))
    return JSONResponse(
        {
            "container": data["container"],
            "docker_name": container_name,
            "tail_requested": tail,
            "lines": lines,
            "line_count": len(lines),
            "truncated": truncated,
            "size_bytes": size_bytes,
        }
    )


CONTAINER_REQUEST_CODES = {
    "container_not_found": "Container nicht gefunden",
    "docker_unavailable": "Docker-Daemon nicht erreichbar",
    "docker_timeout": "Docker-API-Aufruf hat das Zeitlimit überschritten",
    "docker_response_too_large": "Docker-Antwort überschreitet das Größenlimit",
    "invalid_docker_response": "Ungültige Antwort vom Docker-Daemon",
}


def _generic_docker_error_response(code: str, fallback: str = "Docker-Fehler") -> JSONResponse:
    return _docker_error_response(code, CONTAINER_REQUEST_CODES.get(code, fallback))


async def _docker_info(request: Request):
    data = await _read_limited_body(request)
    _validate_empty_request(data)
    try:
        result = await _get_docker_info(request.app.state.docker, request.app.state.semaphore)
    except RuntimeError as e:
        return _generic_docker_error_response(str(e))
    return _bounded_json_response(result)


def _validate_image_request(data: dict) -> str:
    if set(data.keys()) != {"image"}:
        raise HTTPException(400, "Invalid request fields")
    image = data["image"]
    if not isinstance(image, str) or not image:
        raise HTTPException(400, "image must be a non-empty string")
    if image.count("@") > 1 or image.count(":") > 2 or ".." in image or "/../" in image or image.startswith("/"):
        raise HTTPException(400, "Invalid image reference")
    return image


async def _local_images(request: Request):
    data = await _read_limited_body(request)
    _validate_empty_request(data)
    try:
        result = await _get_local_images(request.app.state.docker, request.app.state.semaphore)
    except RuntimeError as e:
        return _generic_docker_error_response(str(e))
    return _bounded_json_response(result)


async def _image_config(request: Request):
    data = await _read_limited_body(request)
    image_ref = _validate_image_request(data)
    try:
        result = await _get_image_config(request.app.state.docker, request.app.state.semaphore, image_ref)
    except RuntimeError as e:
        code = str(e)
        if code == "image_not_allowed":
            return JSONResponse(
                {"error": {"code": code, "message": "Image ist kein lokales OpenClaw-Image"}},
                status_code=403,
            )
        if code == "docker_object_not_found":
            return JSONResponse({"error": {"code": "image_not_found", "message": "Image nicht gefunden"}}, status_code=404)
        return _generic_docker_error_response(code)
    return _bounded_json_response(result)


async def _container_inspect(request: Request):
    data = await _read_limited_body(request)
    container_name = _validate_container(data)
    try:
        result = await _get_container_inspect(
            request.app.state.docker, request.app.state.semaphore, container_name
        )
    except RuntimeError as e:
        code = str(e)
        if code == "docker_object_not_found":
            return JSONResponse({"error": {"code": "container_not_found", "message": "Container nicht gefunden"}}, status_code=404)
        return _generic_docker_error_response(code)
    return _bounded_json_response({**result, "container": data["container"]})


async def resource_status(request: Request):
    data = await _read_limited_body(request)
    _validate_empty_request(data)
    containers = []
    read_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    for key in ["gateway", "app_proxy"]:
        container_name = CONTAINER_MAP[key]
        try:
            stats = await _get_stats(
                request.app.state.docker, request.app.state.semaphore, container_name
            )
            containers.append(
                {
                    "container": key,
                    "docker_name": container_name,
                    **stats,
                }
            )
        except RuntimeError as e:
            code = str(e)
            messages = {
                "container_not_found": f"Container {container_name} nicht gefunden",
                "docker_unavailable": "Docker-Daemon nicht erreichbar",
                "docker_timeout": "Docker-API-Aufruf hat das Zeitlimit überschritten",
                "docker_response_too_large": "Docker-Antwort überschreitet das Größenlimit",
                "invalid_docker_response": "Ungültige Antwort vom Docker-Daemon",
            }
            containers.append(
                {
                    "container": key,
                    "docker_name": container_name,
                    "error": {"code": code, "message": messages.get(code, "Docker-Fehler")},
                }
            )
    return JSONResponse({"read_at": read_at, "containers": containers})


routes = [
    Route("/health", health, methods=["GET"]),
    Route("/v1/docker_info", _docker_info, methods=["POST"]),
    Route("/v1/local_images", _local_images, methods=["POST"]),
    Route("/v1/image_config", _image_config, methods=["POST"]),
    Route("/v1/container_inspect", _container_inspect, methods=["POST"]),
    Route("/v1/container_status", container_status, methods=["POST"]),
    Route("/v1/logs", logs, methods=["POST"]),
    Route("/v1/resource_status", resource_status, methods=["POST"]),
]

app = Starlette(routes=routes, lifespan=lifespan)


def main():
    sock = create_unix_socket()
    config = uvicorn.Config(
        app,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[sock])
    finally:
        try:
            sock.close()
        except OSError:
            pass
        try:
            if _is_unix_socket(SOCK_PATH):
                SOCK_PATH.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


if __name__ == "__main__":
    main()
