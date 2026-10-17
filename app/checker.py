"""Runs update checks across all configured sources and keeps the results."""
import asyncio
import hashlib
import json
import logging
import re
from urllib.parse import urlsplit
from datetime import datetime, timezone

from .community import CommunityScripts
from .config import DATA_DIR, DockerHost, ProxmoxNode, Settings
from .docker_sources import ContainerInfo, DockerAPISource, DockerCLISource, dockhand_port
from .notify import send_notifications
from .packages import PackageReport, parse_check_output
from .proxmox import GATEWAY_VERSION, ProxmoxSession
from .registry import RegistryClient, local_digest_for, parse_image_ref
from .releases import SOURCE_LABEL, VERSION_LABEL, ReleaseNotes, clean_version, github_repo, image_page

log = logging.getLogger("update-manager")
STATE_FILE = DATA_DIR / "state.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _err(exc: BaseException) -> str:
    return str(exc) or exc.__class__.__name__


def _item(kind: str, item_id: str, group: str, source: str, name: str, detail: str, **extra) -> dict:
    item = {
        "id": item_id, "kind": kind, "group": group, "source": source, "name": name, "detail": detail,
        "status": "up_to_date", "note": "", "update_count": 0, "security_count": 0, "packages": [],
        "reboot_required": False, "link": None, "stale": False, "checked_at": _now(),
    }
    item.update(extra)
    return item


def _signature(item: dict) -> str:
    """Identifies *which* update is pending, so a notification is only sent once per update."""
    if item["kind"] == "docker":
        return item.get("remote_digest") or ""
    pkgs = sorted(f"{p['name']}={p['new']}" for p in item["packages"])
    pkgs += sorted(f"app:{a['name']}={a['latest']}" for a in item.get("app_updates", []) if a["update"])
    return hashlib.sha1("\n".join(pkgs).encode()).hexdigest()


async def _docker_item(c: ContainerInfo, registry: RegistryClient, group: str, source: str,
                       id_prefix: str, link: str | None, vmid: int | None = None,
                       notes: ReleaseNotes | None = None, token: str | None = None) -> dict:
    item = _item("docker", f"{id_prefix}:{c.name}", group, source, c.name, c.image, state=c.state, link=link,
                 vmid=vmid,
                 # Used by the "how to update" instructions.
                 container_id=c.id,
                 compose_dir=c.labels.get("com.docker.compose.project.working_dir"),
                 compose_service=c.labels.get("com.docker.compose.service"),
                 compose_project=c.labels.get("com.docker.compose.project"))
    if c.ignored:
        return {**item, "status": "skipped", "note": "ignored by label"}
    if not c.image or c.image.startswith("sha256:"):
        return {**item, "status": "skipped", "note": "created from an image ID, not a tag"}
    ref = parse_image_ref(c.image)
    if ref.digest:
        return {**item, "status": "skipped", "note": "pinned to a digest"}
    local = local_digest_for(ref, c.repo_digests)
    if not local:
        return {**item, "status": "skipped", "note": "locally built image (no registry digest)"}
    item["local_digest"] = local
    try:
        remote = await registry.remote_digest(ref)
    except Exception as exc:
        return {**item, "status": "error", "note": _err(exc)}
    item["remote_digest"] = remote
    item["image_page"] = image_page(ref)
    item["current_version"] = clean_version(c.image_labels.get(VERSION_LABEL))
    code_source = c.image_labels.get(SOURCE_LABEL)
    if remote != local:
        item.update(status="update_available", update_count=1, note="newer image published for this tag")
        new_labels = await registry.image_labels(ref, remote, c.os, c.arch)
        item["new_version"] = clean_version(new_labels.get(VERSION_LABEL))
        code_source = new_labels.get(SOURCE_LABEL) or code_source
        if notes:
            item["release_url"] = await notes.url(code_source, item["new_version"], token)
    if github_repo(code_source):
        item["source_url"] = f"https://github.com/{github_repo(code_source)}"
    return item


class CheckBusy(Exception):
    pass


