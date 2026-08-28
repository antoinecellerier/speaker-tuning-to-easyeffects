#!/usr/bin/env python3
"""Aggregate-statistics sweep over a corpus of Dolby DAX3 tuning XMLs.

Walks one or more directories for Dolby tuning XMLs (``DEV_*``,
``SOUNDWIRE*``, ``SDW*``; ``*_settings.xml`` companions excluded) and prints
the cross-device distributions that back ``docs/cross-device-findings.md`` —
file/profile counts per codec, MBC band-count distribution, IEQ amounts,
volmax-boost spread, regulator slope, PEQ filter-type histogram, and the
"universal constant" invariants the doc claims. Re-run after pulling new
driver packages to see what shifted, then refresh the doc's figures.

Only aggregate statistics are printed; no verbatim tuning arrays are emitted,
so the output is safe to paste into issues or commits.

Corpus discovery (first match wins):
  1. directories passed on the command line
  2. ``ATMOS_CORPUS_DIR`` environment variable
  3. the converter's own probe: every mounted Windows partition's DriverStore
     plus the current directory (walked recursively, hidden directories
     pruned) — the same union ``tests/corpus/`` walks

Point it at a mounted Windows DriverStore, an extracted driver tree, or any
folder of collected XMLs, or let it find them the way the converter does:

    python3 tools/corpus_audit.py
    python3 tools/corpus_audit.py /mnt/c/Windows/System32/DriverStore
    ATMOS_CORPUS_DIR=~/dax3-xmls python3 tools/corpus_audit.py

``--composition`` stops after the makeup block — file, content-unique and
device counts, codecs, driver packages, endpoint/mode/profile coverage. That
is the section ``docs/corpus.md`` tabulates, so it is the one to run against
your own collection to see how it compares.
"""

import argparse
import functools
import hashlib
import os
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.dax.discover import (  # noqa: E402
    autoprobe_all_dolby_xmls,
    is_dolby_tuning_filename as is_dax3_xml,
)


def find_xmls(roots):
    """Every DAX3 XML under the given roots.

    Hidden directories are pruned, as the converter's own walk prunes them:
    a ``.stage/`` left by a review harness held eight tuning copies on
    2026-08-27, and counting them put the makeup block eight files past
    ``docs/corpus.md`` for no real reason.
    """
    xmls = []
    for root in roots:
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if not d.startswith(".")]
            for fn in fns:
                if is_dax3_xml(fn):
                    xmls.append(os.path.join(dp, fn))
    return xmls


def discover_roots(cli_dirs):
    """CLI dirs → ATMOS_CORPUS_DIR → ``[]``, meaning "auto-probe"."""
    if cli_dirs:
        return cli_dirs
    env = os.environ.get("ATMOS_CORPUS_DIR")
    if env:
        return [os.path.expanduser(env)]
    return []


def discover_xmls(roots):
    """Walk explicit roots as given; with none, run the converter's probe —
    every mounted Windows partition's DriverStore plus the current directory.

    Why not just ``"."``: the development machine's installed DAX3 package
    lives on its Windows partition, and ``docs/corpus.md`` counts it. Walking
    only the cwd silently dropped those 219 files whenever nobody remembered
    to pass the mount, and the figures came out 200-odd short.
    """
    if roots:
        return find_xmls(roots)
    return [str(p) for p in autoprobe_all_dolby_xmls()]


# The codec id sits behind a bus prefix naming the Windows hardware-ID
# namespace the tuning's ``.inf`` binds it under — bare ``DEV_0287_…``,
# ``HDAUDIO_DEV_0257_…``, ``INTELAUDIO_DEV_0274_…``, ``PCI_DEV_1803_…``,
# ``AUCD_DEV_0C29_…`` on Qualcomm Aqstic. The package ships every spelling it
# binds; installing it renames nothing (cross-device-findings.md §17), so both
# extracted trees and DriverStores carry a mix. Search rather than anchor, or
# those spellings all fall to UNKNOWN.
_CODEC_RE = re.compile(r"DEV_([0-9A-Za-z]{4})")


def codec_of(fn):
    bn = os.path.basename(fn)
    if bn.startswith("SOUNDWIRE"):
        return "SOUNDWIRE"
    if bn.startswith("SDW"):
        return "SDW"
    m = _CODEC_RE.search(bn)
    return f"DEV_{m.group(1).upper()}" if m else "UNKNOWN"


_SUBSYS_RE = re.compile(r"SUBSYS_([0-9A-Za-z]{8})")


