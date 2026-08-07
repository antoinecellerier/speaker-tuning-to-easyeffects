---
paths:
  - "lib/preset/fir.py"
---

# The FIR is minimum-phase, and that is the whole latency budget

**Zero added latency over the PipeWire quantum is a hard constraint** — video
lip-sync and interactive use both break at a few milliseconds, and nothing in
the output chain may spend any. `make_fir` is where that is won or lost, which
is why this rule loads here and nowhere else.

The cepstral section is load-bearing, not a flourish. A naive inverse FFT of a
target magnitude gives a **linear-phase** filter: audible pre-ringing ahead of
every transient, and a group delay of half the kernel — ~43 ms at the shipped
4096 taps / 48 kHz, a lip-sync failure on its own. Homomorphic (cepstral)
processing realises the same magnitude response as a minimum-phase kernel
whose energy sits at the front, so the delay is a small fraction of the length.

- **The lever is peak position, not IR length.** Taps buy low-frequency
  resolution and cost no latency *while the filter stays minimum-phase*; it is
  the phase design, not the tap count, that decides the delay. So "the FIR is
  too short for the bass" and "the FIR adds delay" are unrelated problems.
- **Surface the trade-off before proposing** a longer FIR, look-ahead anywhere
  in the chain, or a phase-flat / linear-phase design. Those are user-visible
  latency decisions that get named and agreed, never slipped in behind a
  measurement improvement.
- The constraint reaches past this file, so a change here is not the only way
  to break it: `lib/pipewire/plugins.py` pins the LSP autogain's `lkahead` to
  0.0 for exactly this reason, and the limiter's `lk` is whatever the
  EasyEffects preset already carried rather than a value we chose.

`tests/test_fir_math.py` covers the arithmetic. It cannot tell you a design
went linear-phase deliberately, so that judgement stays a human one.
