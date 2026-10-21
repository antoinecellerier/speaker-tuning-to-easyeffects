# Extracting the XML

[README](../README.md) · [All docs](README.md)

This page covers the Dolby DAX3 tuning XML.
[Getting your tuning file](getting-the-tuning-file.md) says which other tunings
the converter reads.

With a Windows partition mounted, the easiest way is to run the script with no
path, as in the README's
[Quick start](../README.md#quick-start). It finds the XML on a mounted Windows
partition, or in a driver package extracted in the current directory. The script
reads your audio codec's device and subsystem IDs from `/proc/asound` and
matches them against the XMLs in the DriverStore. `--windows DIR` points it at a
directory it doesn't find on its own, or picks one when it finds several, as
[How the script finds your XML and codec](#how-the-script-finds-your-xml-and-codec)
describes.

**No Windows partition, on a Lenovo laptop?** Run
`tools/fetch_driver/get_lenovo_dax_xml.py`. It resolves the audio-driver package
for your machine type from Lenovo's update catalog, downloads and
checksum-verifies it, and extracts the DAX3 tuning XML. It needs
[`innoextract`](https://constexpr.org/innoextract/install). If it's missing,
the fetcher says how to install it on your distribution:

```bash
python3 tools/fetch_driver/get_lenovo_dax_xml.py --dry-run   # show what it resolved
python3 tools/fetch_driver/get_lenovo_dax_xml.py             # fetch, verify, unpack
```

It then prints the command to run next. From the repo root that is usually just
`python3 dolby_to_easyeffects.py`, with no path: the fetcher unpacks into the
repo's `driver-cache/`, which the run searches by itself. When it wouldn't find
it there, the printed command has `--windows` filled in. Add `--autoload`, as
in the README's [Quick start](../README.md#quick-start), to have EasyEffects
apply the preset automatically.

## From a vendor driver download

With no Windows partition, take the tuning from your model's audio driver,
downloaded from the vendor's support site. On a Lenovo, the fetcher above finds
and downloads it for you.

1. Download your model's audio driver. Not every audio download carries the
   tuning. On ASUS's support site, take the one titled *Dolby Atmos driver*,
   listed under Software and Utility or under Audio. Its *Realtek Audio
   Driver* and SmartAMP downloads carry none.
2. Install the tool its format needs. If one is missing, the run names it, and
   says how to install it where your distribution packages it.

   | Download | Seen from | Needs |
   |---|---|---|
   | `.exe` installer | Lenovo | [`innoextract`](https://constexpr.org/innoextract/install) |
   | `.exe` 7-Zip archive | Framework | 7-Zip |
   | `.exe` with an archive inside | ASUS | 7-Zip |
   | `.msi` | Microsoft Surface | msitools |
   | `.zip` | Samsung | nothing |

3. From the project root, pass the download in place of the XML:

   ```bash
   python3 dolby_to_easyeffects.py ~/Downloads/FILE.exe
   ```

The run unpacks only the tuning files, into a folder of its own under
`driver-cache/`, then picks your laptop's tuning there. On a Surface whose
download carries Microsoft's own speaker tuning, in a folder beside the Dolby
one, it keeps and applies that too. The folder's `PROVENANCE.txt` names the
download and lists the files kept. A later run with the same download reuses
the folder.

If the download holds no tuning file, the run says so and writes nothing. If it
reports several matching folders, run the same command again with the
`--windows` line it lists in place of the download.
[corpus.md](corpus.md#publicly-downloadable-driver-packages) lists the vendor
downloads this project's tunings come from.

To unpack a download yourself, for example on another machine, unpack all of it
into a folder under the project, then run the converter with no path:

```bash
innoextract -d ./driver-cache/MODEL FILE.exe                # Lenovo
7z x -o./driver-cache/MODEL FILE.exe                         # Framework
7z x -tPE -o./driver-cache/MODEL FILE.exe .rsrc/ZIP/103      # ASUS, then
7z x -o./driver-cache/MODEL ./driver-cache/MODEL/.rsrc/ZIP/103
msiextract -C ./driver-cache/MODEL FILE.msi                  # Surface
unzip -d ./driver-cache/MODEL FILE.zip                       # Samsung
```

Keep the whole unpacked tree rather than copying an XML out of it, so a
Surface's speaker tuning stays beside the Dolby folder.

## Picking the XML by hand

The run finds the XML by itself in the cases above. Pick it yourself only to
pass one file directly. On a Windows partition it is at:
```
C:\Windows\System32\DriverStore\FileRepository\dax3_ext_*.inf_*\DEV_*_SUBSYS_*.xml
```
Match **both** parts of the filename to your codec:

- `DEV_` to its device id, the last four hex digits of `Vendor Id`: `0x10ec0287`
  → `DEV_0287`.
- `SUBSYS_` to its subsystem ID.

`python3 dolby_to_easyeffects.py --speaker-info` shows both IDs for each codec,
as `Vendor:` and `Codec subsystem:`. Before the Python dependencies are
installed, `cat /proc/asound/card*/codec* | grep -E 'Vendor|Subsystem'` shows
them too. The subsystem alone is not enough, because Lenovo reuses subsystem IDs
across different codecs. Picking the other codec's tuning sounds clearly wrong.
See the [details](cross-device-findings.md). You don't need the `_settings.xml`
companion file, which contains UI/profile defaults.

## How the script finds your XML and codec

**Windows partition or extracted DriverStore.** Omitting `--windows` and the
positional XML triggers the autoprobe. It enumerates the NTFS-family mountpoints
in `/proc/mounts`: `ntfs`, `ntfs3` and `fuseblk`. It keeps any whose DriverStore
contains `dax3_ext_*.inf_*` subdirs, whether a full system root like
`/mnt/windows/Windows` or a drive-root mount like `/mnt/c`.

If nothing mounted matches, it falls back to a bounded walk of the current
directory. It looks for any directory whose files include a Dolby-shaped XML:
`DEV_*_SUBSYS_*.xml`, `SOUNDWIRE_*_SUBSYS_*.xml` or `SDW_*_SUBSYS_*.xml`,
excluding `_settings.xml` companions. That covers hand-organised collections and
the raw `innoextract` layout,
`./driver-cache/code$GetExtractPath$/Dolby/03_dax_ext/`. Neither needs a
`dax3_ext_*.inf_*` rename. The walk skips hidden directories, doesn't follow
symlinks, and is depth-capped.

A single unambiguous match is used. When several match, the autoprobe narrows to
those containing an XML for your detected audio hardware. It uses that one if
exactly one survives. Otherwise it errors with the shortlist, so you can pick
one via `--windows DIR`.

**SoundWire codecs on newer Intel platforms.** Auto-detection also handles
SoundWire-based audio on Lunar Lake / Panther Lake and later, Meteor Lake, and
some Tiger/Alder Lake SKUs. That includes Qualcomm Aqstic and Cirrus cs35l56
smart-amp platforms. The script reads device IDs from
`/sys/bus/soundwire/devices/` and the PCI subsystem ID of the HD Audio
controller from `/sys/class/sound/card*/device`. It matches them against Dolby
filenames of the form
`SOUNDWIRE_MAN_<man>_FUNC_<func>_SUBSYS_<device><vendor>.xml`, for example
`SOUNDWIRE_MAN_025D_FUNC_1318_SUBSYS_233917AA.xml`.

The PCI subsystem is the per-device key. On Cirrus platforms the `FUNC` token is
a device id that needn't equal the Linux SoundWire part id, as
[cross-device-findings](cross-device-findings.md) shows. So if no part
matches, the script falls back to the PCI subsystem + manufacturer.

`--windows` accepts any of these:

- a full Windows system root, such as `/mnt/windows/Windows`
- a drive-root mount, such as `/mnt/c`, where the script looks for a
  case-insensitive `Windows/` child
- an already-extracted DriverStore directory containing `dax3_ext_*.inf_*`
  subfolders directly
