# Troubleshooting

## Troubleshooting: a preset that sounds like nothing

If you loaded a generated preset in EasyEffects and hear no difference from
bypass, check the EasyEffects setup before suspecting the preset. Each of the
following makes a correct preset inaudible. Run:

```
python3 dolby_to_easyeffects.py --doctor
```

It checks the common causes and prints a pasteable report:

- **EasyEffects 7.** Version 8 changed the preset format. On EasyEffects 7 the
  speaker-correction filter loads nothing, so the preset is effectively
  bypassed. This repo targets EasyEffects 8.x. If your distro ships 7, install
  the [Flatpak](https://flathub.org/apps/com.github.wwmm.easyeffects), as the
  note at the top of the [README](../README.md#quick-start) says.
- **Wrong install location.** The presets were written to the Flatpak path while
  you run the native package, or vice versa. EasyEffects never sees them.
- **A missing impulse file.** The convolver references a `.irs` that isn't in
  the irs directory, so the speaker correction is silent.
- **No Dolby preset selected**, or EasyEffects' global bypass is on. Its switch
  is the highlighted top-left toggle below. If your output is a headset, HDMI or
  Bluetooth, having `Nothing` selected is the expected
  [bypass fallback](dolby-to-easyeffects.md#autoload), not a fault. `--doctor`
  says so, and reports which preset your speakers autoload instead.
- **EasyEffects not running in the background.** The preset only processes audio
  while EasyEffects is active, so it can vanish after you close the window or
  reboot. In EasyEffects → Preferences → Background Service, shown below, enable
  *Enable service mode* and *Autostart on login*. `--doctor` reports whether
  both are set.

![EasyEffects' global on/off toggle, highlighted at top left. If it's off, every preset is bypassed.](images/ee-global-bypass.jpg)

![EasyEffects Background Service preferences. Enable service mode and autostart on login so the preset keeps applying after you close the window or reboot.](images/ee-background-service.jpg)

A normal generation run also warns at the end if it detects an EasyEffects
version that can't use the presets it just wrote. To check your version
directly, open EasyEffects' About dialog:

![Checking the EasyEffects version](images/ee-version.jpg)

Everything above is about the EasyEffects setup. On the
[PipeWire filter-chain](dolby-to-pipewire.md) route,
run `python3 dolby_to_pipewire.py --doctor` instead. It checks that route's own
failures: chains stacked on one sink, a conf that didn't load, a missing impulse
file, and a target sink that's gone.

## Troubleshooting: correct but too quiet

If the preset sounds right but quieter than Windows, part of the gap is
expected. On HDA speakers, Dolby's dynamic volume leveler ships bypassed,
because without Dolby's content analysis it distorts on quiet→loud transitions
([why](research/adaptive-processing.md#why-autogain-is-bypassed-by-default)).
Try these in order:

- **Re-run the script with `--enable autogain`.** On HDA speakers, the volume
  leveler ships bypassed by default and carries most of the loudness gap: ~+9 dB
  measured on program material. The trade-off: without Dolby's content analysis,
  it can audibly saturate when loud sound arrives over a quiet background. That
  is why it isn't the default. If you hear that, drop the flag again. For still
  more loudness, raise the Autogain *Target* a few dB in the EasyEffects GUI, at
  increased saturation risk. If you instead enable Autogain by hand on a preset
  generated before this option existed, also raise its *Silence threshold* to
  about −50 dB. Otherwise, sounds arriving after silence will crackle.
- **If it is quieter than with the preset switched off entirely**, not just
  quieter than Windows, re-run with `--enable level-restore`. The flag hands
  back the level the preset loses below bypass. **Experimental**: the restored
  level also drives the final limiter harder. On the one device that has
  listened, loud speech picked up audible artifacts. If yours does too, add
  `--disable volmax` or drop the flag. Report either way on
  [#50](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/50).
  The script's end-of-run menu offers this flag only on tunings where it would
  change the level. The `level-restore` note in
  [Disabling and enabling filters](filters.md) explains
  why the preset can play below bypass.
- **Allow volume above 100%** in your desktop environment. GNOME:
  `gsettings set org.gnome.desktop.sound allow-volume-above-100-percent true`.
  KDE Plasma: volume applet settings → *Raise maximum volume*. Any environment:
  `wpctl set-volume @DEFAULT_AUDIO_SINK@ 1.25`, or pavucontrol.
  Over-amplification is digital gain applied after the preset's limiter, so
  extreme values can clip.
- **Check mixer levels.** In `alsamixer`, set Master/PCM/Speaker to 100%.
- **On the PipeWire path, check which output is selected.** If your sound
  settings show the filter chain itself, `Dolby-… (speaker filter)`, instead of
  your speakers, both volumes apply. Pick your speakers, since the chain is
  inserted into them automatically. `python3 dolby_to_pipewire.py --doctor`
  reports which is selected.
- On a device whose regulator is aggressive, `--volmax-slot output-gain` can
  bring back bass and a little loudness that the per-band limiter takes away.
  This was confirmed by ear on one device
  ([#44](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/44)).
  See [Disabling and enabling filters](filters.md).
- **Speakers thin and quiet even with EasyEffects off.** Run `--speaker-info`.
  If it flags an amplifier-firmware error, your distro lacks this machine's
  speaker firmware, which no preset can fix
  ([background](cross-device-findings.md)).
  [Issue #27](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/27)
  links a worked, device-specific example of extracting the firmware from the
  Windows driver.
