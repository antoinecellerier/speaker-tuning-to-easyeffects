#!/usr/bin/env python3
"""Zero-touch fetch of a Lenovo laptop's Dolby DAX3 tuning XML.

The converters (dolby_to_easyeffects.py / dolby_to_pipewire.py) need the DAX3
tuning XML that ships inside the Windows audio driver. On a Linux-only machine
that means hand-downloading the OEM driver EXE and running innoextract. This
script does that step and stops there: it reads the machine type and audio
codec IDs from sysfs, resolves the matching audio-driver package from Lenovo's
public update catalog, downloads and checksum-verifies the EXE, extracts the
Dolby tuning XMLs into ./driver-cache/, and prints the directory to hand to
whichever converter you want to run.

Lenovo only, for now. Other vendors: see the README "Extracting the XML".

  python3 tools/fetch_driver/get_lenovo_dax_xml.py            # fetch + unpack
  python3 tools/fetch_driver/get_lenovo_dax_xml.py --dry-run  # show the plan, touch nothing
"""

from __future__ import annotations

import argparse
import hashlib
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from lib import packages  # noqa: E402
from lib.dax import discover  # noqa: E402
from lib.hardware import codecs  # noqa: E402

CATALOG_URL = "https://download.lenovo.com/catalog/{mt}_Win{win}.xml"
UA = "Mozilla/5.0 (X11; Linux x86_64) get_lenovo_dax_xml/1"
DOLBY_APO_HWID = "VEN_DOLBY&PID_DAX3"
# Package-name prefixes that are dock / USB audio, not the internal codec.
_NOT_INTERNAL = ("DOCK_", "AUDIO_U3AUD")

# innoextract's package name per family (no row in lib/packages.py — it is not
# a dependency of the converters, only of this helper). The install *verb* is
# not ours to keep: `packages.install_verb` owns it, one table for every
# caller.
_INNOEXTRACT = {
    packages.DEBIAN: "innoextract", packages.FEDORA: "innoextract",
    packages.SUSE: "innoextract", packages.ARCH: "innoextract",
    packages.ALPINE: "innoextract", packages.GENTOO: "app-arch/innoextract",
}


class Fail(RuntimeError):
    """A user-facing failure; main() prints it and exits 1."""


# --- machine identity -------------------------------------------------------

def _dmi(field: str) -> str:
    try:
        return Path("/sys/class/dmi/id", field).read_text().strip()
    except OSError:
        return ""


def machine_type() -> str:
    """The 4-char Lenovo machine type (e.g. ``20XL``), or "" if not Lenovo."""
    if "LENOVO" not in _dmi("sys_vendor").upper():
        return ""
    sku = _dmi("product_sku")
    m = re.search(r"MT_([0-9A-Z]{4})", sku)
    if m:
        return m.group(1)
    # `product_name` is either an MT-prefixed code (`20XLS23200`) or a
    # marketing name. Slicing four characters off the latter yields `THIN` /
    # `IDEA`, which look like machine types and 404 the catalog, so match the
    # whole string instead.
    m = re.fullmatch(r"([0-9A-Z]{4})[A-Z0-9]{3,}", _dmi("product_name").upper())
    return m.group(1) if m else ""


def codec_tokens() -> list[tuple[str, str, str, str]]:
    """``(ven, dev, subsys, name)`` per HDA codec.

    ``ven``/``dev`` are the 4-hex halves of the HDA vendor id (10EC0257 ->
    10EC, 0257); together they form the ``VEN_10EC&DEV_0257`` HardwareID a
    driver descriptor advertises, and ``dev``/``subsys`` the filename tokens.
    """
    out = []
    for vendor_id, subsys_id, name in codecs.get_hda_codec_ids():
        # Keyed on the codec name, the way `_card_pci_preference` does it: an
        # SSID test catches the AMD spelling (`00AA0100`) but not Intel's
        # (`80860101`), and a display codec has no Dolby tuning either way.
        if "HDMI" in name.upper():
            continue
        out.append((vendor_id[:4].upper(), vendor_id[-4:].upper(),
                    subsys_id.upper(), name))
    return out


# --- Lenovo catalog --------------------------------------------------------

