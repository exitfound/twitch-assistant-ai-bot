#!/usr/bin/env bash
# Owncast settings for the hall, run on the server next to the stack. See docs/KINOZAL.md
# Values come from the environment or from kinozal/.env beside this script; the environment wins

set -euo pipefail
shopt -s inherit_errexit

HERE="$(cd "$(dirname "$0")" && pwd)"
ENV_FILE="$HERE/.env"

# Also printed on an unknown command, with exit code 2
usage() {
  cat <<'EOF'
Usage: ./kinozal.sh <command>   (from kinozal/ on the server)
  setup    Every Owncast setting, the styling included; refused while Owncast gets a stream
           (KINOZAL_FORCE_STREAM=1 overrides)
  style    The page: custom.css with the avatar, custom.js, the offline text; safe on a live stream
  link     The OBS chat dock link (the streamer's permanent login)
Owncast is OWNCAST_API (default http://127.0.0.1:8080) with OWNCAST_ADMIN_PASSWORD.
EOF
}

# ====================================================================================================================
# config
# ====================================================================================================================

# Print an error and exit. Inside $(...) the caller exits too, thanks to inherit_errexit
die() {
  echo "kinozal: $*" >&2
  exit 1
}

# Exit if any of the tools is missing
need() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null || die "$tool is not installed"
  done
}

# Read NAME from kinozal/.env, empty if absent
from_env() { grep -m1 "^$1=" "$ENV_FILE" 2>/dev/null | cut -d= -f2- || true; }

# conf NAME [default]. The environment wins over the .env, the default comes last
conf() {
  local value="${!1:-}"
  [ -n "$value" ] || value="$(from_env "$1")"
  [ -n "$value" ] || value="${2:-}"
  [ -n "$value" ] || die "$1 is not set: export it or set it in $ENV_FILE (see .env.example)"
  printf '%s' "$value"
}

# checked NAME REGEX WHAT [default]. Like conf, but the value must match REGEX
checked() {
  local value
  value="$(conf "$1" "${4:-}")"
  [[ "$value" =~ ^$2$ ]] || die "$1=$value does not look like $3"
  printf '%s' "$value"
}

# 0 or 1, default 0. Only an explicit 1 lifts a guard
flag() {
  local value="${!1:-0}"
  [[ "$value" =~ ^[01]$ ]] || die "$1=$value: only 0 or 1"
  printf '%s' "$value"
}

channel() { checked TWITCH_CHANNEL '[A-Za-z0-9_]+' "a Twitch login"; }

# ====================================================================================================================
# owncast admin API
# ====================================================================================================================

# Set API and AUTH (the admin login). curl reads the login from a file in this run's private directory:
# on its command line ps would show it
owncast_open() {
  local password
  need curl python3
  API="$(conf OWNCAST_API http://127.0.0.1:8080)"
  API="${API%/}"
  password="$(conf OWNCAST_ADMIN_PASSWORD)"
  RUN_DIR="$(mktemp -d)"
  trap 'rm -rf "$RUN_DIR"' EXIT
  password="${password//\\/\\\\}"
  printf 'user = "admin:%s"\n' "${password//\"/\\\"}" >"$RUN_DIR/curl.conf"
  AUTH=(-K "$RUN_DIR/curl.conf")
}

# post SETTING [JSON]. Sends one setting and prints the reply. Without JSON the body is read from stdin
post() {
  local out
  if [ $# -ge 2 ]; then
    out="$(curl -sS "${AUTH[@]}" -H 'Content-Type: application/json' \
      -X POST "$API/api/admin/config/$1" -d "$2" 2>&1)" || true
  else
    out="$(curl -sS "${AUTH[@]}" -H 'Content-Type: application/json' \
      -X POST "$API/api/admin/config/$1" --data-binary @- 2>&1)" || true
  fi
  printf '%-34s %s\n' "$1" "$out"
  [[ "$out" == *'"success":true'* ]]
}

# Wrap stdin as {"value": "..."}
value() { python3 -c 'import json, sys; print(json.dumps({"value": sys.stdin.read()}))'; }

# A failed setting does not stop the rest. The failures are listed at the end by report
FAILED=()

try() {
  local step="$1"
  [ "$1" != post ] || step="$2"
  "$@" || FAILED+=("$step")
}

report() {
  [ ${#FAILED[@]} -eq 0 ] || die "not applied: ${FAILED[*]}"
}

# ====================================================================================================================
# style (the page only, no restart, safe during a stream)
# ====================================================================================================================

# Owncast keeps one offline text, custom.js swaps in the English one on the English page
OFFLINE='Трансляция ещё не запущена или прервалась. Видео появится здесь само – обновлять страницу не нужно.'

# Owncast cannot serve extra files, so the avatar goes into the CSS as a data URI (~6 KB).
# try turns set -e off, hence the explicit return
post_styles() {
  local avatar
  avatar="$(base64 -w0 "$HERE/owncast/avatar.webp")" || return 1
  {
    cat "$HERE/owncast/custom.css"
    printf '\n/* the streamer'"'"'s avatar (owncast/avatar.webp, 128px) */\n'
    printf '#kz-now .kz-ava { background-image: url("data:image/webp;base64,%s"); }\n' "$avatar"
  } | value | post customstyles
}

# The channel is a Twitch login (checked by channel()), so it is safe inside a JavaScript string and a sed pattern
post_script() { sed "s/__TWITCH_CHANNEL__/$1/g" "$HERE/owncast/custom.js" | value | post customjavascript; }

post_offline() { printf '%s' "$OFFLINE" | value | post offlinemessage; }

# style_upload CHANNEL
style_upload() {
  try post_styles
  try post_script "$1"
  try post_offline
}

cmd_style() {
  local chan
  chan="$(channel)"
  owncast_open
  style_upload "$chan"
  report
}

# ====================================================================================================================
# setup (every Owncast setting, refused during a stream)
# ====================================================================================================================

# Link previews show PREVIEW_SUMMARY from gate/src/gate/pages.py instead, since a crawler never gets past the gate.
# Keep the two texts the same
post_summary() { printf 'Кино со стрима %s · Films from the %s stream' "$1" "$1" | value | post serversummary; }

post_logo() {
  local logo
  logo="$(base64 -w0 "$HERE/owncast/logo.png")" || return 1
  printf 'data:image/png;base64,%s' "$logo" | value | post logo
}

# The key from kinozal/.env replaces Owncast's default abc123
post_stream_key() {
  printf '%s' "$1" | python3 -c 'import json, sys
print(json.dumps({"value": [{"key": sys.stdin.read(), "comment": "OBS Branch Output"}]}))' | post streamkeys
}

cmd_setup() {
  local chan force stream_key status
  chan="$(channel)"
  force="$(flag KINOZAL_FORCE_STREAM)"
  # Without a key (a local Owncast for checks) the stream keys stay as they are
  stream_key="${OWNCAST_STREAM_KEY:-$(from_env OWNCAST_STREAM_KEY)}"
  owncast_open

  # The video settings drop a live stream. Only a plain "online":false lets setup on: silence, an HTTP error or an
  # answer without it counts as streaming
  if [ "$force" != 1 ]; then
    status="$(curl -fsS "$API/api/status")" || die "Owncast did not answer /api/status: nothing changed"
    [[ "$status" == *'"online":false'* ]] ||
      die "Owncast is getting a stream or says nothing of it: the video settings would drop a stream;" \
        "run after it or with KINOZAL_FORCE_STREAM=1"
  fi

  # Server card. Spam protection is off, flooding is allowed in the hall chat
  try post name '{"value":"Кинозал"}'
  try post_summary "$chan"
  try post pagecontent '{"value":""}'
  try post disablesearchindexing '{"value":true}'
  try post tags '{"value":[]}'
  try post socialhandles '{"value":[]}'
  try post notifications/browser '{"value":{"enabled":false}}'
  try post chat/spamprotectionenabled '{"value":false}'

  # Page and logo. The player starts on load, muted by custom.js
  style_upload "$chan"
  try post_logo
  try post autoplay '{"value":"always"}'

  # Video passes through as OBS sends it, no transcoding
  try post video/streamoutputvariants '{"value":[{"name":"Исходник","videoPassthrough":true,"audioPassthrough":true,
    "videoBitrate":5500,"audioBitrate":160,"framerate":30,"cpuUsageLevel":2}]}'

  [ -z "$stream_key" ] || try post_stream_key "$stream_key"
  report
}

# ====================================================================================================================
# link
# ====================================================================================================================

# The OBS chat dock link logs the streamer in for good
cmd_link() {
  local dom secret key
  need python3
  dom="$(checked KINOZAL_DOMAIN '[A-Za-z0-9.-]+' "a domain")"
  secret="$(conf GATE_SECRET)"
  # Same as HOST_KEY in gate/src/gate/security.py. Valid until GATE_SECRET changes
  key="$(SECRET="$secret" python3 -c 'import hashlib, hmac, os
print(hmac.new(os.environ["SECRET"].encode(), b"host-dock", hashlib.sha256).hexdigest()[:32])')"
  echo "https://$dom/_gate/host?key=$key&next=/embed/chat/readwrite/"
}

# ====================================================================================================================
# commands
# ====================================================================================================================

case "${1:-help}" in
  setup) cmd_setup ;;
  style) cmd_style ;;
  link) cmd_link ;;
  help | -h | --help) usage ;;
  *)
    usage >&2
    exit 2
    ;;
esac
