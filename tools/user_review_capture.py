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
    text = proc.stdout.decode("utf-8", errors="replace")
    text = _ANSI.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    return text, proc.returncode


def _redact_preview(text: str) -> tuple[str, list[str], list[str]]:
    """Strip harness framing from preview_output text.

    Returns (redacted text, per-block headers for meta.txt, patterns with no
    corpus match). The `### slug — file.xml` headers become anonymous
    RUN ENDING separators; the scan-progress and no-match lines disappear —
    a user sees none of that, and the slug names are answers.
    """
    lines = text.splitlines()
    out: list[str] = []
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
            i += 3
            continue
        out.append(line)
        i += 1
    return "\n".join(out) + "\n", headers, missing


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

    ee, rc = _pty_capture([py, "dolby_to_easyeffects.py", str(xml),
                           "--dry-run"], args.width)
    if rc != 0:
        failed.append(f"dolby_to_easyeffects.py exited {rc}")
    written.append(_write(out_dir / "cap_ee_full.txt", ee))
    tail = "\n".join(ee.splitlines()[-TAIL_LINES:]) + "\n"
    written.append(_write(out_dir / "slice_ee_tail26.txt", tail))

    pw, rc = _pty_capture([py, "dolby_to_pipewire.py", str(xml),
                           "--dry-run"], args.width)
    if rc != 0:
        failed.append(f"dolby_to_pipewire.py exited {rc}")
    written.append(_write(out_dir / "cap_pw_full.txt", pw))

    pv, rc = _pty_capture([py, "tools/preview_output.py",
                           "--width", str(args.width)], args.width)
    if rc != 0:
        failed.append(f"preview_output.py exited {rc}")
    blocks, headers, missing = _redact_preview(pv)
    written.append(_write(out_dir / "slice_preview_blocks.txt", blocks))

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
