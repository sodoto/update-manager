"""Container registry client: resolves the current digest of an image tag."""
import asyncio
import base64
import hashlib
import re
from dataclasses import dataclass

import httpx

from .config import RegistryAuth

DOCKER_HUB = "docker.io"
DOCKER_HUB_API = "registry-1.docker.io"
DOCKER_HUB_ALIASES = {"docker.io", "index.docker.io", "registry-1.docker.io", "registry.hub.docker.com"}

MANIFEST_ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])


class RegistryError(Exception):
    pass


@dataclass(frozen=True)
class ImageRef:
    registry: str
    repository: str
    tag: str | None
    digest: str | None

    @property
    def same_repo_key(self) -> tuple[str, str]:
        return (self.registry, self.repository)


def parse_image_ref(ref: str) -> ImageRef:
    digest = None
    if "@" in ref:
        ref, digest = ref.split("@", 1)
    first, sep, rest = ref.partition("/")
    if sep and ("." in first or ":" in first or first == "localhost"):
        registry = first
    else:
        registry, rest = DOCKER_HUB, ref
    tag = None
    if ":" in rest.rsplit("/", 1)[-1]:
        rest, tag = rest.rsplit(":", 1)
    if registry in DOCKER_HUB_ALIASES:
        registry = DOCKER_HUB
        if "/" not in rest:
            rest = "library/" + rest
    if tag is None and digest is None:
        tag = "latest"
    return ImageRef(registry, rest.lower(), tag, digest)


def local_digest_for(ref: ImageRef, repo_digests: list[str]) -> str | None:
    """Pick the RepoDigest that belongs to the same registry/repository as the image ref."""
    for rd in repo_digests:
        parsed = parse_image_ref(rd)
        if parsed.same_repo_key == ref.same_repo_key and parsed.digest:
            return parsed.digest
    return None


def _api_host(registry: str) -> str:
    return DOCKER_HUB_API if registry == DOCKER_HUB else registry


def _parse_challenge(header: str) -> tuple[str, dict[str, str]]:
    scheme, _, params = header.partition(" ")
    return scheme.lower(), dict(re.findall(r'(\w+)="([^"]*)"', params))


