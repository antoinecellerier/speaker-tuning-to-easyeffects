# CLAUDE.md

A few things about this repo that aren't obvious from reading the code:

- **There's no automated test suite, but math changes do get verified.**
  For DSP / numerical work (FIR generation, coefficient decoding, gain
  staging, unit conversions, filter design) the pattern is to write a
  throwaway Python script using numpy/scipy that checks the math
  directly — e.g. sweep the generated FIR through an FFT and compare to
  the target frequency response, or decode a Q15 time-constant both ways
  and confirm they match a known formula. Keep these scripts ad-hoc;
  don't commit them and don't graduate them into a formal test harness.
  For preset/structure changes, verification means running the script
  against a real XML, confirming the expected files appear under
  `~/.local/share/easyeffects/`, and loading the preset in EasyEffects.
  If you can't verify locally, say so.

- **Ask the user to confirm audio quality after any change that touches
  the output path.** Local verification (files generated, preset loads,
  math checks out) does not catch audible issues — past sessions shipped
  bugs that only showed up on real listening. Tell the user what to
  listen for based on what the change touched:
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