def subsys_of(fn):
    """PCI/ACPI subsystem id from the filename (the per-device key), or the
    basename when none is embedded."""
    m = _SUBSYS_RE.search(os.path.basename(fn))
    return m.group(1) if m else os.path.basename(fn)


def threshold_schema(th):
    """Classify how a regulator ``threshold_high`` element encodes its array.

    Returns one of:
      ``direct``      — ``value=``/``preset=`` on the element itself (flat schema)
      ``ch_nonzero``  — newer per-channel ``<ch_00>`` with non-zero values
                        (silently dropped before the per-channel parser landed —
                        the SUBSYS_37A317AA gap)
      ``ch_zero``     — per-channel ``<ch_00>`` resolving to all-zero
      ``ch_preset``   — per-channel ``<ch_00 preset=...>`` reference
      ``empty``       — no value/preset/ch_00
      ``None``        — the element is absent
    """
    if th is None:
        return None
    if th.get("value") or th.get("preset"):
        return "direct"
    c0 = th.find("ch_00")
    if c0 is None:
        return "empty"
    v = c0.get("value")
    if v:
        nonzero = any(x.strip() not in ("0", "-0") for x in v.split(","))
        return "ch_nonzero" if nonzero else "ch_zero"
    if c0.get("preset"):
        return "ch_preset"
    return "empty"


def peq_effective_boost(f):
    """Per-filter positive headroom demand, matching the converter's
    output-gain compensation (make_peq_eq): bells contribute
    ``gain * min(1, 2/q)``, shelves their full gain, both only when gain>0;
    high-pass/low-pass and cuts contribute 0. Used to size L vs R peak boost."""
    if f.get("enabled") == "0":
        return 0.0
    try:
        t = int(f.get("type"))
        g = float(f.get("gain", "0"))
        q = float(f.get("q", "0.707"))
    except (TypeError, ValueError):
        return 0.0
    if g <= 0:
        return 0.0
    if t == 1:
        return g * min(1.0, 2.0 / q)
    if t in (3, 4):
        return g
    return 0.0


# Files are bucketed by the directory segment named after the driver package
# they shipped in: a Dolby extension package (``ext_*``), a DAX3/Fusion INF
# wrapper as installed in a Windows DriverStore (``dax3_ext_*``, ``fusion_*``),
# or a Realtek codec drop (``ExtRtk_*``, ``Codec_*``). A prefix match rather
# than a fixed list, because every driver pull brings new package names — the
# old hardcoded list silently binned real ones as OTHER. What is left
# in OTHER is genuinely package-less: hand-organised collections, vendor APO
# folders, and loose one-off XMLs attached to issue reports.
_PACKAGE_PREFIXES = ("ext_", "fusion_", "dax3_ext_", "ExtRtk_", "Codec_")


@functools.lru_cache(maxsize=None)
def _package_inf_in(directory):
    """Package name taken from a Dolby ``.inf`` sitting beside the tunings.

    Not every package gets a directory of its own: Samsung's Cirrus SoundWire
    drop ships ``dax3_ext_cirrus.inf`` flat in an ``APO/Dolby`` folder next to
    the XMLs. Reading the INF stem keeps those attributed instead of dropping
    a real package into OTHER. Cached per directory — packages hold hundreds
    of files and the answer is the same for all of them.
    """
    try:
        for name in sorted(os.listdir(directory)):
            stem, ext = os.path.splitext(name)
            if ext.lower() == ".inf" and stem.startswith(_PACKAGE_PREFIXES):
                return stem
    except OSError:
        pass
    return None


def package_of(fn):
    for seg in fn.split(os.sep):
        if seg.startswith(_PACKAGE_PREFIXES):
            return seg
    return _package_inf_in(os.path.dirname(fn)) or "OTHER"


def parse_int_attr(elt, attr="value"):
    if elt is None:
        return None
    v = elt.get(attr)
    if v is None:
        return None
    try:
        return int(v)
    except ValueError:
        return None


def parse_csv_ints(s):
    if s is None:
        return []
    return [int(x.strip()) for x in s.split(",") if x.strip()]


def resolve_value(el, const):
    """Resolve a ``value=`` / ``preset=`` element to its CSV string (mirrors
    the converter's resolve_xml_value). ``preset=`` names a child of
    ``<constant>`` whose ``target=`` holds the data. Returns None when absent."""
    if el is None:
        return None
    v = el.get("value")
    if v:
        return v
    preset = el.get("preset")
    if preset and const is not None:
        ref = const.find(preset)
        if ref is not None:
            return ref.get("target", "")
    return None


