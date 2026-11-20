---
name: issue-replies
description: >-
  Guides triaging GitHub issues and drafting or posting replies in this repo.
  Load it when starting to triage or investigate an issue ("check issue #NN",
  a new device report, a bug report), including in plan mode, because it
  shapes what the investigation must produce. Load it again before drafting
  any issue or PR reply or running `gh issue comment` / `gh pr comment`.
  Covers writing plain replies a reporter can follow top to bottom, what may
  be asserted versus framed as a hypothesis, copy-paste asks with an expected
  result, citation and link rules, the manufacturer spec lookup,
  and the standard device-report triage asks.
---

# Triaging issues & drafting GitHub replies

The assertion bar, the asks design and the triage asks below shape the
investigation, so validate cheap claims while investigating.

## Process

- Draft the reply as a file in `localresearch/issue<N>/reply-<k>.md`. Run the
  cold read, then the checklist under "Review the draft before showing it",
  then show it for review.
- Before drafting and again before posting, save the thread to
  `localresearch/issue<N>/thread-<k>.txt` and read it: `gh issue view N` for
  the opening post, then `gh issue view N --comments` appended, since the
  second prints the comments alone (`gh pr view` for a PR). The user often
  replies too, and reporters' messages cross ours, so build on what is there
  and never repeat it.
- Read how this reporter took earlier replies, here and in their other
  threads in the repo
  (`gh search issues --involves <login> --repo antoinecellerier/speaker-tuning-to-easyeffects`).
  A remark that one was hard to follow means a shorter reply than the budget
  below.
- Post with `gh` only when explicitly told to ("post it"), because the user
  often posts themselves. Re-read the draft file first, because the user
  edits it.
- To post: if the reply cites unpushed commits, get push permission, push,
  and confirm the push is on the remote. Then post.
