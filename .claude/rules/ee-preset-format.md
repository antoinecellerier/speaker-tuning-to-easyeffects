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
accepts the wrong value, loads the preset, and silently does nothing — so
nothing fails, and the only symptom is that the audio is untreated.

- **Enum parameters are string labels, not integer indices.** `"type":
  "Bell"`, `"mode": "RLC (BT)"`, `"compression-mode": "Downward"` — never the
  LSP integer behind them. Writing the integer produced a preset EE loaded
  with the filter off (fixed in `91423b8`). The integers belong on the
  PipeWire side only, where `lib/pipewire/plugins.py`'s `EE_*` tables convert
  the labels back — which is why that module is in this rule's scope: the two
  sides have to agree on the exact label strings, character for character.
- **Impulse-response files need the `.irs` extension.** EasyEffects filters
  the convolver's file picker on it and ignores anything else, whatever the
  contents are. The file itself is a stereo WAV. `lib/preset/emit.py` is
  where that name is built and the WAV written, which is why it is in scope.
- **The EE 8.x convolver wants `"kernel-name"` — the filename stem, no
  directory, no extension** — not the deprecated `"kernel-path"` EE 7 used.
  A preset carrying `kernel-path` loads with no kernel and no complaint;
  `lib/report/environment.py` has a `--doctor` check for exactly that, which
  is only correct while it and `make_convolver` agree on the literal.

When adding a plugin block, copy the key names and value *types* from a
preset EasyEffects itself wrote, not from the LV2 port list. The two differ
in units (dB vs linear), in naming, and in exactly the enum case above.
