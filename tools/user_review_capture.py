#!/usr/bin/env python3
"""Capture reviewer-ready output for the /user-review skill in one command.

The skill needs four files a role-playing reviewer can be handed cold, and
every one of them used to be assembled by hand each round: pty-wrapped runs
so stdout/stderr interleave the way a terminal shows them, CR/ANSI stripped
so the text is readable outside one, the last-screen slice cut for the
terminal-simulation reviewer, and the preview blocks redacted — their
`### slug — file.xml` headers name the finding each block demonstrates,
which is exactly the comprehension the reviewer is supposed to supply.

    tools/user_review_capture.py <corpus-xml>

Writes into --out-dir:

    cap_ee_full.txt           full dolby_to_easyeffects.py run (--dry-run)
    cap_pw_full.txt           full dolby_to_pipewire.py run (--dry-run)
    slice_ee_tail26.txt       last 26 lines of the EE run — one terminal screen
    slice_preview_blocks.txt  every finding pattern's closing block, redacted
    *.color.txt               the same captures with the terminal's colors
                              kept as ⟦color⟧…⟦/⟧ markers naming what the
                              screen shows (⟦yellow⟧, ⟦faint⟧, ⟦bold-cyan⟧…),
                              never what we mean by it — for reviewers who
                              must judge salience, and so the color choices
                              themselves can catch feedback; plain files stay
                              the verbatim-quoting source
    meta.txt                  orchestrator-only: block↔pattern map, unmatched
                              patterns (never reviewed — say so in the report)

Both full runs are --dry-run on purpose: a real EE run would overwrite the
user's live Dolby-* presets with this XML's tuning, and a real wrapper run
restarts PipeWire. Disclose the flag to reviewers (skill §2); nothing else
about the capture needs explaining to them. meta.txt is for triage only —
handing it to a reviewer grants the comprehension being measured.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT_DIR = REPO_ROOT / "localresearch" / "user_review"

# CSI sequences (colors, cursor) and OSC sequences (window title) — both leak
# from the pty capture; neither survives into what a reviewer should read.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

# One terminal screen for the tail slice: what is on screen when a default
# 80x26-ish window stops scrolling.
TAIL_LINES = 26

REDACTED_HEADER = ("===== RUN ENDING #{n} (captured on a different laptop "
                   "model) =====")


def _pty_capture(cmd: list[str], width: int) -> tuple[str, int]:
    """Run cmd under a pty at the given width; return cleaned text + rc.

    `script -qec` rather than a pipe: piping makes stdout block-buffered and
    reorders it against stderr, which a terminal does not do, and the whole
    point of the capture is the order a user sees.
    """
    env = dict(os.environ, COLUMNS=str(width))
    shell_cmd = " ".join(shlex.quote(c) for c in cmd)
    proc = subprocess.run(["script", "-qec", shell_cmd, "/dev/null"],
                          cwd=REPO_ROOT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    raw = proc.stdout.decode("utf-8", errors="replace")
    return raw, proc.returncode


def _plain(raw: str) -> str:
    """The user-visible text alone — what reviewers quote from."""
    return _ANSI.sub("", raw).replace("\r\n", "\n").replace("\r", "\n")


# The exact SGR sequences rich emits for the scripts' shared six-style
# palette. Marker names are the VISUAL facts, not our semantic style names
# (err/warn/head/…): telling a reviewer a line is "a warning" grants the
# comprehension being measured, and whether the colors themselves
# communicate is feedback they can only give if we don't pre-label it.
# Anything unmapped is dropped, not guessed — an unknown sequence means the
# palette changed and this map is stale.
_SGR = re.compile(r"\x1b\[([0-9;]*)m")
_SGR_STYLE = {
    "1;31": "bold-red", "31": "red",
    "1;36": "bold-cyan",
    "32": "green",
    "33": "yellow",
    "1;35": "bold-magenta",
    "2": "faint",
}


def _annotate(raw: str) -> str:
    """Render colors as ⟦style⟧…⟦/⟧ markers instead of stripping them.

    A model reading a plain capture is effectively color-blind: it cannot
    tell the warn line that pops out on a real screen from the dim table row
    the eye skips, so "buried warning" findings come back inflated. The
    markers carry that salience channel; the plain file stays the source for
    verbatim quoting.
    """
    def sub(m: re.Match) -> str:
        code = m.group(1)
        if code in ("", "0"):
            return "⟦/⟧"
        style = _SGR_STYLE.get(code)
        return f"⟦{style}⟧" if style else ""
    text = _SGR.sub(sub, raw)
    text = _ANSI.sub("", text)          # anything non-SGR (cursor, title)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # An opener with nothing visible before its close is pure noise.
    return re.sub(r"⟦(?!/)[a-z-]+⟧(\s*)⟦/⟧", r"\1", text)


def _redact_preview(text: str, mirror: str | None = None,
                    ) -> tuple[str, str | None, list[str], list[str]]:
    """Strip harness framing from preview_output text.

    Returns (redacted text, redacted mirror, per-block headers for meta.txt,
    patterns with no corpus match). The `### slug — file.xml` headers become
    anonymous RUN ENDING separators; the scan-progress and no-match lines
    disappear — a user sees none of that, and the slug names are answers.

    All decisions come from `text` (the plain rendering); `mirror` is the
    annotated rendering of the same capture, redacted line-for-line by the
    same rules so the two files stay aligned. If it doesn't align (the
    annotator changed line structure — it never should), it is dropped
    rather than shipped skewed.
    """
    lines = text.splitlines()
    mlines = mirror.splitlines() if mirror is not None else None
    if mlines is not None and len(mlines) != len(lines):
        mlines = None
    out: list[str] = []
    mout: list[str] = []
    headers: list[str] = []
    missing: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("Scanning ") and "pattern" in line:
            i += 1
            continue
        if line.startswith("No corpus XML matched:"):
            missing = [s.strip() for s in line.split(":", 1)[1].split(",")]
            i += 1
            continue
        if (line and set(line) == {"─"} and i + 2 < len(lines)
                and lines[i + 1].startswith("### ")):
            headers.append(lines[i + 1][4:].strip())
            out.append("")
            out.append(REDACTED_HEADER.format(n=len(headers)))
            if mlines is not None:
                mout.append("")
                mout.append(REDACTED_HEADER.format(n=len(headers)))
            i += 3
            continue
        out.append(line)
        if mlines is not None:
            mout.append(mlines[i])
        i += 1
    redacted_mirror = "\n".join(mout) + "\n" if mlines is not None else None
    return "\n".join(out) + "\n", redacted_mirror, headers, missing


def _write(path: Path, text: str) -> str:
    path.write_text(text)
    lines = text.splitlines()
    last = next((ln for ln in reversed(lines) if ln.strip()), "")
    return f"  {path}  ({len(lines)} lines; last: {last[:60]!r})"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("xml", type=Path,
                    help="corpus DAX3 XML the two full runs convert "
                         "(tools/preview_output.py --list prints candidates)")
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT_DIR,
                    help=f"where the capture files land "
                         f"(default: {DEFAULT_OUT_DIR})")
    ap.add_argument("--width", type=int, default=80, metavar="COLS",
                    help="terminal width to capture at — 80 is what most "
                         "users see (default: 80)")
    args = ap.parse_args(argv)

    xml = args.xml.resolve()
    if not xml.is_file():
        ap.error(f"not a file: {xml}")
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    py = sys.executable
    written: list[str] = []
    failed: list[str] = []

    raw_ee, rc = _pty_capture([py, "dolby_to_easyeffects.py", str(xml),
                               "--dry-run"], args.width)
    if rc != 0:
        failed.append(f"dolby_to_easyeffects.py exited {rc}")
    ee, ee_ann = _plain(raw_ee), _annotate(raw_ee)
    written.append(_write(out_dir / "cap_ee_full.txt", ee))
    written.append(_write(out_dir / "cap_ee_full.color.txt", ee_ann))
    tail = "\n".join(ee.splitlines()[-TAIL_LINES:]) + "\n"
    written.append(_write(out_dir / "slice_ee_tail26.txt", tail))
    tail_ann = "\n".join(ee_ann.splitlines()[-TAIL_LINES:]) + "\n"
    written.append(_write(out_dir / "slice_ee_tail26.color.txt", tail_ann))

    raw_pw, rc = _pty_capture([py, "dolby_to_pipewire.py", str(xml),
                               "--dry-run"], args.width)
    if rc != 0:
        failed.append(f"dolby_to_pipewire.py exited {rc}")
    written.append(_write(out_dir / "cap_pw_full.txt", _plain(raw_pw)))
    written.append(_write(out_dir / "cap_pw_full.color.txt",
                          _annotate(raw_pw)))

    raw_pv, rc = _pty_capture([py, "tools/preview_output.py",
                               "--width", str(args.width)], args.width)
    if rc != 0:
        failed.append(f"preview_output.py exited {rc}")
    blocks, blocks_ann, headers, missing = _redact_preview(
        _plain(raw_pv), _annotate(raw_pv))
    written.append(_write(out_dir / "slice_preview_blocks.txt", blocks))
    if blocks_ann is not None:
        written.append(_write(out_dir / "slice_preview_blocks.color.txt",
                              blocks_ann))
    else:
        failed.append("annotated preview blocks misaligned with plain — "
                      ".color.txt variant not written")

    meta = ["Orchestrator-only — never hand this file to a reviewer.",
            "",
            f"full-run XML: {xml}",
            "",
            "slice_preview_blocks.txt block map:"]
    meta += [f"  RUN ENDING #{n}: {h}" for n, h in enumerate(headers, 1)]
    meta += ["",
             "patterns with no corpus match (NOT reviewed this round — list "
             "them in the report):"]
    meta += [f"  {slug}" for slug in missing] if missing else ["  (none)"]
    (out_dir / "meta.txt").write_text("\n".join(meta) + "\n")
    written.append(f"  {out_dir / 'meta.txt'}  (orchestrator-only)")

    print("Wrote:")
    print("\n".join(written))
    if missing:
        print(f"\nNo corpus XML matched: {', '.join(missing)} — these "
              "messages go unreviewed; say so in the report.")
    if failed:
        print("\nFAILED: " + "; ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