def find_in_tunings(cp, vlldp, path):
    """Find ``path`` under either tuning block, ``tuning-cp`` first.

    Which block holds a given field is not something to assume. The ad-hoc
    query this replaces looked for the section-14 fields under
    ``tuning-vlldp`` alone; they live under ``tuning-cp`` on every device in
    the corpus, so it reported "present in 0 rows" for all of them and the
    zero read as a finding. Searching both is what makes the count mean
    something.
    """
    for block in (cp, vlldp):
        if block is None:
            continue
        found = block.find(path)
        if found is not None:
            return found
    return None


# Section-1 "universal constants": fields the doc claims are identical on
# every device and profile. Several appear in *both* tuning blocks (the
# table says so outright for postgain and system-gain), so both are read and
# the values pooled — a block that disagreed would show up as a second value
# in the distribution rather than being hidden by preferring one block.
UNIVERSAL_CONSTANT_TAGS = (
    "pregain",
    "postgain",
    "system-gain",
    "calibration-boost",
    "regulator-relaxation-amount",
    "regulator-overdrive",
    "mb-compressor-agc-enable",
    "mb-compressor-slow-gain-enable",
)

# Section-14 "present but not modelled": stages the converter deliberately
# does not translate. What matters is not that the field exists but whether
# any device actually turns it on — a stage that ships disabled everywhere
# costs nothing to skip, one that ships enabled is a real fidelity gap.
UNMODELLED_STAGE_TAGS = (
    "surround-decoder-center-spreading-enable",
    "woofer-regulator-enable",
    "bass-extraction-lfe-gain",
    "regulator-independent-enable",
    "volume-leveler-compressor-enable",
    # Dynamic speaker optimisation hangs off init-info, which itself appears
    # under both tuning blocks.
    "init-info/dynamic_speaker_optimization_enable",
)

# Same section, but these are presence-only markers with no enable flag, and
# they are not anchored to a tuning block — search the whole document.
UNMODELLED_STAGE_MARKERS = (
    "advanced-speaker-virtualizer-rendering-config",
    "advanced-speaker-virtualizer-start-bin",
)