def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        raise Fail(f"{url} -> HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError) as e:
        raise Fail(f"{url} -> {e}")


def catalog_packages(mt: str, wins: list[str]) -> list[str]:
    """Descriptor-XML URLs for every package in the machine-type catalog(s)."""
    seen: dict[str, None] = {}
    errors = []
    for win in wins:
        url = CATALOG_URL.format(mt=mt, win=win)
        try:
            root = ET.fromstring(_get(url))
        except Fail as e:
            errors.append(str(e))
            continue
        for pkg in root.findall("package"):
            loc = pkg.findtext("location", "").strip()
            cat = pkg.findtext("category", "").strip().lower()
            if not loc:
                continue
            if cat == "audio":
                seen.setdefault(loc, None)
    if not seen and errors:
        raise Fail("no catalog reachable for machine type "
                   f"{mt}:\n  " + "\n  ".join(errors))
    return list(seen)


def _version_key(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in re.findall(r"\d+", v)[:4]) or (0,)


class Descriptor:
    def __init__(self, url: str, xml: bytes):
        self.url = url
        root = ET.fromstring(xml)
        pkg = root if root.tag == "Package" else root.find(".//Package")
        self.name = (pkg.get("name") if pkg is not None else "") or ""
        self.version = (pkg.get("version") if pkg is not None else "") or ""
        self.hwids = " ".join(e.text or "" for e in root.iter("HardwareID"))
        self.exe_name = ""
        self.sha256 = ""
        for f in root.iter("File"):
            nm = (f.findtext("Name") or "").strip()
            if nm.lower().endswith(".exe"):
                self.exe_name = nm
                # `<CRC>` is a SHA-256 on every descriptor we've read, but the
                # tag name doesn't promise one. Anything that isn't 64 hex is
                # some other digest: skip verification rather than fail every
                # run of this machine type on a mismatch we'd blame on the file.
                crc = (f.findtext("CRC") or "").strip().lower()
                self.sha256 = crc if re.fullmatch(r"[0-9a-f]{64}", crc) else ""
                break

    @property
    def is_internal_audio(self) -> bool:
        return not self.name.upper().startswith(_NOT_INTERNAL)

    @property
    def has_dolby_apo(self) -> bool:
        return DOLBY_APO_HWID in self.hwids.upper()

    def serves_codec(self, tokens: list[tuple[str, str, str, str]]) -> bool:
        """True if a HardwareID names one of this machine's HDA codecs.

        The descriptor lists the ``VEN_xxxx&DEV_xxxx`` it installs for, so a
        match on the codec read from ``/proc/asound`` is a far stronger signal
        than the package name or the Dolby-APO hwid — and it drops display
        packages (the Dolby *Vision* Provisioning Kit) that carry neither.
        """
        hw = self.hwids.upper()
        return any(f"VEN_{ven}&DEV_{dev}" in hw for ven, dev, _s, _n in tokens)

    @property
    def exe_url(self) -> str:
        return self.url.rsplit("/", 1)[0] + "/" + self.exe_name


def pick_descriptor(urls: list[str],
                    tokens: list[tuple[str, str, str, str]]) -> Descriptor:
    cands = []
    # Why each rejected URL was rejected. Without this a run where every
    # descriptor 403s reports "none look like an internal-codec driver" — a
    # true statement about an empty list, and the wrong thing to go fix.
    skipped = []
    for u in urls:
        try:
            d = Descriptor(u, _get(u))
        except Fail as e:
            skipped.append(str(e))
            continue
        except ET.ParseError as e:
            skipped.append(f"{u} -> not XML ({e})")
            continue
        if d.exe_name and d.is_internal_audio:
            cands.append(d)
    if not cands:
        if not urls:
            raise Fail("this machine type's catalog lists no audio package; "
                       "pass --exe-url with the driver EXE")
        detail = ("\n  " + "\n  ".join(skipped)) if skipped else ""
        raise Fail("found audio packages in the catalog but none look like an "
                   f"internal-codec driver; pass --exe-url with the driver EXE{detail}")
    # Primary key: the descriptor advertises this machine's codec. Only narrow
    # to it when at least one does — a --machine-type override on another box,
    # or a codec sysfs can't read, leaves every candidate in the running.
    serving = [d for d in cands if d.serves_codec(tokens)]
    pool = serving or cands
    # Tiebreak between real audio drivers: the Dolby DAX3 APO, then version.
    return max(pool, key=lambda d: (d.has_dolby_apo, _version_key(d.version)))


# --- download + extract ---------------------------------------------------

def download(url: str, dest: Path, expect_sha: str) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha256()
    if dest.exists() and expect_sha:
        h_existing = hashlib.sha256(dest.read_bytes()).hexdigest()
        if h_existing == expect_sha:
            print(f"  cached {dest.name} (sha256 ok)")
            return dest
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    print(f"  downloading {url}")
    tty = sys.stderr.isatty()
    # Same conversion `_get` does: a 404 on a hand-passed --exe-url, or a
    # connection dropped mid-download, is a user-facing failure, not a traceback.
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as fh:
            total = int(r.headers.get("Content-Length", 0))
            done = last_pct = 0
            while chunk := r.read(1 << 16):
                fh.write(chunk)
                h.update(chunk)
                done += len(chunk)
                if total:
                    pct = done * 100 // total
                    if pct != last_pct and (tty or pct % 20 == 0):
                        last_pct = pct
                        end = "\r" if tty else "\n"
                        print(f"  {done / 1e6:5.1f} / {total / 1e6:.1f} MB "
                              f"({pct:3d}%)", end=end, file=sys.stderr)
            if total and tty:
                print(file=sys.stderr)
    except urllib.error.HTTPError as e:
        raise Fail(f"{url} -> HTTP {e.code}")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        dest.unlink(missing_ok=True)
        raise Fail(f"{url} -> {e}")
    got = h.hexdigest()
    if expect_sha and got != expect_sha:
        dest.unlink(missing_ok=True)
        raise Fail(f"checksum mismatch for {dest.name}\n"
                   f"  expected {expect_sha}\n  got      {got}")
    return dest


def extract(exe: Path, cache: Path) -> Path:
    """Unpack *exe* into ``cache/extract`` and return that directory."""
    if not shutil.which("innoextract"):
        fam = packages.family()
        verb = packages.install_verb(fam)
        hint = (f"{verb} {_INNOEXTRACT[fam]}"
                if verb and fam in _INNOEXTRACT else
                "install 'innoextract' from your distribution")
        raise Fail(f"innoextract is required to unpack {exe.name}\n  {hint}")
    out = cache / "extract"
    if out.exists():
        shutil.rmtree(out)
    dolby_filter = "code$GetExtractPath$/Dolby/03_dax_ext"
    base = ["innoextract", "-s", "-d", str(out)]
    subprocess.run(base + ["-I", dolby_filter, str(exe)],
                   check=False, capture_output=True)
    # Judged by the same filter that reads the result: a directory holding only
    # `_settings`/`_dmic` companions has no tuning XML, and retrying unfiltered
    # is exactly what that case needs.
    if not discover.walk_for_dolby_xml_dirs(out):  # filter missed this layout
        shutil.rmtree(out, ignore_errors=True)
        r = subprocess.run(base + [str(exe)], capture_output=True, text=True)
        if r.returncode != 0:
            raise Fail(f"innoextract failed:\n{r.stderr.strip()}")
    return out


def tuning_xml_dir(extract_root: Path) -> Path:
    """The single directory of Dolby tuning XMLs under a fresh extraction.

    Reuses ``lib/dax/discover`` so the "which files count" question is
    answered once, in the same place the converters answer it — and scoped to
    ``extract_root`` so a hand-run ``innoextract -d ./driver-cache`` elsewhere
    in the cache can't add a second copy.
    """
    dirs = discover.walk_for_dolby_xml_dirs(extract_root)
    if not dirs:
        raise Fail(f"unpacked {extract_root} but found no Dolby DAX3 tuning "
                   "XML in it. Look inside and pass a file to the converter "
                   "directly.")
    if len(dirs) > 1:
        # Some installers (Legion Y540-15IRH, say) fan out into a directory
        # per SKU. Picking the first would be picking another laptop's tuning,
        # so narrow to the one holding an XML for this machine.
        narrowed = discover.dirs_for_this_machine(dirs)
        if len(narrowed) == 1:
            return narrowed[0]
        dirs = narrowed or dirs
    if len(dirs) > 1:
        raise Fail("Dolby tuning XMLs landed in more than one directory:\n  "
                   + "\n  ".join(str(d) for d in dirs)
                   + "\npass one to the converter's --windows yourself.")
    return dirs[0]


# --- driver ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="get_lenovo_dax_xml.py",
        description=__doc__.split("\n\n")[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--windows-version", choices=("11", "10", "both"),
                   default="both", help="which Lenovo Win catalog to read "
                   "(default: both, Win11 first)")
    p.add_argument("--exe-url", help="skip catalog resolution; download this "
                   "driver EXE directly")
    p.add_argument("--machine-type", help="override the detected 4-char "
                   "Lenovo machine type")
    p.add_argument("--driver-cache", type=Path, default=ROOT / "driver-cache",
                   help="working directory for the EXE and extracted XMLs "
                   "(default: ./driver-cache, gitignored)")
    p.add_argument("--keep-exe", action="store_true",
                   help="don't delete the driver EXE after extraction")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve and print the plan; download/extract nothing")
    return p


def _print_next_steps(xml_dir: Path) -> None:
    rel = xml_dir
    try:
        rel = xml_dir.relative_to(Path.cwd())
    except ValueError:
        pass
    # innoextract's layout has a `$` in it (`code$GetExtractPath$`), so the
    # path has to be quoted to survive a paste into a shell.
    q = shlex.quote(str(rel))
    print(f"\nDolby tuning XMLs are in:\n  {rel}\n")
    print("build a preset from that directory with either converter:")
    print(f"  python3 dolby_to_easyeffects.py --windows {q}")
    print(f"  python3 dolby_to_pipewire.py --windows {q}")


def run(args: argparse.Namespace) -> int:
    cache = args.driver_cache
    tokens = codec_tokens()
    if tokens:
        print("audio codec: " + ", ".join(
            f"{name} (DEV_{dev} SUBSYS_{sub})" for _v, dev, sub, name in tokens))

    if args.exe_url:
        exe_url, sha, pkg_name = args.exe_url, "", "(from --exe-url)"
    else:
        mt = args.machine_type or machine_type()
        if not mt:
            raise Fail("this does not look like a Lenovo machine "
                       "(/sys/class/dmi/id/sys_vendor). Only Lenovo is "
                       "automated — see the README 'Extracting the XML' "
                       "for a manual route.")
        wins = {"11": ["11"], "10": ["10"], "both": ["11", "10"]}[
            args.windows_version]
        print(f"machine type: {mt}  (catalog: Win{', Win'.join(wins)})")
        desc = pick_descriptor(catalog_packages(mt, wins), tokens)
        exe_url, sha, pkg_name = desc.exe_url, desc.sha256, desc.name
        print(f"driver package: {pkg_name} v{desc.version}"
              + ("  [Dolby DAX3 APO]" if desc.has_dolby_apo else ""))

    # Not `rsplit("/")`: a --exe-url ending in "/" would name the cache dir
    # itself, and a query string would end up in the filename.
    exe_name = Path(urllib.parse.urlparse(exe_url).path).name
    if not exe_name:
        raise Fail(f"no filename in {exe_url}")
    exe = cache / exe_name
    print(f"driver EXE: {exe_url}")
    if sha:
        print(f"sha256: {sha}")

    if args.dry_run:
        print("\n-- dry run, nothing written --")
        print("would extract Dolby tuning XMLs into:", cache / "extract")
        return 0

    download(exe_url, exe, sha)
    xml_dir = tuning_xml_dir(extract(exe, cache))
    if not args.keep_exe:
        exe.unlink(missing_ok=True)

    xmls = discover.xmls_directly_under(xml_dir)
    print(f"\nextracted {len(xmls)} Dolby tuning XML(s):")
    for p in xmls:
        print("  ", p.name)
    _print_next_steps(xml_dir)
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(argv)
    try:
        return run(args)
    except Fail as e:
        print(f"\nerror: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
