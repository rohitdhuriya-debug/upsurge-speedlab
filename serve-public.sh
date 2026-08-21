#!/bin/bash
# Bring SpeedLab up and expose it on a public URL.
#
# The quick tunnel is ephemeral by design: the URL changes every run and dies
# with this process. Run this again to get a fresh one. For a URL that does not
# change, see the "Remote access" section of README.md.
set -u
cd "$(dirname "$0")"

PORT="${SPEEDLAB_HOST_PORT:-5090}"

if lsof -iTCP:"$PORT" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
  if ! docker ps --filter name=speedlab --format '{{.Ports}}' | grep -q ":$PORT->"; then
    echo "Port $PORT is in use by something that is not SpeedLab."
    echo "Pick another:  SPEEDLAB_HOST_PORT=5091 ./serve-public.sh"
    exit 1
  fi
fi

echo "Starting SpeedLab on 127.0.0.1:$PORT ..."
SPEEDLAB_HOST_PORT="$PORT" docker compose up -d || exit 1

for _ in $(seq 1 30); do
  curl -sf -m 3 -o /dev/null "http://127.0.0.1:$PORT/api/capabilities" && break
  sleep 1
done

ENGINE=$(curl -s -m 5 "http://127.0.0.1:$PORT/api/capabilities" \
  | sed -n 's/.*"engine":"\([a-z]*\)".*/\1/p')
echo "  local:  http://127.0.0.1:$PORT   (audio engine: ${ENGINE:-unknown})"

LOG=$(mktemp -t speedlab-tunnel)
cloudflared tunnel --url "http://127.0.0.1:$PORT" --no-autoupdate > "$LOG" 2>&1 &
TUNNEL_PID=$!
trap 'kill $TUNNEL_PID 2>/dev/null' INT TERM

URL=""
for _ in $(seq 1 40); do
  URL=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG" | head -1)
  [ -n "$URL" ] && break
  kill -0 $TUNNEL_PID 2>/dev/null || { echo "cloudflared exited:"; tail -5 "$LOG"; exit 1; }
  sleep 1
done

if [ -z "$URL" ]; then
  echo "Tunnel did not produce a URL. Output:"; tail -10 "$LOG"; exit 1
fi

echo "$URL" > .public_url
echo
echo "  PUBLIC: $URL"
echo "  (open to anyone with the link - no password)"
echo
echo "Leave this running. Ctrl-C stops the tunnel; the container keeps running"
echo "(stop it with: docker compose down)."
wait $TUNNEL_PID
