#!/bin/bash
# Provision a fresh Ubuntu 22.04/24.04 box to run SpeedLab. Run as root on the VPS.
#   curl -fsSL https://raw.githubusercontent.com/rohitdhuriya-debug/upsurge-speedlab/main/deploy/vps-setup.sh | bash -s -- speedlab.example.com
set -euo pipefail
DOMAIN="${1:-}"

apt-get update
apt-get install -y ca-certificates curl git
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  > /etc/apt/sources.list.d/docker.list
apt-get update
apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

git clone https://github.com/rohitdhuriya-debug/upsurge-speedlab.git /opt/speedlab || true
cd /opt/speedlab

cat > .env <<ENVEOF
SPEEDLAB_AUTH_TOKEN=
SPEEDLAB_PUBLIC=1
SPEEDLAB_MAX_UPLOAD_MB=2048
SPEEDLAB_MAX_FILES=25
SPEEDLAB_MAX_TOTAL_DISK_MB=40960
SPEEDLAB_HOST_PORT=5090
ENVEOF

docker compose up -d --build

if [ -n "$DOMAIN" ]; then
  apt-get install -y debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update && apt-get install -y caddy
  cat > /etc/caddy/Caddyfile <<CADDYEOF
$DOMAIN {
    reverse_proxy 127.0.0.1:5090
    request_body {
        max_size 2GB
    }
}
CADDYEOF
  systemctl reload caddy
  echo "Live at https://$DOMAIN once DNS A record points here. TLS is automatic."
else
  echo "Live on http://<this-server-ip>:5090 . Pass a domain to get HTTPS."
fi
