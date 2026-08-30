"""Building the presets one parsed profile asks for, and writing them out.

The loop `main()` hands a parsed tuning to: for each of the three IEQ voicings
the XML carries, sum that voicing's curve with the audio-optimizer correction,
turn the result into a minimum-phase FIR, write the `.irs` beside the preset
JSON that names it, and check the built filter back against the curve it was
asked for. Everything it needs beyond `lib/preset/fir.py`'s design math and
`lib/preset/build.py`'s preset dict is here: the pass/fail gate for that check,
and the WAV writer.

`save_wav_stereo` sat in the generator through six slices for a reason that has
since expired — binding `wavfile` in another module meant a second deferred
import, which is new code rather than motion. It has nothing to defer to now:
`lib/preset/fir.py` imports numpy at module scope already, so a module the
generator only reaches from inside `main()` may import scipy the same way, in
the top-level block a move commit is allowed to write. It is still the only
caller of `lib/preset/autoload.py`'s `_atomic_write` outside that module.

Imported inside `dolby_to_easyeffects.py`'s `main()` rather than at the top of
it, like `lib/report/profile.py` and for the same reason: numpy and scipy are
~0.35 s of a ~0.5 s startup, so everything that returns before the emit loop —
including a tab completion, which argcomplete re-runs the whole script for on
every TAB press — must not reach them
(`tests/test_layout.py::test_the_dsp_import_is_deferred_past_every_early_return`).

`VOICING_CURVES` comes from `lib/report/messages.py` — the Balanced/Detailed/
Warm table is copy as much as it is data, and neither of its other readers is
a place this module could reach it from: `lib/report/profile.py` sits in a
package this one may not import, and `dolby_to_pipewire.py`'s `--variant`
choices sit in a root script, not a package at all. That is the only edge from
`lib/preset/` into `lib/report/`, and it is one-way: nothing under
`lib/report/` imports this module.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import numpy as np
from scipy.io import wavfile

from lib import console, doctor, ee_paths
from lib.dax import parse
from lib.preset import autoload, build, fir
from lib.report import messages


def stereo_taps(fir_left: np.ndarray, fir_right: np.ndarray) -> np.ndarray:
    """The exact float32 interleaved array save_wav_stereo writes — what
    kernel_name() hashes, so the name follows the bytes on disk and nothing
    else."""
    return np.column_stack([fir_left, fir_right]).astype(np.float32)


def save_wav_stereo(path: Path, fir_left: np.ndarray,
                    fir_right: np.ndarray) -> None:
    """Save stereo impulse response as 32-bit float WAV."""
    with autoload._atomic_write(path) as tmp:
        wavfile.write(str(tmp), fir.SAMPLE_RATE, stereo_taps(fir_left, fir_right))


def kernel_name(preset_name: str, taps: np.ndarray) -> str:
    """`{preset}-{8 hex}`: the impulse's name carries a hash of its samples.

    EasyEffects re-reads an .irs only when the convolver's kernel name
    *changes* — its preset loader skips the setter on an equal name, and the
    generated KConfig setter short-circuits again — so a same-name rewrite
    left the old FIR playing after a reload, `easyeffects -l` or a GUI
    re-pick until EasyEffects restarted. A name that follows the content
    changes exactly when the sound does. Eight hex digits (git-short-SHA
    odds). The samples' last bits can differ between numpy/BLAS builds, so
    the name is stable on one machine, not across machines. Why, and the
    rejected alternative: docs/design-notes.md "Rejected approaches".
    """
    return f"{preset_name}-{hashlib.sha256(taps.tobytes()).hexdigest()[:8]}"


def _impulse_referrers(stem: str, output_dir: Path) -> list[str]:
    """Presets, PipeWire confs and EasyEffects' own saved settings still
    naming `{stem}.irs`.

    A preset saved from EasyEffects' GUI keeps the kernel name of the preset
    it was derived from, so a file this run would call stale can be the only
    impulse another preset has — and EasyEffects loads a missing kernel
    without a word, leaving that preset convolving nothing. A conf written
    with `ee_to_pipewire.py --no-copy-irs` pins the EE-side path the same
    way, and there a missing file stops the whole conf loading. And
    EasyEffects restores the convolver's kernel *name* from its config db on
    start, not from the preset JSON: with that file gone, a fresh instance
    logged "Kernel 'Dolby-Balanced' not found … Entering passthrough mode"
    (dev machine, 2026-08-27) — silent until the next preset load, which
    without autoload is never. Until a load names the new impulse, the one
    the db names is what the next start plays.

    Reads EasyEffects' own preset directory as well as this run's, and both
    PipeWire conf directories — why, beside each scan.
    """
    users: list[str] = []
    try:
        db = (ee_paths.DEFAULT_EASYEFFECTS_RC.parent / "convolverrc").read_text()
        if re.search(rf"^kernelName={re.escape(stem)}\s*$", db, re.M):
            users.append("EasyEffects' saved settings")
    except OSError:
        pass
    # This run's tree and EasyEffects' own — not the same directory when
    # --output-dir alone was passed: --irs-dir then still means the live
    # tree, and the presets naming a file there are the live ones, which
    # this run never rewrote. A directory too many only ever keeps a file.
    preset_dirs = [output_dir]
    if ee_paths.DEFAULT_OUTPUT_DIR.resolve() != output_dir.resolve():
        preset_dirs.append(ee_paths.DEFAULT_OUTPUT_DIR)
    for preset_dir in preset_dirs:
        try:
            presets = sorted(preset_dir.glob("*.json"))
        except OSError:
            continue
        for path in presets:
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            out = data.get("output") if isinstance(data, dict) else None
            if not isinstance(out, dict):
                continue
            if any(key.startswith("convolver") and isinstance(block, dict)
                   and block.get("kernel-name") == stem
                   for key, block in out.items()):
                users.append(path.stem if preset_dir is output_dir
                             else f"{path.stem} in {doctor.tilde(preset_dir)}")
    # Lazy and one-way: nothing under lib/pipewire imports lib/preset, and
    # this is the one place the EasyEffects writer needs to know where the
    # PipeWire confs live. Both directories a conf is live from — the
    # daemon's and filter-chain.service's; docs/alternative-pipelines.md
    # hands out a skeleton for the second.
    from lib.pipewire import checks
    for conf_dir in checks.live_conf_dirs():
        try:
            confs = sorted(conf_dir.glob("*.conf"))
        except OSError:
            continue
        for conf in confs:
            try:
                if f"{stem}.irs" in conf.read_text():
                    users.append(conf.name)
            except OSError:
                continue
    return users


def _drop_stale_impulses(irs_dir: Path, preset_name: str, keep: Path,
                         output_dir: Path) -> None:
    """Delete the .irs files earlier runs of *this* preset left behind.

    Ours only: `{preset_name}-<8 hex>` (an earlier build of the same preset)
    and the legacy unhashed `{preset_name}.irs`. The voicing is always the
    last name part, so nothing else this tool writes can match. A file some
    preset or conf still names is kept, and the run says which — the reader
    may want to know that preset now plays an older impulse. Never reached
    under --dry-run: the caller gates it, beside the write it pairs with, and
    only after the preset JSON is on disk (until then that JSON itself still
    named the old impulse).
    """
    try:
        stale = sorted(p for p in irs_dir.glob(f"{preset_name}*.irs")
                       if p != keep
                       and autoload.kernel_belongs_to(preset_name, p.stem))
    except OSError:
        return
    for path in stale:
        users = _impulse_referrers(path.stem, output_dir)
        if users:
            console.cprint("dim", f"  Kept {doctor.tilde(path)} — still used by "
                           f"{', '.join(users)}; those keep that older impulse")
            continue
        try:
            path.unlink()
        except OSError:
            continue
        console.cprint("dim", f"  Removed {doctor.tilde(path)} (superseded)")


# Verdict gate for the printed FIR verification: far above the minimum-phase
# design's normal residual (~0.05 dB at the 20 probe points) and below
# anything audible, so it warns only when the reconstruction actually broke.
FIR_VERIFY_OK_DB = 0.5


def _worst_shape_error(taps, combined, offset_db, freqs, fft_freqs, *,
                       rows: bool) -> float:
    """Worst |deviation| in dB between one built channel and the curve it was
    asked for, at the XML's own band frequencies.

    Both sides are peak-normalised, so this grades the SHAPE only — the
    absolute level is short by the curve's peak by design (make_fir
    normalises, and level-restore hands that back downstream). Saying
    "matches the curve" without "the shape of" would claim a level match the
    check never makes.
    """
    H = np.fft.rfft(taps, n=fir.FIR_LENGTH)
    mag_db = 20.0 * np.log10(np.abs(H) + fir.LOG_MAG_FLOOR)
    peak = np.max(combined)
    worst = 0.0
    for i, f in enumerate(freqs):
        idx = np.argmin(np.abs(fft_freqs - f))
        target = combined[i] - peak + offset_db
        err = mag_db[idx] - target
        worst = max(worst, abs(err))
        if rows:
            console.cprint("dim", f"  {f:>7} Hz  target: {target:+6.1f}  "
                           f"actual: {mag_db[idx]:+6.1f}  "
                           f"error: {err:+5.2f}")
    return worst


def _emit_ieq_presets(tuning, name_base, is_soundwire, disabled, args,
                      profile_label, all_preset_names, filters_by_profile,
                      kernel_by_preset, warned: bool = False):
    """Generate the Balanced/Detailed/Warm IEQ presets for one parsed profile:
    build each combined FIR, write the .irs + .json, print the verification
    table, and record emitted filters. Mutates ``all_preset_names``,
    ``filters_by_profile`` and ``kernel_by_preset`` (preset name → the
    impulse's stem) in place — main() passes three fields of its
    ``RunTally`` and reads them back off it after the loop."""
    curves = tuning.curves
    peq_filters = tuning.peq_filters
    vol_leveler = tuning.vol_leveler
    dialog_enhancer = tuning.dialog_enhancer
    mb_comp = tuning.mb_comp
    regulator = tuning.regulator
    freqs = tuning.freqs
    volmax_boost = tuning.volmax_boost

    # ieq-amount is a percentage: amount=10 -> the IEQ voicing is applied
    # at 10% weight on top of the audio-optimizer correction, not as a
    # full-depth EQ. DAX steers the IEQ via Media Intelligence
    # (mi-ieq-steering-enable), so a small static weight approximates its
    # steady-state; full weight (the old amount/10 reading) over-applied
    # the IEQ and crashed the HF match to DAX by up to ~28 dB. See
    # docs/design-notes.md "Finding 9".
    scale = tuning.ieq_amount / 100.0

    # Audio-optimizer curves in dB
    ao_db_left = np.array(tuning.ao_left) / parse.DB_FIXED_POINT_SCALE
    ao_db_right = np.array(tuning.ao_right) / parse.DB_FIXED_POINT_SCALE

    ieq_presets = {f"{name_base}-{label}": key
                   for label, key in messages.VOICING_CURVES.items()}

    # One hidden-tables hint per profile, at the spot the first table would
    # have occupied — three identical lines read as a nag.
    tables_hint_pending = not args.verbose
    # (preset_name, worst-deviation) per built FIR — the default view prints
    # one consolidated verdict after the loop; three identical green
    # "passed" lines read as three separate validations (round 6).
    check_results: list[tuple[str, float]] = []

    for preset_name, curve_key in ieq_presets.items():
        if curve_key not in curves:
            console.cprint("warn", f"  Skipping {preset_name}: curve '{curve_key}' not found in XML")
            continue

        gains_raw = curves[curve_key]
        ieq_db = np.array(gains_raw) / parse.DB_FIXED_POINT_SCALE * scale

        # Combined target: IEQ + audio-optimizer (summed in dB)
        combined_left = ieq_db + ao_db_left
        combined_right = ieq_db + ao_db_right

        # Generate FIR impulse responses. freqs is the XML's raw int band
        # list; make_fir casts both of its curve arguments to float itself,
        # so there is nothing to convert on the way in.
        fir_left, peak_left_db = fir.make_fir(freqs, combined_left,
                                              normalize=True)
        fir_right, peak_right_db = fir.make_fir(freqs, combined_right,
                                                normalize=True)

        # --enable level-restore: hand the chain back the level normalisation
        # removed. make_fir divides each channel by its own realised peak, so
        # a curve whose peak outruns its volmax-boost emits a preset quieter
        # than bypass — the deficit is exactly peak_db - volmax_boost, and it
        # is what issues #25/#46/#50 describe. The restored amount is the
        # peak make_fir measured, so nothing here is a tuned offset.
        #
        # Re-reference both channels to the louder peak first. Normalising
        # each channel to its own peak also flattens the L/R level
        # relationship the two AO curves ask for — the two combined peaks
        # diverge on 19.1% of the corpus (median 0.93 dB, max 5.56;
        # re-derived 2026-08-04 over 3051 parsed XMLs). A common reference
        # keeps that relationship and still leaves every channel at or below
        # 0 dBFS, so the on-disk peak-normalisation convention holds.
        fir_peak_db = max(peak_left_db, peak_right_db)
        # Non-zero only on the flag-on path, and only for the quieter
        # channel; the correction check below re-references by the same
        # amount so it keeps grading the filter rather than the re-reference.
        left_offset_db = 0.0
        right_offset_db = 0.0
        if "level-restore" in args.enable:
            left_offset_db = peak_left_db - fir_peak_db
            right_offset_db = peak_right_db - fir_peak_db
            fir_left *= 10.0 ** (left_offset_db / 20.0)
            fir_right *= 10.0 ** (right_offset_db / 20.0)

        # Save stereo impulse response, named after its own samples so a
        # changed FIR gets a new kernel name — see kernel_name().
        irs_stem = kernel_name(preset_name, stereo_taps(fir_left, fir_right))
        irs_path = args.irs_dir / f"{irs_stem}.irs"
        if not args.dry_run:
            save_wav_stereo(irs_path, fir_left, fir_right)

        # Create preset (kernel-name is the WAV filename stem)
        preset, emitted = build.make_preset(irs_stem, peq_filters, vol_leveler,
                                      dialog_enhancer, mb_comp, regulator,
                                      freqs, is_soundwire=is_soundwire,
                                      volmax_boost=volmax_boost,
                                      volmax_slot=args.volmax_slot,
                                      fir_peak_db=fir_peak_db,
                                      enabled=set(args.enable),
                                      disabled=disabled,
                                      virtual_bass=tuning.virtual_bass)
        for name in emitted:
            filters_by_profile.setdefault(name, set()).add(profile_label)
        out_path = args.output_dir / f"{preset_name}.json"
        if not args.dry_run:
            autoload._atomic_write_text(out_path, json.dumps(preset, indent=4) + "\n")

        all_preset_names.append(preset_name)
        kernel_by_preset[preset_name] = irs_stem

        # "Staged", dimmed, when a wrapper is writing into a tempdir it
        # will delete: round-4's wrapper reviewer saw the same green
        # "Wrote" on these doomed files as on the conf that survives, and
        # expected to find them later.
        if args.dry_run:
            style, verb = "ok", "Would write"
        elif getattr(args, "staged", False):
            style, verb = "dim", "Staged"
        else:
            style, verb = "ok", "Wrote"
        # Collapsed at the print only: both variables are write targets a few
        # lines above, and ~ is not a path any of that would expand.
        console.cprint(style, f"{verb} {doctor.tilde(irs_path)}")
        console.cprint(style, f"{verb} {doctor.tilde(out_path)}")
        if not args.dry_run:
            _drop_stale_impulses(args.irs_dir, preset_name, irs_path, args.output_dir)
        # The tables are behind -v: even marked skippable they were the
        # bulk of the output, burying the findings between them, and their
        # only reader is someone diagnosing a wrong-sounding preset — who
        # is told to re-run with -v. The verdict line below prints either
        # way, so the check itself is never hidden.
        if args.verbose:
            print(f"  {curve_key} combined IEQ+AO curve (left channel):")
            print(f"  {'freq':>8}  {'IEQ':>6}  {'AO':>6}  {'combined':>8}")
            for i, f in enumerate(freqs):
                print(f"  {f:>7} Hz  {ieq_db[i]:+5.1f}  {ao_db_left[i]:+5.1f}  {combined_left[i]:+7.1f}")
        elif tables_hint_pending:
            tables_hint_pending = False
            console.cprint("dim", "  (frequency tables hidden — re-run with -v to "
                          "print them)")

        # Verify FIR frequency response — the math runs either way; -v only
        # decides whether the per-frequency rows print.
        fft_freqs = np.fft.rfftfreq(fir.FIR_LENGTH, d=1.0 / fir.SAMPLE_RATE)
        if args.verbose:
            console.cprint("dim", "\n  FIR verification (left, normalized to "
                          "peak=0):")
        worst_left = _worst_shape_error(fir_left, combined_left,
                                        left_offset_db, freqs, fft_freqs,
                                        rows=args.verbose)
        # The right channel carries its own audio-optimizer curve, so a fault
        # can live there alone — grade it too. The rows stay left-only (the
        # table is already sixty lines); the verbose verdict names both
        # figures, so neither side is graded behind the reader's back.
        worst_right = _worst_shape_error(fir_right, combined_right,
                                         right_offset_db, freqs, fft_freqs,
                                         rows=False)
        worst = max(worst_left, worst_right)
        # A table of sixty "error" rows with no verdict reads as a slow
        # drift going wrong; nobody outside this file knows 0.03 dB is a
        # pass. The threshold is far above the minimum-phase design's
        # normal residual (~0.05 dB) and below anything audible.
        # "Correction check", not "FIR check": FIR was the one label in the
        # summary with no plain reading (round 4), and "correction" is the
        # audio-optimizer line's vocabulary for the same curve.
        # No "(inaudible)": printed a few lines under a ⚠ loudness warning,
        # the green all-clear read as canceling it (round 5). This line is
        # about curve accuracy only — keep listening language out.
        check_results.append((preset_name, worst))
        if args.verbose:
            # Next to its own table; the default view gets one verdict for
            # all three after the loop.
            # "its target" named nothing a reader could point at. The target
            # is the curve computed from their tuning — say that, since the
            # whole value of the line is which side it certifies.
            if worst <= FIR_VERIFY_OK_DB:
                console.cprint("ok", f"  Correction check passed: the built filter "
                             f"matches the shape of the curve your tuning "
                             f"file asks for, within {worst:.2f} dB "
                             f"(left {worst_left:.2f}, "
                             f"right {worst_right:.2f})")
            else:
                console.cprint("warn", f"  Correction check: {worst:.2f} dB away from "
                               "the shape of the curve your tuning file asks "
                               f"for, at worst (left {worst_left:.2f}, "
                               f"right {worst_right:.2f}) — unexpected, "
                               "please report this run")
        print()

    fails = [(n, w) for n, w in check_results if w > FIR_VERIFY_OK_DB]
    if not args.verbose and check_results:
        if not fails:
            worst_all = max(w for _, w in check_results)
            # Dim, not green, when a ⚠ fired above: the celebratory color
            # read as cancelling the warning (round 9, user-picked
            # rendering) — the check only covers curve accuracy.
            console.cprint("dim" if warned else "ok",
                   f"  Correction check passed: all "
                   f"{len(check_results)} filters match the shape of the curve "
                   f"your tuning file asks for, within {worst_all:.2f} dB")
        else:
            for name, w in fails:
                console.cprint("warn", f"  Correction check ({name}): {w:.2f} dB away "
                               "from the shape of the curve your tuning file "
                               "asks for, at worst — unexpected, please "
                               "report this run")
        print()
