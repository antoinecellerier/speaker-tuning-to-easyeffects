#!/usr/bin/env bash
# Set up a PipeWire filter-chain rendering of an EasyEffects preset
# and wire its output into the same `ee_capture` null sink that
# tools/measure_ee/setup_null_sink.sh creates. Pre-req for
# tools/measure_pw/capture_battery.py.
#
# After this returns, you can capture against the chain; audio fed
# to `effect_input.<NODE_NAME>` will be processed by the chain and
# recorded from `ee_capture.monitor`.
#
# Usage:
#   bash tools/measure_pw/setup_chain.sh <ee-preset.json> [<node-name>]
#
# Defaults:
#   <node-name> = "Dolby_PW_Test"
#
# The script:
#   1. Generates a conf via ee_to_pipewire.py and drops it in
#      ~/.config/pipewire/filter-chain.conf.d/<node-name>.conf
#   2. Stops EasyEffects (so its `easyeffects_sink` doesn't get
#      auto-linked to the chain output, see comment below).
#   3. Starts a child `pipewire -c filter-chain.conf` process and
#      writes its PID to /tmp/pw_chain.<node-name>.pid
#   4. Waits for the chain to register, then `pw-link`s its playback
#      output into `ee_capture:playback_{FL,FR}`.
#   5. Sets the chain as the default sink (WirePlumber ignores
#      pw-cat --target hints and routes to whatever the default
#      sink is, so this step is load-bearing).
#   6. Disconnects any other auto-routes WirePlumber created.
#
# Pre-flight expectation: tools/measure_ee/setup_null_sink.sh has
# already loaded the ee_capture null sink. If not, this script aborts.
#
# Tear down with `tools/measure_pw/teardown_chain.sh <node-name>`.

set -euo pipefail

PRESET_PATH="${1:-}"
NODE_NAME="${2:-Dolby_PW_Test}"

if [[ -z "$PRESET_PATH" ]]; then
    echo "usage: $0 <ee-preset.json> [<node-name>]" >&2
    exit 2
fi
if [[ ! -f "$PRESET_PATH" ]]; then
    echo "preset not found: $PRESET_PATH" >&2
    exit 2
fi

if ! pw-cli ls Node 2>/dev/null | grep -q "ee_capture"; then
    echo "ee_capture sink not found — run tools/measure_ee/setup_null_sink.sh first" >&2
    exit 2
fi

# Stop EasyEffects so its `easyeffects_sink` doesn't compete with our
# chain for WirePlumber's auto-link policy. Even with target.object
# baked into the conf and explicit pw-link -d at setup, WirePlumber
# re-creates the chain → easyeffects_sink edge whenever a new playback
# stream appears (i.e. every pw-cat invocation), which feeds two
# delayed copies of the chain output back into ee_capture and creates
# a comb-filter pattern on the captured spectrum. teardown_pw_chain.sh
# restarts EE.
WAS_EE_RUNNING=0
# Match the executable name exactly. `pgrep -af easyeffects` would also
# match any parent shell whose argv contains the string "easyeffects",
# leading pkill to terminate the shell running this script.
if pgrep -x easyeffects >/dev/null 2>&1; then
    WAS_EE_RUNNING=1
    pkill -x easyeffects || true
    # Wait until the easyeffects_sink node disappears.
    for _ in $(seq 1 20); do
        if ! pw-cli ls Node 2>/dev/null | grep -q "easyeffects_sink"; then
            break
        fi
        sleep 0.2
    done
    echo "stopped EasyEffects to prevent auto-link conflict"
fi
echo "$WAS_EE_RUNNING" > "/tmp/pw_chain.${NODE_NAME}.was_ee_running"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONF_DIR="${HOME}/.config/pipewire/filter-chain.conf.d"
CONF_PATH="${CONF_DIR}/${NODE_NAME}.conf"
PID_FILE="/tmp/pw_chain.${NODE_NAME}.pid"
LOG_FILE="/tmp/pw_chain.${NODE_NAME}.log"

mkdir -p "$CONF_DIR"

# 1. Generate the conf with playback bound to the ee_capture null sink so
#    the chain output never auto-routes to the actual speakers during
#    measurement. The default-sink hand-off below covers the input side.
#    `--target-sink ''` disables smart-filter mode: ee_to_pipewire.py now
#    defaults to a WirePlumber smart filter pinned to the *speaker* sink, which
#    would fight the `--target-object ee_capture` pin (the chain links to both,
#    yielding "failed to link ports: File exists" + a comb-filtered capture).
#    A plain v1 virtual sink routed only to ee_capture is what measurement wants.
python3 "${REPO_ROOT}/ee_to_pipewire.py" \
    "$PRESET_PATH" \
    --output "$CONF_PATH" \
    --node-name "$NODE_NAME" \
    --node-description "$NODE_NAME (PW filter-chain)" \
    --target-sink '' \
    --target-object ee_capture \
    --force \
    --irs-dir "${HOME}/.local/share/easyeffects/irs"
