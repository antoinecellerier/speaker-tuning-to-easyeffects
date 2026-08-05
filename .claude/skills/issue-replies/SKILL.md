---
name: issue-replies
description: >-
  Checklist for triaging GitHub issues and drafting/posting replies in this
  repo. YOU MUST load this when STARTING to triage or investigate an issue
  ("check issue #NN", a new device report, a bug report) — including in plan
  mode — and again BEFORE drafting any issue/PR reply or running
  `gh issue comment` / `gh pr comment`. Covers structure and tone, what may
  be asserted vs framed as hypothesis, citation/link rules, runnable
  experiments, and the standard device-report triage asks.
---

# Triaging issues & drafting GitHub replies

Load this at the start of issue triage, not just at drafting time: the
assertion bar (validate cheap claims now), the triage asks, and the
experiment design below shape what the investigation must produce — and
remember the watchlist rule (CLAUDE.md: device-issue triage updates
`.github/kernel-watchlist.txt` in the same commit).

Process: draft for review
first; post via `gh` only when explicitly told to ("post it") — the user often
posts themselves. Sequence when posting: commit → push (confirm it's on the
remote) → post. End with the footer
`🤖 Generated with [Claude Code](https://claude.com/claude-code)` — same as
commits get `Co-Authored-By`.

## Structure and tone

- Keep it light. The body carries only what we want confirmed and the ask(s);
  every explanatory "why" goes to footnotes. When there's a ladder of possible
  steps, cut to the single highest-value ask.
- First person ("I removed…", "helps me"), warm and credit-forward: thank the
  reporter and attribute fixes to their report.
- Heavy optional instructions (captures, multi-step experiments) go in a
  collapsed `<details>` block, with a minimal quick option alongside the full
  one.
- Symptom counts must match the structure: if the reply says "three things",
  number exactly three sections.

## Assert only what you validated

- State as fact only what was checked this session; everything else is a
  hypothesis, phrased as one and paired with the experiment that will confirm
  or refute it. Checking is often cheaper than hedging — in #39, reading
  Valve's published kernel tree at the reporter's exact build turned "your
  kernel probably lacks the fix" into a verified fact worth asserting.
- Extra care with claims about the reporter's device: an inference from
  driver-package spelunking must never override the reporter's own observed
  evidence (#29: "your model ships Realtek/Cirrus, not Dolby" contradicted
  the Dolby XML their run had auto-detected).
- Verify issue numbers against the tracker (`gh issue list`) before
  attributing a limitation or observation to one.
- Speculative/optional asks: say plainly the odds are low, make the risk
  concrete and cited (not "might be risky"), and lead with the most promising
  concrete path.

## Citations must be clickable for the reader

- This repo's commits: full unquoted SHA — GitHub auto-links it (backticks
  suppress the link; full over short for unambiguity). When telling a
  reporter a fix landed, cite the fix commit and confirm it's pushed first.
- Anything outside this repo (kernel commits, other projects' files/trees)
  auto-links nowhere: give an explicit markdown URL, and prefer mirrors that
  open without auth or anti-bot walls (e.g. `github.com/torvalds/linux`
  commit URLs over `git.kernel.org`, which blocks anonymous fetches).
- Never reference `localresearch/` paths (CLAUDE.md rule) — cite committed
  paths and SHAs only.

## Make experiments runnable — and validating

- Copy-pasteable commands, each with its revert step (e.g.
  `pw-metadata -n settings 0 clock.force-quantum 1024` … revert with value
  `0`), not descriptions of what to change.
- Design the ask so the reporter exercises the code path you need validated;
  mention easier workarounds only as a failover (#33: autoload-first, direct
  file path as fallback — otherwise the shortcut is what gets tested).

## Look up the manufacturer's audio spec before theorising

Before forming any hypothesis about a device's speaker topology, read what the
manufacturer publishes, and compare its **physical driver count** against the
pins `--speaker-info` reports. Fewer pins than drivers ⇒ suspect a hidden
speaker pin (#53); equal ⇒ the topology is fine and the fault is elsewhere.
This is a lookup you perform, not an ask you send the reporter.

Do it early. Skipping it in #53's triage produced three wrong leads (#50, #36,
#46 all called likely hits on "one pin + no quirk entry"); the spec refuted
every one in a single pass.

- **Lenovo** — the PSREF static spec PDF, and read the `Speakers` line:
  `https://psref.lenovo.com/syspool/Sys/PDF/<Family>/<Slug>/<Slug>_Spec.pdf`
  (e.g. `.../Yoga/Yoga_7_16IAH7/Yoga_7_16IAH7_Spec.pdf`). Use the PDF, not the
  `/Product/…` page — that's a JS app and fetches as an empty shell.
  **Judge by whether the line names woofers/tweeters, never by the leading
  count**: the development X1 Yoga reads "Stereo speakers, 2W x2 woofers and
  0.8W x2 tweeters" — four drivers behind a "Stereo speakers" prefix.
- **ASUS** — the model's `/techspec/` page states it in prose (the #29
  Zenbook S14 UX5406: "dual front-firing tweeters and dual woofers").
- **Other OEMs** — find the official spec page. If it doesn't name drivers,
  record that as *unknown* rather than inferring a count.

## Device-report triage asks

- If the reporter dual-boots, ask for a Windows A/B on the same content —
  Dolby processing toggled off vs on. It separates device voicing (amp
  firmware, unreachable from the XML) from host processing (the surface we
  translate).
- If the device isn't in the corpus, ask them to attach the tuning XML to
  the issue. Availability of the XML online is OEM-dependent — some OEMs'
  downloadable driver packages have contained them in the past, but ASUS's
  don't (Windows-Update-on-device only; checked EXEs + Microsoft Update
  Catalog in #29 and #39) — so a brief check is fine, a hunt is not.
