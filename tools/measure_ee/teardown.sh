#!/usr/bin/env bash
# Reverse setup_null_sink.sh: unload the null sink and restore easyeffectsrc.

set -euo pipefail

EE_RC="${HOME}/.config/easyeffects/db/easyeffectsrc"
SAVED_RC="${HOME}/.config/easyeffects/db/easyeffectsrc.measure_ee.bak"
SINK_NAME="ee_capture"
STATE_DIR="${HOME}/.cache/measure_ee"
STATE_FILE="${STATE_DIR}/null_sink.state"

# 1. unload null sink --------------------------------------------------------

if [[ -f "${STATE_FILE}" ]]; then
    SINK_ID="$(cat "${STATE_FILE}")"
    if pactl list short modules | awk '{print $1}' | grep -qx "${SINK_ID}"; then
        pactl unload-module "${SINK_ID}" || true
        echo "unloaded null sink module id ${SINK_ID}"
    fi
    rm -f "${STATE_FILE}"
fi
# Also catch any orphan ee_capture sinks if state file went missing
while read -r id; do
    [[ -n "${id}" ]] && pactl unload-module "${id}" || true
done < <(pactl list short modules | awk -v s="${SINK_NAME}" \
    '$2 == "module-null-sink" && $0 ~ ("sink_name="s) {print $1}')

# 2. restore easyeffectsrc ---------------------------------------------------

if [[ -f "${SAVED_RC}" ]]; then
    mv -f "${SAVED_RC}" "${EE_RC}"
    echo "restored ${EE_RC} from backup"
fi

# 3. restart EE so it picks up the restored config --------------------------

if pgrep -f "easyeffects.*service-mode" >/dev/null 2>&1; then
    easyeffects -q || true
    sleep 0.5
fi
nohup easyeffects --hide-window --service-mode \
    >"${STATE_DIR}/ee.log" 2>&1 &
echo "restarted easyeffects (service mode)"
sleep 1.0

echo
echo "--- pw-link routing (should show EE -> hw) ---"
pw-link --links | grep -E "ee_soe_output|alsa_output.*Speaker" || true
