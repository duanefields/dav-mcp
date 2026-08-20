#!/usr/bin/env bash
#
# Poll a running dav-mcp server and report to a dead-man's-switch.
#
# Intended for cron:
#   */10 * * * * /Users/you/Code/dav-mcp/scripts/healthcheck.sh >> /Users/you/.dav-mcp/check.log 2>&1
#
# Configuration comes from ~/.dav-mcp/check.env, if it exists:
#
#   HEALTH_URL=http://127.0.0.1:18790/health
#   PING_URL=https://hc-ping.com/your-uuid-here
#
# chmod 600 that file: the ping URL is a capability, not just an address. Use a
# different healthchecks.io UUID from things-mcp, or one service's silence gets
# masked by the other's pings.
#
# Deliberately not `set -e`: the point is to collect every problem and still
# report, rather than dying on the first one and pinging nothing.
set -uo pipefail

CONFIG="${DAV_MCP_CHECK_ENV:-$HOME/.dav-mcp/check.env}"
# shellcheck disable=SC1090
[[ -f "$CONFIG" ]] && source "$CONFIG"

HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:18790/health}"
PING_URL="${PING_URL:-}"

stamp() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(stamp)] $*"; }

ping_hc() {
  [[ -z "$PING_URL" ]] && return 0
  local suffix="$1" body="${2:-}"
  curl -fsS -m 10 --data-raw "$body" "${PING_URL}${suffix}" >/dev/null 2>&1 || true
}

ping_hc "/start"

problems=()

# A timeout is meaningful, not just slow: the server could be wedged mid-request
# against iCloud, in which case it answers nothing rather than answering badly.
body="$(curl -fsS -m 15 "$HEALTH_URL" 2>/dev/null)"

if [[ -z "$body" ]]; then
  problems+=("no response from $HEALTH_URL (down, or wedged)")
else
  # Parsed with plutil, which ships with macOS. Deliberately not /usr/bin/python3:
  # that is a Command Line Tools shim, and an OS update can leave it prompting to
  # install developer tools -- which would break this check exactly when an OS
  # update is the thing most likely to have broken something.
  extract() {
    printf '%s' "$body" | plutil -extract "$1" raw -o - - 2>/dev/null
  }

  status="$(extract status)"
  reachable="$(extract caldav_reachable)"
  calendars="$(extract event_calendars)"
  err="$(extract error)"
  python_version="$(extract python_version)"
  write_ok="$(extract last_write.ok)"
  write_action="$(extract last_write.action)"
  write_error="$(extract last_write.error)"

  if [[ "$status" != "ok" ]]; then
    problems+=("health reports status=$status")
  fi

  # The failure mode with no local symptom: an app-specific password can be
  # revoked from appleid.apple.com without anything on this host changing. The
  # process stays healthy and every tool call fails.
  if [[ "$reachable" != "true" ]]; then
    problems+=("iCloud unreachable (${err:-no detail}); the app-specific password may have been revoked")
  elif [[ -z "$calendars" || "$calendars" == "0" ]]; then
    problems+=("connected to iCloud but found no event calendars")
  fi

  # A calendar that has quietly stopped accepting writes is otherwise invisible
  # until somebody tries to add an event and reads the reply carefully.
  if [[ "$write_ok" == "false" ]]; then
    problems+=("last write (${write_action:-unknown}) failed: ${write_error:-no detail}")
  fi
fi

if (( ${#problems[@]} > 0 )); then
  message="dav-mcp check FAILED"
  for problem in "${problems[@]}"; do
    message+=$'\n'"- $problem"
  done
  log "$message"
  ping_hc "/fail" "$message"
  exit 1
fi

# Logged on every run, not just on failure, so the log doubles as a record of
# how the service has actually been behaving.
log "dav-mcp OK status=$status calendars=$calendars python=$python_version last_write=${write_ok:-none}"
ping_hc ""
