#!/usr/bin/env bash
# Set up the EE → null-sink capture route.
#
#   * Loads a 48 kHz 2-ch null sink named "ee_capture".
#   * Backs up easyeffectsrc, then writes useDefaultOutputDevice=false
#     and outputDevice=ee_capture to its [StreamOutputs] section.
#   * Restarts EE (one mic-indicator pop is expected).
#   * Prints the resulting pw-link routing for verification.
#
# Idempotent: rerunning won't load a duplicate sink. Run teardown.sh
# afterwards to restore.

set -euo pipefail

# EE 8.x (Qt) reads from db/easyeffectsrc, per
# easyeffects_db_streamoutputs.kcfg's <kcfgfile name="easyeffects/db/easyeffectsrc"/>.
# The top-level easyeffectsrc is from a previous EE version and ignored.
EE_RC="${HOME}/.config/easyeffects/db/easyeffectsrc"
SAVED_RC="${HOME}/.config/easyeffects/db/easyeffectsrc.measure_ee.bak"
SINK_NAME="ee_capture"
STATE_DIR="${HOME}/.cache/measure_ee"
mkdir -p "${STATE_DIR}"
STATE_FILE="${STATE_DIR}/null_sink.state"

# 1. null sink ---------------------------------------------------------------

EXISTING_ID="$(pactl list short modules | awk -v s="${SINK_NAME}" \
    '$2 == "module-null-sink" && $0 ~ ("sink_name=" s) {print $1; exit}')"
if [[ -z "${EXISTING_ID}" ]]; then
    SINK_ID="$(pactl load-module module-null-sink \
        sink_name="${SINK_NAME}" \
        sink_properties="device.description=EE-Capture" \
        rate=48000 channels=2 channel_map=front-left,front-right \
        format=float32le)"
    if [[ -z "${SINK_ID}" || ! "${SINK_ID}" =~ ^[0-9]+$ ]]; then
        echo "ERROR: pactl load-module module-null-sink failed (got '${SINK_ID}')" >&2
        exit 2
    fi
    echo "loaded null sink ${SINK_NAME} (module id ${SINK_ID})"
else
    SINK_ID="${EXISTING_ID}"
    echo "null sink ${SINK_NAME} already loaded (module id ${SINK_ID})"
fi
echo "${SINK_ID}" > "${STATE_FILE}"

# 2. easyeffectsrc -----------------------------------------------------------

# Refresh the backup unconditionally: if a previous run crashed before
# teardown, the on-disk EE_RC is the *edited* version, not the user's
# original — keeping a stale backup would silently corrupt teardown.
# So we only write a backup the *first* time we see an unedited rc, and
# we detect "unedited" by the absence of our outputDevice marker.
if [[ -f "${EE_RC}" ]]; then
    if grep -q "^outputDevice=ee_capture$" "${EE_RC}" 2>/dev/null \
       && [[ -f "${SAVED_RC}" ]]; then
        echo "rc already points at ee_capture; preserving existing backup"
    else
        cp -f "${EE_RC}" "${SAVED_RC}"
        echo "backed up ${EE_RC} -> ${SAVED_RC}"
    fi
fi

# Update [StreamOutputs] section: set outputDevice + useDefaultOutputDevice.
# KConfig format — we edit in-place with awk.
python3 - <<'PYEOF'
import os
import re
from pathlib import Path

rc_path = Path(os.path.expandvars("$HOME/.config/easyeffects/db/easyeffectsrc"))
text = rc_path.read_text() if rc_path.exists() else "[StreamOutputs]\n"

sections = re.split(r"(?m)^(\[[^\]]+\])\s*$", text)
# split returns: [pre, header, body, header, body, ...]
out: list[str] = [sections[0]]
i = 1
target_section = "[StreamOutputs]"
seen_target = False
while i < len(sections):
    header = sections[i]
    body = sections[i + 1] if i + 1 < len(sections) else ""
    if header == target_section:
        seen_target = True
        # remove existing keys we manage, then append fresh ones
        new_body_lines = []
        for line in body.splitlines():
            if re.match(r"^\s*(outputDevice|useDefaultOutputDevice)\s*=", line):
                continue
            new_body_lines.append(line)
        # ensure trailing newline before our additions
        body_clean = "\n".join(new_body_lines).rstrip() + "\n"
        body_clean += "outputDevice=ee_capture\n"
        body_clean += "useDefaultOutputDevice=false\n"
        out.append(header + "\n" + body_clean)
    else:
        out.append(header + "\n" + body)
    i += 2

if not seen_target:
    out.append("[StreamOutputs]\noutputDevice=ee_capture\n"
               "useDefaultOutputDevice=false\n")

rc_path.write_text("".join(out))
print(f"updated {rc_path}")
PYEOF

# 3. restart EE in service mode ---------------------------------------------

if pgrep -f "easyeffects.*service-mode" >/dev/null 2>&1; then
    easyeffects -q || true
    sleep 0.5
fi
nohup easyeffects --hide-window --service-mode \
    >"${STATE_DIR}/ee.log" 2>&1 &
echo "restarted easyeffects (service mode)"

# Give EE time to register its nodes
sleep 1.5

# 4. verify ------------------------------------------------------------------

echo
echo "--- pw-link routing (looking for ee_soe_output_level -> ee_capture) ---"
pw-link --links | grep -E "ee_soe_output|ee_capture" || true
echo
echo "Run smoke harness:"
echo "  python3 tools/measure_ee/smoke.py --target ee_capture.monitor --label v3_nullsink"