def analyse(xml_path):
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return None
    root = tree.getroot()
    const = root.find(".//constant")
    markers = [tag for tag in UNMODELLED_STAGE_MARKERS
               if root.find(f".//{tag}") is not None]
    rows = []
    for endpoint in root.iter("endpoint"):
        ep_type = endpoint.get("type", "?")
        op_mode = endpoint.get("operating_mode", "?")
        fs = endpoint.get("fs", "?")
        for profile_index, profile in enumerate(endpoint.findall("profile")):
            ptype = profile.get("type", "?")
            cp = profile.find("tuning-cp")
            vlldp = profile.find("tuning-vlldp")
            row = {
                "path": xml_path,
                "codec": codec_of(xml_path),
                "package": package_of(xml_path),
                "endpoint_type": ep_type,
                "operating_mode": op_mode,
                "fs": fs,
                "profile": ptype,
                # Position within the endpoint: the converter builds from the
                # first profile when --profile is not given, so "which profile
                # is first" is a user-visible default (section 12).
                "profile_index": profile_index,
                "markers": markers,
            }
            universal = {}
            for tag in UNIVERSAL_CONSTANT_TAGS:
                found = [block.find(tag) for block in (cp, vlldp)
                         if block is not None]
                values = [parse_int_attr(el) for el in found if el is not None]
                if values:
                    universal[tag] = values
            row["universal"] = universal
            row["unmodelled"] = {
                tag: parse_int_attr(found)
                for tag in UNMODELLED_STAGE_TAGS
                if (found := find_in_tunings(cp, vlldp, tag)) is not None
            }
            if cp is not None:
                row["ieq_enable"] = parse_int_attr(cp.find("ieq-enable"))
                row["ieq_amount"] = parse_int_attr(cp.find("ieq-amount"))
                ieq_set = cp.find("ieq-bands-set")
                row["ieq_preset"] = ieq_set.get("preset") if ieq_set is not None else None
                row["vl_enable"] = parse_int_attr(cp.find("volume-leveler-enable"))
                row["vl_amount"] = parse_int_attr(cp.find("volume-leveler-amount"))
                row["vl_in_target"] = parse_int_attr(cp.find("volume-leveler-in-target"))
                row["vl_out_target"] = parse_int_attr(cp.find("volume-leveler-out-target"))
                row["volmax_boost"] = parse_int_attr(cp.find("volmax-boost"))
                row["dialog_enable"] = parse_int_attr(cp.find("dialog-enhancer-enable"))
                row["dialog_amount"] = parse_int_attr(cp.find("dialog-enhancer-amount"))
                row["dialog_ducking"] = parse_int_attr(cp.find("dialog-enhancer-ducking"))
                row["bass_enh_enable"] = parse_int_attr(cp.find("bass-enhancer-enable"))
                row["virtual_bass_mode"] = parse_int_attr(cp.find("virtual-bass-mode"))
                row["geq_enable"] = parse_int_attr(cp.find("graphic-equalizer-enable"))
                row["vol_modeler_enable"] = parse_int_attr(cp.find("volume-modeler-enable"))
                row["surround_enable"] = parse_int_attr(cp.find("surround-decoder-enable"))
                row["surround_boost"] = parse_int_attr(cp.find("surround-boost"))
                row["mi_dv_steer"] = parse_int_attr(cp.find("mi-dv-leveler-steering-enable"))
            if vlldp is not None:
                row["mbc_enable"] = parse_int_attr(vlldp.find("mb-compressor-enable"))
                mbc_t = vlldp.find("mb-compressor-tuning")
                if mbc_t is not None:
                    row["group_count"] = parse_int_attr(mbc_t.find("group_count"))
                    bands = []
                    for i in range(4):
                        bg = mbc_t.find(f"band_group_{i}")
                        if bg is not None:
                            bands.append(parse_csv_ints(bg.get("value", "")))
                    row["band_groups"] = bands
                row["ao_enable"] = parse_int_attr(vlldp.find("audio-optimizer-enable"))
                row["peak_level"] = parse_int_attr(vlldp.find("peak-level"))
                ao = vlldp.find("audio-optimizer-bands")
                if ao is not None:
                    left, right = ao.find("ch_00"), ao.find("ch_01")
                    simplified = left is None or right is None
                    if simplified:
                        left, right = ao.find("gain_l"), ao.find("gain_r")
                    row["ao_simplified"] = simplified
                    # Resolved CSV signature for voice-vs-dynamic comparison;
                    # None when the AO uses an unsupported channel layout.
                    if left is not None and right is not None:
                        row["ao_sig"] = (resolve_value(left, const), resolve_value(right, const))
                row["reg_enable"] = parse_int_attr(vlldp.find("regulator-enable"))
                row["reg_slope"] = parse_int_attr(vlldp.find("regulator-distortion-slope"))
                row["reg_timbre"] = parse_int_attr(vlldp.find("regulator-timbre-preservation"))
                row["reg_spk_dist"] = parse_int_attr(vlldp.find("regulator-speaker-dist-enable"))
                reg_tuning = vlldp.find("regulator-tuning")
                if reg_tuning is not None:
                    th = reg_tuning.find("threshold_high")
                    row["reg_th_schema"] = threshold_schema(th)
                    # The curve itself, for the distinct-pattern count. Goes
                    # through resolve_value so a `preset=` reference counts as
                    # the curve it names rather than as an absence.
                    row["reg_th_pattern"] = resolve_value(th, const)
                peq = vlldp.find("speaker-peq-filters")
                if peq is not None:
                    flts = peq.findall("filter")
                    row["peq_filter_types"] = [f.get("type") for f in flts]
                    row["peq_n_per_speaker"] = Counter(f.get("speaker", "?") for f in flts)
                    row["peq_hp_per_speaker"] = Counter(
                        f.get("speaker", "?") for f in flts
                        if f.get("type") in ("7", "9"))
                    row["peq_shelf_has_q"] = sum(
                        1 for f in flts if f.get("type") == "4" and f.get("q") is not None
                    )
                    row["peq_peak_l"] = max(
                        (peq_effective_boost(f) for f in flts if f.get("speaker") == "0"),
                        default=0.0)
                    row["peq_peak_r"] = max(
                        (peq_effective_boost(f) for f in flts if f.get("speaker") == "1"),
                        default=0.0)
            rows.append(row)
    return rows


def _content_digest(path):
    """SHA-1 of the file bytes. The same tuning ships in many packages and
    under many SUBSYS names, so a file count overstates how much distinct
    tuning data a corpus holds — see docs/corpus.md."""
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.digest()


