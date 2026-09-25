# Disabling and enabling filters

[README](../README.md) · [All docs](README.md)

If the generated preset has audible artifacts on your hardware, you can rebuild
it without specific filters rather than hand-editing the chain inside
EasyEffects. Such artifacts include saturation, pumping, harsh highs and
uncomfortable stereo width.

To work out *which* stage you're hearing, switch effects off one at a time in
the EasyEffects window instead of rebuilding. Turning off **Convolver** isolates
the speaker-correction curve from everything dynamic. Turning off the
**Multiband Compressor** that carries the per-band limiter also takes out the
loudness boost riding it. Most tunings produce only one Multiband Compressor,
which is that limiter. Where Dolby's own multi-band compressor is also present,
you get two, and the limiter is the second of them. The run's own output names
which stages it built.

Add `--disable NAME` to the command you ran before,
such as `python3 dolby_to_easyeffects.py --autoload --disable volmax`, and
repeat it as many times as needed:

| Name | What to try if you hear... |
|------|----------------------------|
| `volmax` | Loud parts distort or sound crushed. Drops the static ~+6 dB `volmax-boost` loudness gain. See the note below. |
| `mbc` | A compressed or "squashed" character you don't like. Drops the multi-band dynamics processor, which has 1–4 bands depending on profile. |
| `regulator` | The volume audibly wobbles or surges on its own. Drops the per-band limiter. If `volmax` is enabled, it falls back to the brickwall limiter's input-gain. |
| `coupled-bands` | The loudest moments feel clamped or lose impact. Drops the zones the tuning leaves at full scale but marks non-isolated. **Not yet validated by ear**: see the note below. |
| `autogain` | Loudness pumping tied to the content: quiet passages swell, then duck when things get loud. Drops the volume leveler, which runs by default only on SoundWire speakers. |
| `bass-enhancer` | Bass sounds artificial or distorted on SoundWire devices. Only emitted for SoundWire speakers. |
| `dialog` | Vocals feel over-boosted or harsh in the presence region. Drops the 2.5 kHz speech-band EQ. |
| `high-shelf` | Harsh or sibilant high frequencies on devices whose tuning includes a type-3 shelf. **Experimental** path: see the note below. |
| `lo-pass` | Highs sound rolled off or dull on devices whose tuning includes a type-6/8 low-pass. **Experimental**, with the same caveat as the `high-shelf` note below. |

- **`volmax`.** The default `--volmax-slot input-gain` already handles
  distortion on loud **low** frequencies. If the preset instead sounds
  bass-light, with more bass when you switch it off, try
  `--volmax-slot output-gain`, confirmed by ear on one device
  ([#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44)).
- **`coupled-bands`.** The engaged path has been neither DAX-captured nor heard
  ([docs/reference.md](reference.md)). The per-band limiter covers these
  zones by default.
- **`autogain`.** `--disable autogain` mirrors `--enable autogain` below.
- **`high-shelf`.** The reproduction of the Dolby tuning is numerically
  verified, but not yet audibly validated. Feedback welcome.
  [docs/cross-device-findings.md](cross-device-findings.md#type-3--high-shelf-filter-experimental)
  lists where the type-3 shelf appears.
- **`lo-pass`.**
  [docs/cross-device-findings.md](cross-device-findings.md#types-6-and-8--low-pass-variants-experimental)
  lists where the type-6/8 low-pass appears.

Some filters ship in the preset but inactive. `--enable NAME` switches them on:

| Name | What to try if you hear... |
|------|----------------------------|
| `autogain` | The preset sounds right but noticeably quieter than Windows. Turns on the volume leveler. See [Troubleshooting: correct but too quiet](troubleshooting.md#troubleshooting-correct-but-too-quiet). |
| `level-restore` | The preset is quieter than with it switched off entirely, and thin with it. **Experimental**: see the note below. |
| `virtual-bass` | Bass feels thinner than it did on Windows, on HDA internal speakers. You need `dolby_to_pipewire.py` to hear it. **Experimental**: measured close to DAX on one device. |

- **`level-restore`.** The impulse response is normalised so its loudest band
  sits at 0 dB, which drops everything else below unity. On tunings whose peak
  exceeds their `volmax-boost`, the result plays below bypass. This flag hands
  that level back. Experimental: it also feeds the peak into the final limiter.
  On the one device that has listened, loud speech picked up audible artifacts.
  If yours does too, try `--disable volmax`. Report either way on
  [#50](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/50).
- **`virtual-bass`.** Windows DAX synthesizes harmonics that suggest bass small
  drivers can't physically produce. This flag records the XML's virtual-bass
  parameters so the
  [PipeWire converter](dolby-to-pipewire.md) can build
  that stage. EasyEffects itself can't express it, so the EE preset's audio is
  unchanged. Report what you hear on
  [#14](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/14).

Convolver, PEQ, and the final brickwall limiter can't be toggled from the CLI.
They're the FIR correction, speaker PEQ, and safety net.
