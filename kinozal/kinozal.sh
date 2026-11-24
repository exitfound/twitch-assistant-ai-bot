#!/usr/bin/env bash
# The cinema hall's operations from a workstation (docs/KINOZAL.md «Эксплуатация»); ./kinozal.sh help lists them.
# Every value that differs between operators comes from the environment or from the bot's .env: its «Кинозал: сервер»
# section and the shared FILM_URL, FILM_API_TOKEN and TWITCH_CHANNEL. The host's own secrets never leave the host.

set -euo pipefail
shopt -s inherit_errexit

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="${KINOZAL_ENV:-$HERE/../.env}"
OWNCAST_ADMIN_PORT=8080

usage() {
  cat <<'EOF'
Usage: kinozal/kinozal.sh <command>   (make -C kinozal <command>)
  deploy            rsync kinozal/ to the host, write the host's .env, docker compose up;
                    a changed gate waits while a session is open (KINOZAL_FORCE_GATE=1 overrides)
  setup [--local]   every Owncast setting, the styling included; refused while Owncast gets a stream
                    (KINOZAL_FORCE_STREAM=1 overrides)
  style [--local]   custom.css with the avatar, custom.js and the offline text; safe on a live stream
  status            the stack, the session and the stream on the host
  link              the OBS chat dock link (the streamer's permanent login)
  stream-key        the stream key for OBS
  --local           talks to a local Owncast: OWNCAST_API and OWNCAST_ADMIN_PASSWORD from the environment.
EOF
}

# ====================================================================================================================
# config: the environment first, then the bot's .env; values bound for the host's shell are checked for their shape
# ====================================================================================================================

die() {
  echo "kinozal: $*" >&2
  exit 1
}

need() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null || die "$tool is not installed"
  done
}

from_env() { grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true; }

# conf NAME [default]: the value, else the default, else a stop that names what to set
conf() {
  local value="${!1:-}"
  [ -n "$value" ] || value="$(from_env "$1")"
  [ -n "$value" ] || value="${2:-}"
  [ -n "$value" ] || die "$1 is not set: export it or set it in $ENV_FILE (see .env.example)"
  printf '%s' "$value"
}

# checked NAME REGEX WHAT [default]
checked() {
  local value
  value="$(conf "$1" "${4:-}")"
  [[ "$value" =~ ^$2$ ]] || die "$1=$value does not look like $3"
  printf '%s' "$value"
}

# optional NAME REGEX WHAT: like checked, but empty is fine
optional() {
  local value="${!1:-}"
  [ -n "$value" ] || value="$(from_env "$1")"
  [[ -z "$value" || "$value" =~ ^$2$ ]] || die "$1 does not look like $3"
  printf '%s' "$value"
}

# A guard is lifted only by an explicit 1
flag() {
  local value="${!1:-0}"
  [[ "$value" =~ ^[01]$ ]] || die "$1=$value: only 0 or 1"
  printf '%s' "$value"
}

