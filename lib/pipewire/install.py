"""Where the conf and its impulse response go, and what to do once they're there.

The write itself stays in ``ee_to_pipewire.py``'s ``main`` — this is
everything around it: the smart-filter target sink to pin to, the ``lv2info``
schema self-check the conf is run through before it is written, the convolver
retarget that makes the conf self-contained, and the two blocks a user reads
afterwards. Where the impulse response is read *from* is not decided here:
``--irs-dir`` defaults to ``lib.ee_paths.DEFAULT_IRS_DIR``, the same attribute
the generator's ``--irs-dir`` writes to, so the two cannot drift apart.

``_autodetect_speaker_sink`` shares its probe with the EasyEffects autoload
pathway (``lib.hardware.sinks``). Importing it is free: ``pw-dump`` is read
inside ``_enumerate_audio_sinks``, so a converter run that never needs a sink
never shells out, whether the import sits here or in the function.

``_sanitize_name`` is imported bare from ``lib.pipewire.conf`` because
``_print_next_steps`` calls it and arrived here as a move; see that module's
docstring for why a carried body may not be re-pointed. ``PIPEWIRE_RESTART_CMD``
rides the same import — it was defined here until ``checks.py`` and
``ee_to_pipewire.py`` had to spell it too, and ``conf`` is the module all
three already share. Not stdlib-only —
``lib.console`` owns the optional rich dependency — but nothing here reaches
the DSP stack.

The block from ``QUIT_EE_HINT`` down is the same seam for
``dolby_to_pipewire.py``: restart, poll ``pw-cli`` until the nodes appear,
say what is still to be picked, and say how to undo the whole thing. It has
no edge to ``lib.pipewire.checks`` and adds none — that direction is the one
deliberately left open so the two cannot form a cycle. It is also where this
module's ``shutil``/``subprocess``/``time`` bindings are read, which is what
the wrapper's tests patch through.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

from lib import console
from lib.hardware import sinks
from lib.paths import REPO_ROOT
from lib.pipewire.conf import PIPEWIRE_RESTART_CMD, _sanitize_name


VALIDATE_CONF_SCRIPT = REPO_ROOT / "tools" / "measure_pw" / "validate_conf.py"


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


def _grep_expectation(tail: str = "") -> str:
    """The line printed under a ``pw-cli ls Node | grep`` step, saying what
    success looks like. Both finish paths render it from here, so the two
    cannot tell a user different things about the same grep.

    What success looks like (round 6): with no expected output stated, an
    empty grep couldn't be told apart from "this step doesn't matter". And it
    names the usual cause of an empty grep instead of blaming the restart —
    when an LSP or Calf plugin is missing, module-filter-chain fails to load
    the whole conf and no node ever appears, so a reader told only "the
    restart didn't load it" re-restarts forever. The automated path says it
    too, in its own words (``_verify_sinks``).

    ``tail`` appends what happens *once* the line shows up: the only part that
    differs between the callers, and empty where the caller says nothing about
    it.
    """
    once = f" — {tail}" if tail else ""
    return ('     (it should print a line, showing node.name = "..."; '
            "nothing usually means the LSP or Calf LV2 plugins are "
            f"missing, so the whole file failed to load{once})")


def _print_next_steps(node_name: str,
                      target_object: str | None = None) -> None:
    """The actions to take after a real (non-dry-run) write."""
    console.cprint("head", "Next steps:")
    console.cprint("cta", f"  1. Restart PipeWire:        {PIPEWIRE_RESTART_CMD}")
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
    console.cprint("dim", _grep_expectation())
    if target_object:
        console.cprint("cta", "  4. Verify routing:          "
                      f"pw-link -l | grep {target_object}")


# Conditional on purpose: this script's audience chose it to avoid
# EasyEffects, and an unconditional "quit EasyEffects" read as "did
# something install it behind my back?" (round-3 review). The
# double-processing consequence rides the activation warning, which
# appends it.
# "and stop it starting again" rather than a bare "quit it": EasyEffects
# ships a background service and an autostart entry — both recommended in
# our own README — so quitting the window ends double-processing for this
# session only, and it comes back at the next login.
QUIT_EE_HINT = ("If you also run EasyEffects on this device, quit it and "
                "stop it starting again (its Background Service and "
                "autostart, or remove its autoload)")


def _print_undo(written: list[Path]) -> None:
    """How to get back. Everything else here asks the reader to restart their
    sound server with a config file they can't read, and never said what to do
    if the result is worse — or if PipeWire won't come back. Deleting the conf
    and restarting is the whole answer; it just has to be written down."""
    # Only files that exist: the .irs copy is skipped when the source
    # already sits at the target, and an rm over a missing file aborts the
    # pasted command halfway.
    paths = [p for p in written if p.exists()]
    if not paths:
        return
    files = " ".join(f"'{p}'" for p in paths)
    console.cprint("dim", "  To undo: rm " + files)
    console.cprint("dim", f"           {PIPEWIRE_RESTART_CMD}")


def _print_manual_activation(node_names: list[str],
                             written: list[Path],
                             selectable: bool = False) -> None:
    # The EasyEffects caveat is a footnote, not a numbered step (round
    # 10): this script's reader chose it to avoid EasyEffects, and seeing
    # EE in the critical path made them doubt they had.
    console.cprint("head", "[3/3] Activation skipped (--no-activate) — to finish:")
    console.cprint("cta", f"  1. Restart PipeWire:        {PIPEWIRE_RESTART_CMD}")
    # Numbered per sink rather than all "2.": with --variant all this loop
    # printed three consecutive steps sharing one number, under a note
    # referring back to "step 1".
    for i, name in enumerate(node_names, start=2):
        console.cprint("cta", f"  {i}. Verify the sink:         pw-cli ls Node | grep "
                      f"{name}")
    # "Pinned ... automatically": the verify step proved existence, not
    # routing, and nothing said whether to go pick it in Settings (round
    # 10) — the smart filter pins it, so say so. This path carries a tail
    # where _print_next_steps has none.
    # "pinned automatically" is true of smart-filter routing only. Under
    # --target-sink '' the chain is an ordinary output that does nothing until
    # it is selected, and this sentence promised the opposite.
    tail = ("once the line is there, pick it as your output in sound settings"
            if selectable else
            "once the line is there, it's pinned to your speakers automatically")
    console.cprint("dim", _grep_expectation(tail))
    console.cprint("dim", f"  Note: {QUIT_EE_HINT}.")
    print()
    _print_undo(written)


def _verify_sinks(node_names: list[str], timeout=6.0, interval=0.5) -> int:
    """Poll pw-cli until every expected node shows up — the chain takes a
    moment to load after the restart. Missing after the timeout usually
    means a missing LV2 plugin."""
    if shutil.which("pw-cli") is None:
        console.cprint("warn", "pw-cli not found — can't verify the sinks loaded; "
                       "check with: pw-cli ls Node | grep <name>")
        return 0
    deadline = time.monotonic() + timeout
    missing = list(node_names)
    while missing:
        try:
            listing = subprocess.run(["pw-cli", "ls", "Node"],
                                     capture_output=True, text=True,
                                     timeout=10).stdout
        except (subprocess.TimeoutExpired, OSError):
            listing = ""
        missing = [n for n in missing if n not in listing]
        if not missing or time.monotonic() >= deadline:
            break
        time.sleep(interval)
    for name in node_names:
        if name not in missing:
            console.cprint("ok", f"Sink loaded: {name}")
    if missing:
        for name in missing:
            console.cprint("err", f"error: sink {name} did not appear after the "
                          "restart")
        console.cprint("cta", "Check that the LSP/Calf LV2 plugins are installed "
                      "(README: Plugin dependencies and validation), then "
                      f"retry: {PIPEWIRE_RESTART_CMD}")
        return 1
    return 0


def _activate(node_names: list[str], selectable: bool) -> int:
    console.cprint("head", "[3/3] Activating: restarting PipeWire")
    # Not only "otherwise both chains process the audio": the restart itself
    # stops a running EasyEffects (it doesn't survive its server going away),
    # so whatever EasyEffects was applying stops here whether or not the
    # reader acts. Said plainly, because the alternative is discovering it as
    # "the update broke my audio".
    console.cprint("warn", f"{QUIT_EE_HINT} — otherwise both chains process the "
                   "audio at once. The restart below stops it for this "
                   "session either way, so anything it was applying goes "
                   "with it.")
    try:
        proc = subprocess.run(PIPEWIRE_RESTART_CMD.split())
    except FileNotFoundError:
        console.cprint("warn", "systemctl not found (not a systemd system?) — "
                       "restart PipeWire yourself; the systemd equivalent "
                       f"is: {PIPEWIRE_RESTART_CMD}")
        return 0
    if proc.returncode != 0:
        console.cprint("err", f"error: PipeWire restart failed (rc "
                      f"{proc.returncode}) — run it manually: "
                      f"{PIPEWIRE_RESTART_CMD}")
        return 1
    rc = _verify_sinks(node_names)
    if rc == 0:
        _print_selection_step(node_names, selectable)
    return rc


def _print_selection_step(node_names: list[str], selectable: bool) -> None:
    """Say what still has to happen for the chain to be in the audio path.

    With smart-filter routing (the default) nothing does — WirePlumber
    inserts it. With --target-sink '' the chain is an ordinary output that
    processes nothing until it is selected, and a run that stops at
    "Sink loaded" leaves the reader believing it is already working.
    """
    if not selectable:
        console.cprint("dim", "     (pinned to your speakers automatically — apps "
                      "keep playing to the speaker as usual)")
        return
    console.cprint("cta", "  Now pick it as your output in sound settings — until "
                  "you do, it processes nothing:")
    for name in node_names:
        console.cprint("cta", f"    {name}")