def composition(xmls):
    """Print what the corpus is made of — the makeup figures behind
    ``docs/corpus.md`` — and return the parsed profile rows for ``report``."""
    print(f"{len(xmls)} tuning XMLs total")
    print(f"  {len({_content_digest(f) for f in xmls}):5} content-unique "
          "(by file digest)")
    print(f"  {len({subsys_of(f) for f in xmls}):5} distinct SUBSYS device ids")
    print(f"  {len({os.path.basename(f) for f in xmls}):5} distinct filenames")
    by_codec = Counter(codec_of(f) for f in xmls)
    print("\nBy codec:")
    for k, v in by_codec.most_common():
        print(f"  {k:20} {v:5}")
    by_package = Counter(package_of(f) for f in xmls)
    print("\nBy driver package:")
    for k, v in by_package.most_common():
        print(f"  {k:60} {v:5}")

    all_rows = []
    for x in xmls:
        rs = analyse(x)
        if rs:
            all_rows.extend(rs)
    print(f"\n{len(all_rows)} profile rows total")

    ep_types = Counter(r["endpoint_type"] for r in all_rows)
    op_modes = Counter(r["operating_mode"] for r in all_rows)
    profiles = Counter(r["profile"] for r in all_rows)
    print(f"\nEndpoint types: {dict(ep_types)}")
    print(f"Operating modes: {dict(op_modes.most_common(10))}")
    print(f"Profiles: {dict(profiles.most_common(20))}")
    return all_rows