# The site's domain, taken from the bot's FILM_URL: the bot links to it, Caddy certifies it
domain() {
  local url
  url="$(conf FILM_URL)"
  [[ "$url" =~ ^https://([A-Za-z0-9.-]+)/?$ ]] || die "FILM_URL=$url: expected https://<the hall's domain>"
  printf '%s' "${BASH_REMATCH[1]}"
}

channel() { checked TWITCH_CHANNEL '[A-Za-z0-9_]+' "a Twitch login"; }

host_dir() { checked KINOZAL_DIR '/[A-Za-z0-9._/-]+' "an absolute path" /opt/kinozal; }

# ====================================================================================================================
# ssh: the login and the key come from the config; a key may also come from ssh-agent or ~/.ssh/config
# ====================================================================================================================

ssh_setup() {
  local key
  need ssh
  HOST="$(checked KINOZAL_HOST '([A-Za-z0-9._-]+@)?[A-Za-z0-9._-]+' "user@host or a host from ~/.ssh/config")"
  DIR="$(host_dir)"
  key="$(optional KINOZAL_KEY "[^']+" "a path without a single quote")"
  key="${key/#\~/$HOME}"
  SSH_OPTS=(-o BatchMode=yes)
  RSYNC_SSH="ssh -o BatchMode=yes"
  if [ -n "$key" ]; then
    [ -r "$key" ] || die "KINOZAL_KEY=$key: no such readable file"
    SSH_OPTS+=(-i "$key")
    # rsync splits its -e command on spaces and keeps single-quoted parts whole
    RSYNC_SSH+=" -i '$key'"
  fi
}

# The command is built here on purpose: its values are the checked ones above
# shellcheck disable=SC2029
remote() { ssh "${SSH_OPTS[@]}" "$HOST" "$@"; }

# run_remote FUNCTION ARGS...: the host runs the function, sent with the helpers it calls. Its stdin is
# /dev/null: a command reading stdin would otherwise swallow the rest of the script, which comes on stdin
run_remote() {
  {
    declare -f host_need "$1"
    printf '%q ' "$@"
    echo '</dev/null'
  } | remote bash -s
}

# A value of the host's .env, read when needed: those secrets are kept nowhere else
host_env() {
  local value
  value="$(remote "sed -n 's/^$1=//p' $DIR/.env | head -n1")"
  [ -n "$value" ] || die "the host's $DIR/.env has no $1: run deploy first"
  printf '%s' "$value"
}

# ====================================================================================================================
# host side: these functions run on the host through run_remote, with their arguments only
# ====================================================================================================================

host_need() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null || {
      echo "host: $tool is not installed" >&2
      return 1
    }
  done
  sudo -n true 2>/dev/null || {
    echo "host: sudo asks for a password; the deploy needs it without one" >&2
    return 1
  }
}

# host_deploy DIR FORCE_GATE KEY=value...: the host's .env, then the stack. Exit 3: the gate changed while
# a session is open and keeps running
host_deploy() {
  set -euo pipefail
  local dir="$1" force="$2" line running image hash label changed open
  shift 2
  host_need docker openssl
  umask 077
  cd "$dir"

  # The host's own secrets, generated once; they stay here
  if [ ! -f .env ]; then
    {
      echo "OWNCAST_ADMIN_PASSWORD=$(openssl rand -hex 16)"
      echo "OWNCAST_STREAM_KEY=$(openssl rand -hex 16)"
      echo "GATE_SECRET=$(openssl rand -hex 32)"
      echo "GATE_CODE_LEN=4"
    } >.env
    echo "host: secrets generated"
  fi

  # The operator's values replace their lines on every deploy
  for line in "$@"; do
    grep -v "^${line%%=*}=" .env >.env.new || true
    printf '%s\n' "$line" >>.env.new
    mv .env.new .env
  done

  # A new gate image or a changed gate config (its .env included) recreates the gate, which loses every
  # viewer's Twitch token: not while a session is open. A gate that does not answer counts as open
  sudo docker compose build -q gate
  running="$(sudo docker compose ps -q gate)"
  changed=""
  if [ -n "$running" ] && [ "$force" != 1 ]; then
    image="$(sudo docker image inspect -f '{{.Id}}' kinozal-gate)"
    hash="$(sudo docker compose config --hash gate | awk '{print $2}')"
    [ "$(sudo docker inspect -f '{{.Image}}' "$running")" = "$image" ] || changed=image
    label='{{index .Config.Labels "com.docker.compose.config-hash"}}'
    [ "$(sudo docker inspect -f "$label" "$running")" = "$hash" ] || changed="${changed:+$changed+}config"
  fi

  if [ -n "$changed" ]; then
    open="$(sudo docker compose exec -T gate python -c 'import json, urllib.request
now = urllib.request.urlopen("http://127.0.0.1:8000/_gate/now", timeout=5)
print(json.load(now)["open"])' 2>/dev/null || echo unknown)"
    if [ "$open" != False ]; then
      sudo docker compose up -d --no-deps owncast caddy
      sudo docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
      echo "gate: changed ($changed), but a session is open (open=$open): the old gate keeps running;" \
        "deploy again after !film stop or with KINOZAL_FORCE_GATE=1" >&2
      return 3
    fi
  fi

  sudo docker compose up -d --build --remove-orphans
  sudo docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
}

host_status() {
  set -euo pipefail
  local dir="$1"
  host_need docker
  cd "$dir"
  sudo docker compose ps --format 'table {{.Service}}\t{{.State}}\t{{.Status}}'
  printf 'session: '
  sudo docker compose exec -T gate python -c 'import urllib.request
print(urllib.request.urlopen("http://127.0.0.1:8000/_gate/now", timeout=5).read().decode())' 2>/dev/null ||
    echo "the gate does not answer"
  printf 'stream:  '
  sudo docker compose exec -T owncast wget -qO- http://127.0.0.1:8080/api/status 2>/dev/null ||
    printf 'Owncast does not answer'
  echo
}

# ====================================================================================================================
# owncast: its admin API through an SSH tunnel to a local socket, or a local Owncast with --local
# ====================================================================================================================

# Sets API, PASSWORD and CURL. --local reads OWNCAST_API and OWNCAST_ADMIN_PASSWORD from the environment only,
# so the host's password never reaches a local Owncast
owncast_open() {
  need curl python3
  if [ "${1:-}" = --local ]; then
    if [ -z "${OWNCAST_API:-}" ] || [ -z "${OWNCAST_ADMIN_PASSWORD:-}" ]; then
      die "--local needs OWNCAST_API and OWNCAST_ADMIN_PASSWORD, e.g. OWNCAST_API=http://127.0.0.1:18080"
    fi
    API="${OWNCAST_API%/}"
    PASSWORD="$OWNCAST_ADMIN_PASSWORD"
    CURL=()
    return
  fi
  ssh_setup
  PASSWORD="$(host_env OWNCAST_ADMIN_PASSWORD)"
  # A socket in a private directory: no local port to pick, none for another user to reach
  TUNNEL="$(mktemp -d)"
  trap owncast_close EXIT
  ssh "${SSH_OPTS[@]}" -o ExitOnForwardFailure=yes -M -S "$TUNNEL/control" -fNT \
    -L "$TUNNEL/owncast.sock:127.0.0.1:$OWNCAST_ADMIN_PORT" "$HOST"
  API="http://owncast"
  CURL=(--unix-socket "$TUNNEL/owncast.sock")
}

owncast_close() {
  if [ -n "${TUNNEL:-}" ]; then
    ssh -S "$TUNNEL/control" -O exit "$HOST" 2>/dev/null || true
    rm -rf "$TUNNEL"
  fi
}

# post SETTING [JSON]: one admin setting, its answer printed; without JSON the body comes on stdin
post() {
  local out
  if [ $# -ge 2 ]; then
    out="$(curl -sS "${CURL[@]}" -u "admin:$PASSWORD" -H 'Content-Type: application/json' \
      -X POST "$API/api/admin/config/$1" -d "$2" 2>&1)" || true
  else
    out="$(curl -sS "${CURL[@]}" -u "admin:$PASSWORD" -H 'Content-Type: application/json' \
      -X POST "$API/api/admin/config/$1" --data-binary @- 2>&1)" || true
  fi
  printf '%-34s %s\n' "$1" "$out"
  [[ "$out" == *'"success":true'* ]]
}

# stdin → {"value": stdin} as JSON
value() { python3 -c 'import json, sys; print(json.dumps({"value": sys.stdin.read()}))'; }

# Every setting is tried; the failed ones are named at the end
FAILED=()

# try STEP ARGS...: a failed step is named by its setting (post NAME …) or by its function
try() {
  local step="$1"
  [ "$1" != post ] || step="$2"
  "$@" || FAILED+=("$step")
}

report() {
  [ ${#FAILED[@]} -eq 0 ] || die "not applied: ${FAILED[*]}"
}

# ====================================================================================================================
# style: custom.css with the avatar, custom.js and the offline text; needs no restart, safe on a live stream
# ====================================================================================================================

OFFLINE='Трансляция ещё не запущена или прервалась. Видео появится здесь само – обновлять страницу не нужно.'

post_styles() {
  # the avatar is embedded in the CSS as a data: URI (~6 KB): Owncast cannot serve a separate file
  {
    cat "$HERE/owncast/custom.css"
    printf '\n/* the streamer'"'"'s avatar (owncast/avatar.webp, 128px) */\n'
    printf '#kz-now .kz-ava { background-image: url("data:image/webp;base64,%s"); }\n' \
      "$(base64 -w0 "$HERE/owncast/avatar.webp")"
  } | value | post customstyles
}

post_script() { value <"$HERE/owncast/custom.js" | post customjavascript; }

post_offline() { printf '%s' "$OFFLINE" | value | post offlinemessage; }

style_upload() {
  try post_styles
  try post_script
  try post_offline
}

cmd_style() {
  owncast_open "${1:-}"
  style_upload
  report
}

# ====================================================================================================================
# setup: every Owncast setting; the video ones drop a live stream, so a stream, or no answer, stops it
# ====================================================================================================================

post_summary() { printf 'Кино со стрима %s · Films from the %s stream' "$1" "$1" | value | post serversummary; }

post_logo() {
  printf 'data:image/png;base64,%s' "$(base64 -w0 "$HERE/owncast/logo.png")" | value | post logo
}

post_stream_key() {
  printf '%s' "$1" | python3 -c 'import json, sys
print(json.dumps({"value": [{"key": sys.stdin.read(), "comment": "OBS Source Record"}]}))' | post streamkeys
}

cmd_setup() {
  local chan force stream_key status
  chan="$(channel)"
  force="$(flag KINOZAL_FORCE_STREAM)"
  owncast_open "${1:-}"
  if [ "${1:-}" = --local ]; then
    stream_key="${OWNCAST_STREAM_KEY:-}"
  else
    stream_key="$(host_env OWNCAST_STREAM_KEY)"
  fi
  if [ "$force" != 1 ]; then
    status="$(curl -sS "${CURL[@]}" "$API/api/status")" || die "Owncast did not answer /api/status: nothing changed"
    [[ "$status" != *'"online":true'* ]] ||
      die "Owncast is getting a stream: the video settings would drop it; run after it or with KINOZAL_FORCE_STREAM=1"
  fi

  # --- server card ---
  try post name '{"value":"Кинозал"}'
  # both languages; a link preview takes the same text from the gate's entry page (PREVIEW_SUMMARY in gate/app.py),
  # since a crawler without the cookie never reaches Owncast
  try post_summary "$chan"
  try post pagecontent '{"value":""}'
  try post disablesearchindexing '{"value":true}'
  try post tags '{"value":[]}'
  try post socialhandles '{"value":[]}'
  try post notifications/browser '{"value":{"enabled":false}}'
  # flooding is allowed in the hall chat, so Owncast spam protection is off
  try post chat/spamprotectionenabled '{"value":false}'

  # --- styling and logo; the player starts on page load, muted (custom.js sets the volume to 0) ---
  style_upload
  try post_logo
  try post autoplay '{"value":"always"}'

  # --- video: pass through what OBS sends, no transcoding ---
  try post video/streamoutputvariants '{"value":[{"name":"Исходник","videoPassthrough":true,"audioPassthrough":true,
    "videoBitrate":5500,"audioBitrate":160,"framerate":30,"cpuUsageLevel":2}]}'

  # --- only our stream key: the default abc123 is removed ---
  [ -z "$stream_key" ] || try post_stream_key "$stream_key"
  report
}

# ====================================================================================================================
# link, stream-key, status: read from the host, kept nowhere else
# ====================================================================================================================

cmd_link() {
  local dom secret key
  need python3
  dom="$(domain)"
  ssh_setup
  secret="$(host_env GATE_SECRET)"
  # the gate derives the dock key the same way (HOST_KEY in gate/app.py): valid until GATE_SECRET changes
  key="$(SECRET="$secret" python3 -c 'import hashlib, hmac, os
print(hmac.new(os.environ["SECRET"].encode(), b"host-dock", hashlib.sha256).hexdigest()[:32])')"
  echo "https://$dom/_gate/host?key=$key&next=/embed/chat/readwrite/"
}

cmd_stream_key() {
  ssh_setup
  host_env OWNCAST_STREAM_KEY
  echo
}

cmd_status() {
  ssh_setup
  run_remote host_status "$DIR"
}

# ====================================================================================================================
# deploy: rsync, the host's .env from the bot's values, docker compose up
# ====================================================================================================================

cmd_deploy() {
  local dom chan token force client secret status=0
  need rsync
  dom="$(domain)"
  chan="$(channel)"
  token="$(checked FILM_API_TOKEN '[A-Za-z0-9_-]{16,}' "a token of 16+ letters and digits (openssl rand -hex 24)")"
  client="$(optional KINOZAL_TWITCH_CLIENT_ID '[A-Za-z0-9]+' "a Twitch app id")"
  secret="$(optional KINOZAL_TWITCH_CLIENT_SECRET '[A-Za-z0-9]+' "a Twitch app secret")"
  force="$(flag KINOZAL_FORCE_GATE)"
  ssh_setup

  remote "sudo mkdir -p $DIR && sudo chown \$(id -u):\$(id -g) $DIR"
  # --delete keeps the host's .env: an excluded file is neither sent nor deleted
  rsync -az --delete \
    --exclude .env --exclude kinozal.sh --exclude tools/ \
    --exclude __pycache__ --exclude .pytest_cache --exclude 'test_*.py' --exclude pytest.ini \
    --exclude 'requirements*.in' --exclude requirements-dev.txt --exclude .prettierrc.json --exclude Makefile \
    --exclude ruff.toml \
    -e "$RSYNC_SSH" "$HERE/" "$HOST:$DIR/"

  # TWITCH_CLIENT_* empty: the gate offers the code alone, no Twitch button
  run_remote host_deploy "$DIR" "$force" "KINOZAL_DOMAIN=$dom" "TWITCH_CHANNEL=$chan" "GATE_API_TOKEN=$token" \
    "TWITCH_CLIENT_ID=$client" "TWITCH_CLIENT_SECRET=$secret" || status=$?
  return "$status"
}

# ====================================================================================================================
# commands
# ====================================================================================================================

case "${1:-help}" in
  deploy) cmd_deploy ;;
  setup) cmd_setup "${2:-}" ;;
  style) cmd_style "${2:-}" ;;
  status) cmd_status ;;
  link) cmd_link ;;
  stream-key) cmd_stream_key ;;
  help | -h | --help) usage ;;
  *)
    usage >&2
    exit 2
    ;;
esac
