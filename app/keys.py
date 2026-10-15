"""The app's own SSH key and the Proxmox setup snippet that grants it read-only access."""
import os
from pathlib import Path

import asyncssh

from .config import DATA_DIR

KEY_PATH = DATA_DIR / "id_ed25519"
GATEWAY_PATH = "/usr/local/bin/update-manager-ro"
GATEWAY_SCRIPT = Path(__file__).parent / "gateway" / "update-manager-ro.sh"


def ensure_app_key() -> Path:
    if not KEY_PATH.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        key = asyncssh.generate_private_key("ssh-ed25519", comment="update-manager")
        key.write_private_key(str(KEY_PATH))
        os.chmod(KEY_PATH, 0o600)
    return KEY_PATH


def public_key() -> str:
    key = asyncssh.read_private_key(str(ensure_app_key()))
    return key.export_public_key().decode().strip()


def setup_script() -> str:
    """Shell snippet to paste on each Proxmox node: installs the gateway and authorizes the key, locked to it."""
    pub = public_key()
    key_b64 = pub.split()[1]
    gateway = GATEWAY_SCRIPT.read_text(encoding="utf-8").replace("\r\n", "\n").rstrip()
    return f"""cat > {GATEWAY_PATH} <<'UM_GATEWAY_EOF'
{gateway}
UM_GATEWAY_EOF
chmod 755 {GATEWAY_PATH}
mkdir -p /root/.ssh && touch /root/.ssh/authorized_keys
sed -i --follow-symlinks '\\#{key_b64}#d' /root/.ssh/authorized_keys
echo 'command="{GATEWAY_PATH}",restrict {pub}' >> /root/.ssh/authorized_keys
echo 'Update Manager read-only access installed.'
"""
