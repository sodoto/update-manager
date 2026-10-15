"""App updates for LXCs created with the Proxmox VE community scripts (github.com/community-scripts/ProxmoxVE).

Those LXCs have /usr/bin/update pointing to ct/<app>.sh. That script's update function declares where the app
comes from, e.g.  check_for_gh_release "homarr" "homarr-labs/homarr"  and the installed version is recorded in
the LXC as /root/.homarr (older scripts: /opt/<APP>_version.txt). We read those (read-only, via the gateway) and
compare them with the project's latest GitHub release, the same way the script itself does.
Apps the scripts update through the OS package manager are already covered by the package check.
"""
import asyncio
import re
import time

import httpx

SCRIPT_URL = "https://raw.githubusercontent.com/community-scripts/ProxmoxVE/main/ct/{slug}.sh"
CACHE_SECONDS = 6 * 3600

CHECK_RE = re.compile(r'check_for_gh_release\s+"([^"]+)"\s+"([^"]+)"((?:\s+"[^"]*")*)')
ARG_RE = re.compile(r'"([^"]*)"')
LEGACY_REPO_RE = re.compile(r"api\.github\.com/repos/([\w.-]+/[\w.-]+)/releases/latest")
APP_RE = re.compile(r'^APP="([^"]+)"', re.M)


def _clean(version: str) -> str:
    return version.strip()[1:] if re.match(r"v\d", version.strip()) else version.strip()


class CommunityScripts:
    def __init__(self):
        self._cache: dict[str, tuple[float, object]] = {}
        self._inflight: dict[str, asyncio.Task] = {}

    async def _cached(self, key: str, fetch):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
        if key not in self._inflight:
            self._inflight[key] = asyncio.create_task(fetch())
        try:
            value = await self._inflight[key]
        finally:
            self._inflight.pop(key, None)
        self._cache[key] = (time.time(), value)
        return value

    async def _script(self, slug: str) -> str | None:
        async def fetch():
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(SCRIPT_URL.format(slug=slug))
            return resp.text if resp.status_code == 200 else None
        return await self._cached(f"script:{slug}", fetch)

    async def _latest(self, repo: str, token: str | None) -> str:
        async def fetch():
            headers = {"Accept": "application/vnd.github+json"}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            async with httpx.AsyncClient(timeout=20) as client:
                resp = await client.get(f"https://api.github.com/repos/{repo}/releases/latest", headers=headers)
            if resp.status_code == 200:
                return _clean(resp.json().get("tag_name", ""))
            if resp.status_code in (403, 429):
                raise RuntimeError("GitHub rate limit reached: add a GitHub token in Settings to check more often")
            raise RuntimeError(f"GitHub returned HTTP {resp.status_code} for {repo}")
        try:
            return await self._cached(f"release:{repo}", fetch)
        except RuntimeError:
            self._cache.pop(f"release:{repo}", None)  # don't cache failures
            raise

    async def check(self, slug: str, versions: dict[str, str], token: str | None) -> tuple[list[dict], str]:
        """Returns (components, note). Each component: name, repo, current, latest, update."""
        script = await self._script(slug)
        if not script:
            return [], f"community script ct/{slug}.sh not found"

        wanted = []  # (name, repo, version file keys, pinned)
        for name, repo, rest in CHECK_RE.findall(script):
            extra = ARG_RE.findall(rest)
            if len(extra) >= 3 and extra[2]:
                continue  # releases filtered by a tag prefix: not supported here
            pinned = extra[0] if extra else ""
            var = re.fullmatch(r"\$\{?(\w+)\}?", pinned)
            if var:  # pinned through a variable, e.g. RELEASE="v3.3.1" ... "${RELEASE}"
                assigned = re.search(rf'^\s*{var.group(1)}="([^"$]+)"', script, re.M)
                pinned = assigned.group(1) if assigned else ""
            key = "." + name.lower().replace(" ", "")
            wanted.append((name, repo, [key], pinned))
        if not wanted:
            # Older scripts: a GitHub API call and /opt/<APP>_version.txt
            repo, app = LEGACY_REPO_RE.search(script), APP_RE.search(script)
            if repo and app:
                wanted.append((app.group(1), repo.group(1), [f"{app.group(1)}_version.txt"], ""))
        if not wanted:
            return [], "this app is updated through the OS packages"

        # check_for_gh_release migrates /opt/*_version.txt to ~/.<app>: use it when that file doesn't exist yet.
        legacy = next((v for k, v in versions.items() if k.endswith("_version.txt")), None)
        components, errors = [], []
        for name, repo, keys, pinned in wanted:
            current = next((versions[k] for k in keys if k in versions), None)
            if current is None and len(wanted) == 1:
                current = legacy
            if current is None:
                continue  # component not installed (or version unknown)
            try:
                latest = _clean(pinned) if pinned else await self._latest(repo, token)
            except Exception as exc:
                errors.append(str(exc) or exc.__class__.__name__)
                continue
            current = _clean(current)
            components.append({"name": name, "repo": repo, "current": current, "latest": latest,
                               "update": bool(latest) and current != latest, "pinned": bool(pinned)})
        return components, "; ".join(dict.fromkeys(errors))
