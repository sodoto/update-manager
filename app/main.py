"""Update Manager web app: dashboard, settings, API and periodic scheduler. Read-only by design."""
import asyncio
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .checker import CheckBusy, Checker
from .config import (CONFIG_PATH, DockerHost, Notify, ProxmoxNode, Settings, load_settings,
                     save_settings, validation_message)
from .docker_sources import DockerAPISource
from .keys import ensure_app_key, public_key, setup_script
from .notify import send_test
from .proxmox import GATEWAY_VERSION, ProxmoxSession
from .selfupdate import CURRENT as APP_VERSION, AppUpdates

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("update-manager")
for noisy in ("asyncssh", "httpx"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

STATIC = Path(__file__).parent / "static"
MASK = "********"
APP_CHECK_INTERVAL = 6 * 3600
checker = Checker(load_settings())
app_updates = AppUpdates()
_background: set[asyncio.Task] = set()
_scheduler_task: asyncio.Task | None = None


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


async def _safe_check() -> None:
    try:
        await checker.run_check()
    except Exception:
        log.exception("Update check crashed")


async def _scheduler(run_now: bool) -> None:
    if run_now:
        _spawn(_safe_check())
    while True:
        interval = timedelta(minutes=max(5, checker.settings.check_interval_minutes))
        checker.state["next_check"] = (datetime.now(timezone.utc) + interval).isoformat(timespec="seconds")
        await asyncio.sleep(interval.total_seconds())
        _spawn(_safe_check())


async def _app_update_loop() -> None:
    while True:
        if checker.settings.check_app_updates:
            await app_updates.check()
        await asyncio.sleep(APP_CHECK_INTERVAL)


def _start_scheduler(run_now: bool) -> None:
    global _scheduler_task
    if _scheduler_task:
        _scheduler_task.cancel()
    _scheduler_task = asyncio.create_task(_scheduler(run_now))


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_app_key()
    if not CONFIG_PATH.exists():
        log.warning("No config at %s yet - open the Settings page to configure", CONFIG_PATH)
    _start_scheduler(checker.settings.check_on_start)
    _spawn(_app_update_loop())
    yield
    if _scheduler_task:
        _scheduler_task.cancel()
    for task in list(_background):
        task.cancel()


app = FastAPI(title="Update Manager", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
_basic = HTTPBasic(auto_error=False)


def require_auth(creds: HTTPBasicCredentials | None = Depends(_basic)) -> None:
    s = checker.settings
    if not s.ui_username:
        return
    ok = creds is not None and secrets.compare_digest(
        creds.username.encode(), s.ui_username.encode()
    ) and secrets.compare_digest(creds.password.encode(), (s.ui_password or "").encode())
    if not ok:
        raise HTTPException(401, "Unauthorized", headers={"WWW-Authenticate": 'Basic realm="Update Manager"'})


async def json_body(request: Request) -> dict:
    # Requiring JSON forces a CORS preflight, so other websites can't post to the API.
    if not request.headers.get("content-type", "").startswith("application/json"):
        raise HTTPException(415, "Expected application/json")
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(400, "Expected a JSON object")
    return body


# ---------------------------------------------------------------- secrets masking

def _masked(s: Settings) -> dict:
    d = s.model_dump(mode="json")
    if d["ui_password"]:
        d["ui_password"] = MASK
    for reg in d["registries"]:
        if reg["password"]:
            reg["password"] = MASK
    for key in ("ntfy_token", "discord_webhook"):
        if d["notify"][key]:
            d["notify"][key] = MASK
    return d


def _unmask_notify(new: dict, old: Notify) -> dict:
    new = dict(new)
    for key in ("ntfy_token", "discord_webhook"):
        if new.get(key) == MASK:
            new[key] = getattr(old, key)
    return new


def _unmask(new: dict, old: Settings) -> dict:
    if new.get("ui_password") == MASK:
        new["ui_password"] = old.ui_password
    old_passwords = {r.registry: r.password for r in old.registries}
    for reg in new.get("registries") or []:
        if reg.get("password") == MASK:
            reg["password"] = old_passwords.get(reg.get("registry"), "")
    if isinstance(new.get("notify"), dict):
        new["notify"] = _unmask_notify(new["notify"], old.notify)
    return new


def _validate(model, data: dict):
    try:
        return model.model_validate(data)
    except ValidationError as exc:
        raise HTTPException(422, validation_message(exc))


# ---------------------------------------------------------------- pages & API

@app.get("/api/health")
async def health():
    return {"ok": True}


@app.get("/", dependencies=[Depends(require_auth)])
async def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/state", dependencies=[Depends(require_auth)])
async def state():
    return {**checker.public_state(), "version": APP_VERSION,
            "app": app_updates.public(checker.settings.allow_self_update)}


@app.post("/api/app/check", dependencies=[Depends(require_auth)])
async def check_app_version(_: dict = Depends(json_body)):
    await app_updates.check()
    return app_updates.public(checker.settings.allow_self_update)


@app.post("/api/app/update", status_code=202, dependencies=[Depends(require_auth)])
async def self_update(_: dict = Depends(json_body)):
    info = app_updates.public(checker.settings.allow_self_update)
    if info["install"] != "lxc":
        raise HTTPException(400, "Self-update only works for LXC installs made with install-lxc.sh")
    if not checker.settings.allow_self_update:
        raise HTTPException(403, "Self-update is turned off in Settings")
    app_updates.request_update()
    return {"started": True, "from_version": APP_VERSION}


@app.get("/api/app/update-log", dependencies=[Depends(require_auth)])
async def self_update_log():
    return {"log": app_updates.log_tail()}


@app.post("/api/check", status_code=202, dependencies=[Depends(require_auth)])
async def check_now():
    if not checker.running:
        _spawn(_safe_check())
    return {"started": True}


@app.post("/api/check-item", dependencies=[Depends(require_auth)])
async def check_item(body: dict = Depends(json_body)):
    try:
        await checker.check_item(str(body.get("id", "")))
    except CheckBusy as exc:
        raise HTTPException(409, str(exc))
    except LookupError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:
        raise HTTPException(502, str(exc) or exc.__class__.__name__)
    return {"ok": True}


@app.get("/api/settings", dependencies=[Depends(require_auth)])
async def get_settings():
    return {
        "settings": _masked(checker.settings),
        "public_key": public_key(),
        "setup_script": setup_script(),
        "config_path": str(CONFIG_PATH),
    }


@app.put("/api/settings", dependencies=[Depends(require_auth)])
async def put_settings(body: dict = Depends(json_body)):
    old = checker.settings
    new = _validate(Settings, _unmask(body, old))
    save_settings(new)
    checker.settings = new
    sources_changed = any(
        getattr(old, f) != getattr(new, f) for f in ("docker_hosts", "proxmox", "registries")
    )
    _start_scheduler(run_now=sources_changed)
    log.info("Settings saved%s", " - starting a check" if sources_changed else "")
    return {"settings": _masked(new), "check_started": sources_changed}


@app.post("/api/test/proxmox", dependencies=[Depends(require_auth)])
async def test_proxmox(body: dict = Depends(json_body)):
    node = _validate(ProxmoxNode, body)
    try:
        async with ProxmoxSession(node, verify_host_key=bool(node.host_key)) as session:
            host_key = session.host_key()
            version = await session.gateway_version()
            if version is None:
                return {"ok": False, "host_key": host_key, "message":
                        "Connected, but the read-only gateway is not installed. Run the setup snippet on this node."}
            containers = await session.list_lxc()
    except Exception as exc:
        return {"ok": False, "message": str(exc) or exc.__class__.__name__}
    running = sum(1 for c in containers if c.get("status") == "running")
    message = f"Read-only gateway v{version} OK - {len(containers)} LXC(s), {running} running"
    if version < GATEWAY_VERSION:
        message += f" · Gateway outdated (v{GATEWAY_VERSION} available): paste the setup block again"
    return {"ok": True, "host_key": host_key, "message": message}


@app.post("/api/test/docker", dependencies=[Depends(require_auth)])
async def test_docker(body: dict = Depends(json_body)):
    host = _validate(DockerHost, body)
    source = DockerAPISource(host)
    try:
        version = await source.version()
        containers = await source.list_containers()
    except Exception as exc:
        return {"ok": False, "message": str(exc) or exc.__class__.__name__}
    return {"ok": True, "message": f"Docker {version} - {len(containers)} container(s)"}


@app.post("/api/test/notify", dependencies=[Depends(require_auth)])
async def test_notify(body: dict = Depends(json_body)):
    cfg = _validate(Notify, _unmask_notify(body, checker.settings.notify))
    errors = await send_test(cfg)
    return {"ok": not errors, "message": "; ".join(errors) or "Test notification sent"}
