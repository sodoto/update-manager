# Changelog

## 1.4.1 (2026-10-15)
- Update instructions only mention the Proxmox Console to open a container's shell (no SSH).

## 1.4.0 (2026-10-15)
- Click the version number in the header to check GitHub for a new version right away.
- Update instructions now run inside the container (its console or SSH, with its IP address)
  instead of through the Proxmox Shell. Snapshots are taken from the Proxmox web UI.

## 1.3.0 (2026-10-15)
- Update Manager tells you when a new version is out: a banner on the dashboard with "What's new".
- "Update now" button updates Update Manager itself from the dashboard (LXC installs). It only updates
  this app, never your containers or Proxmox. Can be turned off in Settings.

## 1.2.0 (2026-10-15)
- "How to update" button (i) on every item with updates: step-by-step instructions with copyable commands
  for LXCs (snapshot, upgrade, restart), the Proxmox host, and Docker containers (Dockhand or docker compose).
- Dashboard groups can be collapsed; the app remembers which ones. "Collapse all / Expand all" button.

## 1.1.0 (2026-10-15)
- Dockhand running inside each LXC is detected automatically; containers link to their own LXC's Dockhand.
- Read-only gateway v2 (adds `lxc-interfaces` to find an LXC's IP). Paste the setup block from Settings again on each node.
- The dashboard shows a real version number.

## 1.0.0 (2026-10-15)
- First release: read-only update dashboard for Proxmox LXCs (packages) and Docker containers (new images).
- Read-only SSH gateway on Proxmox, settings page, scheduled checks, ntfy / Discord / webhook notifications.
