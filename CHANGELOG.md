# Changelog

## 1.1.0 (2026-10-15)
- Dockhand running inside each LXC is detected automatically; containers link to their own LXC's Dockhand.
- Read-only gateway v2 (adds `lxc-interfaces` to find an LXC's IP). Paste the setup block from Settings again on each node.
- The dashboard shows a real version number.

## 1.0.0 (2026-10-15)
- First release: read-only update dashboard for Proxmox LXCs (packages) and Docker containers (new images).
- Read-only SSH gateway on Proxmox, settings page, scheduled checks, ntfy / Discord / webhook notifications.
