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
    machine's DAX driver but stops generalising). However: the
    XML → filter-parameter mappings the converter implements are
    themselves *hypotheses about what the schema means*, not
    revealed truth. We have no Dolby spec; everything in
    `parse_xml` is empirically inferred from corpus patterns and
    sanity checks. DAX captures (where available) are the only
    external signal we have for whether those mappings are
    correct, so capture experiments that consistently move EE
    closer to DAX *across all bands* without per-band
    regressions are evidence the current mapping is wrong and
    worth revising — even if the new mapping diverges from the
    "vsXML" reference (which is computed against our existing
    interpretation and so begs the question). The bar to
    actually change a default mapping is high — at least one
    second-device DAX capture confirming the new rule generalises
    — but the mappings themselves are not above empirical
    falsification. What stays out of bounds is per-device
    hand-tuned offsets that don't trace back to *any* XML
    field. If empirical tuning is ever desired, ship it as
    opt-in (a flag, a separate converter mode) so the
    principled XML-only path stays the
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

- **`ee_to_pipewire.py` is the companion converter.** Turns the
  generated EasyEffects preset into a PipeWire `filter-chain` `.conf`
  for users who'd rather not run EE. Covers convolver / PEQ /
  dialog / MBC / regulator / limiter (LSP-backed) plus
  `bass_enhancer` / `stereo_tools` (Calf-backed; URIs and control
  symbols cross-checked against the EasyEffects sources, see the
  per-emitter comments in `ee_to_pipewire.py`). Stereo only. Not
  translated: non-bypassed `autogain` (EE's implementation is native
  libebur128 — no LV2 equivalent exposes EBU R 128 metering, so the
  converter warns and skips); 4-channel upmix (chain is stereo-only,
  gated on a real-world report).
  By default the converter copies the `.irs` next to the generated
  conf and rewrites the convolver `filename`, so the PW chain has no
  runtime dependency on the EasyEffects directory layout
  (`--no-copy-irs` reverts to the v1 cross-tree reference). The conf
  is emitted as a WirePlumber 0.5+ smart filter pinned to the
  auto-detected internal-speaker sink (`filter.smart` +
  `filter.smart.target` + `node.link-group` + `priority.session=-1`),
  so the speaker remains the default sink, apps target it as usual,
  the chain inserts itself transparently into the path, HDMI / BT /
  USB outputs bypass automatically, and there's no second volume
  layer. `--target-sink <node.name>` overrides the auto-detected
  sink; `--target-sink ''` reverts to a v1 virtual-sink conf that
  apps target directly.
  Equivalence to the EE chain is verified by `tools/measure_pw/`
  (frequency-domain ≤0.5 dB, time-domain ≥30 dB S/R) and by the
  `tools/measure_pw/validate_conf.py` deterministic schema check
  (lv2info-driven), which `ee_to_pipewire.py` runs automatically
  unless `--no-validate` is passed. Corpus tier:
  `tests/corpus/test_ee_to_pipewire_corpus.py` exercises the full
  XML→conf pipeline against every discovered DAX3 XML. The
  follow-up tracker is `localresearch/ee_to_pipewire_followups.md`.

Everything else — plugin chain rationale, gain-staging, unit
conversions, cross-device findings — lives in `docs/` and is linked
from the README's "Further reading" section.
