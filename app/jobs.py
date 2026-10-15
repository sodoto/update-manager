"""Runs updates (optional feature) through the update gateway and keeps their live output.

Only three actions exist, matching gateway/update-manager-apply.sh:
  upgrade         install package updates in an LXC
  reboot          restart an LXC
  compose-update  pull + recreate one Docker Compose service inside an LXC
"""
import asyncio
import itertools
import logging
from datetime import datetime, timezone

from .proxmox import UpdateSession

log = logging.getLogger("update-manager")

MAX_LINES = 4000
KEEP_JOBS = 30
MANAGERS = {"apt", "apk", "dnf", "pacman"}


class JobBusy(Exception):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def can_apply(item: dict, action: str) -> bool:
    if action in ("upgrade", "reboot"):
        return item["kind"] == "lxc" and item.get("vmid") is not None and (
            action == "reboot" or item.get("manager") in MANAGERS)
    if action == "compose-update":
        return (item["kind"] == "docker" and item.get("vmid") is not None
                and bool(item.get("compose_dir")) and bool(item.get("container_id")))
    return False


class Job:
    _ids = itertools.count(1)

    def __init__(self, item: dict, action: str):
        self.id = str(next(self._ids))
        self.item_id = item["id"]
        self.action = action
        verb = {"upgrade": "Installing updates on", "reboot": "Restarting", "compose-update": "Updating"}[action]
        where = f"CT {item['vmid']}" if item["kind"] == "lxc" else item.get("lxc_name") or f"CT {item['vmid']}"
        self.title = f"{verb} {item['name']} ({where})"
        self.status = "running"  # running | succeeded | failed
        self.exit_code: int | None = None
        self.started = _now()
        self.finished: str | None = None
        self.rechecked = False
        self.done = False  # True once the job and the re-check after it are both over
        self.lines: list[str] = []

    def add(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > MAX_LINES:
            del self.lines[: len(self.lines) - MAX_LINES]

    def summary(self) -> dict:
        return {"id": self.id, "item_id": self.item_id, "action": self.action, "title": self.title,
                "status": self.status, "exit_code": self.exit_code, "started": self.started,
                "finished": self.finished, "rechecked": self.rechecked, "done": self.done}

    def detail(self) -> dict:
        return {**self.summary(), "output": "\n".join(self.lines)}


class JobRunner:
    def __init__(self, checker):
        self.checker = checker
        self.jobs: dict[str, Job] = {}
        self._tasks: set[asyncio.Task] = set()

    def running_for(self, item_id: str) -> Job | None:
        return next((j for j in self.jobs.values() if j.item_id == item_id and j.status == "running"), None)

    def summaries(self) -> list[dict]:
        return [j.summary() for j in self.jobs.values()]

    def start(self, item: dict, action: str) -> Job:
        if not can_apply(item, action):
            raise ValueError("this action is not available for this item")
        if self.running_for(item["id"]):
            raise JobBusy("an update is already running for this item")
        if self.checker.running:
            raise JobBusy("a full check is running, try again when it finishes")
        node = next((n for n in self.checker.settings.proxmox if n.name == item.get("node")
                     or f"pve:{n.name}" == item.get("group")), None)
        if not node:
            raise ValueError("this Proxmox node is no longer configured")

        job = Job(item, action)
        self.jobs[job.id] = job
        for old in list(self.jobs)[:-KEEP_JOBS]:
            if self.jobs[old].status != "running":
                del self.jobs[old]

        vmid = int(item["vmid"])
        if action == "upgrade":
            args, recheck = ("upgrade-lxc", vmid), item["id"]
        elif action == "reboot":
            args, recheck = ("reboot-lxc", vmid), item["id"]
        else:
            args, recheck = ("compose-update", vmid, item["container_id"]), item["id"]
        task = asyncio.create_task(self._run(job, node, args, recheck))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        log.info("Job %s started: %s", job.id, job.title)
        return job

    async def _run(self, job: Job, node, args: tuple, recheck_id: str) -> None:
        try:
            async with UpdateSession(node) as session:
                if await session.apply_version() is None:
                    raise RuntimeError(UpdateSession.not_authorized)
                job.add(f"$ {' '.join(str(a) for a in args)}")
                code = await session.stream(*args, on_line=job.add)
            job.exit_code = code
            job.status = "succeeded" if code == 0 else "failed"
            if code != 0:
                job.add(f"Exited with code {code}.")
        except Exception as exc:
            job.add(f"ERROR: {str(exc) or exc.__class__.__name__}")
            job.status = "failed"
        job.finished = _now()
        log.info("Job %s %s: %s", job.id, job.status, job.title)

        # Refresh the row so the dashboard reflects the result.
        if job.action == "reboot":
            await asyncio.sleep(15)  # let the container boot
        try:
            await self.checker.check_item(recheck_id)
            job.rechecked = True
        except Exception as exc:
            log.warning("Re-check after job %s failed: %s", job.id, exc)
        job.done = True