echo "generated $CONF_PATH"

# 2. Start the child pipewire process — it picks up filter-chain.conf
#    plus any drop-ins under ~/.config/pipewire/filter-chain.conf.d/.
nohup pipewire -c filter-chain.conf >"$LOG_FILE" 2>&1 &
echo $! >"$PID_FILE"
echo "started pipewire -c filter-chain.conf (pid=$(cat "$PID_FILE"))"

# 3. Wait for the chain to register
for i in $(seq 1 30); do
    if pw-cli ls Node 2>/dev/null | grep -q "effect_input.${NODE_NAME}"; then
        break
    fi
    sleep 0.2
done
if ! pw-cli ls Node 2>/dev/null | grep -q "effect_input.${NODE_NAME}"; then
    echo "filter-chain did not register within 6 s — see $LOG_FILE" >&2
    cat "$LOG_FILE" >&2
    exit 1
fi
echo "chain registered: effect_input.${NODE_NAME} / effect_output.${NODE_NAME}"

# 4. Wire playback into ee_capture (in case target.object isn't honored)
pw-link "effect_output.${NODE_NAME}:output_FL" "ee_capture:playback_FL" || true
pw-link "effect_output.${NODE_NAME}:output_FR" "ee_capture:playback_FR" || true
echo "linked effect_output.${NODE_NAME}:output_{FL,FR} -> ee_capture:playback_{FL,FR}"

# 5. Set chain as default sink so pw-cat's playback streams reach it
#    (WirePlumber ignores --target hints and routes to the default sink).
PREV_DEFAULT_SINK="$(pactl get-default-sink 2>/dev/null || true)"
echo "$PREV_DEFAULT_SINK" > "/tmp/pw_chain.${NODE_NAME}.prev_default_sink"
pactl set-default-sink "effect_input.${NODE_NAME}" || true
echo "default sink: $(pactl get-default-sink) (was: $PREV_DEFAULT_SINK)"

# 6. Disconnect any auto-link that WirePlumber created from the chain
#    output to *any* sink other than ee_capture. Even with
#    `target.object = ee_capture` baked into the conf, WirePlumber's
#    policy will still auto-link to easyeffects_sink and the system
#    speaker; if any of those slip through, ee_capture ends up
#    receiving multiple delayed copies of the same audio (chain →
#    direct, plus chain → other-sink → routed-back) and the captured
#    spectrum gets a comb-filter pattern. This step is load-bearing
#    for measurement validity.
sleep 0.5  # let WirePlumber finish auto-linking
for sink in $(pw-cli ls Node 2>/dev/null \
              | awk '/media.class = "Audio\/Sink"/{p=1; next} p && /node.name/{print $4; p=0}' \
              | tr -d '"'); do
    if [[ "$sink" == "ee_capture" ]]; then
        continue
    fi
    pw-link -d "effect_output.${NODE_NAME}:output_FL" "${sink}:playback_FL" 2>/dev/null || true
    pw-link -d "effect_output.${NODE_NAME}:output_FR" "${sink}:playback_FR" 2>/dev/null || true
done

# 7. Verify only ee_capture receives chain output. Anything else means
#    the comb-filter trap is active.
extra="$(pw-link -l 2>/dev/null \
         | awk -v n="effect_output.${NODE_NAME}" '$0 ~ n":output_(FL|FR)"{f=1; next} f && /^[[:space:]]*\|/{print; next} {f=0}' \
         | grep -v "ee_capture:playback" | head -5)"
if [[ -n "$extra" ]]; then
    echo "WARNING: chain output is also linked to non-ee_capture sinks:" >&2
    echo "$extra" >&2
    echo "Captures from ee_capture.monitor will have comb-filter artifacts." >&2
fi

cat <<EOF

Run capture battery:
  python3 ${REPO_ROOT}/tools/measure_pw/capture_battery.py \\
      --node-name ${NODE_NAME} \\
      --label pw_dolby_balanced

Tear down:
  bash ${REPO_ROOT}/tools/measure_pw/teardown_chain.sh ${NODE_NAME}
EOF
