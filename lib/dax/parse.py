"""Read one DAX3 tuning XML into the values the preset is built from.

Everything here is schema knowledge and nothing here is DSP: the resolvers
that follow a `preset=` reference into `<constant>`, the per-channel `ch_00`
fallback the SoundWire schema needs, the 1/16-dB fixed-point scale DAX3
stores dB in, the `ParsedTuning` record `parse_xml` fills, and the table of
DSP blocks the corpus shows but this script does not model.

**No numpy.** Nothing in this module needs it, so it is imported eagerly by
`dolby_to_easyeffects.py` and costs a few milliseconds — unlike
`lib/preset/fir.py`, which the generator has to defer. Keep it that way: the
preset builders read `DB_FIXED_POINT_SCALE` from here, and a numpy import
behind that constant would be paid by everything downstream of it.

Two names are imported *bare* from `lib.report.findings`, against the
"import the module, not the name" rule the rest of `lib/` keeps
(`docs/design-notes.md`, "The monkeypatch hazard"). That is not a preference:
this code arrived as a pure move, and qualifying the call sites it already
had — `_print_finding_detail(finding)` at the end of `parse_xml`,
`Finding(...)` inside `collect_unmodeled_features` — would have rewritten
lines `git blame -C` needs unchanged. The hazard the rule exists for reaches
neither name: `Finding` is a frozen dataclass no test patches, and
`_print_finding_detail` reads `_TAG_CONVENTION_SHOWN` out of its *own*
module globals at call time, so the one patch that does exist still lands
wherever it is called from. Rewrite these two as module imports the next
time those bodies change for another reason.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from lib import console
from lib.report.findings import Finding, _print_finding_detail


def list_endpoints(path: Path):
    """Print available endpoints and profiles in the XML."""
    tree = ET.parse(path)
    root = tree.getroot()
    for ep in root.findall(".//endpoint"):
        ep_type = ep.get("type")
        op_mode = ep.get("operating_mode")
        profiles = [p.get("type") for p in ep.findall("profile")]
        print(f"  endpoint: {ep_type} (operating_mode={op_mode})")
        for p in profiles:
            print(f"    profile: {p}")


_SAFE_PROFILE_RE = re.compile(r"[^A-Za-z0-9_-]")


def sanitize_profile_type(t: str) -> str:
    """Normalize a profile type for safe use in output file paths.

    Profile names flow into `{output_dir}/{...}-{profile}-....json` and the
    matching `.irs`, so values like `../foo` from a crafted XML would escape
    the intended directory. Replace anything outside a plain identifier with
    `_` rather than rejecting — unknown vendor profile names should still
    produce a usable (if ugly) preset name.
    """
    safe = _SAFE_PROFILE_RE.sub("_", t)
    return safe or "_"


def get_profile_types(path: Path, endpoint_type: str, operating_mode: str) -> list[str]:
    """Return all profile type names for the given endpoint/mode, excluding 'off'."""
    tree = ET.parse(path)
    root = tree.getroot()
    ep = root.find(
        f".//endpoint[@type='{endpoint_type}'][@operating_mode='{operating_mode}']"
    )
    if ep is None:
        return []
    return [p.get("type") for p in ep.findall("profile") if p.get("type") != "off"]


def parse_csv_ints(s: str) -> list[int]:
    return [int(x) for x in s.split(",")]


def resolve_xml_value(element, constants):
    """Resolve a value from either a value= attribute or a preset= reference.

    SoundWire XMLs (e.g. Lunar Lake) use preset references like
    <ch_00 preset="array_20_zero" /> instead of inline value="..." attributes.
    The preset name refers to a named element under <constant> whose target=
    attribute holds the actual CSV data.
    """
    if element is None:
        return ""
    val = element.get("value")
    if val is not None and val != "":
        return val
    preset_name = element.get("preset")
    if preset_name and constants is not None:
        ref = constants.find(preset_name)
        if ref is not None:
            return ref.get("target", "")
    return ""


def resolve_channel_or_direct(element, constants):
    """Resolve a CSV array that may live directly on ``element`` or on a
    per-channel ``<ch_00>..<ch_07>`` sub-element.

    Older/flat DAX3 regulator tunings put the array directly on
    ``threshold_high``/``threshold_low`` via ``value=``/``preset=``. The newer
    SoundWire schema (e.g. ``SUBSYS_37A317AA``) nests it per channel instead::

        <threshold_high>
          <ch_00 value="-282,-294,..." />
          <ch_01 value="-282,-294,..." />
          <ch_02 preset="array_20_zero" /> ...
        </threshold_high>

    Returns the direct value when present, otherwise the ``ch_00`` channel
    (resolved through the same ``value=``/``preset=`` mechanism as the audio
    optimizer's ch_00/ch_01), otherwise "". ``ch_00`` is the stereo-limiter
    reference; callers warn if ``ch_01`` diverges.
    """
    direct = resolve_xml_value(element, constants)
    if direct:
        return direct
    if element is None:
        return ""
    ch0 = element.find("ch_00")
    if ch0 is not None:
        return resolve_xml_value(ch0, constants)
    return ""


def _int_attr(element, default=None, name="value"):
    """Read an integer ``name=`` attribute, degrading to ``default``.

    Returns ``default`` when ``element`` is None or the attribute is absent
    or empty. Centralises the ``int(el.get("value"))`` idiom, which
    otherwise raises ``TypeError`` on a present element with a missing or
    blank ``value=`` — a plausible hand-edited or schema-variant shape that
    the CLI top-level did not catch. A present, non-empty but non-integer
    value still raises ``ValueError`` (surfaced cleanly by the CLI handler).
    """
    if element is None:
        return default
    raw = element.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


@dataclass
class ParsedTuning:
    """Everything parse_xml extracts from one DAX3 endpoint/profile.

    Field order matches the legacy 12-tuple this replaced. Values are a mix of
    raw schema ints (freqs, curves, ao_left/ao_right, ieq_amount) and
    already-dB-scaled fields (vol_leveler, surround, volmax_boost); main() and
    the converters apply the remaining /16 and /100 scalings (see M-COUP in
    docs for the layering).
    """
    freqs: list[int]
    curves: dict[str, list[int]]
    ieq_amount: int
    ao_left: list[int]
    ao_right: list[int]
    peq_filters: list[dict]
    vol_leveler: dict | None
    dialog_enhancer: dict | None
    surround: dict | None
    mb_comp: dict | None
    regulator: dict | None
    volmax_boost: float
    # Which profile this tuning came from, and which one the XML says the
    # device ships on (<setting><default_profile>, absent on most files).
    # We build the endpoint's first profile; when Dolby names a different one,
    # the run says so rather than silently diverging from Windows (issue #46).
    profile_used: str | None = None
    default_profile: str | None = None
    # <setting><geq_maximum_range>, raw 1/16 dB: the widest per-band gain this
    # XML expresses (192 = 12 dB). Only the older files state it; the rest are
    # assumed to share the same range, which no corpus file contradicts.
    geq_max_range: int = 192
    # False when <audio-optimizer-enable> is 0. ao_left/ao_right are already
    # zeroed in that case; this records *why* they are flat, so the profile
    # report can say so instead of showing an unexplained zero curve.
    ao_enabled: bool = True
    # False when <ieq-enable> is 0, which is roughly 45% of dynamic-profile
    # corpus rows. ieq_amount then holds our assumed 10, not a value the
    # tuning stated, and Dolby engages no voicing at all — so the report has
    # to say whose number it is printing.
    ieq_enabled: bool = True
    # Enabled-but-unreproducible stages, surfaced together at end of run
    # rather than printed here where they'd be buried (see
    # collect_unmodeled_features / _unmodeled_summary).
    findings: list[Finding] = field(default_factory=list)
    leveler_substages: list[str] = field(default_factory=list)


# DAX3 stores most dB-valued fields as integers in 1/16-dB fixed point
# (gains, thresholds, targets, slope/timbre); divide by this to get dB.
DB_FIXED_POINT_SCALE = 16.0


def parse_xml(path: Path, endpoint_type="internal_speaker",
              operating_mode="normal", profile_type=None,
              announce_profile=False) -> ParsedTuning:
    """Parse a DAX3 tuning XML into a ``ParsedTuning`` (see that dataclass for
    the fields and their units). Raises ``ValueError`` with an actionable
    message for unsupported schema variants or missing required elements.

    ``announce_profile`` prints the resolved "Profile: …" banner line as soon
    as the profile is selected — before the finding details this function
    also prints — so main's run header can carry the actual profile name.
    The pre-parse banner can only name the request, and "Profile: first in
    the file" with the real name arriving lines later read as broken output
    (two review rounds)."""
    tree = ET.parse(path)
    root = tree.getroot()
    constant = root.find("constant")

    if constant is None:
        # Dolby Fusion (microphone AEC / noise-suppression) XMLs share the
        # ``DEV_*_SUBSYS_*`` filename shape but carry a completely different
        # schema — no ``<constant>``, no ``<endpoint>``. They ship under
        # ``fusion_ext_*`` or ``ext_*_*/fusion/`` with ``_dmic.xml`` /
        # ``_amic.xml`` suffixes. The probe filters them by suffix; this
        # guard catches the case where the user passes one explicitly.
        raise ValueError(
            f"{path.name}: no <constant> element at XML root. This looks "
            "like a Dolby Fusion (microphone AEC) tuning, not a DAX3 "
            "playback tuning. Pick an XML without a '_dmic' / '_amic' "
            "suffix — those live alongside the DAX3 tunings in the same "
            "driver package but are for mic processing only."
        )

    band_20_freq = constant.find("band_20_freq")
    if band_20_freq is None:
        raise ValueError(
            f"{path.name}: <constant> has no <band_20_freq> child — cannot "
            "read the 20-band frequency grid. This XML uses a DAX3 schema "
            "variant this script does not support."
        )
    freqs = parse_csv_ints(band_20_freq.get("fs_48000"))

    curves = {}
    for el in constant:
        if el.tag.startswith("ieq_"):
            curves[el.tag] = parse_csv_ints(el.get("target"))

    endpoint = root.find(
        f".//endpoint[@type='{endpoint_type}'][@operating_mode='{operating_mode}']"
    )
    if endpoint is None:
        available = sorted({
            f"{ep.get('type')}/{ep.get('operating_mode')}"
            for ep in root.findall(".//endpoint")
        })
        available_str = ", ".join(available) if available else "(none)"
        raise ValueError(
            f"Endpoint type='{endpoint_type}' operating_mode='{operating_mode}' "
            f"not found. Available endpoint/mode pairs: {available_str}. "
            f"Pass --endpoint TYPE --mode MODE to pick one."
        )

    # Select the profile for vlldp settings (AO, PEQ, MB compressor)
    if profile_type:
        profile = endpoint.find(f"profile[@type='{profile_type}']")
        if profile is None:
            available = [p.get("type") for p in endpoint.findall("profile")]
            raise ValueError(
                f"Profile '{profile_type}' not found. "
                f"Available: {', '.join(available)}"
            )
    else:
        profile = endpoint.find("profile")
        if profile is None:
            raise ValueError(
                f"{path.name}: endpoint type='{endpoint_type}' "
                f"operating_mode='{operating_mode}' has no <profile> "
                "elements to default to. Pass --profile TYPE, or use "
                "--list to see this XML's endpoints and profiles."
            )

    # <setting><default_profile> names the profile the device ships on under
    # Windows. It's rare (23 of 2802 corpus XMLs, re-counted 2026-08-04, and
    # every one of them names `music`) and we don't act on it — we
    # still build the first profile — but a run that silently diverges from
    # Dolby's own default is worth one line of output (issue #46). Read here
    # so the banner below can also say where the pick stands.
    declared_default = root.find("setting/default_profile")
    declared_name = (declared_default.get("value")
                     if declared_default is not None else None)

    # Excludes the `off` profile, matching get_profile_types: it is the
    # disabled state, not a mode anyone selects, and --all-profiles doesn't
    # build it. Counting it made the banner say "9 sound modes" on a device
    # where "--all-profiles builds every mode" then built 8.
    n_profiles = len([p for p in endpoint.findall("profile")
                      if p.get("type") != "off"])

    if announce_profile:
        name = profile.get("type")
        if profile_type:
            console.cprint("head", f"Profile: {profile_type}")
        else:
            # "this speaker's first-listed", one anchor phrase everywhere:
            # the banner, the profile-mismatch detail, and the parse error
            # (three variants read as three facts, round 3; "the file's
            # first" is loose — profiles are per-endpoint, which matters on
            # multi-output XMLs). "Sound modes" + the count answer the
            # round-4 question all three reviewers had — is "first" a
            # sensible pick or an arbitrary one — and give profiles a plain
            # name distinct from the three voicings two lines down.
            shown = name if name else "(unnamed)"
            if n_profiles == 1:
                console.cprint("head", f"Profile: {shown} (this speaker's only "
                               "sound mode)")
            else:
                console.cprint("head", f"Profile: {shown} (the first-listed of "
                               f"this speaker's {n_profiles} sound modes — "
                               "--list names them; --profile picks)")
                # Round 5, all three reviewers: understanding HOW the pick
                # was made didn't answer whether it's the right one. Say
                # where it stands against the Windows default — a match is
                # a confidence line, an undeclared default is honest doubt.
                # "Default", never "the mode Windows uses": default_profile
                # is the shipping default, and the mode actually active on
                # the user's Windows install may differ (they can switch in
                # the Dolby app). The mismatch case says nothing here:
                # [profile-mismatch] owns it, with the ask.
                if name and declared_name == name:
                    console.cprint("dim", "  (also the Windows default for this "
                                  "device)")
                elif not declared_name:
                    # A dim aside, not a finding (user decision, round 6):
                    # first-listed-as-default has held on every device
                    # checked so far, so a tagged note or a fix-menu row
                    # over-promoted a non-issue. The Done block repeats
                    # the assumption once, beside --all-profiles.
                    # The where-to-check pointer (rounds 4-9, every
                    # reviewer; user approved round 9): the app shows the
                    # ACTIVE profile — never claim it shows "the default".
                    console.cprint("dim", "  (we assume it's also the Windows "
                                  "default — your file doesn't say)")
                    console.cprint("dim", "  (the Dolby app on Windows shows the "
                                  "profile you used — --profile matches "
                                  "it)")

    # IEQ amount from the selected profile's tuning-cp (or first with IEQ enabled)
    ieq_amount = 10  # innovation-EQ weight assumed when ieq-amount is absent
    ieq_enabled = True
    cp = profile.find("tuning-cp")
    if cp is not None:
        enable = cp.find("ieq-enable")
        ieq_enabled = enable is None or enable.get("value") == "1"
        if enable is not None and enable.get("value") == "1":
            ieq_amount = _int_attr(cp.find("ieq-amount"), default=ieq_amount)

    vlldp = profile.find("tuning-vlldp")
    if vlldp is None:
        name = profile.get("type")
        label = (f"profile '{name}'" if name
                 else "this speaker's first-listed profile")
        raise ValueError(
            f"{path.name}: {label} has "
            "no <tuning-vlldp> — no audio-optimizer, PEQ, or MBC data to read. "
            "This XML uses a DAX3 schema variant this script does not support."
        )

    ao_bands = vlldp.find("audio-optimizer-bands")
    if ao_bands is None:
        raise ValueError(
            f"{path.name}: tuning-vlldp has no <audio-optimizer-bands>. "
            "This XML uses a DAX3 schema variant this script does not support."
        )
    # Per-channel audio-optimizer correction. Full-schema DAX3 names the
    # channels <ch_00>..<ch_07>; simplified-schema XMLs (older Lenovo drivers,
    # xml_version ~3.2.x — e.g. ThinkPad X1 Carbon Gen 8, see issue #22) store
    # the same 20-band, 1/16-dB arrays under a <gain_l>/<gain_r>/<gain_c>/…
    # surround layout instead. Both resolve through the identical value=/preset=
    # mechanism, so for a 2-channel speaker gain_l→left, gain_r→right. The
    # simplified variant also omits the MBC and speaker-PEQ blocks; those are
    # handled by the enable-gates below (absent element → block skipped).
    left_band = ao_bands.find("ch_00")
    right_band = ao_bands.find("ch_01")
    simplified_ao = left_band is None or right_band is None
    if simplified_ao:
        left_band = ao_bands.find("gain_l")
        right_band = ao_bands.find("gain_r")
    if left_band is None or right_band is None:
        found_tags = sorted({c.tag for c in ao_bands})
        # no_next_step: the sentence already ends on both things to try, and
        # the generic --help pointer under it would offer a flag list to
        # someone who has just been told which two flags to reach for. The
        # sibling schema guards above stop at "not supported" and name nothing
        # to do, so they keep the default pointer rather than closing on
        # silence.
        raise console.no_next_step(ValueError(
            f"{path.name}: audio-optimizer-bands has neither ch_00/ch_01 nor "
            f"gain_l/gain_r — found {found_tags or '[]'} instead. This XML uses "
            "a DAX3 schema variant this script does not support. Pick another "
            "endpoint/profile, or open an issue if you need this variant "
            "supported."
        ))
    ao_left = parse_csv_ints(resolve_xml_value(left_band, constant))
    ao_right = parse_csv_ints(resolve_xml_value(right_band, constant))

    # Dolby can ship a correction curve and still declare the optimizer off —
    # 773 content-unique internal_speaker/normal rows do, and 18 of those, in
    # 17 XMLs, carry a *non-zero* curve, so applying it regardless emits a
    # correction the tuning says not to apply. Almost all are the `off`
    # profile, but `music` is affected too and it is a profile users select.
    # The deepest affected band is 13.7 dB (`off`; median 12.0), 7.0 dB on
    # `music`. Same absent-means-enabled convention as speaker-peq-enable
    # below. The IEQ voicing is a separate stage and stays untouched.
    # Figures re-derived 2026-08-04 against the 3056-XML corpus through
    # resolve_xml_value — a plain grep misses the preset= indirection, and
    # tuning-cp has an audio-optimizer-enable of its own we never read
    # (cross-device-findings.md §8, "Curves shipped with the optimizer
    # switched off", carries the method and the raw-corpus cut).
    ao_enable = vlldp.find("audio-optimizer-enable")
    ao_enabled = ao_enable is None or ao_enable.get("value") != "0"
    if not ao_enabled:
        ao_left = [0] * len(ao_left)
        ao_right = [0] * len(ao_right)

    if simplified_ao:
        # Informational, not a warning: round-4 reviewers read the yellow
        # filename-led schema line as "my laptop is missing something".
        # Plain color, plain words, reassurance first; "simplified-schema
        # DAX3" stays as the grep handle triage and the docs use.
        # Not "speaker-EQ stages" (round 6): the Audio-optimizer section
        # two lines down prints cuts/boosts that read as exactly that, so
        # the absent optional stages get non-EQ words and the line says
        # outright that the correction itself is converted.
        #
        # Printed after the audio-optimizer gate rather than before it: the
        # two conditions are independent, and on a profile that is both
        # simplified AND declares the optimizer off, "the speaker correction
        # below is all there and converted" contradicted the flat-curve
        # explanation printed a few lines later.
        converted = ("the speaker correction below is all there and converted"
                     if ao_enabled else
                     "the speaker correction it carries is read in full")
        console._cprint_wrapped("", "  Your tuning uses Dolby's simpler format — "
                            f"normal for this device, nothing is missing: "
                            f"{converted}; this format just never carries "
                            "Dolby's optional multi-band compressor or "
                            "extra filter stages (simplified-schema DAX3).",
                        indent="  ")

    peq_filters = []
    peq_enable = vlldp.find("speaker-peq-enable")
    if peq_enable is None or peq_enable.get("value") != "0":
        for f in vlldp.findall(".//speaker-peq-filters/filter"):
            if f.get("enabled") == "0":
                continue
            try:
                ftype = int(f.get("type"))
            except (TypeError, ValueError):
                console.warn(f"PEQ filter has missing/garbage type {f.get('type')!r}, skipping")
                continue
            if ftype not in (1, 3, 4, 6, 7, 8, 9):
                console.warn(f"unknown PEQ filter type {ftype}, skipping")
                continue
            try:
                peq_filters.append({
                    "speaker": int(f.get("speaker")),
                    "type": ftype,
                    "f0": float(f.get("f0")),
                    "gain": float(f.get("gain", "0")),
                    "q": float(f.get("q", "0.707")),
                    "s": float(f.get("s", "1.0")),
                    "order": int(f.get("order", "0")),
                })
            except (TypeError, ValueError):
                console.warn("PEQ filter has missing/garbage f0/speaker/order, skipping")
                continue

    # Volume leveler settings (from tuning-cp of the selected profile)
    vol_leveler = None
    if cp is not None:
        vl_enable = cp.find("volume-leveler-enable")
        if vl_enable is not None:
            vl_amount = cp.find("volume-leveler-amount")
            vl_in = cp.find("volume-leveler-in-target")
            vl_out = cp.find("volume-leveler-out-target")
            VOL_LEVELER_TARGET_DEFAULT = -320  # -320/16 = -20.0 dBFS in/out target when absent
            vol_leveler = {
                "enable": _int_attr(vl_enable, default=0),
                "amount": _int_attr(vl_amount, default=0),
                "in_target": _int_attr(vl_in, default=VOL_LEVELER_TARGET_DEFAULT) / DB_FIXED_POINT_SCALE,
                "out_target": _int_attr(vl_out, default=VOL_LEVELER_TARGET_DEFAULT) / DB_FIXED_POINT_SCALE,
            }
    # Sub-stages Dolby pairs with its leveler that we cannot reproduce. Unlike
    # every other mapping, these carry *no* parameters — the schema has only an
    # on/off bit, no threshold, ratio, attack or release anywhere in either
    # tuning block — so there is nothing to derive a stage from, and inventing
    # one is exactly the per-device hand-tuning the XML-only rule forbids. They
    # are recorded so the end-of-run summary can ask affected users for the
    # capture that could settle what they do.
    leveler_substages = [
        name for name, tag in (
            ("volume-leveler-compressor", "volume-leveler-compressor-enable"),
            ("volume-leveler-drc", "volume-leveler-drc-enable"),
        )
        if cp is not None and _int_attr(cp.find(tag), default=0) == 1
    ]

    # volmax-boost (tuning-cp) — Dolby's loudness-maximiser ceiling: the
    # maximum gain above the volume leveler's out-target. Parsed outside
    # the MBC block because the regulator is the preferred injection point
    # and MBC may be disabled on some profiles.
    volmax_boost = 0.0
    if cp is not None:
        volmax = cp.find("volmax-boost")
        if volmax is not None:
            volmax_boost = _int_attr(volmax, default=0) / DB_FIXED_POINT_SCALE

    # Dialog enhancer settings (from tuning-cp)
    dialog_enhancer = None
    if cp is not None:
        de_enable = cp.find("dialog-enhancer-enable")
        if de_enable is not None and de_enable.get("value") == "1":
            dialog_enhancer = {
                # dialog-enhancer-amount: assume 5 when the field is absent
                "amount": _int_attr(cp.find("dialog-enhancer-amount"), default=5),
            }

    # Surround virtualizer settings (from tuning-cp)
    surround = None
    if cp is not None:
        sr_enable = cp.find("surround-decoder-enable")
        if sr_enable is not None and sr_enable.get("value") == "1":
            surround = {
                "boost": _int_attr(cp.find("surround-boost"), default=0) / DB_FIXED_POINT_SCALE,
            }

    # Multi-band compressor settings (from tuning-vlldp)
    mb_comp = None
    mbc_enable = vlldp.find("mb-compressor-enable")
    if mbc_enable is not None and mbc_enable.get("value") == "1":
        mbc_tuning = vlldp.find("mb-compressor-tuning")
        if mbc_tuning is not None:
            band_groups = []
            for i in range(4):
                bg = mbc_tuning.find(f"band_group_{i}")
                if bg is not None:
                    group = parse_csv_ints(bg.get("value"))
                    if len(group) != 6:
                        raise ValueError(
                            f"{path.name}: band_group_{i} has {len(group)} "
                            "values, expected 6 (xover, threshold, ratio, "
                            "attack, release, makeup)."
                        )
                    band_groups.append(group)
            # group_count is present on every corpus XML; default to the
            # number of band groups actually found if a variant omits it.
            group_count = _int_attr(mbc_tuning.find("group_count"),
                                    default=len(band_groups))
            target_power = vlldp.find("mb-compressor-target-power-level")
            # Also grab regulator stress for additional context (same
            # regulator-stress-amount element `_parse_regulator` re-reads
            # for its own `stress`; named distinctly to keep the two
            # consumers' intent clear).
            mbc_reg_stress_el = vlldp.find("regulator-stress-amount")
            mb_comp = {
                "group_count": group_count,
                "band_groups": band_groups,
                "target_power": _int_attr(target_power, default=-80) / DB_FIXED_POINT_SCALE,   # -80/16 = -5.0 dB
                "reg_stress": parse_csv_ints(mbc_reg_stress_el.get("value")) if mbc_reg_stress_el is not None else [],
            }

    regulator = _parse_regulator(vlldp, constant, freqs, path)

    # declared_default (<setting><default_profile>) was read above, before
    # the profile banner.

    # Report each unmodeled block where it was found, and carry the findings
    # out so main() can render their asks once at the end. Both halves matter:
    # printed only here they are buried under the per-band tables, and printed
    # only at the end they lose the context that makes them legible.
    findings = collect_unmodeled_features(profile)
    for finding in findings:
        _print_finding_detail(finding)

    return ParsedTuning(
        freqs, curves, ieq_amount, ao_left, ao_right, peq_filters,
        vol_leveler, dialog_enhancer, surround, mb_comp, regulator, volmax_boost,
        profile_used=profile.get("type"),
        default_profile=(declared_default.get("value")
                         if declared_default is not None else None),
        geq_max_range=_int_attr(root.find("setting/geq_maximum_range"),
                                default=192),
        ao_enabled=ao_enabled,
        ieq_enabled=ieq_enabled,
        findings=findings,
        leveler_substages=leveler_substages,
    )


def _parse_regulator(vlldp, constant, freqs, path):
    """Regulator settings (per-band limiter from tuning-vlldp).

    Returns the regulator dict, or None when the tuning has no
    ``regulator-speaker-dist-enable=1`` / ``regulator-tuning`` pair —
    the shape `ParsedTuning.regulator` and `make_regulator` expect.
    """
    regulator = None
    reg_dist = vlldp.find("regulator-speaker-dist-enable")
    if reg_dist is not None and reg_dist.get("value") == "1":
        reg_tuning = vlldp.find("regulator-tuning")
        if reg_tuning is not None:
            th_el = reg_tuning.find("threshold_high")
            tl_el = reg_tuning.find("threshold_low")
            # The newer SoundWire schema nests per-channel <ch_00>..<ch_07>
            # arrays under threshold_high/low; resolve_channel_or_direct reads
            # ch_00. make_regulator is a single stereo limiter that consumes
            # only threshold_high, so ch_00 is the reference. Warn (rather than
            # silently picking one) when ch_01 diverges so a future genuinely
            # L/R-asymmetric device surfaces — ch_00==ch_01 on the only device
            # with this schema today. (per-band-min would protect both channels
            # but can over-limit the one that didn't need it — left XML-only for
            # a later call once such a device exists.)
            th_val = resolve_channel_or_direct(th_el, constant)
            tl_val = resolve_channel_or_direct(tl_el, constant)
            for _el, _name in ((th_el, "threshold_high"), (tl_el, "threshold_low")):
                _c0 = _el.find("ch_00") if _el is not None else None
                _c1 = _el.find("ch_01") if _el is not None else None
                if (_c0 is not None and _c1 is not None
                        and resolve_xml_value(_c0, constant) != resolve_xml_value(_c1, constant)):
                    console.cprint("warn", f"  {path.name}: regulator {_name} ch_00 ≠ ch_01 "
                                   "(L/R asymmetric); using ch_00 for the stereo limiter.")
            if not th_val:
                console.cprint("warn", f"  {path.name}: regulator enabled but threshold_high "
                               "has no value/preset/ch_00 — no per-band limiting applied.")
            th = [x / DB_FIXED_POINT_SCALE for x in parse_csv_ints(th_val)] if th_val else [0.0] * len(freqs)
            tl = [x / DB_FIXED_POINT_SCALE for x in parse_csv_ints(tl_val)] if tl_val else [-12.0] * len(freqs)
            # make_regulator walks `th` and indexes `freqs` at positions
            # derived from it; a length mismatch would IndexError deep in the
            # zone loop. Fail loud here instead.
            if len(th) != len(freqs):
                raise ValueError(
                    f"{path.name}: regulator threshold_high has {len(th)} "
                    f"values but the band grid has {len(freqs)} — the "
                    "regulator zone mapping requires one threshold per band."
                )
            reg_stress_el = vlldp.find("regulator-stress-amount")
            stress = parse_csv_ints(reg_stress_el.get("value")) if reg_stress_el is not None else [0] * 8
            reg_slope = vlldp.find("regulator-distortion-slope")
            slope = _int_attr(reg_slope, default=16) / DB_FIXED_POINT_SCALE   # 16/16 = 1.0
            reg_timbre = vlldp.find("regulator-timbre-preservation")
            timbre = _int_attr(reg_timbre, default=12) / DB_FIXED_POINT_SCALE   # 12/16 = 0.75
            # `regulator-overdrive` and `regulator-relaxation-amount` are read
            # for visibility (debug print + watch warn) but not yet mapped to
            # any LSP plugin parameter — the corpus shows them as constants
            # (overdrive=0, relaxation=96 in 1/16-dB units) so we have no
            # signal to disambiguate the right mapping.
            reg_overdrive = vlldp.find("regulator-overdrive")
            overdrive = _int_attr(reg_overdrive, default=0)
            reg_relax = vlldp.find("regulator-relaxation-amount")
            relaxation = _int_attr(reg_relax, default=96)
            # `isolated_band` (0/1 per band) feeds the experimental
            # `--enable coupled-bands` mapping (design-notes Finding 10 /
            # unvalidated-scaling entry 11 (f)): a second-device capture
            # showed DAX applying band dynamics on bands whose
            # threshold_high is 0 dBFS but which the XML marks
            # non-isolated. The default path ignores this field entirely.
            iso_el = reg_tuning.find("isolated_band")
            iso_val = resolve_channel_or_direct(iso_el, constant)
            isolated = parse_csv_ints(iso_val) if iso_val else None
            if isolated is not None and len(isolated) != len(freqs):
                console.cprint("warn", f"  {path.name}: regulator isolated_band has "
                               f"{len(isolated)} values for {len(freqs)} "
                               "bands — ignoring it.")
                isolated = None
            regulator = {
                "threshold_high": th,
                "threshold_low": tl,
                "stress": [x / DB_FIXED_POINT_SCALE for x in stress],
                "distortion_slope": slope,
                "timbre_preservation": timbre,
                "overdrive": overdrive,
                "relaxation": relaxation,
                "isolated_band": isolated,
            }
    return regulator


# Newer-pipeline DSP blocks observed in the corpus that the script does not
# model. Flag them when they're enabled so users can correlate with audible
# gaps. The list intentionally omits features that are universally present
# (e.g. `output-mode-partial-{surround,height}-virtualizer-enable`, MI
# steering) — those are documented in CLAUDE.md / docs/ and flagging them
# every run would be noise. Only rare, enabled-only feature blocks belong here.
#
# `active` takes the matched element and returns True if the feature is live
# in this profile; `detail`/`ask` take the same element and return the two
# halves of the Finding. A row's `ask` sits right here, next to the predicate
# that raises it, so adding a field states its own urgency and its own handle
# — there is no central table to keep in sync.
@dataclass(frozen=True)
class _UnmodeledFeature:
    xpath: str
    slug: str
    active: Callable[[ET.Element], bool]
    detail: Callable[[ET.Element], str]
    # Empty for the two blocks below that the user genuinely cannot act on:
    # they are dropped whatever anyone does, so they report inline and stay
    # out of the closing ask.
    ask: Callable[[ET.Element], str] | None = None


_UNMODELED_FEATURES = [
    _UnmodeledFeature(
        ".//dynamic_speaker_optimization_enable", "speaker-optimizer",
        lambda el: el.get("value") == "1",
        # Naming a dropped "bass limiting" stage and stopping there reads as
        # "nothing is protecting your woofers now", and a reader who fears
        # for their speakers has no way to check — so the line still
        # reassures, but only with something true. "Nothing here plays
        # louder than your laptop normally would" was not: the same run can
        # add a volmax boost and, on SoundWire, +12 dB of bass harmonics
        # into the band this dropped stage was protecting. Program level is
        # raised; what is actually capped is the peak.
        lambda el: "Your tuning has an extra bass-protection stage (Dynamic "
                   "Speaker Optimization) that this converter doesn't "
                   "reproduce. Nothing here clips, but the preset does add "
                   "loudness of its own, so very loud bass may sound less "
                   "controlled than on Windows."),
    _UnmodeledFeature(
        ".//advanced-speaker-virtualizer-rendering-config", "virtualizer",
        lambda el: True,  # presence implies the newer virtualizer pipeline
        # What you'd notice, not the internal name: "silently dropped" read
        # as ominous (and false — this line is the announcement), and
        # reviewers took it for the same thing as the "Surround virtualizer"
        # section printed later. Each message now carries its own identity;
        # no cross-reference, since either can appear without the other.
        # "Nothing more" bounded a cost nobody has measured: this stage is
        # on one corpus device, has never been captured, and the one
        # measurement we do have of Dolby virtualization on 2-channel
        # content found no widening at all — so "narrower" isn't even the
        # direction the evidence points. The reassurance stays, but as the
        # true one: dropping it doesn't disturb anything else.
        lambda el: "Your tuning switches on Dolby's newer speaker-widening "
                   "effect (advanced speaker virtualizer), which this "
                   "converter doesn't rebuild — so the stereo image may "
                   "differ from Windows. Nothing else in the preset changes "
                   "because of it."),
    # Watching-only fields below: the corpus shows these as effectively
    # constants and the script doesn't act on them. An XML that breaks the
    # assumption is exactly the data that would move the mapping, so these
    # carry an ask — and it asks for the XML, which is what settles them.
    _UnmodeledFeature(
        ".//peak-level", "peak-level",
        lambda el: (el.get("value") or "0") != "0",
        # Device terms, not schema terms: "corpus rows" and "the standard
        # 1/16-dB convention" read as leaked internal notes to all three
        # round-4 reviewers. The raw value stays verbatim (triage greps
        # for it); the /16 math stays, uncited.
        # Reassurance before caveat (round 7): opening on "unverified"
        # made the tool sound shaky when the behaviour described is the
        # safe default. Two sentences (round 9): one packing what/why/risk
        # took two reads to untangle.
        lambda el: (
            f"peak-level={el.get('value')} (raw value; about "
            f"{int(el.get('value', '0')) / 16:+.2f} dB) — a setting almost "
            "no device we've tested uses. We skip it safely: the presets "
            "are built as if it were 0, which is what every other device "
            "gets. Applying our unverified reading of it could audibly "
            "cost volume, so we don't."),
        # Says where it stands. "a value we've never seen" alone left the
        # reader unable to tell whether their presets were wrong, so the
        # choice was between ignoring it and not installing at all.
        # Three rewrites of history here: "confirm it" dangled its
        # antecedent; "check it's safe" implied a hazard; "should sound
        # right — we'll double-check" read as taking the reassurance back
        # (round 2). The reason to ask (a rare ignored setting) now leads,
        # so the confirmation has an object and the status stands alone.
        # "your tuning XML", the sibling asks' vocabulary — "the XML" cold
        # was a jump for a round-4 reviewer ("is that the file I copied?").
        # Keep the token "XML": the attach-path print in print_project_asks
        # gates on it.
        lambda el: ("Your tuning has a rare setting we ignore; the presets "
                    "should sound right — attach your tuning XML and we'll "
                    "confirm.")),
    _UnmodeledFeature(
        ".//ieq-bands-set", "ieq-preset",
        lambda el: (el.get("preset") or "ieq_balanced") != "ieq_balanced",
        lambda el: (
            f"ieq-bands-set preset={el.get('preset')!r} — this XML names a "
            "non-balanced curve as the profile default, but every device in "
            "our corpus uses 'ieq_balanced'. We still emit the usual "
            "Balanced/Detailed/Warm presets."),
        lambda el: ("Your tuning defaults to a non-balanced voicing — which "
                    "of the three presets sounds closest?")),
    _UnmodeledFeature(
        ".//regulator-overdrive", "regulator-overdrive",
        lambda el: (el.get("value") or "0") != "0",
        lambda el: (
            f"regulator-overdrive={el.get('value')} — this is 0 on every "
            "device we've seen. The script does not currently map it (the "
            "schema interpretation is unverified for non-zero values)."),
        lambda el: (f"regulator-overdrive={el.get('value')} is a value we've "
                    "never seen — send us your tuning XML and we can map it.")),
    _UnmodeledFeature(
        ".//regulator-relaxation-amount", "regulator-relaxation",
        lambda el: (el.get("value") or "96") != "96",
        lambda el: (
            f"regulator-relaxation-amount={el.get('value')} — this is 96 on "
            "every device we've seen. The script does not currently map it "
            "(the schema interpretation is unverified for other values)."),
        lambda el: (f"relaxation-amount={el.get('value')} is a value we've "
                    "never seen — send us your tuning XML and we can map it.")),
]


def collect_unmodeled_features(profile: ET.Element) -> list[Finding]:
    """Return one Finding per unmodeled-but-enabled DSP block in ``profile``.

    Returned rather than printed: printed here they land in the middle of a
    couple of hundred lines of per-band tables where nobody sees them, and
    staying print-free keeps this callable straight from a test. main()
    gathers them across profiles, keyed by slug, and renders one block at the
    end (see ``_unmodeled_summary``).
    """
    found = []
    for feat in _UNMODELED_FEATURES:
        el = profile.find(feat.xpath)
        if el is not None and feat.active(el):
            found.append(Finding(
                slug=feat.slug,
                detail=feat.detail(el),
                ask=feat.ask(el) if feat.ask is not None else "",
                kind="ask",
            ))
    return found
