---
name: kernel-watch-triage
description: >-
  Guides triaging a kernel-sound-watch hit comment on the "Kernel
  sound-tree watch" issue (#40) — the weekly workflow's per-tag report of
  `.github/kernel-watchlist.txt` grep hits against a new tiwai/sound pull
  tag. Load this when asked to "analyse/check the latest kernel watchlist
  hit", when a kernel-sound-watch comment needs a verdict, or before
  appending a `### Triage` section to one. Covers reading the commit range
  cheaply, judging a hit against the watch that fired it, verifying claims
  against the kernel source rather than the changelog, and the blast-radius
  checks on our own tables.
---

# Triaging a kernel-sound-watch hit

The output is a `### Triage (YYYY-MM-DD)` section appended to the hit comment
itself, in the format under "Record the verdict" below. That edit is the whole
point: nothing else distinguishes an unassessed hit from a cleared one. The
workflow never re-reads hit comments, so editing them is safe.

This is bookkeeping, not a reply: no draft-for-review cycle. The citation
rules in `/issue-replies` still apply: external SHAs need explicit markdown
URLs to `github.com/torvalds/linux`, and only this repo's SHAs auto-link.

Copy this checklist and tick items off. Each maps to a section below.

```
Triage progress:
- [ ] 1. Read the commit range — local clone, else one request
- [ ] 2. Re-grep every watch term over full messages, zeros included
- [ ] 3. Read each hit's diff; verify what its message claims
- [ ] 4. Resolve PCI vs codec SSID for any tested device involved
- [ ] 5. Skim the non-hit subjects for a class nothing watches yet
- [ ] 6. Check the blast radius on our own tables
- [ ] 7. Append the Triage section; touch the watchlist only if something moved
```

## Read the range once

The comment already carries the grep hits, a `scan_sound_tag.py` commit scan,
and the full pull text folded below. It carries no commit *body* or diff, and
that is what you fetch.

- Prefer the local `torvalds/linux` clone at `~/src/linux`. Pull tags live in
  tiwai's tree, so a tag published in the last few days may not be merged to
  mainline yet. Check with `git cat-file -t <sha>` before relying on it, and
  ask the user to pull if the clone is merely stale.
- Fallback: one range request, not one per commit.
  `<mirror>/+log/<base>..<tag>?format=JSON&n=200` returns every commit with
  its full message in a single call, which is what `tools/scan_sound_tag.py`
  already does. Per-commit fetches load someone else's mirror for data you can
  get in one.
- `git.kernel.org` blocks anonymous fetches, so use the clone or the
  googlesource mirror.

## Attribute every hit to its watch before judging it

`.github/kernel-watchlist.txt` groups terms under `# watch:` headers naming
the issue or the standing lesson that owns them. Find the header whose term
fired, and let it decide what "relevant" means. A commit that matched
`ideapad` but limits *mic* boost is a clear pass. Say so rather than listing
it unexplained.

Re-run the grep yourself over the range's full messages, per term, and record
the count for every term, zeros included. The comment names hit commits
without saying which term matched, and a term can fire from a `Fixes:` line or
a body mention rather than the subject. The re-grep turns "no commit
touches #39" into "`tas2781` fired, but only via two Lenovo ALC287 quirks".

Give a verdict for **every** watch, including the silent ones, such as
"untouched; ids byte-identical to the previous tag". A reader has to be able
to tell a checked watch from a forgotten one.

## Read the diff, not the changelog

A commit message can claim more than the diff delivers. `7e77c09e23da` says it
reorders two Lenovo quirk entries "restoring internal speaker functionality".
The diff swaps two adjacent entries carrying the *same* fixup, so no machine's
behaviour changes.

Check any ordering or matching claim against `snd_hda_pick_fixup` in
`sound/hda/common/auto_parser.c`:

- It makes one pass over the table, and the first match wins. Each entry is
  compared against the codec SSID when its `match_codec_ssid` flag is set,
  and against the PCI SSID otherwise. `HDA_CODEC_QUIRK` sets the flag, and
  `SND_PCI_QUIRK` does not. A codec-SSID sweep runs afterwards as a fallback.
- So two entries with different ids and the same fixup cannot differ in
  effect. Ordering only matters where the fixups differ.
- Since 7.1, a PCI SSID with either half zero, such as `17aa:0000` under SOF,
  skips the PCI comparison entirely. Every entry there, `SND_PCI_QUIRK`
  included, is then compared against the **codec** SSID. So a PCI-keyed entry
  *can* match such a machine, on its codec id.

## Resolve SSIDs both ways

