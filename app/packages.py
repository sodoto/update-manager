"""Parses the output of the gateway's package check (see gateway/update-manager-ro.sh)."""
import re
from dataclasses import dataclass, field


@dataclass
class PackageReport:
    manager: str = "unknown"
    os_name: str = ""
    packages: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    reboot_required: bool = False
    has_docker: bool = False
    community_script: str | None = None  # e.g. "homarr" for an LXC made by ct/homarr.sh
    app_versions: dict[str, str] = field(default_factory=dict)  # version files, e.g. {".homarr": "1.30.0"}

    @property
    def security_count(self) -> int:
        return sum(1 for p in self.packages if p.get("security"))


APT_RE = re.compile(r"^(\S+?)/(\S+)\s+(\S+)\s+\S+\s+\[upgradable from: ([^\]]+)\]")
APK_RE = re.compile(r"^(\S+)\s+\S+\s+\{.*?\}\s+\(.*?\)\s+\[upgradable from: (\S+)\]")
APK_VER_RE = re.compile(r"^(.+?)-(\d[^-]*-r\d+)$")
PACMAN_RE = re.compile(r"^(\S+)\s+(\S+)\s+->\s+(\S+)")


def _split_apk(pkg: str) -> tuple[str, str]:
    m = APK_VER_RE.match(pkg)
    return (m.group(1), m.group(2)) if m else (pkg, "")


def parse_check_output(output: str) -> PackageReport:
    report = PackageReport()
    for line in output.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("@@"):
            tag, _, value = line[2:].partition(" ")
            if tag == "PM":
                report.manager = value
            elif tag == "WARN":
                report.warnings.append(value)
            elif tag == "REBOOT":
                report.reboot_required = True
            elif tag == "OS":
                report.os_name = value.strip('"')
            elif tag == "DOCKER":
                report.has_docker = True
            elif tag == "CS" and value.startswith("ct/") and value.endswith(".sh"):
                report.community_script = value[3:-3]
            elif tag == "CSV" and "=" in value:
                key, _, version = value.partition("=")
                report.app_versions[key] = version
            continue
        pkg = None
        if report.manager == "apt":
            m = APT_RE.match(line)
            if m:
                pkg = {"name": m.group(1), "current": m.group(4), "new": m.group(3),
                       "security": "-security" in m.group(2)}
        elif report.manager == "apk":
            m = APK_RE.match(line)
            if m:
                name, new = _split_apk(m.group(1))
                pkg = {"name": name, "current": _split_apk(m.group(2))[1], "new": new}
        elif report.manager == "dnf":
            parts = line.split()
            if len(parts) == 3 and "." in parts[0] and not line.lower().startswith(("obsoleting", "last metadata")):
                pkg = {"name": parts[0], "current": "", "new": parts[1], "security": False}
        elif report.manager == "pacman":
            m = PACMAN_RE.match(line)
            if m:
                pkg = {"name": m.group(1), "current": m.group(2), "new": m.group(3)}
        if pkg:
            pkg.setdefault("security", False)
            report.packages.append(pkg)
    return report
