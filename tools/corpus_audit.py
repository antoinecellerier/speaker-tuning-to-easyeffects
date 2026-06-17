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
  3. the current directory (walked recursively)

Point it at a mounted Windows DriverStore, an extracted driver tree, or any
folder of collected XMLs:

    python3 tools/corpus_audit.py
    python3 tools/corpus_audit.py /mnt/c/Windows/System32/DriverStore
    ATMOS_CORPUS_DIR=~/dax3-xmls python3 tools/corpus_audit.py
"""

import argparse
import os
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict


def is_dax3_xml(name):
    if not name.endswith(".xml") or name.endswith("_settings.xml"):
        return False
    return name.startswith(("DEV_", "SOUNDWIRE", "SDW"))


def find_xmls(roots):
    xmls = []
    for root in roots:
        for dp, _, fns in os.walk(root):
            for fn in fns:
                if is_dax3_xml(fn):
                    xmls.append(os.path.join(dp, fn))
    return xmls


def discover_roots(cli_dirs):
    """CLI dirs → ATMOS_CORPUS_DIR → current directory."""
    if cli_dirs:
        return cli_dirs
    env = os.environ.get("ATMOS_CORPUS_DIR")
    if env:
        return [os.path.expanduser(env)]
    return ["."]


def codec_of(fn):
    bn = os.path.basename(fn)
    if bn.startswith("DEV_"):
        return bn[:8]  # e.g. DEV_0287
    if bn.startswith("SOUNDWIRE"):
        return "SOUNDWIRE"
    if bn.startswith("SDW"):
        return "SDW"
    return "UNKNOWN"


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


# Driver-package wrapper prefixes seen in the wild, used only to bucket files
# by source package in the report (purely cosmetic — unknowns fall to OTHER).
_PACKAGE_KEYS = (
    "dax3_ext_rtk", "ext_lenovo_AIO_rtk", "ext_thinkpad_AIO_rtk",
    "ext_capg_thinkpad", "ext_capg_lenovo", "ext_amd_thinkpad",
    "ext_amic_rtk_thinkpad", "fusion_ext_intel",
    "ExtRtk_9826", "ExtRtk_9915", "Codec_",
)


def package_of(fn):
    for seg in fn.split(os.sep):
        for key in _PACKAGE_KEYS:
            if seg.startswith(key):
                return seg
    return "OTHER"


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


def analyse(xml_path):
    try:
        tree = ET.parse(xml_path)
    except ET.ParseError:
        return None
    root = tree.getroot()
    const = root.find(".//constant")
    rows = []
    for endpoint in root.iter("endpoint"):
        ep_type = endpoint.get("type", "?")
        op_mode = endpoint.get("operating_mode", "?")
        fs = endpoint.get("fs", "?")
        for profile in endpoint.findall("profile"):
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
                    row["reg_th_schema"] = threshold_schema(reg_tuning.find("threshold_high"))
                peq = vlldp.find("speaker-peq-filters")
                if peq is not None:
                    flts = peq.findall("filter")
                    row["peq_filter_types"] = [f.get("type") for f in flts]
                    row["peq_n_per_speaker"] = Counter(f.get("speaker", "?") for f in flts)
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


def report(xmls):
    print(f"{len(xmls)} tuning XMLs total")
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
             "else the current directory)",
    )
    args = ap.parse_args(argv)

    roots = discover_roots(args.corpus_dirs)
    xmls = find_xmls(roots)
    if not xmls:
        print(
            f"No Dolby DAX3 XMLs found under: {', '.join(roots)}\n"
            "Pass a directory, set ATMOS_CORPUS_DIR, or run from a folder "
            "containing DEV_*/SOUNDWIRE*/SDW* tuning XMLs.",
            file=sys.stderr,
        )
        return 1
    report(xmls)
    return 0


if __name__ == "__main__":
    sys.exit(main())
