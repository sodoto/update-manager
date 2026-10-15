"""Dashboard login: hashed password, signed session cookies and login throttling."""
import base64
import hashlib
import hmac
import os
import secrets
import time

from .config import DATA_DIR

COOKIE = "um_session"
SESSION_DAYS = 30
SECRET_FILE = DATA_DIR / "session.key"
HASH_PREFIX = "scrypt$"

MAX_FAILURES = 5  # wrong passwords allowed per address...
LOCKOUT_SECONDS = 60  # ...before it has to wait this long
_failures: dict[str, list[float]] = {}


# ------------------------------------------------------------------ passwords

def is_hashed(value: str | None) -> bool:
    return bool(value) and value.startswith(HASH_PREFIX)


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"{HASH_PREFIX}{salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str | None) -> bool:
    if not is_hashed(stored):
        return False
    _, salt, digest = stored.split("$")
    candidate = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1)
    return hmac.compare_digest(candidate.hex(), digest)


# ------------------------------------------------------------------ sessions

def _secret() -> bytes:
    if not SECRET_FILE.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        SECRET_FILE.write_bytes(secrets.token_bytes(32))
        os.chmod(SECRET_FILE, 0o600)
    return SECRET_FILE.read_bytes()


def _sign(username: str, expires: int, password_hash: str) -> str:
    # The password hash is part of the signature: changing the password signs everyone out.
    msg = f"{username}\n{expires}\n{password_hash}".encode()
    return hmac.new(_secret(), msg, hashlib.sha256).hexdigest()


def make_session(username: str, password_hash: str) -> str:
    expires = int(time.time()) + SESSION_DAYS * 86400
    user = base64.urlsafe_b64encode(username.encode()).decode()
    return f"{user}.{expires}.{_sign(username, expires, password_hash)}"


def valid_session(token: str | None, username: str | None, password_hash: str | None) -> bool:
    if not token or not username or not password_hash:
        return False
    try:
        user, expires, sig = token.split(".")
        name = base64.urlsafe_b64decode(user.encode()).decode()
        expires_at = int(expires)
    except ValueError:
        return False
    return (name == username and expires_at > time.time()
            and hmac.compare_digest(sig, _sign(name, expires_at, password_hash)))


# ------------------------------------------------------------------ throttling

def retry_after(client: str) -> int:
    """Seconds this address must wait before trying again (0 = may try now)."""
    now = time.time()
    recent = [t for t in _failures.get(client, []) if now - t < LOCKOUT_SECONDS]
    _failures[client] = recent
    return int(LOCKOUT_SECONDS - (now - recent[0])) + 1 if len(recent) >= MAX_FAILURES else 0


def record_failure(client: str) -> None:
    _failures.setdefault(client, []).append(time.time())


def clear_failures(client: str) -> None:
    _failures.pop(client, None)
