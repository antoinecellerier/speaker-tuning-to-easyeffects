# tools/fetch_driver — get the Dolby tuning XML without Windows

The converters need the Dolby DAX3 tuning XML that ships inside the Windows
audio driver. On a Lenovo laptop with no Windows partition,
`get_lenovo_dax_xml.py` does the fetch-and-unpack step and prints the
directory to hand to whichever converter you run.

This is a staging area, not a permanent home: the fetch step is meant to move
*inside* the converters (`dolby_to_easyeffects.py` growing a "no XML found,
fetch it?" prompt), so it deliberately stops at the XML rather than driving a
converter itself.

## get_lenovo_dax_xml.py

```bash
python3 tools/fetch_driver/get_lenovo_dax_xml.py --dry-run   # resolve only, touch nothing
python3 tools/fetch_driver/get_lenovo_dax_xml.py             # fetch + verify + unpack
```

What it does:

1. Reads the Lenovo machine type and HDA codec IDs from sysfs / `/proc/asound`.
2. Downloads the machine-type catalog from `download.lenovo.com` and picks the
   audio package whose descriptor advertises a HardwareID for this machine's
   codec (`VEN_10EC&DEV_xxxx`), breaking ties toward the Dolby DAX3 APO and
   then the highest version.
3. Downloads the driver EXE into `./driver-cache/` and verifies its SHA-256
   against the catalog descriptor.
4. Runs `innoextract` to pull the `DEV_*_SUBSYS_*.xml` tuning files out, then
   prints that directory and the converter command to run next.

Prerequisites: `innoextract` — the script names the package for your distro if
it's missing. The converters' own dependencies (`lsp-plugins-lv2` etc.) are
listed in the repo README; you only need them once you run a converter.

Flags: `--windows-version {11,10,both}`, `--exe-url URL` (skip catalog
resolution), `--machine-type MT`, `--driver-cache DIR`, `--keep-exe`,
`--dry-run`.

Other vendors aren't automated — see the repo README "Extracting the XML".
