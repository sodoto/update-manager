# Changelog

## 1.7.0 (2026-10-15)
- Built-in sign-in page instead of the browser's login pop-up, with a Log out button.
  You stay signed in for 30 days; changing the password signs out every other browser.
- The dashboard password is now stored hashed (existing passwords are converted automatically).
- Repeated wrong passwords lock sign-in for 60 seconds.
- Confirmations and messages use the app's own windows instead of the browser's pop-ups.

## 1.6.0 (2026-10-15)
- Optional: run updates from the app. An **Update** button installs package updates in an LXC or updates a
  Docker Compose container, with live output, then re-checks the row. Offers to restart the LXC when needed.
- Uses a second SSH key locked to a new gateway (`update-manager-apply`) that only allows those actions.
  Off by default, requires a dashboard login. Paste the update setup block from Settings on each node.
- The browser always loads the current scripts after an update (no more stale cached pages).

## 1.5.0 (2026-10-15)
- ↻ button on every row to check just that item again: an LXC (with the Docker containers inside it),
  a single Docker container, or the Proxmox host. Handy right after updating something.

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