def report(xmls):
    all_rows = composition(xmls)
    profiles = Counter(r["profile"] for r in all_rows)

    ieq_presets = Counter(r.get("ieq_preset") for r in all_rows
                          if r.get("ieq_enable") == 1)
    ieq_amounts = Counter(r.get("ieq_amount") for r in all_rows
                          if r.get("ieq_enable") == 1)
    print(f"\nIEQ presets when enabled: {dict(ieq_presets)}")
    print(f"IEQ amounts when enabled: {dict(ieq_amounts.most_common())}")

    def _share_by_profile(field, predicate=None, profs=None):
        for prof in (profs or [p for p, _ in profiles.most_common(8)]):
            rs = [r for r in all_rows if r["profile"] == prof
                  and r.get(field) is not None
                  and (predicate is None or predicate(r))]
            if not rs:
                continue
            c = Counter(r[field] for r in rs)
            t = len(rs)
            top = ", ".join(f"{k}:{v * 100 // t}%" for k, v in c.most_common(6))
            print(f"  {prof:20} (n={t})  {top}")

    print("\nvl_amount by profile:")
    _share_by_profile("vl_amount")
    print("\nIEQ amount by profile (ieq_enable=1):")
    _share_by_profile("ieq_amount", predicate=lambda r: r.get("ieq_enable") == 1)
    print("\nDialog-enhancer enabled by profile:")
    for prof, _ in profiles.most_common(8):
        rs = [r for r in all_rows if r["profile"] == prof
              and r.get("dialog_enable") is not None]
        if rs:
            en = sum(1 for r in rs if r["dialog_enable"] == 1)
            print(f"  {prof:20} {en * 100 // len(rs)}% enabled ({en}/{len(rs)})")
    print("\nDialog-enhancer amount by profile (when enabled):")
    _share_by_profile("dialog_amount", predicate=lambda r: r.get("dialog_enable") == 1)

    print("\nMBC enable by profile:")
    for prof, _ in profiles.most_common(10):
        rows = [r for r in all_rows if r["profile"] == prof]
        en = sum(1 for r in rows if r.get("mbc_enable") == 1)
        print(f"  {prof:25} {en}/{len(rows)}  ({en/len(rows)*100:.0f}%)")

    gc_total = Counter()
    gc_enabled = Counter()
    gc_disabled_but_populated = 0
    for r in all_rows:
        gc = r.get("group_count")
        if gc is None:
            continue
        gc_total[gc] += 1
        if r.get("mbc_enable") == 1:
            gc_enabled[gc] += 1
        elif gc and gc >= 3:
            gc_disabled_but_populated += 1
    print(f"\nMBC group_count all profiles: {dict(gc_total.most_common())}")
    print(f"MBC group_count when ENABLED: {dict(gc_enabled.most_common())}")
    print(f"3+/4 group_count with MBC disabled: {gc_disabled_but_populated}")

    peq_types = Counter()
    for r in all_rows:
        for t in r.get("peq_filter_types", []) or []:
            peq_types[t] += 1
    print(f"\nPEQ filter types: {dict(peq_types.most_common())}")

    n_shelves = sum(1 for r in all_rows
                    for t in (r.get("peq_filter_types") or [])
                    if t == "4")
    shelf_q = sum(r.get("peq_shelf_has_q", 0) or 0 for r in all_rows)
    print(f"Shelf filters total={n_shelves}, with explicit q={shelf_q}")

    print("\nvolmax-boost distribution by profile (1/16 dB, only non-None):")
    by_prof = defaultdict(Counter)
    for r in all_rows:
        v = r.get("volmax_boost")
        if v is not None:
            by_prof[r["profile"]][v] += 1
    for prof in ["dynamic", "movie", "music", "game", "voice",
                 "voice_onlinecourse", "personalize", "off"]:
        c = by_prof.get(prof)
        if not c:
            continue
        total = sum(c.values())
        top = c.most_common(5)
        print(f"  {prof:25} n={total}  top: " +
              ", ".join(f"{k}->{v}/{v*100//total}%" for k, v in top))

    slope = Counter(r.get("reg_slope") for r in all_rows
                    if r.get("reg_slope") is not None)
    print(f"\nRegulator distortion slope: {dict(slope.most_common())}")

    # threshold_high encoding (internal_speaker, regulator enabled): the
    # ch_nonzero bucket is the newer SoundWire per-channel schema that the
    # parser dropped to no-limiting before the per-channel reader landed
    # (docs/cross-device-findings.md §12, follow-up #1).
    reg_schema = Counter()
    reg_schema_devs = defaultdict(set)
    for r in all_rows:
        if r["endpoint_type"] != "internal_speaker" or r.get("reg_spk_dist") != 1:
            continue
        sch = r.get("reg_th_schema")
        if sch is None:
            continue
        reg_schema[sch] += 1
        reg_schema_devs[sch].add(subsys_of(r["path"]))
    print("\nRegulator threshold_high schema (internal_speaker, reg-enable=1):")
    for k, v in reg_schema.most_common():
        print(f"  {k:12} rows={v:6}  devices={len(reg_schema_devs[k])}")

    # PEQ L/R peak-boost asymmetry (cross-device-findings §12 / follow-up #2).
    # The converter applies one global max(L,R) output-gain (EE has no
    # per-channel output-gain), which preserves the Dolby-tuned L/R balance;
    # this sizes the divergence so the doc's magnitude claims regenerate.
    div = [r for r in all_rows
           if r.get("peq_peak_l") is not None
           and abs(r["peq_peak_l"] - r["peq_peak_r"]) > 0.01]
    print("\nPEQ L/R peak-boost asymmetry (profiles with a PEQ):")
    if div:
        deltas = sorted(abs(r["peq_peak_l"] - r["peq_peak_r"]) for r in div)
        devs = {subsys_of(r["path"]) for r in div}
        buckets = Counter()
        for d in deltas:
            buckets["<1" if d < 1 else "1-2" if d < 2 else "2-4" if d < 4 else ">=4"] += 1
        print(f"  divergent rows={len(div)}  devices={len(devs)}  "
              f"median={statistics.median(deltas):.2f} dB  max={deltas[-1]:.2f} dB")
        print(f"  |L-R| histogram: {dict(buckets)}")
    else:
        print("  none")

    # Voice audio-optimizer divergence (cross-device-findings §8 / follow-up #3).
    # Per-endpoint (file + normal mode), full-schema only: does the voice AO
    # vector differ from the dynamic AO vector? Exact-int (resolved-CSV)
    # inequality. Also rolled up per device (subsys). The original "97%" was a
    # per-device figure, so both denominators are printed for a like-for-like.
    endpoints = defaultdict(dict)  # (path, mode) -> {profile: (ao_sig, simplified)}
    for r in all_rows:
        if r["endpoint_type"] != "internal_speaker" or r["operating_mode"] != "normal":
            continue
        if "ao_sig" not in r:
            continue
        endpoints[(r["path"], r["operating_mode"])][r["profile"]] = (
            r["ao_sig"], r.get("ao_simplified"))
    qualifying = diverge = excl_simpl = excl_missing = 0
    dev_total, dev_div = set(), set()
    for (path, _mode), profs in endpoints.items():
        if "voice" not in profs or "dynamic" not in profs:
            excl_missing += 1
            continue
        (v_sig, v_simpl), (d_sig, d_simpl) = profs["voice"], profs["dynamic"]
        if v_simpl or d_simpl:
            excl_simpl += 1
            continue
        qualifying += 1
        dev = subsys_of(path)
        dev_total.add(dev)
        if v_sig != d_sig:
            diverge += 1
            dev_div.add(dev)
    print("\nVoice audio-optimizer divergence (internal_speaker/normal, full-schema):")
    if qualifying:
        print(f"  per-endpoint: {diverge}/{qualifying} "
              f"({diverge * 100 / qualifying:.0f}%) have voice AO != dynamic AO")
        print(f"  per-device:   {len(dev_div)}/{len(dev_total)} "
              f"({len(dev_div) * 100 / len(dev_total):.0f}%) devices diverge on >=1 endpoint")
        print(f"  excluded: {excl_simpl} simplified-schema endpoints, "
              f"{excl_missing} without both voice and dynamic")
    else:
        print("  no qualifying endpoints")

    # peak-level disposition (cross-device-findings §1 / follow-up #5). It's
    # watch-listed (not read) in the converter; confirm it stays near-constant
    # zero so reading it would buy nothing — and so a future deviation is visible.
    pk = Counter(r.get("peak_level") for r in all_rows if r.get("peak_level") is not None)
    total_pk = sum(pk.values())
    nonzero = total_pk - pk.get(0, 0)
    print("\npeak-level (tuning-vlldp; watch-only in the converter):")
    print(f"  rows={total_pk}  nonzero={nonzero}  "
          f"nonzero values={dict(sorted(((k, v) for k, v in pk.items() if k), key=lambda kv: -kv[1]))}")

    in_tgt = Counter(r.get("vl_in_target") for r in all_rows
                     if r.get("vl_in_target") is not None)
    out_tgt = Counter(r.get("vl_out_target") for r in all_rows
                      if r.get("vl_out_target") is not None)
    print(f"\nvl_in_target: {dict(in_tgt.most_common())}")
    print(f"vl_out_target: {dict(out_tgt.most_common())}")

    invariants = [
        ("bass_enh_enable", 0), ("virtual_bass_mode", 0),
        ("geq_enable", 0), ("vol_modeler_enable", 0),
        ("dialog_ducking", 0),
    ]
    print("\nUniversal-constant checks (doc claim vs corpus):")
    for field, expected in invariants:
        vals = Counter(r.get(field) for r in all_rows
                       if r.get(field) is not None)
        total = sum(vals.values())
        n_match = vals.get(expected, 0)
        print(f"  {field:25} claim={expected}  match={n_match}/{total}  "
              f"distinct={dict(vals.most_common(5))}")

    # The rest of the section-1 table, claimed zero everywhere. Values from
    # tuning-cp and tuning-vlldp are pooled (see UNIVERSAL_CONSTANT_TAGS).
    print("\nUniversal-constant checks, gain/AGC block "
          "(pooled across tuning-cp and tuning-vlldp):")
    for tag in UNIVERSAL_CONSTANT_TAGS:
        vals = Counter(v for r in all_rows
                       for v in r.get("universal", {}).get(tag, ())
                       if v is not None)
        total = sum(vals.values())
        if not total:
            print(f"  {tag:32} absent from every profile")
            continue
        # Not every one of these is claimed *zero* — regulator-relaxation-amount
        # is claimed constant at 96 — so report the dominant value and its
        # share rather than assuming what the constant should be.
        common, n = vals.most_common(1)[0]
        print(f"  {tag:32} {common}={n}/{total} ({n * 100 / total:.1f}%)  "
              f"distinct={dict(vals.most_common(4))}")

    # Section 14: stages present in the schema that the converter does not
    # translate. Presence is cheap; "enabled" is what would make one a gap.
    # Files as well as rows: the doc counts XMLs, and one XML contributes many
    # endpoint x profile rows, so the two differ by more than an order of
    # magnitude. Devices (subsys) are narrower still.
    print("\nPresent-but-not-modelled stages:")
    for tag in UNMODELLED_STAGE_TAGS:
        having = [r for r in all_rows if tag in r.get("unmodelled", {})]
        if not having:
            print(f"  {tag:48} absent")
            continue
        vals = Counter(r["unmodelled"][tag] for r in having)
        on = [r for r in having if r["unmodelled"][tag] not in (None, 0)]
        files_on = {r["path"] for r in on}
        devs_on = {subsys_of(r["path"]) for r in on}
        print(f"  {tag:48} files={len({r['path'] for r in having}):5} "
              f"rows={len(having):6} | enabled: files={len(files_on):4} "
              f"rows={len(on):5} devices={len(devs_on):3} "
              f"values={dict(vals.most_common(4))}")
        if 0 < len(devs_on) <= 3:
            print(f"    {'':46} enabled on: {sorted(devs_on)}")

    for tag in UNMODELLED_STAGE_MARKERS:
        files = {r["path"] for r in all_rows if tag in r.get("markers", ())}
        devs = {subsys_of(p) for p in files}
        print(f"  {tag:48} files={len(files):5} devices={len(devs):3}"
              + (f"  {sorted(devs)}" if 0 < len(devs) <= 3 else ""))

    # Distinct regulator curves (section 7). Counts the threshold_high
    # vectors themselves, where the schema classification above counts only
    # the *shape* the values are stored in.
    patterns = {r["reg_th_pattern"] for r in all_rows
                if r.get("reg_th_pattern")}
    print(f"\nDistinct threshold_high curves (value= and resolved preset=): "
          f"{len(patterns)}")

    # Which profile a user gets without --profile (section 12): the first one
    # declared on the default endpoint.
    first = [r for r in all_rows
             if r["profile_index"] == 0
             and r["endpoint_type"] == "internal_speaker"
             and r["operating_mode"] == "normal"]
    if first:
        dyn = sum(1 for r in first if r["profile"] == "dynamic")
        others = Counter(r["profile"] for r in first if r["profile"] != "dynamic")
        print(f"\nFirst profile on internal_speaker/normal: {dyn}/{len(first)} "
              f"are 'dynamic'" + (f"; others={dict(others.most_common(5))}"
                                  if others else ""))

    # PEQ L/R structural asymmetry (section 12). The peak-boost asymmetry
    # above measures magnitude; this measures whether the two channels carry
    # a different *number* of filters at all, which no output-gain can
    # reconcile.
    peq_rows = [r for r in all_rows if r.get("peq_n_per_speaker")]
    count_diff = hp_diff = 0
    for r in peq_rows:
        per = r["peq_n_per_speaker"]
        if per.get("0", 0) != per.get("1", 0):
            count_diff += 1
        hp = r.get("peq_hp_per_speaker", Counter())
        if hp.get("0", 0) != hp.get("1", 0):
            hp_diff += 1
    print(f"PEQ profiles={len(peq_rows)}  L!=R filter count={count_diff}  "
          f"L!=R high-pass count={hp_diff}")

    # Dynamic-profile MBC, per file as well as per row (section 2). The doc
    # quotes both, and they differ because one file contributes many endpoint
    # rows. Denominator is files whose dynamic profile actually *declares*
    # mb-compressor-enable — a file that omits the field is not a device that
    # chose to leave the compressor off, and counting it as one understates
    # the rate.
    dyn_files = defaultdict(set)
    for r in all_rows:
        if r["profile"] == "dynamic" and r.get("mbc_enable") is not None:
            dyn_files[r["path"]].add(r["mbc_enable"])
    if dyn_files:
        on = sum(1 for vals in dyn_files.values() if 1 in vals)
        dyn_rows = [r for r in all_rows
                    if r["profile"] == "dynamic" and r.get("mbc_enable") is not None]
        row_on = sum(1 for r in dyn_rows if r["mbc_enable"] == 1)
        print(f"Dynamic-profile MBC enabled (of those declaring the field): "
              f"files {on}/{len(dyn_files)} ({on * 100 / len(dyn_files):.0f}%); "
              f"rows {row_on}/{len(dyn_rows)} "
              f"({row_on * 100 / len(dyn_rows):.0f}%)")

    mi_by_prof = defaultdict(lambda: [0, 0])
    for r in all_rows:
        mi = r.get("mi_dv_steer")
        if mi is None:
            continue
        mi_by_prof[r["profile"]][1] += 1
        if mi == 1:
            mi_by_prof[r["profile"]][0] += 1
    print("\nmi-dv-leveler-steering-enable=1 by profile:")
    for prof, (on, tot) in sorted(mi_by_prof.items(), key=lambda kv: -kv[1][0]):
        if on:
            print(f"  {prof:25} {on}/{tot}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument(
        "corpus_dirs", nargs="*",
        help="directories to walk for DAX3 XMLs (default: $ATMOS_CORPUS_DIR, "
             "else every mounted Windows partition's DriverStore plus the "
             "current directory — the converter's own probe)",
    )
    ap.add_argument(
        "--composition", action="store_true",
        help="print only what the corpus is made of — file/content-unique/"
             "device counts, codecs, driver packages, endpoint-mode-profile "
             "coverage — and stop, to compare a collection against the one "
             "described in docs/corpus.md",
    )
    args = ap.parse_args(argv)

    roots = discover_roots(args.corpus_dirs)
    xmls = discover_xmls(roots)
    if not xmls:
        where = (", ".join(roots) if roots
                 else "any mounted Windows partition or the current directory")
        print(
            f"No Dolby DAX3 XMLs found under: {where}\n"
            "Pass a directory, set ATMOS_CORPUS_DIR, or run from a folder "
            "containing DEV_*/SOUNDWIRE*/SDW* tuning XMLs.",
            file=sys.stderr,
        )
        return 1
    if args.composition:
        composition(xmls)
    else:
        report(xmls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
