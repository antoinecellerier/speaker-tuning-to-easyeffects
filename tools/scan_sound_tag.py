#!/usr/bin/env python3
"""Scan a sound.git pull tag's *commits* for kernel-watchlist terms.

A tag's annotation is the pull-request text Linus receives, and for a
``sound-fix-*`` pull that text names the device quirks it carries — which is
why the watch was built on it. A merge-window ``-rc1`` annotation is something
else: a prose highlights list, with the ~900 commits behind it, and every
per-device quirk in them, appearing nowhere. ``sound-7.3-rc1`` went past the
watch reporting "no watchlist hits" while carrying two new Lenovo speaker-pin
quirks, a re-keying that made a row in our own table wrong, and a first-of-its
-kind smart-amp driver.

So the watch reads the commit range too. One request to the googlesource
mirror returns the whole range with full messages, which is cheap enough to do
on every tag and needs no clone.

    python3 tools/scan_sound_tag.py sound-7.3-rc1
    python3 tools/scan_sound_tag.py sound-7.3-rc1 --base sound-7.2
"""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

# tiwai's tree, not mainline: the pull tags only exist here. Same host as the
# other updaters use, chosen because git.kernel.org blocks anonymous fetches.
SOUND_MIRROR = ("https://kernel.googlesource.com/pub/scm/linux/kernel/git"
                "/tiwai/sound")

DEFAULT_WATCHLIST = (Path(__file__).resolve().parent.parent
                     / ".github" / "kernel-watchlist.txt")


# --- picking the range ------------------------------------------------------

# Only version-numbered pull tags can serve as a base. `sound-fix-*` and the
# stray refs/tags/tags/sound-sdw-kconfig-fixes in the tree are excluded: they
# sort unpredictably against a version and would silently pick a wrong range.
_VERSION_TAG_RE = re.compile(r"^sound-(\d+)\.(\d+)(?:-rc(\d+))?$")


def _tag_order(tag: str) -> tuple[int, int, int]:
    """Sort key placing ``sound-7.2`` *after* every ``sound-7.2-rcN``.

    The release follows its own candidates, which no general-purpose version
    sort gets right — GNU ``sort -V`` puts ``sound-7.2`` first, and taking that
    as the base for ``sound-7.3-rc1`` widened the range from 871 commits to
    1359 by reaching back across a branch point.
    """
    major, minor, rc = _VERSION_TAG_RE.match(tag).groups()
    return int(major), int(minor), int(rc) if rc else 1 << 30


def pick_base(seen: list[str], tag: str) -> str:
    """The newest already-processed version tag to diff *tag* against.

    Empty when nothing qualifies — the first run on a fresh tree, or a tree
    holding only ``sound-fix-*``. The caller reports that rather than guessing
    a range.
    """
    candidates = [t for t in seen
                  if _VERSION_TAG_RE.match(t) and t != tag]
    if _VERSION_TAG_RE.match(tag):
        # A run catching up on two tags at once feeds them in the order `comm`
        # produced, which is lexicographic and not chronological — sound-7.2
        # sorts before sound-7.2-rc7, sound-7.10-rc1 before sound-7.9. Without
        # this the second tag can be handed a base that already contains it,
        # and an empty range gets reported as "not scanned" for a pull that did
        # carry quirks.
        candidates = [t for t in candidates if _tag_order(t) < _tag_order(tag)]
    return max(candidates, key=_tag_order) if candidates else ""


# --- reading the commits ----------------------------------------------------

# Far above any real pull (7.3-rc1, a busy merge window, was 871). Its job is
# to make a paginated reply mean "the base is wrong", not "this pull was big".
_MAX_COMMITS = 10000


class ScanUnavailable(Exception):
    """The range could not be read — reported to the reader, never swallowed.

    A silent fallback to the annotation alone would restore exactly the blind
    spot this exists to close.
    """


