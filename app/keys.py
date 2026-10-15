"""The app's SSH keys and the Proxmox setup snippets that authorize them.

Two separate keys, each locked on Proxmox to its own gateway script:
- the read-only key (always): checks only, via update-manager-ro
- the update key (optional): installing updates, via update-manager-apply
"""
import os
from pathlib import Path

import asyncssh

from .config import DATA_DIR

GATEWAY_DIR = Path(__file__).parent / "gateway"

KEY_PATH = DATA_DIR / "id_ed25519"
GATEWAY_PATH = "/usr/local/bin/update-manager-ro"
GATEWAY_SCRIPT = GATEWAY_DIR / "update-manager-ro.sh"

UPDATE_KEY_PATH = DATA_DIR / "id_ed25519_update"
UPDATE_GATEWAY_PATH = "/usr/local/bin/update-manager-apply"
UPDATE_GATEWAY_SCRIPT = GATEWAY_DIR / "update-manager-apply.sh"


def _ensure_key(path: Path, comment: str) -> Path:
    if not path.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = asyncssh.generate_private_key("ssh-ed25519", comment=comment)
        key.write_private_key(str(path))
        os.chmod(path, 0o600)
    return path


def ensure_app_key() -> Path:
    return _ensure_key(KEY_PATH, "update-manager")


def ensure_update_key() -> Path:
    return _ensure_key(UPDATE_KEY_PATH, "update-manager-apply")


def _public(path: Path) -> str:
    return asyncssh.read_private_key(str(path)).export_public_key().decode().strip()


def public_key() -> str:
    return _public(ensure_app_key())


def _snippet(pub: str, gateway_path: str, gateway_script: Path, done: str) -> str:
    """Shell snippet to paste on each Proxmox node: installs a gateway and authorizes a key, locked to it."""
    key_b64 = pub.split()[1]
    gateway = gateway_script.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip()
    return f"""cat > {gateway_path} <<'UM_GATEWAY_EOF'
{gateway}
UM_GATEWAY_EOF
chmod 755 {gateway_path}
mkdir -p /root/.ssh && touch /root/.ssh/authorized_keys
sed -i --follow-symlinks '\\#{key_b64}#d' /root/.ssh/authorized_keys
echo 'command="{gateway_path}",restrict {pub}' >> /root/.ssh/authorized_keys
echo '{done}'
"""


def setup_script() -> str:
    return _snippet(public_key(), GATEWAY_PATH, GATEWAY_SCRIPT, "Update Manager read-only access installed.")


def update_setup_script() -> str:
    return _snippet(_public(ensure_update_key()), UPDATE_GATEWAY_PATH, UPDATE_GATEWAY_SCRIPT,
                    "Update Manager update access installed.")


def update_revoke_script() -> str:
    key_b64 = _public(ensure_update_key()).split()[1]
    return (f"sed -i --follow-symlinks '\\#{key_b64}#d' /root/.ssh/authorized_keys && rm -f {UPDATE_GATEWAY_PATH}\n"
            "echo 'Update access removed: Update Manager is read-only again.'\n")
