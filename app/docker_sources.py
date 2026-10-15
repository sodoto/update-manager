"""Ways to list Docker containers: the Docker Engine API, or the docker CLI run inside an LXC."""
import json
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import httpx

from .config import DockerHost
from .registry import parse_image_ref

IGNORE_LABEL = "update-manager.ignore"
DOCKHAND_PORT = 3000  # Dockhand's default listening port


@dataclass
class ContainerInfo:
    id: str
    name: str
    image: str  # image reference the container was created from, e.g. "nginx:latest"
    image_id: str
    state: str
    repo_digests: list[str] = field(default_factory=list)
    labels: dict[str, str] = field(default_factory=dict)
    ports: list[tuple[int, int, str]] = field(default_factory=list)  # (container port, host port, host IP)
    network_mode: str = ""

    @property
    def ignored(self) -> bool:
        return self.labels.get(IGNORE_LABEL, "").lower() in ("1", "true", "yes")


def _from_inspect(container: dict, images: dict[str, dict]) -> ContainerInfo:
    image_id = container.get("Image", "")
    config = container.get("Config") or {}
    return ContainerInfo(
        id=container.get("Id", ""),
        name=container.get("Name", "").lstrip("/"),
        image=config.get("Image", ""),
        image_id=image_id,
        state=(container.get("State") or {}).get("Status", "unknown"),
        repo_digests=(images.get(image_id) or {}).get("RepoDigests") or [],
        labels=config.get("Labels") or {},
        ports=_published_ports(container),
        network_mode=(container.get("HostConfig") or {}).get("NetworkMode", ""),
    )


def _published_ports(container: dict) -> list[tuple[int, int, str]]:
    ports = []
    for spec, bindings in ((container.get("NetworkSettings") or {}).get("Ports") or {}).items():
        port, _, proto = spec.partition("/")
        if proto != "tcp" or not bindings:
            continue
        for b in bindings:
            if b.get("HostPort", "").isdigit():
                ports.append((int(port), int(b["HostPort"]), b.get("HostIp", "")))
    return ports


def dockhand_port(containers: list[ContainerInfo]) -> int | None:
    """Port on which a running Dockhand container is reachable from the network, if there is one."""
    for c in containers:
        if c.state != "running" or not c.image or c.image.startswith("sha256:"):
            continue
        if "dockhand" not in parse_image_ref(c.image).repository.rsplit("/", 1)[-1]:
            continue
        if c.network_mode == "host":
            return DOCKHAND_PORT
        public = [(cp, hp) for cp, hp, ip in c.ports if ip not in ("127.0.0.1", "::1")]
        for cp, hp in public:
            if cp == DOCKHAND_PORT:
                return hp
        if public:
            return public[0][1]
    return None


class DockerAPISource:
    def __init__(self, host: DockerHost):
        self.host = host

    def _client(self) -> httpx.AsyncClient:
        url = self.host.url
        if url.startswith("unix://"):
            transport = httpx.AsyncHTTPTransport(uds=url[len("unix://"):])
            return httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=30)
        if url.startswith("tcp://"):
            url = "http://" + url[len("tcp://"):]
        return httpx.AsyncClient(base_url=url, verify=self.host.tls_verify, timeout=30)

    async def version(self) -> str:
        async with self._client() as client:
            resp = await client.get("/version")
            resp.raise_for_status()
            return resp.json().get("Version", "?")

    async def list_containers(self) -> list[ContainerInfo]:
        # Only GET requests: works behind a docker-socket-proxy with POST=0.
        async with self._client() as client:
            resp = await client.get("/containers/json", params={"all": "true"})
            resp.raise_for_status()
            containers, images = [], {}
            for summary in resp.json():
                detail = await client.get(f"/containers/{summary['Id']}/json")
                detail.raise_for_status()
                container = detail.json()
                image_id = container.get("Image", "")
                if image_id and image_id not in images:
                    img = await client.get(f"/images/{image_id}/json")
                    images[image_id] = img.json() if img.status_code == 200 else {}
                containers.append(container)
            return [_from_inspect(c, images) for c in containers]


DockerRunner = Callable[[str, list[str]], Awaitable[tuple[int, str, str]]]


class DockerCLISource:
    """Lists containers through the Proxmox gateway's read-only docker-ps / docker-inspect commands."""

    def __init__(self, run: DockerRunner):
        self.run = run

    async def list_containers(self) -> list[ContainerInfo]:
        code, out, err = await self.run("ps", [])
        if code != 0:
            raise RuntimeError(f"docker ps failed: {err.strip() or out.strip() or f'exit code {code}'}")
        ids = out.split()
        if not ids:
            return []
        code, out, err = await self.run("inspect", ids)
        if code != 0:
            raise RuntimeError(f"docker inspect failed: {err.strip()}")
        containers = json.loads(out)
        image_ids = sorted({c.get("Image", "") for c in containers if c.get("Image")})
        images = {}
        if image_ids:
            code, out, _ = await self.run("image-inspect", image_ids)
            if code == 0:
                images = {img["Id"]: img for img in json.loads(out)}
        return [_from_inspect(c, images) for c in containers]
