"""Checks GitHub for new versions of Update Manager itself, and requests self-updates.

The app never updates itself in-process (the restart would kill the update halfway). On an LXC install,
install-lxc.sh sets up a systemd path unit that watches REQUEST_FILE and runs update-manager-update.
"""
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx

from .config import DATA_DIR

log = logging.getLogger("update-manager")

REPO = os.environ.get("UPDATE_REPO", "sodoto/update-manager")
BRANCH = os.environ.get("UPDATE_BRANCH", "main")
RAW_URL = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
INSTALL = os.environ.get("UM_INSTALL", "manual")  # "lxc" (install-lxc.sh), "docker", or "manual"
REQUEST_FILE = DATA_DIR / "self-update.request"
LOG_FILE = DATA_DIR / "self-update.log"

VERSION_FILE = Path(__file__).parent.parent / "VERSION"
CURRENT = VERSION_FILE.read_text(encoding="utf-8").strip() if VERSION_FILE.exists() else "dev"

SECTION_RE = re.compile(r"^## (\d+\.\d+\.\d+)\s*(?:\((.*?)\))?\s*$")


def parse_version(v: str) -> tuple[int, ...]:
    try:
        return tuple(int(p) for p in v.strip().lstrip("v").split("."))
    except ValueError:
        return (0,)


def parse_changelog(text: str) -> list[dict]:
    """Splits CHANGELOG.md into [{version, date, items: [str]}], newest first."""
    sections, current = [], None
    for line in text.splitlines():
        m = SECTION_RE.match(line)
        if m:
            current = {"version": m.group(1), "date": m.group(2) or "", "items": []}
            sections.append(current)
        elif current is not None:
            if line.startswith("- "):
                current["items"].append(line[2:].strip())
            elif line.startswith("  ") and current["items"] and line.strip():
                current["items"][-1] += " " + line.strip()
    return sections


class AppUpdates:
    def __init__(self):
        self.latest: str | None = None
        self.notes: list[dict] = []
        self.checked_at: str | None = None
        self.error: str | None = None

    @property
    def update_available(self) -> bool:
        return bool(self.latest) and CURRENT != "dev" and parse_version(self.latest) > parse_version(CURRENT)

    async def check(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                resp = await client.get(f"{RAW_URL}/VERSION")
                resp.raise_for_status()
                self.latest = resp.text.strip()
                notes = await client.get(f"{RAW_URL}/CHANGELOG.md")
                sections = parse_changelog(notes.text) if notes.status_code == 200 else []
            self.notes = [s for s in sections if parse_version(s["version"]) > parse_version(CURRENT)]
            self.error = None
        except Exception as exc:
            self.error = f"could not reach GitHub: {str(exc) or exc.__class__.__name__}"
            log.warning("Version check failed: %s", self.error)
        self.checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    @staticmethod
    def request_update() -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        REQUEST_FILE.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
        log.info("Self-update requested")

    @staticmethod
    def log_tail(lines: int = 60) -> str:
        if not LOG_FILE.exists():
            return ""
        return "\n".join(LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()[-lines:])

    def public(self, allow_self_update: bool) -> dict:
        return {
            "version": CURRENT, "latest": self.latest, "update_available": self.update_available,
            "notes": self.notes, "checked_at": self.checked_at, "error": self.error, "install": INSTALL,
            "can_self_update": INSTALL == "lxc" and allow_self_update,
            "update_pending": REQUEST_FILE.exists(), "repo": REPO,
        }
