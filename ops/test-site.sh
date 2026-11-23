#!/usr/bin/env bash
# Run the AgencyOS Assistant test site on this computer, with Docker Desktop. Guide: docs/TEST_SITE.md.
#
#   ops/test-site.sh              start it and open your staff link (http://localhost:8090)
#   ops/test-site.sh --public     also make a public https address for testers elsewhere (needs: brew install cloudflared)
#   ops/test-site.sh --stop       stop it (chats and settings are kept)
#   ops/test-site.sh --reset      stop it and delete all its data
#
# The first run creates .env.testsite with fresh secrets. To use the real model, add your Together key there.
# The last line printed is your staff link.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV_FILE=.env.testsite
TUNNEL_LOG=.testsite-tunnel.log
TUNNEL_PID=.testsite-tunnel.pid
COMPOSE=(docker compose --env-file "$ENV_FILE" -f docker-compose.yml -f docker-compose.testsite.yml)
PUBLIC=0 OPEN=1 ACTION=start
for arg in "$@"; do
  case "$arg" in
    --public) PUBLIC=1 ;;
    --no-browser) OPEN=0 ;;
    --stop) ACTION=stop ;;
    --reset) ACTION=reset ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (see --help)" >&2; exit 2 ;;
  esac
done

say() { echo "==> $*" >&2; }

stop_tunnel() {
  if [[ -f $TUNNEL_PID ]]; then
    kill "$(cat "$TUNNEL_PID")" 2>/dev/null || true
    rm -f "$TUNNEL_PID"
  fi
}

if ! docker info >/dev/null 2>&1; then
  echo "Docker isn't running. Install Docker Desktop (docker.com), start it, then run this again." >&2
  exit 1
fi

if [[ ! -f $ENV_FILE ]]; then
  umask 077
  cat > "$ENV_FILE" <<EOF
# Test site settings (ops/test-site.sh). Private: don't share or commit this file.
CHAT_INVITE_SECRET=$(openssl rand -hex 32)
CHAT_DATA_ENCRYPTION_KEYS=$(openssl rand -base64 32)
CHAT_METRICS_TOKEN=$(openssl rand -hex 16)

# Real model (Together AI): remove the # from these three lines, put your API key after the =, and run
# ops/test-site.sh again. Without them, a stand-in gives the same canned answer to everything.
# CHAT_MODEL_BASE_URL=https://api.together.xyz/v1
# CHAT_MODEL_NAME=Qwen/Qwen2.5-7B-Instruct-Turbo
# CHAT_MODEL_API_KEY=
EOF
  say "Created $ENV_FILE with new secrets."
fi

if [[ $ACTION != start ]]; then
  stop_tunnel
  if [[ $ACTION == reset ]]; then
    "${COMPOSE[@]}" --profile monitoring down -v
    sed -i.bak '/^STAFF_INVITE=/d' "$ENV_FILE" && rm -f "$ENV_FILE.bak"
    say "Stopped, and deleted the test site's data."
  else
    "${COMPOSE[@]}" --profile monitoring down
    say "Stopped. Chats and settings are kept; start again with ops/test-site.sh."
  fi
  exit 0
fi

say "Building and starting (the first time takes a few minutes)..."
"${COMPOSE[@]}" up -d --build >&2

say "Waiting for the assistant to be ready..."
for _ in $(seq 1 90); do
  curl -sf http://localhost:8090/readyz >/dev/null 2>&1 && break
  sleep 2
done
curl -sf http://localhost:8090/readyz >/dev/null || { echo "It didn't start. Logs: docker compose logs api" >&2; exit 1; }

if grep -Eq '^CHAT_MODEL_API_KEY=.+' "$ENV_FILE"; then
  say "Model: real (Together AI)."
else
  say "Model: stand-in with canned answers. For real answers, add your Together key in $ENV_FILE and run this again."
fi

# Your own staff link: made once and kept in .env.testsite, so you stay the same user across restarts.
STAFF_INVITE=$(sed -n 's/^STAFF_INVITE=//p' "$ENV_FILE")
if [[ -z $STAFF_INVITE ]] || [[ $(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $STAFF_INVITE" \
    http://localhost:8090/chat/me) != 200 ]]; then
  created=$("${COMPOSE[@]}" exec -T api python -m chat_service.invites create --name "AgencyOS staff" \
    --org "AgencyOS" --role staff --days 365 --base-url http://localhost:8090 | tail -1)
  STAFF_INVITE=${created#*invite=}
  sed -i.bak '/^STAFF_INVITE=/d' "$ENV_FILE" && rm -f "$ENV_FILE.bak"
  echo "STAFF_INVITE=$STAFF_INVITE" >> "$ENV_FILE"
fi

BASE=http://localhost:8090
if [[ $PUBLIC == 1 ]]; then
  if ! command -v cloudflared >/dev/null; then
    echo "For a public address, install cloudflared first: brew install cloudflared" >&2
    exit 1
  fi
  stop_tunnel
  say "Opening a public address (Cloudflare quick tunnel)..."
  nohup cloudflared tunnel --no-autoupdate --url http://localhost:8090 >"$TUNNEL_LOG" 2>&1 &
  echo $! > "$TUNNEL_PID"
  for _ in $(seq 1 60); do
    BASE=$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)
    [[ -n $BASE ]] && break
    sleep 1
  done
  [[ -n $BASE ]] || { echo "The tunnel didn't start; see $TUNNEL_LOG" >&2; exit 1; }
  say "Public address: $BASE"
  say "It changes every time the tunnel restarts, and works only while this computer is awake and online."
  say "Make tester links after opening your staff link at this address (Testers tab), so they use it too."
fi

LINK="$BASE/?invite=$STAFF_INVITE"
say "Your staff link (private: it signs you in as staff):"
if [[ $OPEN == 1 ]] && command -v open >/dev/null; then open "$LINK"; fi
echo "$LINK"
