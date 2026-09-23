---
name: issue-replies
description: >-
  Guides triaging GitHub issues and drafting or posting replies in this repo.
  Load it when starting to triage or investigate an issue ("check issue #NN",
  a new device report, a bug report), including in plan mode, because it
  shapes what the investigation must produce. Load it again before drafting
  any issue or PR reply or running `gh issue comment` / `gh pr comment`.
  Covers structure and tone, what may be asserted versus framed as a
  hypothesis, citation and link rules, runnable experiments, and the
  standard device-report triage asks.
---

# Triaging issues & drafting GitHub replies

The assertion bar, the triage asks and the experiment design below shape the
investigation, so validate cheap claims while investigating.

Draft each reply for review first. Post it with `gh` only when explicitly told
to ("post it"), because the user often posts themselves. To post: commit,
push, confirm the push is on the remote, then post. End the reply with the
footer `🤖 Generated with [Claude Code](https://claude.com/claude-code)`, as
commits end with `Co-Authored-By`.

## Structure and tone

- Keep it light. The body carries only what we want confirmed and the asks,
  and every explanatory "why" goes to a footnote. When there is a ladder of
  possible steps, cut to the single highest-value ask.
- Write in the first person ("I removed…", "helps me"). Be warm and
  credit-forward: thank the reporter and attribute fixes to their report.
- Put heavy optional instructions, such as captures or multi-step
  experiments, in a collapsed `<details>` block. Offer a minimal quick option
  alongside the full one.
- Make symptom counts match the structure: a reply that says "three things"
  has exactly three numbered sections.

## Assert only what you validated

- State as fact only what was checked this session. Phrase everything else as
  a hypothesis, paired with the experiment that will confirm or refute it.
  Checking is often cheaper than hedging. In #39, reading Valve's published
  kernel tree at the reporter's exact build turned "your kernel probably lacks
  the fix" into a verified fact.
- Claims about the reporter's device need extra care. An inference from
  driver-package spelunking must never override the reporter's own observed
  evidence. In #29, "your model ships Realtek/Cirrus, not Dolby" contradicted
  the Dolby XML their run had auto-detected.
- Verify issue numbers against the tracker with `gh issue list` before
  attributing a limitation or observation to one.
- For a speculative or optional ask, say plainly that the odds are low. Make
  the risk concrete and cited, not "might be risky". Lead with the most
  promising concrete path.

## Let GitHub do the wrapping

Write each paragraph as **one long line**, with no hand-wrapping at 72/80
columns. GitHub reflows prose to the reader's window, so hard breaks buy
nothing and cost on every later edit: a one-word change reflows the whole
block, and the diff hides the actual edit. Fenced code blocks are the
exception. GitHub renders them verbatim, so break them exactly as they should
be run.

## Citations must be clickable for the reader

- Cite this repo's commits by full unquoted SHA, which GitHub auto-links.
  Backticks suppress the link, and a short SHA can be ambiguous. When telling
  a reporter a fix landed, confirm it is pushed, then cite the fix commit.
- Give anything outside this repo, such as kernel commits or other projects'
  files and trees, an explicit markdown URL, since it auto-links nowhere.
  Prefer mirrors that open without auth or anti-bot walls:
  `github.com/torvalds/linux` commit URLs over `git.kernel.org`, which blocks
  anonymous fetches.

## Make experiments runnable and validating

- Give copy-pasteable commands, each with its revert step, not descriptions
  of what to change. For example,
  `pw-metadata -n settings 0 clock.force-quantum 1024` is reverted with the
  value `0`.
- Design the ask so the reporter exercises the code path you need validated.
  Mention an easier workaround only as a failover, or the shortcut is what
  gets tested. In #33, autoload came first and the direct file path was the
  fallback.

## Look up the manufacturer's audio spec before theorising

