"""Where the conf and its impulse response go, and what to do once they're there.

The write itself stays in ``ee_to_pipewire.py``'s ``main`` — this is
everything around it: the default install locations, the smart-filter target
sink to pin to, the ``lv2info`` schema self-check the conf is run through
before it is written, the convolver retarget that makes the conf
self-contained, and the two blocks a user reads afterwards.

``_autodetect_speaker_sink`` shares its probe with the EasyEffects autoload
pathway (``lib.hardware.sinks``), imported inside the function so a converter
run that never needs a sink never shells out to ``pw-dump``.

``_sanitize_name`` is imported bare from ``lib.pipewire.conf`` because
``_print_next_steps`` calls it and arrived here as a move; see that module's
docstring for why a carried body may not be re-pointed. Not stdlib-only —
``lib.console`` owns the optional rich dependency — but nothing here reaches
the DSP stack.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

from lib import console
from lib.ee_paths import easyeffects_base
from lib.paths import REPO_ROOT
from lib.pipewire.conf import _sanitize_name


VALIDATE_CONF_SCRIPT = REPO_ROOT / "tools" / "measure_pw" / "validate_conf.py"


# Matches whichever EasyEffects install the generator writes to — native
# or Flatpak, which keep their presets in different trees. Hardcoding the
# native path sent the standalone two-step looking for the impulse
# response somewhere Flatpak users never had it.
DEFAULT_IRS_DIR = easyeffects_base() / "irs"


def _retarget_convolver_irs(stages: list["Stage"],
                            target_irs: Path) -> Path | None:
    """Rewrite every convolver node's `filename` to ``target_irs`` and
    return the original source path (or ``None`` if the chain has no
    convolver). All convolver nodes share one source IRS, so we copy
    once and point both channels at the same destination.
    """
    src: Path | None = None
    for stage in stages:
        for node in stage.nodes:
            if node.get("label") != "convolver":
                continue
            if src is None:
                src = Path(node["config"]["filename"])
            node["config"]["filename"] = str(target_irs)
    return src


def _autodetect_speaker_sink() -> tuple[str | None, list[str]]:
    """Return (chosen_sink_name, warnings) for the smart-filter target.

    Uses ``lib.hardware.sinks.select_speaker_sinks()`` (same probe as the
    EasyEffects autoload pathway): ``pw-dump`` filtered to ``Audio/Sink``
    nodes, preferring those tagged ``device.icon_name == audio-speakers`` (the
    "strict" tier, which excludes HDMI / Bluetooth / headsets). When nothing is
    tagged as a speaker — e.g. a laptop whose UCM2 profile omits the speaker
    icon (issue #18) — it falls back to a "relaxed" tier of internal analog
    sinks; a single relaxed candidate is used, with a warning. Returns
    ``(None, [reasons])`` when no unique sink can be chosen — the caller
    surfaces that, falls back to the v1 virtual-sink conf, and asks the user to
    pass ``--target-sink``.
    """
    try:
        from lib.hardware import sinks
    except Exception as e:  # pragma: no cover — defensive
        return None, [f"could not import speaker probe: {e}"]

    # Terse, description-less form of the shared diagnostic (console warnings).
    def _diag(sink: dict) -> str:
        return sinks._sink_diag_line(sink, with_description=False)

    sel = sinks.select_speaker_sinks()
    tier, selected, all_sinks = sel["tier"], sel["selected"], sel["all_sinks"]

    if tier == "strict":
        if len(selected) == 1:
            return selected[0]["name"], []
        return None, [
            f"multiple speaker sinks found ({len(selected)}): "
            + ", ".join(s["name"] for s in selected)
            + "; pass --target-sink to pick one"
        ]

    if tier == "relaxed":
        if len(selected) == 1:
            sink = selected[0]
            return sink["name"], [
                "no sink tagged device.icon_name=audio-speakers; using the "
                f"only internal analog sink {_diag(sink)}; pass --target-sink "
                "to override"
            ]
        return None, [
            f"multiple internal analog sinks found ({len(selected)}): "
            + ", ".join(_diag(s) for s in selected)
            + "; pass --target-sink to pick one"
        ]

    # tier == "none"
    if not all_sinks:
        return None, [
            "no Audio/Sink nodes found via pw-dump (no PipeWire daemon running?)"
        ]
    return None, [
        "no internal-speaker sink found (none tagged "
        "device.icon_name=audio-speakers, and no internal analog output); "
        "sinks seen: " + ", ".join(_diag(s) for s in all_sinks)
        + "; pass --target-sink to pick one"
    ]


def _validate_conf(conf_text: str) -> tuple[int, str]:
    """Run validate_conf.py against `conf_text`.

    Returns (returncode, combined_output). Returncode -1 signals a setup
    skip (script absent, or `lv2info`/`spa-json-dump` missing); the
    caller treats that as a soft warning, not a hard failure. The
    `validate_conf.py` script's own contract: 0 = clean, 1 = errors,
    2 = setup error.
    """
    if not VALIDATE_CONF_SCRIPT.is_file():
        return -1, f"validate_conf.py not found at {VALIDATE_CONF_SCRIPT}"
    if not shutil.which("lv2info") or not shutil.which("spa-json-dump"):
        return -1, ("lv2info or spa-json-dump not in PATH "
                    "(install lilv-utils and pipewire)")
    rc = subprocess.run(
        [sys.executable, str(VALIDATE_CONF_SCRIPT), "-", "-q"],
        input=conf_text, capture_output=True, text=True, timeout=30,
    )
    return rc.returncode, (rc.stderr or "") + (rc.stdout or "")


def _print_results(conf_path: Path, irs_path: Path | None,
                   *, dry_run: bool) -> None:
    """Report where the conf (and copied IRS) landed — or, under --dry-run,
    where they *would* land."""
    # "impulse response (.irs)", not the bare acronym: it is never expanded
    # anywhere else a user reads (round 5).
    if dry_run:
        console.cprint("ok", f"Would write conf: {conf_path}")
        if irs_path is not None:
            console.cprint("ok", f"Would copy impulse response (.irs): {irs_path}")
    else:
        console.cprint("ok", f"Wrote conf: {conf_path}")
        if irs_path is not None:
            console.cprint("ok", f"Copied impulse response (.irs): {irs_path}")


def _print_next_steps(node_name: str,
                      target_object: str | None = None) -> None:
    """The actions to take after a real (non-dry-run) write."""
    console.cprint("head", "Next steps:")
    console.cprint("cta", "  1. Restart PipeWire:        "
                  "systemctl --user restart pipewire pipewire-pulse")
    # Stays a numbered step here, unlike the wrapper's footnote: this script
    # converts an existing EasyEffects preset, so its caller almost certainly
    # runs EasyEffects. "Stop it starting again" because quitting the window
    # ends double-processing for this session only — the background service
    # and autostart entry bring it back at the next login.
    console.cprint("cta", "  2. Avoid double-processing: quit EasyEffects and stop "
                  "it starting again (its Background Service and autostart, "
                  "or remove its autoload for this device)")
    console.cprint("cta", "  3. Verify the sink:         pw-cli ls Node | grep "
                  f"{_sanitize_name(node_name)}")
    # What success looks like (round 6): with no expected output stated, an
    # empty grep couldn't be told apart from "this step doesn't matter".
    # Names the usual cause of an empty grep rather than blaming the restart:
    # a missing LSP/Calf plugin makes module-filter-chain drop the whole
    # conf, so no node appears and re-restarting never helps.
    console.cprint("dim", "     (it should print a line, showing node.name = \"...\"; "
                  "nothing usually means the LSP or Calf LV2 plugins are "
                  "missing, so the whole file failed to load)")
    if target_object:
        console.cprint("cta", "  4. Verify routing:          "
                      f"pw-link -l | grep {target_object}")
