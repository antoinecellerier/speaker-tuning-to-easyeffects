#!/usr/bin/env bash
# Reverse setup_pw_chain.sh: kill the child pipewire process and
# remove the conf drop-in. Leaves the ee_capture null sink in place
# (use teardown.sh to remove that too).
#
# Usage:
#   bash tools/measure_ee/teardown_pw_chain.sh [<node-name>]
#
# Defaults:
#   <node-name> = "Dolby_PW_Test"

set -euo pipefail

NODE_NAME="${1:-Dolby_PW_Test}"
PID_FILE="/tmp/pw_chain.${NODE_NAME}.pid"
CONF_PATH="${HOME}/.config/pipewire/filter-chain.conf.d/${NODE_NAME}.conf"
LOG_FILE="/tmp/pw_chain.${NODE_NAME}.log"

if [[ -f "$PID_FILE" ]]; then
    pid="$(cat "$PID_FILE")"
    if kill -0 "$pid" 2>/dev/null; then
        kill "$pid"
        # Wait briefly for graceful exit, then SIGKILL if still alive.
        for _ in $(seq 1 10); do
            kill -0 "$pid" 2>/dev/null || break
            sleep 0.2
        done
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        echo "killed child pipewire pid=$pid"
    else
        echo "pid $pid no longer running"
    fi
    rm -f "$PID_FILE"
else
    # Fall back to pgrep if PID file is missing (e.g. setup script
    # crashed before writing it).
    pids="$(pgrep -f "pipewire -c filter-chain.conf" || true)"
    if [[ -n "$pids" ]]; then
        echo "no PID file; killing pgrep-found pipewire children: $pids"
        echo "$pids" | xargs -r kill
    fi
fi

if [[ -f "$CONF_PATH" ]]; then
    rm -f "$CONF_PATH"
    echo "removed $CONF_PATH"
fi

if [[ -f "$LOG_FILE" ]]; then
    rm -f "$LOG_FILE"
fi

# Restore the previous default sink (set by setup_pw_chain.sh).
PREV_SINK_FILE="/tmp/pw_chain.${NODE_NAME}.prev_default_sink"
if [[ -f "$PREV_SINK_FILE" ]]; then
    PREV="$(cat "$PREV_SINK_FILE")"
    if [[ -n "$PREV" ]]; then
        pactl set-default-sink "$PREV" 2>/dev/null || true
        echo "restored default sink to $PREV"
    fi
    rm -f "$PREV_SINK_FILE"
fi

# Restart EasyEffects if setup_pw_chain.sh stopped it.
WAS_EE_FILE="/tmp/pw_chain.${NODE_NAME}.was_ee_running"
if [[ -f "$WAS_EE_FILE" ]] && [[ "$(cat "$WAS_EE_FILE")" == "1" ]]; then
    nohup easyeffects --hide-window --service-mode \
        > "/tmp/ee_restart.${NODE_NAME}.log" 2>&1 &
    disown
    echo "restarted EasyEffects (--service-mode)"
fi
rm -f "$WAS_EE_FILE"

echo "PW chain $NODE_NAME torn down. ee_capture null sink left in place "
echo "(run teardown.sh to remove it and restore EE)."
