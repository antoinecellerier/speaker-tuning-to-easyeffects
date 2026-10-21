# Sonitude speaker tunings

[README](../README.md) · [All docs](README.md)

Some laptops ship no Dolby file. Their Windows speaker tuning is a preset for
the Sonitude audio processing object (APO), formerly Sound Research, which
comes in the laptop's audio driver. The converter builds its presets from that
preset instead. This is experimental: one laptop has been heard. On some
laptops the OEM's audio app calls this tuning by a speaker brand's name, such
as HP's Bang & Olufsen Audio Control.

## Running it

The README's [Quick start](../README.md#quick-start) works unchanged: the same
commands find a Sonitude tuning on a mounted Windows partition or in the audio
driver download. The run says `Found Sonitude speaker tuning` when it reads
one. A Dolby file that matches the device takes precedence over it.

The run builds `Sonitude-Music`, from the tuning's Music sound mode.
`--all-profiles` also builds `Sonitude-Movie` and `Sonitude-Voice`, and
`--profile movie` builds one of them alone. `--list` names the sound modes the
tuning holds. Each preset is scaled to its own peak, not to Windows' loudness
between sound modes.

The tuning has no Dolby stages, so the run refuses the flags that act on one:
every `--disable` name, `--enable autogain`, `dialog` and `virtual-bass`, and
`--volmax-slot`. It refuses `--endpoint`, `--mode` and `--best-guess` too,
because they pick within a Dolby file. It refuses `--enable level-restore`,
because the preset's filter loses next to no level to give back.

On the [PipeWire filter-chain](dolby-to-pipewire.md) route, the presets
install as `Sonitude_Music` and so on. The tuning has no voicings, so that
route refuses `--variant`.

## What the presets hold

Each preset is a convolver, which carries the tuning's EQ curve, then the
limiter. The EQ keeps the curve's cuts and leaves out its boosts. Where the
tuning switches on its high-pass, the preset has it too. The tuning's dynamics
and its psychoacoustic bass are not built. The run lists each stage of the
tuning and says whether the preset builds it.
[reference.md](reference.md#sonitude-input) has each mapping and how far it
has been checked against Windows.

## Status

Experimental. One listener has compared the Music preset with Windows, on one
laptop. They found it close to Windows, with less dense bass, and quieter at the
same volume setting. Nobody has listened to Movie or Voice. They are built the
same way as Music.

If the preset plays quieter than Windows, allow volume above 100% in your
desktop environment, as
[Troubleshooting](troubleshooting.md#troubleshooting-correct-but-too-quiet)
describes. The flags that page suggests act on Dolby stages, so the run refuses
them here.

## Reporting how it sounds

Open a
[device report](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/new?template=device-report.yml),
say which sound mode you compared with Windows, and give the link to your
laptop's audio driver download. The laptop heard so far is in
[#140](https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/issues/140).
