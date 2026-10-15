#!/bin/bash
# Update Manager update gateway - installed on the Proxmox host as /usr/local/bin/update-manager-apply.
#
# OPTIONAL. Only installed if you allow Update Manager to run updates. It uses its own SSH key, locked to
# this script in /root/.ssh/authorized_keys:
#   command="/usr/local/bin/update-manager-apply",restrict ssh-ed25519 AAAA... update-manager-apply
# The read-only checks keep using update-manager-ro with the other key.
#
# Allowed actions (anything else is refused and logged):
#   upgrade-lxc <vmid>                 install package updates inside an LXC
#   reboot-lxc <vmid>                  restart an LXC
#   compose-update <vmid> <container>  pull + recreate one Docker Compose service inside an LXC; the folder and
#                                      service come from the container's own labels, never from the caller
set -u
VERSION=1

read -r -d '' UPGRADE <<'EOF'
# um-upgrade
export LC_ALL=C DEBIAN_FRONTEND=noninteractive
if command -v apt-get >/dev/null 2>&1; then
  apt-get update && apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold dist-upgrade
elif command -v apk >/dev/null 2>&1; then
  apk update && apk upgrade
elif command -v dnf >/dev/null 2>&1; then
  dnf -y upgrade
elif command -v yum >/dev/null 2>&1; then
  yum -y update
elif command -v pacman >/dev/null 2>&1; then
  pacman -Syu --noconfirm
else
  echo "No supported package manager found."; exit 3
fi
EOF

read -r -d '' COMPOSE <<'EOF'
# um-compose-update
set -f
cid="$1"
label() {
  v=$(docker inspect -f "{{ index .Config.Labels \"$1\" }}" "$cid" 2>/dev/null) || return 1
  [ "$v" = "<no value>" ] && v=""
  printf '%s' "$v"
}
docker inspect "$cid" >/dev/null 2>&1 || { echo "Container $cid not found."; exit 3; }
dir=$(label com.docker.compose.project.working_dir)
svc=$(label com.docker.compose.service)
proj=$(label com.docker.compose.project)
files=$(label com.docker.compose.project.config_files)
if [ -z "$dir" ] || [ -z "$svc" ]; then
  echo "This container was not started with Docker Compose. Update it from Dockhand."; exit 3
fi
if [ ! -d "$dir" ]; then
  echo "The Compose folder $dir does not exist in this container (the stack may be managed by Dockhand)."
  echo "Update it from Dockhand."; exit 3
fi
if docker compose version >/dev/null 2>&1; then dc="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then dc="docker-compose"
else echo "Docker Compose is not installed."; exit 3; fi
cd "$dir" || exit 3
set -- -p "$proj"
old_ifs=$IFS; IFS=,
for f in $files; do [ -f "$f" ] && set -- "$@" -f "$f"; done
IFS=$old_ifs
echo "==> $dc pull $svc   (in $dir)"
$dc "$@" pull "$svc" || exit $?
echo "==> $dc up -d $svc"
$dc "$@" up -d "$svc"
EOF

CMD="${SSH_ORIGINAL_COMMAND:-}"

deny() {
  logger -t update-manager-apply "refused: $CMD" 2>/dev/null
  echo "update-manager-apply: refused: $CMD" >&2
  exit 126
}

[[ "$CMD" == *$'\n'* ]] && deny
read -r -a ARGS <<< "$CMD"
N=${#ARGS[@]}

need_vmid() { [[ "${ARGS[1]:-}" =~ ^[0-9]{1,9}$ ]] || deny; VMID=${ARGS[1]}; }

case "${ARGS[0]:-}" in
  version)
    [ "$N" -eq 1 ] || deny
    echo "update-manager-apply $VERSION" ;;
  upgrade-lxc)
    [ "$N" -eq 2 ] || deny
    need_vmid
    logger -t update-manager-apply "upgrade-lxc $VMID" 2>/dev/null
    exec pct exec "$VMID" -- sh -c "$UPGRADE" ;;
  reboot-lxc)
    [ "$N" -eq 2 ] || deny
    need_vmid
    logger -t update-manager-apply "reboot-lxc $VMID" 2>/dev/null
    exec pct reboot "$VMID" ;;
  compose-update)
    [ "$N" -eq 3 ] || deny
    need_vmid
    [[ "${ARGS[2]}" =~ ^[0-9a-f]{12,64}$ ]] || deny
    logger -t update-manager-apply "compose-update $VMID ${ARGS[2]}" 2>/dev/null
    exec pct exec "$VMID" -- sh -c "$COMPOSE" sh "${ARGS[2]}" ;;
  *)
    deny ;;
esac
