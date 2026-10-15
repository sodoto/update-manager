#!/bin/bash
# Update Manager read-only gateway - installed on the Proxmox host as /usr/local/bin/update-manager-ro.
#
# Update Manager's SSH key is locked to this script in /root/.ssh/authorized_keys:
#   command="/usr/local/bin/update-manager-ro",restrict ssh-ed25519 AAAA... update-manager
# so whatever the app sends, only the read-only commands below can run. Anything else is refused.
# The only thing a check may write is the package index cache (apt-get update / apk update),
# and only when the app asks for "refresh" - installed software is never touched.
set -u
VERSION=3

read -r -d '' CHECK <<'EOF'
export LC_ALL=C
if command -v apt-get >/dev/null 2>&1; then
  echo "@@PM apt"
  if [ "$REFRESH" = 1 ]; then apt-get update -qq >/dev/null 2>&1 || echo "@@WARN apt-get update failed"; fi
  apt list --upgradable 2>/dev/null | grep -v '^Listing'
elif command -v apk >/dev/null 2>&1; then
  echo "@@PM apk"
  if [ "$REFRESH" = 1 ]; then apk update -q >/dev/null 2>&1 || echo "@@WARN apk update failed"; fi
  apk list -u 2>/dev/null
elif command -v dnf >/dev/null 2>&1; then
  echo "@@PM dnf"
  if [ "$REFRESH" = 1 ]; then dnf -q check-update 2>/dev/null; else dnf -q -C check-update 2>/dev/null; fi
elif command -v yum >/dev/null 2>&1; then
  echo "@@PM dnf"
  if [ "$REFRESH" = 1 ]; then yum -q check-update 2>/dev/null; else yum -q -C check-update 2>/dev/null; fi
elif command -v pacman >/dev/null 2>&1; then
  echo "@@PM pacman"
  if command -v checkupdates >/dev/null 2>&1; then checkupdates 2>/dev/null
  else echo "@@WARN install pacman-contrib (checkupdates) to check Arch updates"; fi
else
  echo "@@PM unknown"
fi
[ -f /var/run/reboot-required ] && echo "@@REBOOT"
[ -f /etc/os-release ] && . /etc/os-release && echo "@@OS ${PRETTY_NAME:-$ID}"
command -v docker >/dev/null 2>&1 && echo "@@DOCKER"
# Proxmox VE community-scripts LXC: which script created it, and the app versions it recorded (read only).
if [ -f /usr/bin/update ] && grep -q "community-scripts/ProxmoxVE" /usr/bin/update 2>/dev/null; then
  echo "@@CS $(grep -o 'ct/[A-Za-z0-9._-]*\.sh' /usr/bin/update | head -n 1)"
  for f in /root/.[!.]* /opt/*_version.txt; do
    [ -f "$f" ] && [ "$(wc -c < "$f")" -le 64 ] || continue
    v=$(head -n 1 "$f" | tr -d '\r\n\t ')
    case "$v" in [0-9]*|v[0-9]*) echo "@@CSV $(basename "$f")=$v" ;; esac
  done
fi
exit 0
EOF

CMD="${SSH_ORIGINAL_COMMAND:-}"

deny() {
  logger -t update-manager-ro "refused: $CMD" 2>/dev/null
  echo "update-manager-ro: refused: $CMD" >&2
  exit 126
}

[[ "$CMD" == *$'\n'* ]] && deny
read -r -a ARGS <<< "$CMD"
N=${#ARGS[@]}

need_vmid() { [[ "${ARGS[1]:-}" =~ ^[0-9]{1,9}$ ]] || deny; VMID=${ARGS[1]}; }
need_refresh() {
  case "${1:-}" in refresh) R=1 ;; no-refresh) R=0 ;; *) deny ;; esac
}

case "${ARGS[0]:-}" in
  version)
    [ "$N" -eq 1 ] || deny
    echo "update-manager-ro $VERSION" ;;
  list-lxc)
    [ "$N" -eq 1 ] || deny
    exec pvesh get "/nodes/$(hostname)/lxc" --output-format json ;;
  lxc-interfaces)
    [ "$N" -eq 2 ] || deny
    need_vmid
    exec pvesh get "/nodes/$(hostname)/lxc/$VMID/interfaces" --output-format json ;;
  check-host)
    [ "$N" -eq 2 ] || deny
    need_refresh "${ARGS[1]}"
    exec sh -c "REFRESH=$R
$CHECK" ;;
  check-lxc)
    [ "$N" -eq 3 ] || deny
    need_vmid; need_refresh "${ARGS[2]}"
    exec pct exec "$VMID" -- sh -c "REFRESH=$R
$CHECK" ;;
  docker-ps)
    [ "$N" -eq 2 ] || deny
    need_vmid
    exec pct exec "$VMID" -- docker ps -aq --no-trunc ;;
  docker-inspect|docker-image-inspect)
    [ "$N" -ge 3 ] || deny
    need_vmid
    for id in "${ARGS[@]:2}"; do [[ "$id" =~ ^(sha256:)?[0-9a-f]{12,64}$ ]] || deny; done
    if [ "${ARGS[0]}" = docker-inspect ]; then
      exec pct exec "$VMID" -- docker container inspect "${ARGS[@]:2}"
    else
      exec pct exec "$VMID" -- docker image inspect "${ARGS[@]:2}"
    fi ;;
  *)
    deny ;;
esac