PCI SSID ≠ codec SSID, and a machine collides with a different quirk under
each. Pull the pair from the device report before claiming a tested device is
or isn't affected, and say which id you matched on. `--speaker-info` names
both: `Codec subsystem:` under `HDA codecs`, and `Controller subsystem:` under
`PCI audio subsystem`. The README tested table's "Codec / Subsystem" column
may hold either.

## Sweep what the grep did not hit

Skim all subjects in the range for a shape we have no term for: a new
smart-amp part, a new speaker-path failure mode, a first machine on a platform
we watch. The comment's folded "Speaker-path commits" list matches subject
lines only, and the watchlist only knows the classes we already track. An
unwatched class first appears this way, which is why the watch reads commits
at all rather than the pull text alone.

## Check the blast radius on our side

For anything that touches a speaker path, ask which of our surfaces should
have carried it:

- `lib/data/speaker_pin_quirks.py` holds pin-*adding* fixups only.
  `tools/update_speaker_pin_quirks.py` regenerates it weekly, taking
  membership from `HDA_FIXUP_PINS` tables plus the hand-verified
  `_FUNC_FIXUP_PINS` allowlist of `HDA_FIXUP_FUNC` helpers. A new pin-writing
  helper is **silently missed**, because the updater's own guard only catches
  *renames* of listed ones. Audit the allowlist against upstream while you are
  in the tree:

  ```bash
  python3 - <<'PY'
  import re
  src = open('sound/hda/codecs/realtek/alc269.c').read()
  found = {m.group(1) for m in re.finditer(
      r'^static void (\w+)\(struct hda_codec \*codec,.*?\n\}\n', src, re.S|re.M)
      if re.search(r'0x9017[0-9a-f]{4}', m.group(0))}
  print(sorted(found))  # compare against _FUNC_FIXUP_PINS
  PY
  ```

- `lib/hardware/amps.py` `_AMP_FAMILIES`: check for a missing *amplifier*,
  the AW88399 shape. Its membership bar is `r-smart-amp-families` in
  `docs/research/hardware-and-drivers.md`.
- `lib/data/speaker_route_quirks.py` holds fixups that reroute one speaker pin
  off a widget with no volume amplifier: `snd_hda_override_conn_list` with no
  pincfg write, plus `alc289_fixup_asus_ga401`'s `preferred_dacs`.
  `tools/update_speaker_route_quirks.py` regenerates it weekly the same way,
  taking membership from the hand-verified `_FUNC_FIXUP_ROUTES` allowlist
  only. It has the same one-sided guard, so a new routing helper is silently
  missed too. Audit while you are in the tree:

  ```bash
  python3 - <<'PY'
  import re
  src = open('sound/hda/codecs/realtek/alc269.c').read()
  found = {m.group(1) for m in re.finditer(
      r'^static void (\w+)\(struct hda_codec \*codec,.*?\n\}\n', src, re.S|re.M)
      if 'snd_hda_override_conn_list' in m.group(0)
      and not re.search(r'0x9017[0-9a-f]{4}', m.group(0))}
  print(sorted(found))  # compare against _FUNC_FIXUP_ROUTES + its
  PY                    # recorded exclusions
  ```

  `preferred_dacs`-only helpers don't show up in that sweep. All five in
  mainline were hand-read 2026-09-01, and every one but GA401 was excluded.
  The membership bar and each exclusion's reason are in
  `docs/research/hardware-and-drivers.md`, `r-speaker-dac-misrouted`. Re-read a
  helper only when the range you are triaging adds or edits one.
- `_FILE_MOVES` in `tools/update_speaker_pin_quirks.py`: both tables carry a
  `commit=` link resolved by GitHub's blame, which follows a rename but not a
  split. A commit in the range that moves or splits
  `sound/hda/codecs/realtek/alc269.c` needs a new hop there. Without one, the
  symptom is the updater's mass-edit rail refusing that commit, never a wrong
  link: a stderr warning names it, and new rows are left `commit=""`.
- README tested table: grep the SSIDs in the range against it.

## Record the verdict

- Append the `### Triage (YYYY-MM-DD)` section to the hit comment with
  `gh api -X PATCH repos/<owner>/<repo>/issues/comments/<id> -F body=@file`.
  It holds the resolved commit(s), impact, action, then the Claude footer.
  Put per-watch verdicts first, then anything found while checking them. That
  includes problems the check exposed in our data, which land here too.
- Touch `.github/kernel-watchlist.txt` only if the triage opened or closed
  an investigation, in the commit that does so. Don't add a term already
  covered by a broader one in the file: `alc287`
  already hits most Lenovo quirks. A redundant term doubles every future hit
  comment.
- Triage alone gets no CHANGELOG entry, because `.claude/rules/changelog.md`
  excludes research-log notes. A code fix the triage prompts is judged on its
  own merits.
