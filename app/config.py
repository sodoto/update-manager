"""Configuration: stored in config.yaml and edited from the Settings page."""
import logging
import os
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, ValidationError, model_validator

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", DATA_DIR / "config.yaml"))

log = logging.getLogger("update-manager")


class _Model(BaseModel):
    @model_validator(mode="before")
    @classmethod
    def _blank_to_default(cls, data):
        # Empty form fields mean "not set", so the default applies.
        if isinstance(data, dict):
            return {k: v for k, v in data.items() if v is not None and not (isinstance(v, str) and not v.strip())}
        return data


class RegistryAuth(_Model):
    registry: str = Field(min_length=1)  # e.g. docker.io, ghcr.io, registry.example.com:5000
    username: str = Field(min_length=1)
    password: str = ""


class DockerHost(_Model):
    name: str = Field(min_length=1)
    # unix:///var/run/docker.sock, tcp://socket-proxy:2375, https://host:2376
    url: str = Field(min_length=1)
    tls_verify: bool = True
    dockhand_url: str | None = None


class ProxmoxNode(_Model):
    name: str = Field(min_length=1)
    host: str = Field(min_length=1)
    port: int = 22
    username: str = "root"
    host_key: str | None = None  # trusted SSH host key ("ssh-ed25519 AAAA..."); filled by "Test"
    check_host: bool = True
    check_lxc: bool = True
    check_docker_in_lxc: bool = True
    exclude: list[int] = Field(default_factory=list)
    dockhand_url: str | None = None


class Notify(_Model):
    ntfy_url: str | None = None
    ntfy_token: str | None = None
    discord_webhook: str | None = None
    webhook_url: str | None = None


class Settings(_Model):
    check_interval_minutes: int = Field(default=360, ge=5)
    check_on_start: bool = True
    refresh_package_lists: bool = True  # run apt-get update / apk update before checking
    concurrency: int = Field(default=4, ge=1, le=32)
    ui_username: str | None = None
    ui_password: str | None = None
    docker_hosts: list[DockerHost] = Field(default_factory=list)
    proxmox: list[ProxmoxNode] = Field(default_factory=list)
    registries: list[RegistryAuth] = Field(default_factory=list)
    notify: Notify = Field(default_factory=Notify)

    @model_validator(mode="after")
    def _unique_names(self):
        names = [h.name for h in self.docker_hosts] + [n.name for n in self.proxmox]
        dupes = sorted({n for n in names if names.count(n) > 1})
        if dupes:
            raise ValueError(f"names must be unique: {', '.join(dupes)}")
        return self


def load_settings() -> Settings:
    if not CONFIG_PATH.exists():
        return Settings()
    try:
        return Settings.model_validate(yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8")) or {})
    except (ValidationError, yaml.YAMLError) as exc:
        log.error("Invalid %s, starting with defaults: %s", CONFIG_PATH, exc)
        return Settings()


def save_settings(settings: Settings) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(settings.model_dump(mode="json"), sort_keys=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    tmp.replace(CONFIG_PATH)


def validation_message(exc: ValidationError) -> str:
    parts = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        parts.append(f"{loc}: {err['msg']}" if loc else err["msg"])
    return "; ".join(parts)
