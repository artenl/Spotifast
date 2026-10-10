#!/usr/bin/env bash
# One-time hardening + Docker install for a fresh Debian/Ubuntu VPS.
# Run as root:  sudo bash scripts/bootstrap-vps.sh
#
# What it does (and nothing else):
#   - installs security updates and turns on automatic security upgrades
#   - installs Docker Engine + compose plugin (official repository)
#   - firewall (ufw): deny all inbound except SSH, 80/tcp, 443/tcp, 443/udp
#   - fail2ban for SSH brute force
# It does NOT touch your SSH configuration: see README for the key-only step.
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "run as root (sudo bash $0)" >&2
  exit 1
fi
. /etc/os-release
case "${ID}" in
  debian|ubuntu) ;;
  *) echo "only Debian/Ubuntu are supported (found ${ID})" >&2; exit 1 ;;
esac

echo "==> system updates"
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get -y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold upgrade
apt-get install -y --no-install-recommends \
  ca-certificates curl gnupg ufw fail2ban unattended-upgrades
dpkg-reconfigure -f noninteractive unattended-upgrades

echo "==> docker"
if ! command -v docker >/dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
systemctl enable --now docker

echo "==> firewall"
# Keep whatever port sshd really listens on, so this can't lock you out.
SSH_PORTS=$(sshd -T 2>/dev/null | awk '$1 == "port" {print $2}')
SSH_PORTS=${SSH_PORTS:-22}
ufw default deny incoming
ufw default allow outgoing
for port in $SSH_PORTS; do ufw limit "${port}/tcp" comment "ssh"; done
ufw allow 80/tcp comment "http (redirect + ACME)"
ufw allow 443/tcp comment "https"
ufw allow 443/udp comment "http/3"
ufw --force enable
ufw status verbose

echo "==> fail2ban (ssh)"
cat > /etc/fail2ban/jail.d/sshd.local <<EOF
[sshd]
enabled = true
port = ${SSH_PORTS// /,}
backend = systemd
maxretry = 5
bantime = 1h
EOF
systemctl enable --now fail2ban
systemctl restart fail2ban

echo
echo "Done. Note: Docker publishes ports itself, outside ufw; Spotifast only"
echo "publishes 80/443 (Caddy), which are open anyway. The app has no port."