- End the reply with the footer
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`, as
  commits end with `Co-Authored-By`.

## Assert only what you validated

- State as fact only what was checked this session. Phrase everything else as
  a hypothesis, once and briefly where it applies ("That is my reading of the
  module; I can't test it."), paired with the experiment that will confirm or
  refute it. The reasoning behind it stays in the research log.
  Checking is often cheaper than hedging. In #39, reading Valve's published
  kernel tree at the reporter's exact build turned "your kernel probably lacks
  the fix" into a verified fact.
- Claims about the reporter's device need extra care. An inference from
  driver-package spelunking must never override the reporter's own observed
  evidence. In #29, "your model ships Realtek/Cirrus, not Dolby" contradicted
  the Dolby XML their run had auto-detected.
- Cite past research at the strength it reached. Before leaning on a finding,
  read what its research doc concluded: validated, or measured on one device
  and left inconclusive.
- Check every statement about how a test behaves against the script that runs
  it, such as whether volume matters for a capture.
- Check `docs/reference.md` and `docs/cross-device-findings.md` before
  asking for anything, and never ask for what is already established.
- Verify issue numbers against the tracker with `gh issue list` before
  attributing a limitation or observation to one.
- For a speculative or optional ask, say plainly that the odds are low. Make
  the risk concrete and cited, not "might be risky". Lead with the most
  promising concrete path.

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
  PSREF carries only global names, so a search on the name they typed lands in
  regional retail listings and never reaches PSREF. The machine type is the
  region-independent key, and `--speaker-info` prints it as `Product:`, e.g.
  `83SG`. Search that alone, restricted to `psref.lenovo.com`. Result titles
  read `<Family>, <Model name>, Model:<MTM>`. Verified both ways: `83SG` gives
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

## Write for the reporter

A reply is read once, by someone who wants to know what happened and what to
do next. It should never read like a research paper. Write the shortest reply
that gets them there, then cut again: a first draft runs long, and the
sentences that go first are the ones listed below.

- Lead with what is new. Every sentence must help the reporter act or
  understand their result. Cut the rest:
  - pleasantries that carry nothing, such as "Glad it works", "no obligation
    at all" or "a short yes or no is plenty";
  - a verdict on them such as "you were right": state the mechanism instead;
  - how we found or checked a fact, such as which containers we tested, which
    package version we read, or where we found a file they attached;
  - their own results replayed back to them, when the conclusion is what they
    need;
  - an explanation already given in an earlier comment: refer back to it in
    a clause instead of restating it;
  - a side topic they didn't take up, or a fix that doesn't affect them.
- Use plain words for what they will hear or see, and the reporter's own
  words for things. Our names for internals, such as hook, fragment, staged
  or bypass, stay out unless they type or click them. Keep a number only where
  it changes what they do or answers one they quoted, and drop tables of
  internals such as per-zone thresholds or the chain order. "Our ears hear
  bass differently at different volumes, so a volume difference alone can
  sound like a bass difference" explains a result in terms they can check.
- Match the register to the report. A technical report gets a technical
  reply, in the same plain structure.
- Shape: the @-mention, the result in a sentence or two, then the ask as a
  question. The question comes before any numbered step. Budget: two short
  paragraphs before the steps, and one clause of why per step.
- Outside the numbered steps, a reply is plain paragraphs. Bold labels are
  for three or more separate topics: a few words each, not a bold sentence.
  Make counts match the structure: a reply that says "three things" has
  exactly three sections.
- Thank them at most once per reply, for what this round brought. Skip it
  when the previous comment already thanked them for the same thing.
- Write in the first person ("I removed…", "helps me"). Be warm and
  credit-forward: attribute fixes to their report. Own the tool's bugs that
  hit their run in a sentence each.
- Put everything inline. Never use `<details>`.
- Use no footnotes by default. Keep one only for evidence worth keeping
  available that would break the body's flow, such as the source XML's name
  or the measurement behind a claim. What to do, what to listen for and why
  an ask matters always go inline.

## Make asks runnable and self-checkable

- Ask only for what changes our next step. A works report whose fixes have
  shipped needs no ask. While nothing works yet, ask for the single
  highest-value test. Once a fix exists, send every remaining ask in one
  reply, because each round trip costs the reporter a session.
- Number the steps they perform, gate later ones on earlier outcomes, and
  mark optional ones. Add nothing the plan didn't call for, such as an extra
  flag, package or check.
- Give copy-pasteable commands, not descriptions of what to change. Write one
  command per line, with no shell variables, loops, `&&` chains or clever
  quoting. Fenced blocks run exactly as written. For a capture battery
  that repeats one stimulus list across profiles, define the list once
  and give one loop per profile. That is shorter to paste and to read.
- Assume a reporter who takes on a capture can handle the basics, such as
  installing Python, opening a terminal in a folder or copying files to
  another machine. Name the prerequisite in a clause and leave the how to
  them, since they can ask: "With Python 3 installed, open PowerShell in the
  `to-windows` folder".
- Where a menu or button does the job, such as picking or deleting a preset
  in EasyEffects, name the menu path instead of writing a command.
- Start where their last run started: "from the folder you ran
  `dolby_to_easyeffects.py` in". Never invent their paths or folder layout,
  including the clone's folder name and config paths that differ between
  distro and Flatpak installs. A folder the ask itself creates is fine to
  name. Drop any argument their run auto-detected.
- Walk the ask as the reporter, on their setup, at the build they will run.
  Every step the tool needs must be there, such as `--force` over an
  installed conf or reloading the preset in EasyEffects. Check each flag's
  default at that build.
- Have them check a setting before changing it: "Verify the speakers are set
  to 48 kHz. If not, pick any 48000 Hz entry, and revert once done."
- Give one go-back line for a change that persists, such as a rebuilt preset
  or a removed package. Leave out cleanup of throwaway files. A toggle that
  would leave their sound broken, such as EasyEffects bypass, gets its line.
- Give each step whose outcome decides our next move its expected result, on
  screen or by ear. For on-screen results, quote the exact string the script
  prints, checked in the source. Name the outcomes that change what we do
  next, and for anything else ask them to paste the output. A step that only
  prepares, such as adding a flag, moving a file, rebuilding or restoring,
  needs no result line. If something they will see reads ambiguously, fix it
  in the code before asking.
- Design the ask so the reporter exercises the code path you need validated.
  Mention an easier workaround only as a failover, or the shortcut is what
  gets tested. In #33, autoload came first and the direct file path was the
  fallback.
- When a bug reporter has already worked around the bug, ask them to put the
  failing state back temporarily, so the fix is tested on the real failure.

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
  because availability is OEM-dependent. Look in every audio download the OEM
  publishes for the model, and extract nested archives.

## Formatting and citations

- Write each paragraph as **one long line**, with no hand-wrapping. GitHub
  reflows prose to the reader's window, and hard breaks make a one-word edit
  reflow the whole block. Fenced code blocks render verbatim, so break them
  exactly as they should be run. Indent a fenced block under a numbered step
  by three spaces, or GitHub ends the list there.
- Escape angle brackets outside backticks as `\<foo\>`, because a bare
  `<foo>` renders as an invisible HTML tag.
- Cite this repo's commits by full unquoted SHA, which GitHub auto-links.
  Backticks suppress the link, and a short SHA can be ambiguous. When telling
  a reporter a fix landed, confirm it is pushed, then cite the fix commit.
- Link this repo's files by full URL,
  `https://github.com/antoinecellerier/speaker-tuning-to-easyeffects/blob/master/<path>`,
  plus `#heading-anchor` where one exists. Relative paths don't link from a
  comment.
