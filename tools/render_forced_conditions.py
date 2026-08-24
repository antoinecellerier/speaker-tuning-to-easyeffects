#!/usr/bin/env python3
"""Render the conditional messages no corpus device reaches.

``tools/preview_output.py`` selects real XMLs by predicate, so a message whose
trigger value never occurs in the corpus has no example to show — and stays
unread. Three of the generator's twelve findings are in that position:
`regulator-overdrive` is 0 on every corpus row, `regulator-relaxation-amount`
is 96 on every row, and all but one device (two files) select
`ieq_balanced`. The very universality those messages assert is what makes
them unreachable.

This patches one field of a real XML to an off-default value and runs the
generator on the result, so the copy is read in the surroundings it would
really print in. The smart-amp gate is reached the same way, through the
``DEMO_FIRMWARE_GATE`` hook rather than an XML edit.

    python3 tools/render_forced_conditions.py
    python3 tools/render_forced_conditions.py --xml path/to/DEV_0257_....xml

Exit status is non-zero if any condition failed to fire — that means the
patch no longer matches the schema, not that the copy is fine.
"""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from corpus_audit import discover_roots, find_xmls          # noqa: E402

REPO = Path(__file__).resolve().parents[1]
DEFAULT_OUT_DIR = REPO / "localresearch" / "msg-verify" / "renders"

# slug -> (text as it appears in a stock XML, the off-default that fires it).
# Substring patches, not XML surgery: the point is to leave every other field
# exactly as the device ships it.
CASES = {
    "ieq-preset": ('preset="ieq_balanced"', 'preset="ieq_warm"'),
    "regulator-overdrive": ('regulator-overdrive value="0"',
                            'regulator-overdrive value="5"'),
    "regulator-relaxation": ('regulator-relaxation-amount value="96"',
                             'regulator-relaxation-amount value="80"'),
}
# Conditions reached through an env hook instead of the XML. Both are keyed to
# the machine rather than the tuning, so no XML patch can reach them: the amp
# gate is an ALSA control, and the speaker pin is a subsystem-id match against
# upstream's quirk table (17AA386A = issue #53's Yoga 7 16IAH7).
ENV_CASES = {"firmware-gate": {"DEMO_FIRMWARE_GATE": "off"},
             "speaker-pin": {"DEMO_SPEAKER_PIN": "17AA386A"}}

GENERATOR_ARGS = ["--dry-run", "--skip-ee-check", "--no-color"]


def _pick_xml() -> Path | None:
    """A stock HDA tuning that carries all three patchable fields."""
    for xml in find_xmls(discover_roots([])):
        text = Path(xml).read_text(errors="ignore")
        if all(old in text for old, _ in CASES.values()):
            return Path(xml)
    return None


def _run(xml: Path, out: Path, label: str, env_extra: dict) -> bool:
    env = {**os.environ, "COLUMNS": "80", **env_extra}
    proc = subprocess.run(
        [sys.executable, "dolby_to_easyeffects.py", str(xml), *GENERATOR_ARGS],
        cwd=REPO, env=env, capture_output=True, text=True)
    dest = out / f"forced_{label}.txt"
    dest.write_text(f"# forced: {label}\n# exit={proc.returncode}\n\n"
                    f"{proc.stdout}{proc.stderr}")
    fired = f"[{label}]" in proc.stdout
    print(f"{label:24s} exit={proc.returncode} fired={fired}  -> {dest.name}")
    return fired and proc.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--xml", type=Path,
                    help="stock tuning XML to patch (default: the first "
                         "discovered corpus file carrying all three fields)")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help=f"where to write the renders "
                         f"(default: {DEFAULT_OUT_DIR})")
    args = ap.parse_args()

    base = args.xml or _pick_xml()
    if base is None:
        print("no corpus XML carrying all three patchable fields; pass --xml",
              file=sys.stderr)
        return 2
    print(f"base XML: {base}")
    args.out_dir.mkdir(parents=True, exist_ok=True)
    text = base.read_text()

    ok = True
    # Keep the Dolby filename shape so discovery and the SoundWire check
    # behave exactly as they would for a real device.
    staging = args.out_dir / "_forced_xml"
    staging.mkdir(exist_ok=True)
    try:
        for slug, (old, new) in CASES.items():
            if old not in text:
                print(f"{slug:24s} SKIPPED — {old!r} not in {base.name}")
                ok = False
                continue
            xml = staging / base.name
            xml.write_text(text.replace(old, new))
            ok &= _run(xml, args.out_dir, slug, {})
        for slug, env_extra in ENV_CASES.items():
            xml = staging / base.name
            xml.write_text(text)
            ok &= _run(xml, args.out_dir, slug, env_extra)
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