class Checker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()
        self.community = CommunityScripts()
        self.releases = ReleaseNotes()
        self._busy: set[str] = set()  # items being re-checked on their own
        self.state = {
            "running": False, "last_check_started": None, "last_check_finished": None,
            "next_check": None, "items": [], "errors": [], "warnings": [],
        }
        self._warnings: list[dict] = []
        if STATE_FILE.exists():
            try:
                saved = json.loads(STATE_FILE.read_text(encoding="utf-8"))
                self.state.update({k: saved.get(k, v) for k, v in self.state.items()}, running=False)
            except Exception as exc:
                log.warning("Could not read %s: %s", STATE_FILE, exc)

    @property
    def running(self) -> bool:
        return self._lock.locked()

    def public_state(self) -> dict:
        return {**self.state, "running": self.running, "busy": sorted(self._busy),
                "configured": bool(self.settings.docker_hosts or self.settings.proxmox)}

    def _save(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        tmp.replace(STATE_FILE)

    # ------------------------------------------------------------------ full check

    async def run_check(self) -> None:
        if self._lock.locked():
            return
        async with self._lock:
            self.state["last_check_started"] = _now()
            self._warnings = []
            log.info("Update check started")
            registry = RegistryClient(self.settings.registries)
            sem = asyncio.Semaphore(max(1, self.settings.concurrency))
            jobs = [(f"docker:{h.name}", h.name, self._check_docker_host(h, registry, sem))
                    for h in self.settings.docker_hosts]
            jobs += [(f"pve:{n.name}", n.name, self._check_node(n, registry, sem)) for n in self.settings.proxmox]
            try:
                results = await asyncio.gather(*(j[2] for j in jobs), return_exceptions=True)
            finally:
                await registry.close()

            previous = {i["id"]: i for i in self.state["items"]}
            items, errors = [], []
            for (group, source, _), result in zip(jobs, results):
                if isinstance(result, BaseException):
                    log.warning("Check of %s failed: %s", source, _err(result))
                    errors.append({"source": source, "message": _err(result), "at": _now()})
                    # Keep the last known results for an unreachable source, flagged as stale.
                    items += [{**i, "stale": True} for i in previous.values() if i.get("group") == group]
                else:
                    items += result

            new_updates = [
                i for i in items
                if i["status"] == "update_available" and not i["stale"]
                and not (previous.get(i["id"], {}).get("status") == "update_available"
                         and _signature(previous[i["id"]]) == _signature(i))
            ]
            self.state.update(items=items, errors=errors, warnings=self._warnings, last_check_finished=_now())
            self._save()
            log.info("Update check finished: %d items, %d with updates, %d new",
                     len(items), sum(i["status"] == "update_available" for i in items), len(new_updates))
        await send_notifications(self.settings.notify, new_updates)

    async def _check_docker_host(self, host: DockerHost, registry: RegistryClient, sem,
                                 only: str | None = None) -> list[dict]:
        async with sem:
            containers = await DockerAPISource(host).list_containers()
        group = f"docker:{host.name}"
        link = host.dockhand_url
        port = dockhand_port(containers)
        hostname = None if host.url.startswith("unix://") else urlsplit(host.url).hostname
        if port and hostname:
            link = f"http://{hostname}:{port}"
        return list(await asyncio.gather(*(
            _docker_item(c, registry, group, host.name, group, link,
                         notes=self.releases, token=self.settings.github_token)
            for c in containers if only is None or c.name == only
        )))

    async def _check_node(self, node: ProxmoxNode, registry: RegistryClient, sem) -> list[dict]:
        group = f"pve:{node.name}"
        items = []
        async with ProxmoxSession(node) as session:
            version = await session.require_gateway()
            if version < GATEWAY_VERSION:
                self._warnings.append({"source": node.name, "message":
                    f"read-only gateway v{version} is outdated (v{GATEWAY_VERSION} available): paste the read-only "
                    "setup block from Settings on this node again (needed for community-script app updates)."})
            if node.check_host:
                async with sem:
                    items.append(await self._check_host(session, node))
            if node.check_lxc or node.check_docker_in_lxc:
                containers = [c for c in await session.list_lxc() if int(c["vmid"]) not in node.exclude]
                for result in await asyncio.gather(*(
                    self._check_lxc(session, node, ct, registry, sem) for ct in containers
                )):
                    items += result
        return items

    async def _check_host(self, session: ProxmoxSession, node: ProxmoxNode) -> dict:
        item = _item("host", f"host:{node.name}", f"pve:{node.name}", node.name, node.name, "Proxmox host",
                     node=node.name)
        code, out, err = await session.check_host(self.settings.refresh_package_lists)
        return self._apply_report(item, code, out, err)

    async def _check_lxc(self, session: ProxmoxSession, node: ProxmoxNode, ct: dict,
                         registry: RegistryClient, sem) -> list[dict]:
        vmid = int(ct["vmid"])
        name = ct.get("name") or f"CT {vmid}"
        group = f"pve:{node.name}"
        item = _item("lxc", f"lxc:{node.name}:{vmid}", group, node.name, name, f"CT {vmid}",
                     vmid=vmid, node=node.name)
        if ct.get("status") != "running":
            item.update(status="skipped", note="container is stopped")
            return [item] if node.check_lxc else []

        async with sem:
            try:
                code, out, err = await session.check_lxc(vmid, self.settings.refresh_package_lists)
            except Exception as exc:
                item.update(status="error", note=_err(exc))
                return [item] if node.check_lxc else []
        item, report = self._apply_report_full(item, code, out, err)
        has_docker = bool(report and report.has_docker)
        if report and report.community_script is not None:
            if not report.community_script:
                # Older gateway that couldn't read the script name: scripts name the LXC after themselves.
                report.community_script = re.sub(r"[^A-Za-z0-9._-]", "", name.lower())
            await self._check_app(item, report)
        ip = await session.lxc_ip(vmid)
        item["ip"] = ip  # shown in the "how to update" instructions (ssh root@ip)
        items = [item] if node.check_lxc else []

        if node.check_docker_in_lxc and has_docker:
            source = f"{node.name} / {name}"
            id_prefix = f"docker:{node.name}/{vmid}"
            try:
                async with sem:
                    containers = await DockerCLISource(session.docker_runner(vmid)).list_containers()
                link = node.dockhand_url
                port = dockhand_port(containers)
                if port and ip:
                    link = f"http://{ip}:{port}"
                    item["link"] = link  # this LXC runs its own Dockhand
                docker_items = await asyncio.gather(*(
                    _docker_item(c, registry, group, source, id_prefix, link, vmid,
                                 notes=self.releases, token=self.settings.github_token) for c in containers
                ))
                for d in docker_items:
                    d.update(ip=ip, lxc_name=name)
                items += docker_items
            except Exception as exc:
                items.append(_item("docker", f"{id_prefix}:*", group, source, f"Docker in {name}", "",
                                   status="error", note=_err(exc), vmid=vmid, lxc_name=name))
        return items

    # ------------------------------------------------------------------ single item

    async def check_item(self, item_id: str) -> None:
        """Re-checks one item: a host, an LXC (with the Docker containers inside it) or one Docker container."""
        item = next((i for i in self.state["items"] if i["id"] == item_id), None)
        if not item:
            raise LookupError("this item is no longer in the results, run a full check")
        if self.running:
            raise CheckBusy("a full check is running, wait for it to finish")
        if item_id in self._busy:
            raise CheckBusy("this item is already being checked")
        self._busy.add(item_id)
        registry = RegistryClient(self.settings.registries)
        try:
            fresh, remove = await self._recheck(item, registry)
        finally:
            await registry.close()
            self._busy.discard(item_id)

        # Swap the old entries for the fresh ones, keeping their place in the list.
        items, inserted = [], False
        for i in self.state["items"]:
            if i["id"] in remove:
                if not inserted:
                    items += fresh
                    inserted = True
                continue
            items.append(i)
        if not inserted:
            items += fresh
        self.state["items"] = items
        self._save()
        log.info("Re-checked %s", item_id)

    async def _recheck(self, item: dict, registry: RegistryClient) -> tuple[list[dict], set[str]]:
        """Returns (fresh items, ids of the items they replace)."""
        sem = asyncio.Semaphore(max(1, self.settings.concurrency))
        group = item["group"]
        if group.startswith("docker:"):  # Docker host reached through its API
            host = next((h for h in self.settings.docker_hosts if f"docker:{h.name}" == group), None)
            if not host:
                raise LookupError("this Docker host is no longer configured")
            return await self._check_docker_host(host, registry, sem, only=item["name"]), {item["id"]}

        node = next((n for n in self.settings.proxmox if f"pve:{n.name}" == group), None)
        if not node:
            raise LookupError("this Proxmox node is no longer configured")
        async with ProxmoxSession(node) as session:
            await session.require_gateway()
            if item["kind"] == "host":
                return [await self._check_host(session, node)], {item["id"]}

            vmid = item.get("vmid")
            docker_prefix = f"docker:{node.name}/{vmid}:"
            if item["kind"] == "lxc" or item["id"].endswith(":*"):
                # The LXC itself plus the Docker containers running inside it.
                ct = next((c for c in await session.list_lxc() if int(c["vmid"]) == vmid), None)
                remove = {i["id"] for i in self.state["items"]
                          if i["id"] == f"lxc:{node.name}:{vmid}" or i["id"].startswith(docker_prefix)}
                if not ct or vmid in node.exclude:
                    return [], remove  # the container was deleted or excluded
                return await self._check_lxc(session, node, ct, registry, sem), remove

            # One Docker container inside an LXC.
            containers = await DockerCLISource(session.docker_runner(vmid)).list_containers()
            match = next((c for c in containers if c.name == item["name"]), None)
            if not match:
                return [], {item["id"]}  # the container was removed
            fresh = await _docker_item(match, registry, group, item["source"], docker_prefix.rstrip(":"),
                                       item.get("link"), vmid, notes=self.releases,
                                       token=self.settings.github_token)
            fresh.update(ip=item.get("ip"), lxc_name=item.get("lxc_name"))
            return [fresh], {item["id"]}

    async def _check_app(self, item: dict, report: PackageReport) -> None:
        """Community-script LXC: compare the app version it recorded with the latest release."""
        item["community_script"] = report.community_script
        try:
            apps, note, self_updating = await self.community.check(
                report.community_script, report.app_versions, self.settings.github_token)
        except Exception as exc:
            apps, note, self_updating = [], f"app version check failed: {_err(exc)}", []
        item["app_updates"] = apps
        item["app_note"] = note
        item["app_self"] = self_updating
        if any(a["update"] for a in apps) and item["status"] == "up_to_date":
            item["status"] = "update_available"

    def _apply_report(self, item: dict, code: int, out: str, err: str) -> dict:
        return self._apply_report_full(item, code, out, err)[0]

    @staticmethod
    def _apply_report_full(item: dict, code: int, out: str, err: str) -> tuple[dict, PackageReport | None]:
        """Fills a package item from the check script output; also returns the parsed report."""
        if "@@PM" not in out:
            item.update(status="error", note=(err or out).strip()[-300:] or f"check failed (exit {code})")
            return item, None
        report: PackageReport = parse_check_output(out)
        prefix = f"{item['detail']} · " if item["kind"] == "lxc" else ""
        item.update(
            detail=f"{prefix}{report.os_name or 'Unknown OS'}",
            manager=report.manager,
            packages=report.packages,
            update_count=len(report.packages),
            security_count=report.security_count,
            reboot_required=report.reboot_required,
            note="; ".join(report.warnings),
            checked_at=_now(),
        )
        if report.manager == "unknown":
            item.update(status="skipped", note="no supported package manager found")
        elif report.packages:
            item["status"] = "update_available"
        else:
            item["status"] = "up_to_date"
        return item, report
