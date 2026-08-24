"""The sound-tree commit scanner feeds an unattended watch comment.

Its failure mode is quiet: a range that reads as empty, or a base picked one
tag too far back, produces a comment that looks like a clean pull. So the
tests lock the base-picking order, that a bad range raises instead of
returning nothing, and that a capped list says what it dropped.

All offline: no request leaves the process.
"""

import http.client
import io
import json
import urllib.error

import pytest

from tools.scan_sound_tag import (
    ScanUnavailable,
    fetch_commits,
    main,
    pick_base,
    render,
    render_unavailable,
    speaker_subjects,
    watchlist_hits,
    watchlist_terms,
)
import tools.scan_sound_tag as scan


SEEN = ["sound-7.1", "sound-7.2-rc6", "sound-7.2-rc7", "sound-7.2",
        "sound-fix-7.2-rc3", "tags/sound-sdw-kconfig-fixes"]


# --- picking the base -------------------------------------------------------

def test_a_release_outranks_its_own_candidates():
    """The trap this exists for: `sort -V` ranks sound-7.2 *before*
    sound-7.2-rc7, and taking rc7 as the base for sound-7.3-rc1 reaches back
    across a branch point — 1359 commits instead of the pull's 871."""
    assert pick_base(SEEN, "sound-7.3-rc1") == "sound-7.2"


def test_the_base_is_a_version_tag_and_never_the_tag_itself():
    """A sound-fix-* tag sorts after any version string and the tree carries a
    stray refs/tags/tags/… ref; either as a base is a wrong range, silently."""
    assert pick_base(SEEN + ["sound-7.3-rc1"], "sound-7.3-rc1") == "sound-7.2"
    assert pick_base(["sound-fix-7.2-rc3"], "sound-7.3-rc1") == ""


def test_the_base_must_order_before_the_tag_being_scanned():
    """A catch-up run feeds tags in `comm` order, which is lexicographic:
    sound-7.10-rc1 comes before sound-7.9. Handing the older tag a base that
    already contains it turns a pull that carried quirks into "not scanned"."""
    seen = ["sound-7.8", "sound-7.9", "sound-7.10-rc1"]
    assert pick_base(seen, "sound-7.9") == "sound-7.8"
    assert pick_base(seen, "sound-7.10-rc1") == "sound-7.9"


def test_a_non_version_tag_can_still_take_the_newest_base():
    """A sound-fix-* tag has no place in the version order, so it keeps the
    newest base rather than being left unscanned."""
    assert pick_base(["sound-7.2", "sound-7.3-rc1"],
                     "sound-fix-7.3-rc2") == "sound-7.3-rc1"


def test_no_base_when_nothing_has_been_processed():
    assert pick_base([], "sound-7.3-rc1") == ""


def test_release_candidates_order_within_a_series():
    assert pick_base(["sound-7.2-rc2", "sound-7.2-rc10"],
                     "sound-7.2") == "sound-7.2-rc10"


# --- reading the range ------------------------------------------------------

def _reply(commits, nxt=None, guard=True):
    payload = {"log": [{"commit": sha, "message": msg}
                       for sha, msg in commits]}
    if nxt:
        payload["next"] = nxt
    body = json.dumps(payload)
    text = ")]}'\n" + body if guard else body

    def opener(_url, timeout=None):
        return io.BytesIO(text.encode())
    return opener


def test_fetch_strips_the_gitiles_xssi_guard():
    """gitiles prefixes its JSON with `)]}'` — parsed as JSON it is a syntax
    error, so a missed strip looks like an unreadable range."""
    got = fetch_commits("sound-7.2", "sound-7.3-rc1",
                        _reply([("abcdef1234567890", "ALSA: subject\n\nbody")]))
    assert got == [{"sha": "abcdef123456", "subject": "ALSA: subject",
                    "message": "ALSA: subject\n\nbody"}]


def test_fetch_reads_a_reply_without_the_guard():
    got = fetch_commits("sound-7.2", "sound-7.3-rc1",
                        _reply([("abcdef1234567890", "s")], guard=False))
    assert got[0]["subject"] == "s"


def test_an_empty_range_is_an_error_not_a_quiet_pass():
    """sound-7.2..sound-7.2-rc7 really does return zero commits — rc7 is an
    ancestor of the release. Reported as "clean pull" that is a lie."""
    with pytest.raises(ScanUnavailable):
        fetch_commits("sound-7.2", "sound-7.3-rc1", _reply([]))


def test_a_paginated_reply_means_the_base_is_wrong():
    """No real pull approaches the cap, so hitting it is a bad base, not a
    big merge window — and a truncated range hides commits."""
    with pytest.raises(ScanUnavailable):
        fetch_commits("sound-7.2", "sound-7.3-rc1",
                      _reply([("a" * 40, "s")], nxt="deadbeef"))


def test_no_base_is_reported_rather_than_guessed():
    with pytest.raises(ScanUnavailable):
        fetch_commits("", "sound-7.3-rc1", _reply([("a" * 40, "s")]))


@pytest.mark.parametrize("boom", [
    OSError("connection reset"),
    # Not an OSError — a truncated body raises this, and letting it out kills
    # the workflow step under `bash -e`, posting no comment for the tag at all.
    http.client.IncompleteRead(b"half"),
    urllib.error.URLError("no route"),
])
def test_any_read_failure_is_reported_not_raised_bare(boom):
    def opener(_url, timeout=None):
        raise boom
    with pytest.raises(ScanUnavailable):
        fetch_commits("sound-7.2", "sound-7.3-rc1", opener)


