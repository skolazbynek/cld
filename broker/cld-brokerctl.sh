#!/usr/bin/env bash
# cld-brokerctl -- operate the cld host broker sshd. First-time setup is manual
# (see ../broker-setup-home.md); day-to-day this is all you need:
#
#     cld-brokerctl start | restart | shutdown | status | logs [N] | graphql-sweep
#     cld-brokerctl run [-C repo] [-r revset] [-s session] [-t target]... <action> [args...]
#
# Everything lives under $CLD_BROKER_DIR (default ~/.cld/broker): the sshd config,
# its PID file, and its log. No sshd flags to remember.
set -euo pipefail

BROKER="${CLD_BROKER_DIR:-$HOME/.cld/broker}"
CONF="$BROKER/sshd_cld_broker.conf"
PIDFILE="$BROKER/sshd.pid"
LOG="$BROKER/sshd.log"
SSHD="$(command -v sshd || echo /usr/sbin/sshd)"

# Echo the running pid (and succeed) only if the PID file names a live process.
running() {
    local pid
    pid=$(cat "$PIDFILE" 2>/dev/null) || return 1
    [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null || return 1
    echo "$pid"
}

start() {
    local pid
    if pid=$(running); then echo "already running (pid $pid)"; return 0; fi
    [ -r "$CONF" ] || { echo "no broker config at $CONF -- run first-time setup" >&2; exit 1; }
    rm -f "$PIDFILE" 2>/dev/null || true          # clear any stale pidfile
    # No -D: sshd daemonizes, detaching from this shell. -E logs to the file.
    if ! "$SSHD" -f "$CONF" -E "$LOG"; then
        echo "sshd failed to start. Last log lines:" >&2
        tail -n 15 "$LOG" 2>/dev/null >&2 || true
        echo "(if it mentions /run/sshd: 'sudo mkdir -p /run/sshd' once, or start via sudo)" >&2
        exit 1
    fi
    if pid=$(running); then echo "started (pid $pid)"; else
        echo "sshd exited immediately -- check $LOG" >&2; exit 1; fi
}

shutdown() {
    local pid
    if pid=$(running); then kill "$pid" && echo "stopped (pid $pid)"; else echo "not running"; fi
    rm -f "$PIDFILE" 2>/dev/null || true
}

restart() { shutdown; sleep 0.3; start; }

# Reaps any `graphql` broker-action server whose owning session container is
# gone -- useful after a host reboot, without needing to start any session
# first. Runs the broker's own sweep (cld-broker.py:sweep_gql_orphans), so the
# jj store checks guarding its `workspace forget` are the broker's, not a copy.
graphql-sweep() { run graphql-sweep; }

# Run one broker action from the host, for debugging -- no sshd, no container.
# Runs the same ForceCommand with the sshd config's SetEnv lines and an
# otherwise cleared environment, close to what sshd gives it (sshd's
# compiled-in PATH varies by build; broker.conf's PATH wins either way). The
# arguments select the broker's CLI route; see cld-broker.py:main_cli.
# SetEnv values are split on whitespace, so quoted values with spaces are not
# supported.
run() {
    local cmd
    local -a setenv
    [ -r "$CONF" ] || { echo "no broker config at $CONF -- run first-time setup" >&2; exit 1; }
    cmd=$(awk 'tolower($1) == "forcecommand" { $1 = ""; sub(/^[ \t]+/, ""); print; exit }' "$CONF")
    [ -n "$cmd" ] || { echo "no ForceCommand in $CONF" >&2; exit 1; }
    mapfile -t setenv < <(awk 'tolower($1) == "setenv" { for (i = 2; i <= NF; i++) print $i }' "$CONF")
    # sshd runs the ForceCommand through a shell too; "$@" passes the args intact.
    exec env -i HOME="$HOME" USER="${USER:-$(id -un)}" LOGNAME="${LOGNAME:-${USER:-$(id -un)}}" \
        SHELL=/bin/sh PATH=/usr/bin:/bin:/usr/sbin:/sbin "${setenv[@]}" \
        /bin/sh -c "$cmd \"\$@\"" cld-broker "$@"
}

status() {
    local pid
    if pid=$(running); then
        echo "running (pid $pid)"
        grep -E '^(ListenAddress|Port|AllowUsers)' "$CONF" 2>/dev/null | sed 's/^/  /' || true
    else
        echo "stopped"
    fi
}

case "${1:-}" in
    start)            start ;;
    restart)          restart ;;
    shutdown|stop)    shutdown ;;
    status)           status ;;
    logs)             tail -n "${2:-40}" "$LOG" 2>/dev/null || echo "no log at $LOG" ;;
    graphql-sweep)    graphql-sweep ;;
    run)              shift; run "$@" ;;
    *) echo "usage: cld-brokerctl {start|restart|shutdown|status|logs [N]|graphql-sweep|run [-h] <action> [args...]}" >&2; exit 2 ;;
esac
