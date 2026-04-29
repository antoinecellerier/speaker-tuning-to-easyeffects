# CLAUDE.md

A few things about this repo that aren't obvious from reading the code:

- **There is a `pytest` suite under `tests/` with no proprietary
  inputs.** Run it as `pytest tests/` — the bulk of the suite (DSP
  math, output schema, the trap-regression suite that locks in every
  shipped bug listed below, and `--disable`/argparse coverage) runs in
  a couple of seconds and needs no setup. Corpus tests under
  `tests/corpus/` run the full pipeline against real DAX3 XMLs that
  the test code auto-discovers the same way the script does (NTFS
  mounts and CWD); set `ATMOS_CORPUS_DIR=/path/to/xmls` to override
  discovery, or do nothing and the corpus tier will skip cleanly when
  no corpus is reachable. When changing math (FIR generation,
  coefficient decoding, gain staging, unit conversions, filter
  design), add or extend a unit test — ad-hoc numpy/scipy scripts
  under `localresearch/` are still fine for exploration, but if a
  check is worth re-running it belongs in `tests/`. For
  preset/structure changes, run the script against a real XML,
  confirm the expected files appear under
  `~/.local/share/easyeffects/`, and load the preset in EasyEffects.
  If you can't verify locally, say so. **The suite catches structural
  regressions, not audible ones — see below.**

- **Ask the user to confirm audio quality after any change that touches
  the output path.** The `tests/` suite catches structural regressions
  (the trap list below) but **does not** substitute for listening —
  past sessions shipped bugs that only showed up on real content. Tell
  the user what to listen for based on what the change touched:
  - *Clipping or sudden level jumps* on loud content — past traps
    include the convolver autogain +50 dB bug and MBC output-gain
    misconfiguration.
  - *Pumping or saturation on quiet → loud transitions* — the reason
    autogain is bypassed by default; re-enabling or moving it will
    likely reintroduce it unless Media Intelligence steering is
    somehow approximated.
  - *Frequency-response ripple or muddy mids / harsh highs* — what
    parametric-bell stacking on the IEQ curve produced before it was
    replaced with FIR convolution.
  - *Loss of loudness / content sounding quieter than reference* —
    over-conservative PEQ output-gain compensation or headroom trims.
  - *Audible noise-floor boost during silence* — the upward-compression
    trap on LSP MBC defaults.

- **The filter chain must be derivable solely from the published
  Dolby DAX3 tuning XML.** The project's value prop is "feed in any
  per-device XML, get a faithful EE preset"; empirical / hand-tuned
  offsets that don't trace back to an XML field break that
  invariant. Concretely:
  - Every parameter the script emits (FIR coefficients, biquad
    frequencies/Q/gain, compressor thresholds, regulator gains, etc.)
    must be derived from a parsed XML field. If a value falls back
    to a hardcoded default, that's a topology filler, not a tuning
    choice — keep it out of the audible path or document the
    fallback as part of the schema interpretation.
  - "DAX-on-Windows captures something different from our
    EE-on-Linux output" is **not** a license to fit our chain to
    the captured response. Empirical fits are pragmatic shortcuts
    that *invert* the value prop (a Linux preset that matches one
    machine's DAX driver but stops generalising). If empirical
    tuning is ever desired, ship it as opt-in (a flag, a separate
    converter mode) so the principled XML-only path stays the
    default for every other XML the script consumes. See
    `docs/design-notes.md` "Follow-ups" section for the standing
    list of empirical shortcuts that have been considered but not
    adopted by default.
  - Investigation flags introduced to test a hypothesis on the
    main converter (`dolby_to_easyeffects.py`) are temporary
    scaffolding. Once the hypothesis is closed (decisive result
    documented), revert the flag — the experiment is more
    valuable as a permanent design-notes finding than as a
    permanent CLI surface that future readers feel obliged to
    keep correct. The same rule does *not* apply to harness /
    measurement tooling under `tools/` (e.g.
    `tools/measure_ee/sweep_variants.sh`,
    `tools/measure_ee/summarise_variants.py`); those are
    test-rig infrastructure, kept around so future variant
    experiments don't reinvent the wheel.

- **Past rabbit holes worth skipping:**
  - *`filter_coefficients`* (the base64 blob in `tuning-vlldp`) is not an
    audio EQ. It's VLLDP-internal analysis filters; the audio-optimizer
    and PEQ parameters already carry the speaker correction.
  - *EasyEffects preset format has quirks*: enum parameters must be
    string labels, not integer indices (commit `91423b8` was this exact
    bug); impulse-response files need the `.irs` extension and EE 8.x
    convolver wants `"kernel-name"` (filename stem), not the deprecated
    `"kernel-path"`.
  - *FIR must be minimum-phase.* A naive inverse-FFT on a target
    magnitude produces a linear-phase filter with audible pre-ringing.
    The cepstral processing in the script is load-bearing — don't
    "simplify" it.

- **GitHub issue comments need a Claude Code attribution footer**
  (`🤖 Generated with [Claude Code](https://claude.com/claude-code)`),
  same as commits get `Co-Authored-By`. Applies to `gh issue/pr comment`
  bodies.

Everything else — plugin chain rationale, gain-staging, unit
conversions, cross-device findings — lives in `docs/` and is linked
from the README's "Further reading" section.