def test_a_reply_shaped_unlike_a_log_is_reported():
    """A payload missing `commit`/`message`, or not a dict at all, raises
    KeyError/AttributeError — same contract, same handling."""
    def missing_fields(_url, timeout=None):
        return io.BytesIO(b')]}\'\n{"log": [{"nope": 1}]}')

    def not_a_dict(_url, timeout=None):
        return io.BytesIO(b')]}\'\n["a list"]')
    for opener in (missing_fields, not_a_dict):
        with pytest.raises(ScanUnavailable):
            fetch_commits("sound-7.2", "sound-7.3-rc1", opener)


def test_unparseable_json_is_reported():
    def opener(_url, timeout=None):
        return io.BytesIO(b")]}'\nnot json at all")
    with pytest.raises(ScanUnavailable):
        fetch_commits("sound-7.2", "sound-7.3-rc1", opener)


# --- what counts ------------------------------------------------------------

def test_watchlist_terms_drops_comments_and_blanks(tmp_path):
    f = tmp_path / "w.txt"
    f.write_text("# a section header\n\ncs35l56\n  # indented comment\nalc287\n")
    assert watchlist_terms(f) == ["cs35l56", "alc287"]


def test_a_hit_may_be_in_the_body_not_the_subject():
    """A quirk's subject names the machine; the SSID, codec and fixup a watch
    is actually about are in the body. Subject-only matching misses those."""
    commits = [{"sha": "1", "subject": "ALSA: hda/realtek: Add quirk for Foo",
                "message": "ALSA: hda/realtek: Add quirk for Foo\n\n"
                           "The codec SSID 0x17aa:0x38dc needs it.\n"}]
    assert watchlist_hits(commits, ["38dc"]) == commits
    assert watchlist_hits(commits, ["38de"]) == []


def test_hits_are_case_insensitive():
    commits = [{"sha": "1", "subject": "s", "message": "uses CS35L56 here"}]
    assert watchlist_hits(commits, ["cs35l56"]) == commits


def test_speaker_subjects_pick_the_speaker_path():
    commits = [{"sha": "1", "subject": "ALSA: hda/realtek: quirk", "message": ""},
               {"sha": "2", "subject": "ALSA: usb-audio: Pioneer DJ", "message": ""},
               {"sha": "3", "subject": "ASoC: foo: enable woofer", "message": ""}]
    assert [c["sha"] for c in speaker_subjects(commits)] == ["1", "3"]


# --- the rendered section ---------------------------------------------------

def test_a_capped_list_says_what_it_dropped():
    """CLAUDE.md: never a silent cap. A truncated list with no count reads as
    the whole of it."""
    commits = [{"sha": f"{i:012d}", "subject": f"ALSA: hda/realtek: q{i}",
                "message": "x"} for i in range(scan._MAX_SUBJECTS + 5)]
    out = render("sound-7.3-rc1", "sound-7.2", commits, [])
    assert "and 5 more" in out
    assert f"Speaker-path commits ({scan._MAX_SUBJECTS + 5})" in out


def test_an_uncapped_list_says_nothing_about_dropping():
    commits = [{"sha": "1", "subject": "ALSA: hda/realtek: q", "message": "x"}]
    assert "more —" not in render("sound-7.3-rc1", "sound-7.2", commits, [])


def test_no_hits_is_stated_alongside_the_commit_count():
    commits = [{"sha": "1", "subject": "ASoC: unrelated", "message": "x"}]
    out = render("sound-7.3-rc1", "sound-7.2", commits, ["cs35l56"])
    # Not "…either": the pull-text hits block is rendered by the workflow, not
    # here, and on a sound-fix-* pull that block can be non-empty.
    assert "No watchlist hits in the commits." in out
    assert "either" not in out
    assert "(1 total)" in out


def test_an_unscanned_pull_does_not_read_as_a_clean_one():
    out = render_unavailable("the base is wrong")
    assert "not scanned" in out.lower()
    assert "the base is wrong" in out


# --- the CLI the workflow calls ---------------------------------------------

def test_cli_prints_the_section(tmp_path, capsys, monkeypatch):
    w = tmp_path / "w.txt"
    w.write_text("cs35l56\n")
    monkeypatch.setattr(scan, "fetch_commits", lambda *a, **k: [
        {"sha": "abc123abc123", "subject": "ASoC: cs35l56: fix",
         "message": "ASoC: cs35l56: fix"}])
    assert main(["sound-7.3-rc1", "--base", "sound-7.2",
                 "--watchlist", str(w)]) == 0
    out = capsys.readouterr().out
    assert "abc123abc123 ASoC: cs35l56: fix" in out


def test_cli_succeeds_and_says_so_when_the_scan_is_unavailable(
        tmp_path, capsys, monkeypatch):
    """Exit 0 on purpose: a comment saying "not scanned" beats a failed job
    that posts nothing at all about the tag."""
    w = tmp_path / "w.txt"
    w.write_text("cs35l56\n")
    monkeypatch.setattr(scan, "fetch_commits", lambda *a, **k: (_ for _ in ())
                        .throw(ScanUnavailable("mirror lag")))
    assert main(["sound-7.3-rc1", "--base", "sound-7.2",
                 "--watchlist", str(w)]) == 0
    captured = capsys.readouterr()
    assert "not scanned" in captured.out.lower()
    assert "::warning::" in captured.err


def test_cli_picks_the_base_from_the_seen_file(tmp_path, monkeypatch):
    seen = tmp_path / "seen.txt"
    seen.write_text("\n".join(SEEN) + "\n")
    w = tmp_path / "w.txt"
    w.write_text("cs35l56\n")
    used = {}

    def fake(base, tag, *a, **k):
        used["base"] = base
        return [{"sha": "a" * 12, "subject": "s", "message": "s"}]
    monkeypatch.setattr(scan, "fetch_commits", fake)
    main(["sound-7.3-rc1", "--seen", str(seen), "--watchlist", str(w)])
    assert used["base"] == "sound-7.2"
