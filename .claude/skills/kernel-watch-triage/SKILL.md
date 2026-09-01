---
name: kernel-watch-triage
description: >-
  Method for triaging a kernel-sound-watch hit comment on the "Kernel
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
itself (format under "Record the verdict" below). That edit is the whole
point: nothing else distinguishes an unassessed hit from a cleared one, and
the workflow never re-reads hit comments, so editing them is safe.

This is bookkeeping, not a reply — no draft-for-review cycle. The citation
rules in `/issue-replies` still apply (external SHAs need explicit markdown
URLs to `github.com/torvalds/linux`; only this repo's SHAs auto-link).

Copy this checklist and tick items off; each maps to a section below.

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
and the full pull text folded below. What it does not carry is any commit
*body* or diff — that is what you fetch.

- **Prefer the local clone at `~/src/linux`** (`torvalds/linux`). Pull tags
  live in **tiwai's** tree, so a tag published in the last few days may not be
  merged to mainline yet; check with `git cat-file -t <sha>` before relying on
  it and ask the user to pull if the clone is merely stale.
- **Fallback: one range request**, not one per commit —
  `<mirror>/+log/<base>..<tag>?format=JSON&n=200` returns every commit with its
  full message in a single call, which is what `tools/scan_sound_tag.py`
  already does. Per-commit fetches are load on someone else's mirror for
  data you can get in one.
- `git.kernel.org` blocks anonymous fetches; use the clone or the
  googlesource mirror.

## Attribute every hit to its watch before judging it

`.github/kernel-watchlist.txt` groups terms under `# watch:` headers naming
the issue or the standing lesson that owns them. Find the header whose term
fired, and let it decide what "relevant" means. A commit that matched
`ideapad` but limits *mic* boost is a clear pass; say so rather than listing
it unexplained.

The comment names hit commits without saying which term matched, and a term
can fire from a `Fixes:` line or a body mention rather than the subject — so
re-run the grep yourself over the range's full messages, per term, recording
the count for every term including the zeros. That is what turns "no commit
touches #39" into "`tas2781` fired, but only via two Lenovo ALC287 quirks".

Give a verdict for **every** watch, including the silent ones ("untouched;
ids byte-identical to the previous tag") — a reader has to be able to tell a
checked watch from a forgotten one.

## Read the diff, not the changelog

A commit message can claim more than the diff delivers. `7e77c09e23da` says it
reorders two Lenovo quirk entries "restoring internal speaker functionality";
the diff swaps two adjacent entries carrying the *same* fixup, so no machine's
behaviour changes.

For any ordering or matching claim, check it against
`snd_hda_pick_fixup` (`sound/hda/common/auto_parser.c`):

- One pass over the table, first match wins. Each entry is compared against
  the **codec** SSID when its `match_codec_ssid` flag is set
  (`HDA_CODEC_QUIRK`) and against the **PCI** SSID otherwise
  (`SND_PCI_QUIRK`); a codec-SSID sweep runs afterwards as a fallback.
- So two entries with different ids and the same fixup cannot differ in
  effect, and ordering only matters where the fixups differ.
- SOF zeroes the PCI SSID the HDA layer sees, so `SND_PCI_QUIRK` entries
  cannot match on those machines at all.

## Resolve SSIDs both ways

PCI SSID ≠ codec SSID, and a machine collides with a different quirk under
each. `--speaker-info` prints both (`HDA codecs … Subsystem:` is the codec
one; `PCI audio subsystem: Subsystem:` is the PCI one), while the README
tested table's "Codec / Subsystem" column may hold either. Pull the pair from
the device report before claiming a tested device is or isn't affected, and
say which id you matched on.

## Sweep what the grep did not hit

The comment's folded "Speaker-path commits" list matches subject lines only,
and the watchlist only knows the classes we already track. Skim all subjects
in the range for a shape we have no term for — a new smart-amp part, a new
speaker-path failure mode, a first machine on a platform we watch. That is how
an unwatched class first appears, and it is the reason the watch reads commits
at all rather than the pull text alone.

## Check the blast radius on our side

For anything that touches a speaker path, ask which of our surfaces should
have carried it:

- **`lib/data/speaker_pin_quirks.py`** — pin-*adding* fixups only. Regenerated
  weekly by `tools/update_speaker_pin_quirks.py`, which derives membership
  from `HDA_FIXUP_PINS` tables plus the hand-verified `_FUNC_FIXUP_PINS`
  allowlist of `HDA_FIXUP_FUNC` helpers. **A new pin-writing helper is silently
  missed** — the updater's own guard only catches *renames* of listed ones —
  so audit the allowlist against upstream while you are in the tree:

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

- **`lib/hardware/amps.py`** `_AMP_FAMILIES` — a missing *amplifier* (the
  AW88399 shape). Its membership bar is in `docs/design-notes.md`.
- **Neither** — a fixup that only reroutes DACs on a pin that already exists.
  That class is deliberately unmodelled; the scope numbers and the reasoning
  are in `docs/design-notes.md`, "The class next door". Don't re-derive it,
  and don't quietly start modelling it without the user.
- **README tested table** — grep the SSIDs in the range against it.

## Record the verdict

- Append the `### Triage (YYYY-MM-DD)` section to the hit comment
  (`gh api -X PATCH repos/<owner>/<repo>/issues/comments/<id> -F body=@file`):
  resolved commit(s), impact, action, then the Claude footer. Per-watch
  verdicts first, then anything found while checking them — including
  problems the check exposed in **our** data, which is where they land.
- Update `.github/kernel-watchlist.txt` in the same commit **only** if the
  investigation opened or closed something. Don't add a term already covered
  by a broader one in the file (`alc287` already hits most Lenovo quirks);
  a redundant term doubles every future hit comment.
- **No CHANGELOG entry for triage alone** — `.claude/rules/changelog.md`
  excludes research-log notes. A code fix the triage prompts is judged on its
  own merits.
