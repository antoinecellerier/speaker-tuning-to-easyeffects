#!/usr/bin/env bash
# Drive a per-variant capture matrix through the live EasyEffects
# pipeline. Variants are declared in a TSV spec passed on the command
# line: each row produces one (build → capture) cycle.
#
# This is harness, not a hardcoded experiment: each session writes its
# own spec file describing which presets / patches to test. The
# bracketing (null-sink setup, smoke gate, teardown trap, output
# layout) is constant.
#
# Spec format: tab-separated, comments and blank lines allowed.
#   label<TAB>build_cmd<TAB>preset_name
#
#   label       : output subdir name + capture file label
#                 (e.g. b1_eq_iir, sub_min, my_test)
#   build_cmd   : shell command to run before capture; expected to
#                 produce all needed EE presets (typically a
#                 dolby_to_easyeffects.py invocation, possibly with
#                 a temporary patch to the main script applied first
#                 and reverted in build_cmd itself).
#   preset_name : EE preset to load before capturing
#                 (e.g. Dolby-Dynamic-Balanced).
#
# Usage:
#   tools/measure_ee/sweep_variants.sh path/to/spec.tsv [out_base]
#
# Required env / defaults:
#   STIM_DIR        : stimulus directory (default: localresearch/measure_dax)
#   TARGET          : pw-record target (default: ee_capture.monitor)
#
# Outputs:
#   $out_base/$label/loopback_*_<label>.{wav,json}
#   plus the per-variant summary printed by capture_battery.py.

set -euo pipefail

if [[ $# -lt 1 ]]; then
    echo "usage: sweep_variants.sh <spec.tsv> [out_base]" >&2
    exit 2
fi

SPEC_FILE="$1"
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." &> /dev/null && pwd)"
OUT_BASE="${2:-$REPO/localresearch/measure_ee/variants}"
STIM_DIR="${STIM_DIR:-$REPO/localresearch/measure_dax}"
TARGET="${TARGET:-ee_capture.monitor}"

if [[ ! -f "$SPEC_FILE" ]]; then
    echo "ERROR: spec file not found: $SPEC_FILE" >&2
    exit 2
fi

mkdir -p "$OUT_BASE"
echo "sweep_variants: spec=$SPEC_FILE  out_base=$OUT_BASE"

bash "$REPO/tools/measure_ee/setup_null_sink.sh"
trap 'bash "$REPO/tools/measure_ee/teardown.sh"' EXIT

# Smoke-gate flag: empty for the first variant (runs the smoke gate),
# --skip-smoke afterward (wiring is invariant under preset content).
SMOKE_FLAG=""

variant_idx=0
while IFS=$'\t' read -r label build_cmd preset; do
    # Skip blank lines and comments
    [[ -z "${label// }" || "$label" =~ ^# ]] && continue
    if [[ -z "${build_cmd:-}" || -z "${preset:-}" ]]; then
        echo "WARN: malformed spec line, label=$label — skipping" >&2
        continue
    fi
    variant_idx=$((variant_idx + 1))
    echo "================================================================"
    echo "VARIANT $variant_idx ($label)"
    echo "  build: $build_cmd"
    echo "  preset: $preset"
    echo "================================================================"

    # build_cmd is responsible for producing the right EE presets (and
    # for cleaning up after itself, e.g. reverting any source patch).
    bash -c "$build_cmd"

    out_dir="$OUT_BASE/$label"
    mkdir -p "$out_dir"

    python3 "$REPO/tools/measure_ee/capture_battery.py" \
        --stimulus-dir "$STIM_DIR" \
        --preset "$preset" \
        --label "$label" \
        --target "$TARGET" \
        --out-dir "$out_dir" \
        $SMOKE_FLAG

    SMOKE_FLAG="--skip-smoke"
done < "$SPEC_FILE"

echo
echo "=================================================================="
echo "Sweep complete ($variant_idx variants). Captures in $OUT_BASE/"
echo "Next: tools/measure_ee/summarise_variants.py --variant-base $OUT_BASE \\"
echo "        --variants <labels...>"
echo "=================================================================="
