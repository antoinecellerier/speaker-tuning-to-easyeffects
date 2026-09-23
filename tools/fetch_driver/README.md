# tools/fetch_driver — get the Dolby tuning XML without Windows

The converters need the Dolby DAX3 tuning XML that ships inside the Windows
audio driver. On a Lenovo laptop with no Windows partition,
`get_lenovo_dax_xml.py` fetches and unpacks it, then prints the directory to
hand to whichever converter you run.

This directory is a staging area, not a permanent home. The fetch step is
meant to move *inside* the converters, as a "no XML found, fetch it?" prompt
in `dolby_to_easyeffects.py`. So the script deliberately stops at the XML
rather than driving a converter itself.

## get_lenovo_dax_xml.py

```bash
python3 tools/fetch_driver/get_lenovo_dax_xml.py --dry-run   # resolve only, touch nothing
python3 tools/fetch_driver/get_lenovo_dax_xml.py             # fetch + verify + unpack
```

What it does:

1. Reads the Lenovo machine type and HDA codec IDs from sysfs / `/proc/asound`.
2. Downloads the machine-type catalog from `download.lenovo.com`. It picks the
   audio package whose descriptor advertises this machine's codec as a
   HardwareID, such as `VEN_10EC&DEV_xxxx`. Ties go to the Dolby DAX3 APO,
   then to the highest version.
3. Downloads the driver EXE into the repo's gitignored `driver-cache/` and
   verifies its SHA-256 against the catalog descriptor. `--driver-cache DIR`
   uses another cache directory.
4. Runs `innoextract` to pull out the `DEV_*_SUBSYS_*.xml` tuning files. It
   then prints that directory and the converter command to run next: a bare
   `python3 dolby_to_easyeffects.py` where the autoprobe would find the
   extraction on its own, `--windows DIR` where it wouldn't.

Prerequisites: `innoextract`. If it's missing, the script names the package
for your distro. The repo README lists the converters' own dependencies, such
as `lsp-plugins-lv2`. You only need them once you run a converter.

Flags: `--windows-version {11,10,both}`, `--exe-url URL` to skip catalog
resolution, `--machine-type MT`, `--driver-cache DIR`, `--keep-exe`,
`--dry-run`.

Other vendors aren't automated: see the repo README "Extracting the XML".
