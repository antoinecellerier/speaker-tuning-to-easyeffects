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

Captures run inside a fake-home namespace when the kernel allows it (see
FAKE_HOME below): the EE run is then REAL — its writes land on a tmpfs and
vanish — and the wrapper runs with --no-activate, so the only remaining
reviewer disclosure is the skipped activation. Without the namespace the
helper falls back to --dry-run against real paths (a real EE run would
overwrite the user's live Dolby-* presets), and the skill's fallback
disclosures apply. meta.txt records which mode ran; it is for triage only —
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

# The persona's world, built inside an unprivileged user namespace: tmpfs
# over /home, the repo bind-mounted where the persona's clone would be, the
# tuning XML where a user who copied it off the Windows partition would put
# it. Every path the scripts print is then authentically the persona's —
# no post-editing of captures (which would silently change wrapping) and no
# "ignore the odd paths" disclosure eating reviewer attention. Writes from
# real runs land on the tmpfs and vanish with the namespace.
FAKE_HOME = "/home/user"
FAKE_REPO = f"{FAKE_HOME}/speaker-tuning-to-easyeffects"
FAKE_XML_DIR = f"{FAKE_HOME}/dax3-tuning"
# Staging area the namespace can see (it is under the repo bind). Pre-copied
# here so XMLs living outside the repo still reach the sandbox.
_STAGE_REL = "localresearch/user_review/.stage"


def _sandbox_available() -> bool:
    probe = subprocess.run(
        ["unshare", "-rm", "sh", "-c",
         "mount -t tmpfs tmpfs /home && mkdir /home/user"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return probe.returncode == 0


def _preview_matches() -> dict[str, list[Path]]:
    """slug → matched XML paths, from `preview_output.py --list`.

    Run outside the sandbox (it scans the real corpus); the paths feed the
    staging copy so the sandboxed preview run scans only those files and
    prints fake-home paths for them.
    """
    out = subprocess.run(
        [sys.executable, "tools/preview_output.py", "--list"],
        cwd=REPO_ROOT, capture_output=True, text=True).stdout
    matches: dict[str, list[Path]] = {}
    slug = None
    for line in out.splitlines():
        if line and not line.startswith(" "):
            slug = line.strip()
            matches[slug] = []
        elif slug and line.strip() and "(no match)" not in line:
            matches[slug].append(Path(line.strip()))
    return matches

# CSI sequences (colors, cursor) and OSC sequences (window title) — both leak
# from the pty capture; neither survives into what a reviewer should read.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")

# One terminal screen for the tail slice: what is on screen when a default
# 80x26-ish window stops scrolling.
TAIL_LINES = 26

REDACTED_HEADER = ("===== RUN ENDING #{n} (captured on a different laptop "
                   "model) =====")


def _pty_capture(cmd: list[str], width: int, sandbox: bool = False,
                 stage: list[Path] = ()) -> tuple[str, int]:
    """Run cmd under a pty at the given width; return raw text + rc.

    `script -qec` rather than a pipe: piping makes stdout block-buffered and
    reorders it against stderr, which a terminal does not do, and the whole
    point of the capture is the order a user sees.

    With ``sandbox=True`` the whole thing runs inside `unshare -rm` with the
    fake-home world assembled first (see FAKE_HOME above); ``stage`` names
    real XML files that must appear in FAKE_XML_DIR before cmd runs, and cmd
    should reference them by their FAKE_XML_DIR paths. The staging copies
    are made under the repo (``_STAGE_REL``) so the bind mount carries them in.
    """
    env = dict(os.environ, COLUMNS=str(width))
    shell_cmd = " ".join(shlex.quote(c) for c in cmd)
    if not sandbox:
        proc = subprocess.run(["script", "-qec", shell_cmd, "/dev/null"],
                              cwd=REPO_ROOT, env=env,
                              stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT)
        return proc.stdout.decode("utf-8", errors="replace"), proc.returncode

    stage_dir = REPO_ROOT / _STAGE_REL
    stage_dir.mkdir(parents=True, exist_ok=True)
    for xml in stage:
        target = stage_dir / xml.name
        if not target.exists():
            target.write_bytes(Path(xml).read_bytes())
    setup = (
        "set -e; "
        # Pin the repo outside /home BEFORE the tmpfs covers it — the repo
        # usually lives under /home, and covering first erases the bind
        # source (the probe can't catch this; it binds nothing).
        "mkdir -p /tmp/.user_review_repo; "
        f"mount --bind {shlex.quote(str(REPO_ROOT))} /tmp/.user_review_repo; "
        "mount -t tmpfs tmpfs /home; "
        f"mkdir -p {FAKE_REPO} {FAKE_XML_DIR}; "
        f"mount --bind /tmp/.user_review_repo {FAKE_REPO}; "
        "umount /tmp/.user_review_repo; "
        f"cp {FAKE_REPO}/{_STAGE_REL}/*.xml {FAKE_XML_DIR}/ 2>/dev/null"
        " || true; "
        f"export HOME={FAKE_HOME}; cd {FAKE_REPO}; "
        f"exec script -qec {shlex.quote(shell_cmd)} /dev/null"
    )
    proc = subprocess.run(["unshare", "-rm", "sh", "-c", setup],
                          cwd=REPO_ROOT, env=env,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    return proc.stdout.decode("utf-8", errors="replace"), proc.returncode


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
    ap.add_argument("--no-sandbox", action="store_true",
                    help="skip the fake-home namespace even if available; "
                         "captures then show real harness paths and use "
                         "--dry-run, which the skill must disclose")
    args = ap.parse_args(argv)

    xml = args.xml.resolve()
    if not xml.is_file():
        ap.error(f"not a file: {xml}")
    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    py = sys.executable
    written: list[str] = []
    failed: list[str] = []

    sandbox = not args.no_sandbox and _sandbox_available()
    if not sandbox and not args.no_sandbox:
        print("note: unprivileged user namespace unavailable — falling back "
              "to real-path --dry-run captures (the skill must disclose "
              "both)", file=sys.stderr)
    stage_dir = REPO_ROOT / _STAGE_REL
    if stage_dir.exists():
        for old in stage_dir.glob("*.xml"):
            old.unlink()

    if sandbox:
        # A real EE run: writes land on the namespace tmpfs, so no --dry-run
        # and no disclosure — reviewers finally see the real closing, which
        # is what most actual users read. The wrapper still can't run for
        # real (it restarts PipeWire); --no-activate writes real confs into
        # the fake home and prints the genuine to-finish steps.
        xml_arg = f"{FAKE_XML_DIR}/{xml.name}"
        ee_cmd = [py, "dolby_to_easyeffects.py", xml_arg]
        pw_cmd = [py, "dolby_to_pipewire.py", xml_arg, "--no-activate"]
        pv_stage = {p.name: p for ps in _preview_matches().values()
                    for p in ps}
        pv_cmd = [py, "tools/preview_output.py", "--width", str(args.width),
                  "--corpus-dir", FAKE_XML_DIR]
    else:
        ee_cmd = [py, "dolby_to_easyeffects.py", str(xml), "--dry-run"]
        pw_cmd = [py, "dolby_to_pipewire.py", str(xml), "--dry-run"]
        pv_stage = {}
        pv_cmd = [py, "tools/preview_output.py", "--width", str(args.width)]

    raw_ee, rc = _pty_capture(ee_cmd, args.width, sandbox=sandbox,
                              stage=[xml])
    if rc != 0:
        failed.append(f"dolby_to_easyeffects.py exited {rc}")
    ee, ee_ann = _plain(raw_ee), _annotate(raw_ee)
    written.append(_write(out_dir / "cap_ee_full.txt", ee))
    written.append(_write(out_dir / "cap_ee_full.color.txt", ee_ann))
    tail = "\n".join(ee.splitlines()[-TAIL_LINES:]) + "\n"
    written.append(_write(out_dir / "slice_ee_tail26.txt", tail))
    tail_ann = "\n".join(ee_ann.splitlines()[-TAIL_LINES:]) + "\n"
    written.append(_write(out_dir / "slice_ee_tail26.color.txt", tail_ann))

    raw_pw, rc = _pty_capture(pw_cmd, args.width, sandbox=sandbox,
                              stage=[xml])
    if rc != 0:
        failed.append(f"dolby_to_pipewire.py exited {rc}")
    written.append(_write(out_dir / "cap_pw_full.txt", _plain(raw_pw)))
    written.append(_write(out_dir / "cap_pw_full.color.txt",
                          _annotate(raw_pw)))

    raw_pv, rc = _pty_capture(pv_cmd, args.width, sandbox=sandbox,
                              stage=list(pv_stage.values()))
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

    mode = ("sandbox: fake-home namespace (paths read /home/user/…; EE run "
            "is REAL, wrapper used --no-activate, preview blocks remain "
            "--dry-run by design)" if sandbox else
            "sandbox: OFF — captures show real harness paths and --dry-run; "
            "the skill's fallback disclosures apply")
    meta = ["Orchestrator-only — never hand this file to a reviewer.",
            "",
            f"full-run XML: {xml}",
            mode,
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