Before forming any hypothesis about a device's speaker topology, read what the
manufacturer publishes. Compare its **physical driver count** against the pins
`--speaker-info` reports. With fewer pins than drivers, suspect a hidden
speaker pin, as in #53. With equal counts, the topology is fine and the fault
is elsewhere. This is a lookup you perform, not an ask you send the reporter.

Do it early: skipping it in #53's triage produced three wrong leads that the
spec refuted in one pass.

- Lenovo: resolve the machine type to the global model name first. The name a
  reporter gives may be regional, such as China's XiaoXin / 小新 line, and
  PSREF carries only global names. The machine type is the region-independent
  key, and `--speaker-info` prints it as `Product:`, e.g. `83SG`. Search that
  alone, restricted to `psref.lenovo.com`. Result titles read
  `<Family>, <Model name>, Model:<MTM>`. Verified both ways: `83SG` gives
  IdeaPad Pro 5 14AGP11 for #67, whose DMI says "XiaoXinPro 14GT AGP11", and
  `21CD` gives ThinkPad X1 Yoga Gen 7, the development machine. PSREF's own
  APIs return 403/404 and its pages are JS shells, so run this search by hand.
- Never identify a Lenovo by its model suffix. The suffix narrows the
  candidates, and only the machine type picks one. `14AGP11` alone is shared
  by the IdeaPad Pro 5, the IdeaPad Slim 5 and the Yoga Slim 7. In #67 the
  Yoga Slim 7 14AGP11 publishes four drivers and an amplifier, where the
  reporter's IdeaPad Pro 5 14AGP11 has "Stereo speakers, 2W x2".
- Lenovo, next: read the `Speakers` line of the PSREF static spec PDF,
  `https://psref.lenovo.com/syspool/Sys/PDF/<Family>/<Slug>/<Slug>_Spec.pdf`,
  e.g. `.../Yoga/Yoga_7_16IAH7/Yoga_7_16IAH7_Spec.pdf`. Use the PDF, not the
  `/Product/…` page, which is a JS app and fetches as an empty shell. Judge by
  whether the line names woofers or tweeters, never by the leading count. The
  development X1 Yoga reads "Stereo speakers, 2W x2 woofers and 0.8W x2
  tweeters": four drivers behind a "Stereo speakers" prefix.
- ASUS: the model's `/techspec/` page states it in prose. The #29 Zenbook S14
  UX5406 reads "dual front-firing tweeters and dual woofers".
- Other OEMs: find the official spec page. If it doesn't name drivers, record
  the count as *unknown* rather than inferring one.

## Device-report triage asks

- If the reporter dual-boots, ask for a Windows A/B on the same content, with
  Dolby processing toggled off and on. It separates device voicing in amp
  firmware, which the XML cannot reach, from host processing, the surface we
  translate.
- Check the corpus **by `SUBSYS`** before asking for the XML. The collection
  is keyed by device id and holds no model names, so it can say whether a
  `SUBSYS` is in it but not a *model*. The id is the `Codec subsystem:` value
  in `--speaker-info`: `0x17AA3941` becomes `SUBSYS_17AA3941`. It is also the
  first `SUBSYS_` token of any filename the reporter quotes. The
  `Controller subsystem:` line below it is a different id: the machine's PCI
  id. It keys SoundWire and Apple filenames instead, reversed: `17AA:2339`
  becomes `SUBSYS_233917AA`.

  ```
  find "${ATMOS_CORPUS_DIR:-.}" -iname '*SUBSYS_17AA3941*'
  ```

  A hit means their attachment would land as a byte-identical duplicate, and
  the ask buys nothing but goodwill. In #67 it went out twice, asked by model
  name, for a tuning the corpus already held.

- If the `SUBSYS` isn't in the corpus, ask them to attach the tuning XML to
  the issue. A brief online check for the XML is fine and a hunt is not,
  because availability is OEM-dependent. Some OEMs' downloadable driver
  packages have contained it in the past. ASUS's don't: it ships only through
  Windows Update on the device, per checks of the EXEs and the Microsoft Update
  Catalog in #29 and #39.
