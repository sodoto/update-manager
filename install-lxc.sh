#!/usr/bin/env bash
# Installs or updates Updrift as a systemd service in a Debian/Ubuntu LXC.
#
# First install (as root):
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/sodoto/updrift/main/install-lxc.sh)"
# Update later:
#   update-manager-update
#
# Settings, results and the SSH key live in /var/lib/update-manager and are kept across updates.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/sodoto/updrift.git}"
BRANCH="${BRANCH:-main}"
APP_DIR=/opt/update-manager
SRC_DIR=$APP_DIR/src
DATA_DIR=/var/lib/update-manager

[ "$(id -u)" -eq 0 ] || { echo "Please run as root"; exit 1; }

echo "==> Installing system packages"
apt-get update -q
apt-get install -y -q python3 python3-venv git ca-certificates

echo "==> Downloading the latest version from $REPO_URL"
mkdir -p "$APP_DIR" "$DATA_DIR"
chmod 700 "$DATA_DIR"
rm -rf "$APP_DIR/app" "$APP_DIR/requirements.txt"  # layout used by older versions of this script
if [ -d "$SRC_DIR/.git" ]; then
  git -C "$SRC_DIR" remote set-url origin "$REPO_URL"  # the repository was renamed (update-manager -> updrift)
  git -C "$SRC_DIR" fetch -q --depth 1 origin "$BRANCH"
  git -C "$SRC_DIR" reset -q --hard FETCH_HEAD
else
  rm -rf "$SRC_DIR"
  git clone -q --depth 1 --branch "$BRANCH" "$REPO_URL" "$SRC_DIR"
fi
VERSION="$(cat "$SRC_DIR/VERSION")"

echo "==> Installing Python dependencies"
[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install -q --upgrade pip
"$APP_DIR/venv/bin/pip" install -q -r "$SRC_DIR/requirements.txt"

cat > /etc/systemd/system/update-manager.service <<EOF
[Unit]
Description=Updrift
After=network-online.target
Wants=network-online.target

[Service]
Environment=DATA_DIR=$DATA_DIR
Environment=UM_INSTALL=lxc
WorkingDirectory=$SRC_DIR
ExecStart=$APP_DIR/venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8080
Restart=on-failure

[Install]
WantedBy=multi-user.target
EOF

cat > /usr/local/bin/update-manager-update <<EOF
#!/usr/bin/env bash
# Downloads the latest Updrift from GitHub and reinstalls it (settings are kept).
set -euo pipefail
git -C "$SRC_DIR" fetch -q --depth 1 origin "$BRANCH"
git -C "$SRC_DIR" reset -q --hard FETCH_HEAD
exec bash "$SRC_DIR/install-lxc.sh"
EOF
chmod 755 /usr/local/bin/update-manager-update

# Self-update from the dashboard: the app only drops a request file; this separate systemd unit
# sees it and runs update-manager-update (the app can't update itself: the restart would cut it off).
# Written to a temp file then moved: this script may be the one running the update right now.
cat > /usr/local/bin/update-manager-selfupdate.tmp <<EOF
#!/usr/bin/env bash
rm -f "$DATA_DIR/self-update.request"
{
  echo "Started: \$(date -Is)"
  /usr/local/bin/update-manager-update
  echo "Exit code: \$?"
} > "$DATA_DIR/self-update.log" 2>&1
EOF
chmod 755 /usr/local/bin/update-manager-selfupdate.tmp
mv -f /usr/local/bin/update-manager-selfupdate.tmp /usr/local/bin/update-manager-selfupdate

cat > /etc/systemd/system/update-manager-selfupdate.service <<EOF
[Unit]
Description=Updrift self-update

[Service]
Type=oneshot
TimeoutStartSec=15min
ExecStart=/usr/local/bin/update-manager-selfupdate
EOF

cat > /etc/systemd/system/update-manager-selfupdate.path <<EOF
[Unit]
Description=Watch for Updrift self-update requests

[Path]
PathExists=$DATA_DIR/self-update.request

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable -q update-manager
systemctl enable -q --now update-manager-selfupdate.path
systemctl restart update-manager

IP=$(hostname -I | awk '{print $1}')
echo
echo "Updrift v$VERSION is running: http://$IP:8080"
echo "To update later, run: update-manager-update"
