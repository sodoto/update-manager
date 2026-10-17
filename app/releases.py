"""Human-readable versions and release-note links for Docker images.

Images commonly carry OCI labels: org.opencontainers.image.version and org.opencontainers.image.source.
We read them from the running image and from the new one, and turn the source into a release-notes link.
"""
import re
import time

import httpx

from .registry import DOCKER_HUB, ImageRef

VERSION_LABEL = "org.opencontainers.image.version"
SOURCE_LABEL = "org.opencontainers.image.source"
CACHE_SECONDS = 6 * 3600

GITHUB_RE = re.compile(r"github\.com[/:]([\w.-]+)/([\w.-]+?)(?:\.git)?/*$")


def clean_version(value: str | None) -> str | None:
    """Keeps real versions; drops values like 'latest', 'main' or a commit hash."""
    value = (value or "").strip()
    if not value or not re.search(r"\d", value) or re.fullmatch(r"(sha-)?[0-9a-f]{7,40}", value):
        return None
    return value


def github_repo(source: str | None) -> str | None:
    match = GITHUB_RE.search((source or "").strip())
    return f"{match.group(1)}/{match.group(2)}" if match else None


def image_page(ref: ImageRef) -> str | None:
    """Web page listing the image's tags, when we know where it is."""
    if ref.registry == DOCKER_HUB:
        if ref.repository.startswith("library/"):
            return f"https://hub.docker.com/_/{ref.repository[8:]}/tags"
        return f"https://hub.docker.com/r/{ref.repository}/tags"
    return None  # other registries' web pages can't be derived reliably from the image name


class ReleaseNotes:
    def __init__(self):
        self._cache: dict[tuple, tuple[float, str]] = {}

    async def url(self, source: str | None, version: str | None, token: str | None) -> str | None:
        """The GitHub release page for that version if it exists, else the project's releases page."""
        repo = github_repo(source)
        if not repo:
            return source if (source or "").startswith("https://") else None
        releases = f"https://github.com/{repo}/releases"
        if not version:
            return releases
        key = (repo, version)
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        tags = [version] + ([version[1:]] if version.startswith("v") else [f"v{version}"])
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        found = releases
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                for tag in tags:
                    resp = await client.get(f"https://api.github.com/repos/{repo}/releases/tags/{tag}", headers=headers)
                    if resp.status_code == 200:
                        found = resp.json().get("html_url") or found
                        break
                    if resp.status_code in (403, 429):  # rate limited: don't cache the fallback
                        return releases
        except httpx.HTTPError:
            return releases
        self._cache[key] = (time.time(), found)
        return found
