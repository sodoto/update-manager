"""Updrift web app: dashboard, settings, API and periodic scheduler. Read-only by design."""
import asyncio
import logging
import re
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import auth
from .checker import CheckBusy, Checker
from .config import (CONFIG_PATH, DockerHost, Notify, ProxmoxNode, Settings, load_settings,
                     save_settings, validation_message)
from .docker_sources import DockerAPISource
from .jobs import JobBusy, JobRunner
from .keys import ensure_app_key, public_key, setup_script, update_revoke_script, update_setup_script
from .notify import send_test
from .proxmox import APPLY_VERSION, GATEWAY_VERSION, ProxmoxSession, UpdateSession
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
jobs = JobRunner(checker)
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


def _migrate_plain_password() -> None:
    """Older versions stored the dashboard password in plain text: hash it."""
    s = checker.settings
    if s.ui_password and not auth.is_hashed(s.ui_password):
        checker.settings = s.model_copy(update={"ui_password": auth.hash_password(s.ui_password)})
        save_settings(checker.settings)
        log.info("Dashboard password is now stored hashed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    ensure_app_key()
    _migrate_plain_password()
    if not CONFIG_PATH.exists():
        log.warning("No config at %s yet - open the Settings page to configure", CONFIG_PATH)
    _start_scheduler(checker.settings.check_on_start)
    _spawn(_app_update_loop())
    yield
    if _scheduler_task:
        _scheduler_task.cancel()
    for task in list(_background):
        task.cancel()


app = FastAPI(title="Updrift", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def _login_enabled() -> bool:
    return bool(checker.settings.ui_username and checker.settings.ui_password)


def _signed_in(request: Request) -> bool:
    s = checker.settings
    return not _login_enabled() or auth.valid_session(request.cookies.get(auth.COOKIE), s.ui_username, s.ui_password)


def require_auth(request: Request) -> None:
    if not _signed_in(request):
        raise HTTPException(401, "Please sign in")


def _set_session(response: Response, request: Request, username: str, password_hash: str) -> None:
    response.set_cookie(
        auth.COOKIE, auth.make_session(username, password_hash), max_age=auth.SESSION_DAYS * 86400,
        httponly=True, samesite="strict", secure=request.url.scheme == "https", path="/",
    )


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
    if d["github_token"]:
        d["github_token"] = MASK
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
    elif new.get("ui_password"):
        new["ui_password"] = auth.hash_password(new["ui_password"])  # a new password was typed
    if new.get("github_token") == MASK:
        new["github_token"] = old.github_token
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


def _page(name: str) -> HTMLResponse:
    # Version-stamp the scripts and styles so browsers fetch fresh copies after every update.
    html = (STATIC / name).read_text(encoding="utf-8")
    html = re.sub(r'(static/[\w.-]+\.(?:js|css))"', rf'\1?v={APP_VERSION}"', html)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/login")
async def login_page(request: Request):
    if not _login_enabled() or _signed_in(request):
        return RedirectResponse("./", status_code=303)
    return _page("login.html")


@app.post("/api/login")
async def login(request: Request, response: Response, body: dict = Depends(json_body)):
    client = request.client.host if request.client else "?"
    wait = auth.retry_after(client)
    if wait:
        raise HTTPException(429, f"Too many attempts. Try again in {wait} seconds.")
    s = checker.settings
    username, password = str(body.get("username", "")), str(body.get("password", ""))
    ok = (_login_enabled() and secrets.compare_digest(username.encode(), (s.ui_username or "").encode())
          and auth.verify_password(password, s.ui_password))
    if not ok:
        auth.record_failure(client)
        log.warning("Failed sign-in for %r from %s", username, client)
        raise HTTPException(401, "Wrong username or password")
    auth.clear_failures(client)
    _set_session(response, request, s.ui_username, s.ui_password)
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response, _: dict = Depends(json_body)):
    response.delete_cookie(auth.COOKIE, path="/")
    return {"ok": True}


@app.get("/")
async def index(request: Request):
    if not _signed_in(request):
        return RedirectResponse("login", status_code=303)
    return _page("index.html")


@app.get("/api/state", dependencies=[Depends(require_auth)])
async def state():
    return {**checker.public_state(), "version": APP_VERSION,
            "allow_updates": checker.settings.allow_updates, "jobs": jobs.summaries(),
            "login_enabled": _login_enabled(),
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
async def check_now(_: dict = Depends(json_body)):
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
        "update_setup_script": update_setup_script(),
        "update_revoke_script": update_revoke_script(),
        "config_path": str(CONFIG_PATH),
    }


@app.put("/api/settings", dependencies=[Depends(require_auth)])
async def put_settings(request: Request, response: Response, body: dict = Depends(json_body)):
    old = checker.settings
    new = _validate(Settings, _unmask(body, old))
    save_settings(new)
    checker.settings = new
    if new.ui_username and (new.ui_username, new.ui_password) != (old.ui_username, old.ui_password):
        _set_session(response, request, new.ui_username, new.ui_password)  # keep whoever changed it signed in
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


@app.post("/api/test/apply", dependencies=[Depends(require_auth)])
async def test_apply(body: dict = Depends(json_body)):
    node = _validate(ProxmoxNode, body)
    try:
        async with UpdateSession(node) as session:
            version = await session.apply_version()
    except Exception as exc:
        return {"ok": False, "message": str(exc) or exc.__class__.__name__}
    if version is None:
        return {"ok": False, "message": "Connected, but the update gateway did not answer. Paste the update setup block again."}
    if version < APPLY_VERSION:
        return {"ok": True, "message": f"Update access v{version} works, but v{APPLY_VERSION} is available "
                                       "(live output): paste the update setup block on this node again"}
    return {"ok": True, "message": f"Update access v{version} OK on this node"}


@app.post("/api/jobs", dependencies=[Depends(require_auth)])
async def start_job(body: dict = Depends(json_body)):
    if not checker.settings.allow_updates:
        raise HTTPException(403, "Running updates is turned off (Settings > Run updates)")
    item = next((i for i in checker.state["items"] if i["id"] == body.get("id")), None)
    if not item:
        raise HTTPException(404, "this item is no longer in the results")
    try:
        return jobs.start(item, str(body.get("action", ""))).summary()
    except JobBusy as exc:
        raise HTTPException(409, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/jobs/{job_id}", dependencies=[Depends(require_auth)])
async def get_job(job_id: str):
    job = jobs.jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job.detail()


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
