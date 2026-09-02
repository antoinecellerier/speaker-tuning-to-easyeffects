#!/usr/bin/env bash
# A/B the Dolby voicings against the raw speaker, by ear.
#
# Run `dolby_to_pipewire.py --variant all --target-sink ''` and you have one
# sink per voicing alongside your untouched speaker sink. This flips the
# default sink between them with wpctl, which moves already-playing streams
# over immediately -- loop a track and cycle sinks under it. Independent of
# the converters: it lists whatever Audio/Sink nodes exist, Dolby ones first.
#
#   tools/ab_sink.sh          # show the menu + current default
#   tools/ab_sink.sh next     # cycle to the next option
#   tools/ab_sink.sh raw      # jump to the raw (unprocessed) speaker
#   tools/ab_sink.sh 2        # pick menu entry 2
set -uo pipefail

command -v wpctl  >/dev/null || { echo "wpctl not found (install wireplumber)" >&2; exit 1; }
command -v pw-dump >/dev/null || { echo "pw-dump not found" >&2; exit 1; }

# Menu order: Dolby variants first (sorted), then everything else. One entry per
# line as "<id>\t<rank>\t<label>", where rank picks what `raw` means: 0 = this
# machine's analog speaker, 1 = some other output (HDMI, Bluetooth, a null
# sink), 9 = a Dolby variant, never raw.
mapfile -t SINKS < <(
  pw-dump | python3 -c '
import json, sys
sinks = [n for n in json.load(sys.stdin)
         if n.get("info", {}).get("props", {}).get("media.class") == "Audio/Sink"]
def label(n):
    p = n["info"]["props"]
    return p.get("node.description") or p.get("node.name") or "?"
def rank(n):
    if "Dolby" in label(n):
        return 9
    name = (n["info"]["props"].get("node.name") or "").lower()
    analog = name.startswith("alsa_output.") and not any(
        d in name for d in ("hdmi", "iec958", "spdif", "bluez"))
    return 0 if analog else 1
for n in sorted(sinks, key=lambda n: (0 if "Dolby" in label(n) else 1, label(n))):
    print(str(n["id"]) + "\t" + str(rank(n)) + "\t" + label(n))
'
)
(( ${#SINKS[@]} )) || { echo "no audio sinks found" >&2; exit 1; }

# Numeric id of the current default sink, or empty.
current() {
  wpctl inspect @DEFAULT_AUDIO_SINK@ 2>/dev/null | sed -n 's/^id \([0-9]\+\).*/\1/p'
}

id_of()   { printf '%s' "${SINKS[$1]%%$'\t'*}"; }
name_of() { printf '%s' "${SINKS[$1]##*$'\t'}"; }
rank_of() { local r="${SINKS[$1]#*$'\t'}"; printf '%s' "${r%%$'\t'*}"; }

index_of_current() {
  local cur; cur=$(current)
  local i
  for i in "${!SINKS[@]}"; do
    [[ "$(id_of "$i")" == "$cur" ]] && { printf '%s' "$i"; return; }
  done
  printf '%s' -1
}

pick() {
  local i=$1
  wpctl set-default "$(id_of "$i")" || {
    echo "wpctl could not make [$(id_of "$i")] the default sink" >&2; return 1; }
  echo "-> [$(id_of "$i")] $(name_of "$i")"
}

menu() {
  local cur; cur=$(current)
  echo "current default sink: ${cur:-unknown}"
  local i
  for i in "${!SINKS[@]}"; do
    local mark=" "; [[ "$(id_of "$i")" == "$cur" ]] && mark="*"
    printf " %s %d  [%s]  %s\n" "$mark" $((i + 1)) "$(id_of "$i")" "$(name_of "$i")"
  done
  echo
  echo "usage: ab_sink.sh next | raw | <number>"
}

case "${1:-}" in
  ""|-h|--help|status)
    menu
    ;;
  next)
    cur_idx=$(index_of_current)
    pick $(( (cur_idx + 1) % ${#SINKS[@]} ))
    ;;
  raw)
    # Best rank wins, so a Bluetooth headset or an HDMI sink can never stand in
    # for the speaker you are trying to hear the voicings against.
    best=-1
    for i in "${!SINKS[@]}"; do
      r=$(rank_of "$i")
      (( r == 9 )) && continue
      if (( best < 0 )) || (( r < $(rank_of "$best") )); then best=$i; fi
    done
    (( best >= 0 )) || { echo "no non-Dolby sink found" >&2; exit 1; }
    pick "$best"; exit $?
    ;;
  *[!0-9]*)
    echo "unknown arg: $1" >&2; menu; exit 1
    ;;
  *)
    n=$((10#$1))
    (( n >= 1 && n <= ${#SINKS[@]} )) || { echo "out of range (1-${#SINKS[@]})" >&2; exit 1; }
    pick $(( n - 1 ))
    ;;
esac