- Give anything outside this repo, such as kernel commits or other projects'
  files and trees, an explicit markdown URL, since it auto-links nowhere.
  Prefer mirrors that open without auth or anti-bot walls:
  `github.com/torvalds/linux` commit URLs over `git.kernel.org`, which blocks
  anonymous fetches.

## Cold read

Every reply gets a cold read: its reader has only the thread, and whoever
drafted it cannot tell what else the reply leans on.

Dispatch one `model: sonnet` reader with the absolute paths of the thread
file saved under "Process" and of the draft, and the prompt below: a less
capable reader is a more faithful proxy for the person replied to.
Give it nothing that was never posted: no plan, no summary, no definition of
a term.

Fill `<login>` with the account the reply is addressed to. Fill `<situation>`
with what they last did, as a full clause that says nothing about the answer:
"reported that the tool's sound plays on your headphones", "sent a pull
request", "asked how to install it". For a reply read by someone other than
the thread's author, describe that reader.

```
You are <login>. On an open-source tool's GitHub page, you <situation>, and
its maintainer has just replied. You can use a terminal and copy-paste
commands. Read these two files and run nothing:
<abs path of the thread>   the conversation so far; <login>'s messages are
                           yours
<abs path of the draft>    the new reply

Answer in under 200 words, about the new reply only. "None" is a full
answer to 2, 3 or 4:
1. In one sentence each: what are you being told, and what, if anything, are
   you being asked to do?
2. Which one step are you most likely to skip or get wrong, and why? Then any
   others.
3. Which words or sentences did you have to read twice?
4. What is in the reply that you did not need, or had already been told?
```

Fix what it misreads or would skip, starting with the step it names first,
and cut what it did not need. It lists something for almost any reply, so
weigh each item: an ambiguous step needs a fix, but every ask the plan calls
for stays, even one it would skip or did not need. A term it reads twice is
one of ours: replace it with the word `<login>` used, or the plainest one.

## Review the draft before showing it

Check each line, and revise and re-check until all hold:

- [ ] The thread is re-read, and nothing repeats what the user or the reporter
  already said.
- [ ] Every sentence helps the reporter act or understand their result.
- [ ] From the first two paragraphs and the numbered steps alone, the
  reporter can say what they are asked to do and why.
- [ ] The ask is a question placed before the steps, and bold labels appear
  only with three or more topics.
- [ ] The cold read is done, and re-run after any change to the ask or the
  steps.
- [ ] No `<details>`, and at most one footnote, holding evidence only.
- [ ] Every command copy-pastes as written from where their last run started,
  and every lasting change has a go-back line.
- [ ] Each deciding step says what result to expect, with on-screen strings
  checked in the source, and no step the tool needs is missing.
- [ ] No invented paths or folder names, and a menu step wherever a menu
  does the job.
- [ ] Every claim is validated this session or framed as a hypothesis, and
  past research is cited at the strength it reached.
- [ ] Cited SHAs are pushed, and every link is clickable.
- [ ] The footer is present.
