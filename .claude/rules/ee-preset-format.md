---
paths:
  - "lib/preset/bands.py"
  - "lib/preset/build.py"
  - "lib/preset/emit.py"
  - "lib/preset/plugins.py"
  - "lib/pipewire/plugins.py"
  - "lib/report/environment.py"
---

# EasyEffects preset-format traps

Three of these have shipped as bugs. All three share a shape: EasyEffects
accepts the wrong value, loads the preset and silently does nothing. Nothing
fails, and the only symptom is untreated audio.

- Enum parameters are string labels, not integer indices. Write
  `"type": "Bell"`, `"mode": "RLC (BT)"` and `"compression-mode": "Downward"`,
  never the LSP integer behind them. Writing the integer made EE load the
  preset with the filter off, a bug `91423b8` fixed in our emitter. The
  integers belong on the PipeWire side only, where
  `lib/pipewire/plugins.py`'s `EE_*` tables convert the labels back. That
  module is in this rule's scope because the two sides have to agree on the
  exact label strings, character for character.
- Impulse-response files need the `.irs` extension. EasyEffects filters
  the convolver's file picker on it and ignores anything else, whatever the
  contents. The file itself is a stereo WAV. `lib/preset/emit.py` builds
  that name and writes the WAV, which is why it is in scope.
- The EE 8.x convolver wants `"kernel-name"`, not the deprecated
  `"kernel-path"` EE 7 used. Its value is the filename stem: no directory,
  no extension. A preset carrying `kernel-path` loads with no kernel and no
  complaint. `lib/report/environment.py` has a `--doctor` check for exactly
  that. The check is only correct while it and `make_convolver` agree on the
  literal.

When adding a plugin block, copy the key names and value *types* from a
preset EasyEffects itself wrote, not from the LV2 port list. The two differ
in naming, in units such as dB vs linear, and in exactly the enum case
above.