def fetch_commits(base: str, tag: str, opener=urllib.request.urlopen) -> list[dict]:
    """``[{sha, subject, message}]`` for the commits *tag* adds over *base*."""
    if not base:
        raise ScanUnavailable("no earlier version tag to diff against")
    url = f"{SOUND_MIRROR}/+log/{base}..{tag}?format=JSON&n={_MAX_COMMITS}"
    # Everything below is inside one broad handler on purpose: the caller's
    # contract is that this raises ScanUnavailable or returns commits, and the
    # workflow step dies on anything else, posting no comment for the tag at
    # all. The ways to get out of here are not all OSError — a truncated body
    # raises http.client.IncompleteRead (an HTTPException), and a reply shaped
    # unlike a gitiles log raises KeyError or AttributeError.
    try:
        with opener(url, timeout=60) as resp:
            raw = resp.read().decode("utf-8", "replace")
        # gitiles guards its JSON against cross-site script inclusion with a
        # `)]}'` line; the rest is ordinary JSON.
        payload = json.loads(raw.split("\n", 1)[1] if raw.startswith(")]}'")
                             else raw)
        if payload.get("next"):
            raise ScanUnavailable(f"{base}..{tag} exceeds {_MAX_COMMITS} "
                                  "commits — the base is wrong")
        log = payload.get("log") or []
        if not log:
            raise ScanUnavailable(f"{base}..{tag} is empty — the base is wrong")
        return [{"sha": c["commit"][:12],
                 "subject": c["message"].split("\n", 1)[0],
                 "message": c["message"]}
                for c in log]
    except ScanUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 — see the comment above
        raise ScanUnavailable(f"could not read {base}..{tag}: "
                              f"{type(exc).__name__}: {exc}") from exc


# --- what counts ------------------------------------------------------------

def watchlist_terms(path: Path) -> list[str]:
    """The grep terms in the watchlist file — comments and blanks dropped."""
    return [line.strip() for line in path.read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def watchlist_hits(commits: list[dict], terms: list[str]) -> list[dict]:
    """Commits whose *whole message* names a watched term.

    The message, not the subject: a quirk's subject names the machine, while
    the SSID, the codec and the fixup a watch is really about are in the body.
    """
    lowered = [t.lower() for t in terms]
    return [c for c in commits
            if any(t in c["message"].lower() for t in lowered)]


# Speaker-path commits worth a human glance even when no term matched — this
# is how a device family we do not watch yet first shows up. Kept to the
# subject line: matching bodies here would pull in most of the tree.
_SPEAKER_SUBJECT_RE = re.compile(
    r"hda/realtek|hda/conexant|hda/scodec|hda/tas2781|hda/cs35l"
    r"|speaker|woofer|tweeter", re.I)

# The reader is scrolling a GitHub comment, so the list is capped — but never
# silently: what was dropped is stated, with the log link to go and see it.
_MAX_SUBJECTS = 60


def speaker_subjects(commits: list[dict]) -> list[dict]:
    return [c for c in commits if _SPEAKER_SUBJECT_RE.search(c["subject"])]


# --- the comment section ----------------------------------------------------

def render(tag: str, base: str, commits: list[dict], terms: list[str]) -> str:
    hits = watchlist_hits(commits, terms)
    speaker = speaker_subjects(commits)
    out = [f"### Commits in `{base}..{tag}` ({len(commits)} total)", ""]
    if hits:
        out += ["#### :rotating_light: Watchlist hits in commits", "```"]
        out += [f"{c['sha']} {c['subject']}" for c in hits]
        out += ["```", ""]
    else:
        out += ["_No watchlist hits in the commits._", ""]
    if speaker:
        shown, dropped = speaker[:_MAX_SUBJECTS], speaker[_MAX_SUBJECTS:]
        out += [f"<details><summary>Speaker-path commits "
                f"({len(speaker)})</summary>", "", "```"]
        out += [f"{c['sha']} {c['subject']}" for c in shown]
        if dropped:
            out.append(f"… and {len(dropped)} more — see the commit log linked "
                       "above.")
        out += ["```", "", "</details>", ""]
    return "\n".join(out)


def render_unavailable(reason: str) -> str:
    """Said out loud in the comment: an unscanned pull must not read as a clean
    one, which is the failure this whole section exists to fix."""
    return "\n".join([
        "### Commits not scanned", "",
        f"_{reason} — the section below covers the pull text only, which on a "
        "merge-window tag does not name individual device quirks._", ""])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tag", help="the pull tag to scan, e.g. sound-7.3-rc1")
    parser.add_argument("--base", default="",
                        help="tag to diff against (default: picked from --seen)")
    parser.add_argument("--seen", type=Path, metavar="FILE",
                        help="file of already-processed tags, one per line, "
                             "to pick the base from")
    parser.add_argument("--watchlist", type=Path, default=DEFAULT_WATCHLIST,
                        help="grep terms (default: .github/kernel-watchlist.txt)")
    args = parser.parse_args(argv)

    base = args.base
    if not base and args.seen:
        base = pick_base(args.seen.read_text().split(), args.tag)
    try:
        commits = fetch_commits(base, args.tag)
    except ScanUnavailable as exc:
        # Exit 0 on purpose: a comment saying "not scanned" is worth more than
        # a failed job that posts nothing. The warning still marks the run.
        print(f"::warning::commit scan skipped for {args.tag}: {exc}",
              file=sys.stderr)
        print(render_unavailable(str(exc)))
        return 0
    print(render(args.tag, base, commits, watchlist_terms(args.watchlist)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
