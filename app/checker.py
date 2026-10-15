"""Runs update checks across all configured sources and keeps the results."""
import asyncio
import hashlib
import json
import logging
from urllib.parse import urlsplit
from datetime import datetime, timezone

from .config import DATA_DIR, DockerHost, ProxmoxNode, Settings
from .docker_sources import ContainerInfo, DockerAPISource, DockerCLISource, dockhand_port
from .notify import send_notifications
from .packages import PackageReport, parse_check_output
from .proxmox import GATEWAY_VERSION, ProxmoxSession
from .registry import RegistryClient, local_digest_for, parse_image_ref

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
    return hashlib.sha1("\n".join(pkgs).encode()).hexdigest()


async def _docker_item(c: ContainerInfo, registry: RegistryClient, group: str, source: str,
                       id_prefix: str, link: str | None, vmid: int | None = None) -> dict:
    item = _item("docker", f"{id_prefix}:{c.name}", group, source, c.name, c.image, state=c.state, link=link,
                 vmid=vmid,
                 # Used by the "how to update" instructions.
                 compose_dir=c.labels.get("com.docker.compose.project.working_dir"),
                 compose_service=c.labels.get("com.docker.compose.service"))
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
    if remote != local:
        item.update(status="update_available", update_count=1, note="newer image published for this tag")
    return item


class Checker:
    def __init__(self, settings: Settings):
        self.settings = settings
        self._lock = asyncio.Lock()
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
        return {**self.state, "running": self.running,
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

    async def _check_docker_host(self, host: DockerHost, registry: RegistryClient, sem) -> list[dict]:
        async with sem:
            containers = await DockerAPISource(host).list_containers()
        group = f"docker:{host.name}"
        link = host.dockhand_url
        port = dockhand_port(containers)
        hostname = None if host.url.startswith("unix://") else urlsplit(host.url).hostname
        if port and hostname:
            link = f"http://{hostname}:{port}"
        return list(await asyncio.gather(*(
            _docker_item(c, registry, group, host.name, group, link) for c in containers
        )))

    async def _check_node(self, node: ProxmoxNode, registry: RegistryClient, sem) -> list[dict]:
        group = f"pve:{node.name}"
        items = []
        refresh = self.settings.refresh_package_lists
        async with ProxmoxSession(node) as session:
            version = await session.require_gateway()
            if version < GATEWAY_VERSION:
                self._warnings.append({"source": node.name, "message":
                    f"read-only gateway v{version} is outdated (v{GATEWAY_VERSION} available): paste the setup "
                    "block from Settings on this node again to enable automatic Dockhand links."})
            if node.check_host:
                item = _item("host", f"host:{node.name}", group, node.name, node.name, "Proxmox host", node=node.name)
                async with sem:
                    code, out, err = await session.check_host(refresh)
                items.append(self._apply_report(item, code, out, err))
            if node.check_lxc or node.check_docker_in_lxc:
                containers = [c for c in await session.list_lxc() if int(c["vmid"]) not in node.exclude]
                for result in await asyncio.gather(*(
                    self._check_lxc(session, node, ct, registry, sem) for ct in containers
                )):
                    items += result
        return items

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
        item, has_docker = self._apply_report_full(item, code, out, err)
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
                    _docker_item(c, registry, group, source, id_prefix, link, vmid) for c in containers
                ))
                for d in docker_items:
                    d.update(ip=ip, lxc_name=name)
                items += docker_items
            except Exception as exc:
                items.append(_item("docker", f"{id_prefix}:*", group, source, f"Docker in {name}", "",
                                   status="error", note=_err(exc)))
        return items

    def _apply_report(self, item: dict, code: int, out: str, err: str) -> dict:
        return self._apply_report_full(item, code, out, err)[0]

    @staticmethod
    def _apply_report_full(item: dict, code: int, out: str, err: str) -> tuple[dict, bool]:
        """Fills a package item from the check script output; also returns whether Docker is installed."""
        if "@@PM" not in out:
            item.update(status="error", note=(err or out).strip()[-300:] or f"check failed (exit {code})")
            return item, False
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
        return item, report.has_docker
