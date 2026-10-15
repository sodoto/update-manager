# Update Manager

One dashboard that shows which of your homelab apps need updates, without checking each one by hand. It is **read-only**: it can look, but it can never install, restart or change anything.

- **LXC containers (Proxmox)**: every running LXC is checked for pending OS package updates (apt, apk, dnf/yum, pacman), including security updates and pending reboots. The Proxmox host itself is checked too.
- **Docker containers**: each container's image digest is compared with the registry (Docker Hub, ghcr.io, lscr.io, quay.io, private registries). If a newer image was published for the same tag, it shows **New image**. Apply it from Dockhand; each container links there.
- **Automatic detection**: new LXCs and containers are picked up on every check, including **Docker running inside an LXC**. Checks run on a schedule, or whenever you click **Check now**.
- **Everything is configured in the web UI** (Settings page), with a *Test* button for each connection.
- **Notifications** (optional): ntfy, Discord or a generic webhook, sent only for *new* updates.

## How read-only is guaranteed

The app's limits are enforced by the systems it connects to, not just by the app's own code:

| Where | Enforcement |
|---|---|
| Proxmox | The app's SSH key is locked in `authorized_keys` with `command="/usr/local/bin/update-manager-ro",restrict`. Whatever the app sends, Proxmox only runs that gateway script. The script accepts a fixed list of read commands (list LXCs, check packages, `docker ps`/`inspect`), validates every argument, and refuses everything else. The key gets no shell, no port forwarding and no file transfer. |
| Docker hosts | Use a docker-socket-proxy with `POST=0`. It blocks every request that changes something (start, stop, pull, delete…). The app itself only sends GET requests. |
| The app | It contains no upgrade or write code at all. |

The only thing a check writes is the **package list cache** (`apt-get update` / `apk update`), so it knows which versions exist. It never installs anything. You can turn this off in Settings: *Refresh package lists before checking*.

The gateway script is [app/gateway/update-manager-ro.sh](app/gateway/update-manager-ro.sh); read it before installing.

## Install in an LXC (Proxmox)

1. Create a Debian 12 LXC (unprivileged, 1 core, 512 MB RAM, 4 GB disk) and open its console.
2. Run:
   ```bash
   apt-get update && apt-get install -y curl
   bash -c "$(curl -fsSL https://raw.githubusercontent.com/sodoto/update-manager/main/install-lxc.sh)"
   ```
3. Open `http://<lxc-ip>:8080` and go to **Settings**.

**Update to the latest version** (your settings are kept):
```bash
update-manager-update
```
The installed version is shown at the top of the dashboard.

## Install with Docker

```bash
git clone https://github.com/sodoto/update-manager.git && cd update-manager
docker compose up -d --build
```
Open `http://<host>:8080` and go to **Settings**. To also watch the Docker host this runs on, add a Docker host with URL `tcp://docker-socket-proxy:2375`. The proxy is already in the compose file, with `POST=0`.

To update: `git pull && docker compose up -d --build`.

## Connect a Proxmox node

1. In **Settings → Proxmox nodes**, open *Read-only setup* and click **Copy**.
2. In Proxmox, open **your node → Shell**, paste, and press Enter. This installs the gateway and authorizes the app's key for it only. Repeat on each node of a cluster.
3. Back in Settings, click **+ Add Proxmox node**, enter its IP, then click **Test connection**. The node's SSH host key is trusted automatically.
4. Click **Save settings**. A check starts right away.

**To revoke access:** remove the line containing `update-manager` from `/root/.ssh/authorized_keys` on the node.

## Notes

- **Ignore a container:** add the Docker label `update-manager.ignore=true`.
- **Ignore an LXC:** list its ID under *Skip these LXC IDs*.
- **Pinned and local images:** containers pinned to a digest, and locally built images, are shown as *Skipped*.
- **Where things are stored:** settings live in `config.yaml` in the data folder (`/var/lib/update-manager` in an LXC, `./data` with Docker), next to the app's SSH key. Keep that folder private.
- **Set a dashboard login** under **Settings → Dashboard login**. Without one, anyone on your network can open the dashboard and its settings.