class RegistryClient:
    """One client per check run: digests are cached so each image is queried once."""

    def __init__(self, auths: list[RegistryAuth]):
        self._http = httpx.AsyncClient(timeout=20, follow_redirects=True)
        self._auths = {
            (DOCKER_HUB if a.registry in DOCKER_HUB_ALIASES else a.registry): a for a in auths
        }
        self._cache: dict[tuple, asyncio.Task] = {}
        self._labels: dict[str, asyncio.Task] = {}
        self._tokens: dict[tuple, str] = {}

    async def close(self):
        await self._http.aclose()

    async def remote_digest(self, ref: ImageRef) -> str:
        key = (ref.registry, ref.repository, ref.tag)
        if key not in self._cache:
            self._cache[key] = asyncio.create_task(self._fetch_digest(ref))
        return await self._cache[key]

    async def _fetch_digest(self, ref: ImageRef) -> str:
        host = _api_host(ref.registry)
        try:
            return await self._fetch_from(f"https://{host}", ref)
        except httpx.TransportError:
            # Plain-HTTP private registries (e.g. 192.168.1.5:5000)
            if ":" in host or host.startswith("localhost"):
                return await self._fetch_from(f"http://{host}", ref)
            raise

    async def _fetch_from(self, base: str, ref: ImageRef) -> str:
        url = f"{base}/v2/{ref.repository}/manifests/{ref.tag}"
        headers = {"Accept": MANIFEST_ACCEPT}
        resp = await self._http.head(url, headers=headers)
        if resp.status_code == 401:
            headers["Authorization"] = await self._authorize(ref, resp.headers.get("www-authenticate", ""))
            resp = await self._http.head(url, headers=headers)
        digest = resp.headers.get("docker-content-digest")
        if resp.status_code == 200 and digest:
            return digest
        if resp.status_code in (200, 405):
            # Some registries don't support HEAD or omit the digest header: fall back to GET.
            resp = await self._http.get(url, headers=headers)
            if resp.status_code == 200:
                return resp.headers.get("docker-content-digest") or "sha256:" + hashlib.sha256(resp.content).hexdigest()
        if resp.status_code == 404:
            raise RegistryError(f"tag '{ref.tag}' not found in registry")
        if resp.status_code in (401, 403):
            raise RegistryError("registry denied access (add credentials under 'registries')")
        if resp.status_code == 429:
            raise RegistryError("registry rate limit hit")
        raise RegistryError(f"registry returned HTTP {resp.status_code}")

    async def image_labels(self, ref: ImageRef, digest: str, os_: str = "linux", arch: str = "amd64") -> dict:
        """Labels of the image published at `digest` (for the given platform). Empty if unavailable."""
        key = f"{ref.registry}/{ref.repository}@{digest}/{os_}/{arch}"
        if key not in self._labels:
            self._labels[key] = asyncio.create_task(self._fetch_labels(ref, digest, os_, arch))
        try:
            return await self._labels[key]
        except Exception:
            return {}

    async def _fetch_labels(self, ref: ImageRef, digest: str, os_: str, arch: str) -> dict:
        manifest = (await self._get(ref, f"manifests/{digest}")).json()
        if "manifests" in manifest:  # multi-platform index: pick ours
            entries = [m for m in manifest["manifests"] if (m.get("platform") or {}).get("os") not in (None, "unknown")]
            match = next((m for m in entries if m["platform"].get("os") == os_
                          and m["platform"].get("architecture") == arch), entries[0] if entries else None)
            if not match:
                return {}
            manifest = (await self._get(ref, f"manifests/{match['digest']}")).json()
        config_digest = (manifest.get("config") or {}).get("digest")
        if not config_digest:
            return {}
        config = (await self._get(ref, f"blobs/{config_digest}")).json()
        return (config.get("config") or {}).get("Labels") or {}

    async def _get(self, ref: ImageRef, path: str) -> httpx.Response:
        """GET /v2/<repo>/<path>, authenticating once per repository."""
        url = f"https://{_api_host(ref.registry)}/v2/{ref.repository}/{path}"
        headers = {"Accept": MANIFEST_ACCEPT}
        key = (ref.registry, ref.repository)
        if key in self._tokens:
            headers["Authorization"] = self._tokens[key]
        resp = await self._http.get(url, headers=headers)
        if resp.status_code == 401:
            self._tokens[key] = await self._authorize(ref, resp.headers.get("www-authenticate", ""))
            headers["Authorization"] = self._tokens[key]
            resp = await self._http.get(url, headers=headers)
        resp.raise_for_status()
        return resp

    async def _authorize(self, ref: ImageRef, challenge: str) -> str:
        scheme, params = _parse_challenge(challenge)
        creds = self._auths.get(ref.registry)
        if scheme == "basic":
            if not creds:
                raise RegistryError("registry requires credentials")
            return "Basic " + base64.b64encode(f"{creds.username}:{creds.password}".encode()).decode()
        if scheme != "bearer" or "realm" not in params:
            raise RegistryError(f"unsupported registry auth: {challenge[:80]}")
        query = {"scope": params.get("scope") or f"repository:{ref.repository}:pull"}
        if "service" in params:
            query["service"] = params["service"]
        auth = (creds.username, creds.password) if creds else None
        resp = await self._http.get(params["realm"], params=query, auth=auth)
        if resp.status_code in (401, 403) and auth:
            # The saved login was rejected: public images still work anonymously.
            resp = await self._http.get(params["realm"], params=query)
            if resp.status_code != 200:
                raise RegistryError(f"the {ref.registry} login in Settings > Registry logins was rejected")
        if resp.status_code != 200:
            raise RegistryError(f"token request failed (HTTP {resp.status_code})")
        body = resp.json()
        token = body.get("token") or body.get("access_token")
        if not token:
            raise RegistryError("registry returned no token")
        return f"Bearer {token}"
