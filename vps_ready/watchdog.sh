#!/usr/bin/env bash
#
# watchdog.sh — restart any dead bot screens.
#
# Each bot runs in a named GNU screen session. This script checks
# whether each session exists and, if not, restarts it. Safe to run
# every minute via cron.
#
# Crontab:
#   * * * * * /root/bots/watchdog.sh >> /root/bots/watchdog.log 2>&1
#
# IMPORTANT: this script does NOT kill running bots. It only revives
# dead ones. Use `screen -X -S NAME quit` to stop a bot manually.

set -u

BOTS_DIR="${BOTS_DIR:-/root/bots}"
PYTHON="${PYTHON:-/usr/bin/python3}"
ENV_FILE="${ENV_FILE:-/root/bots/.env}"

# Sessions to manage:
#   name|cwd|command
SESSIONS=(
    "arb1|${BOTS_DIR}|${PYTHON} copy_trader.py --paper"
    "arb2|${BOTS_DIR}|${PYTHON} oracle_arb_wss.py --paper"
    "arb3|${BOTS_DIR}|${PYTHON} sports_consensus_bot.py --paper"
)

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

session_alive() {
    screen -ls "$1" 2>/dev/null | grep -q "\.$1\b"
}

start_session() {
    local name="$1"
    local cwd="$2"
    local cmd="$3"

    log "starting $name"

    # Source env file if present (no -e to avoid leaking on errors)
    local env_prefix=""
    if [[ -f "$ENV_FILE" ]]; then
        env_prefix="set -a; . $ENV_FILE; set +a; "
    fi

    screen -dmS "$name" bash -c "${env_prefix}cd $cwd && $cmd"
    sleep 1

    if session_alive "$name"; then
        log "started $name OK"
    else
        log "FAILED to start $name"
    fi
}

main() {
    if ! command -v screen >/dev/null 2>&1; then
        log "ERROR: screen not installed"
        exit 1
    fi

    for entry in "${SESSIONS[@]}"; do
        IFS='|' read -r name cwd cmd <<< "$entry"
        if session_alive "$name"; then
            : # alive, do nothing
        else
            start_session "$name" "$cwd" "$cmd"
        fi
    done
}

main "$@"
