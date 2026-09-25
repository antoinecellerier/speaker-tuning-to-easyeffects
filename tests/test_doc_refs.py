"""Citations of the docs from anywhere in the repo still land on something.

Cross-device § sections and follow-ups, quoted section names, `r-` tags and
markdown links are all checked against the doc they point into. A
design-notes citation resolves against design-notes.md and the per-class
research files under docs/research/, since units move between them. The
research log's retired numbers (Finding N, scaling entry N, Follow-ups item
N) no longer resolve: a cite of one fails and names the unit's `r-` tag.
A cited commit of this repo must be in HEAD's history.
"""

import html
import re
import subprocess
import unicodedata
from bisect import bisect_right
from collections import namedtuple
from pathlib import Path, PurePosixPath
from urllib.parse import unquote

import pytest

ROOT = Path(__file__).resolve().parent.parent

DESIGN_NOTES = "docs/design-notes.md"
RESEARCH_DIR = "docs/research/"
CROSS_DEVICE = "docs/cross-device-findings.md"
README = "README.md"

NUMS = r"\d{1,2}(?!\d)(?:\s*(?:/|,|and|&|–|-)\s*§?\d{1,2}(?!\d))*"
QUOTE = r"[\"“](?P<name>[\w`][^\"”]{0,160}?)[\"”]"

Numbered = namedtuple("Numbered", "shape cite doc section item context")

NUMBERED = (
    Numbered("cross-device §N",
             rf"§(?P<n>{NUMS})",
             CROSS_DEVICE, None, r"## (\d+)\. ", r"(?i)cross-device"),
    Numbered("cross-device follow-up #N",
             r"§\d+\s*[/,]\s*follow-ups?(?:-|\s+)#(?P<n>\d+)",
             CROSS_DEVICE, "Open follow-ups", r"(\d+)\.\s", r"(?i)cross-device"),
)

# The research log's retired numbers. Each unit now carries an `r-` tag, and
# design-notes "Legacy numbers" maps each old number to it. `kind` is the
# number's spelling in that table. The patterns take every spelling the
# numbers were cited in, in any case. A word and its number are joined by a
# hyphen or by whitespace, which may wrap at one line break but never crosses
# a blank line. With nothing between them, as in a file or function name
# like `finding9()`, they are not a cite.
SEP = r"[^\S\n]*\n?[^\S\n]*"
WRAP = r"(?:[^\S\n]+\n?|\n)[^\S\n]*"
GAP = rf"(?:-|{WRAP})"
RETIRED_NUMS = (rf"\d{{1,2}}(?!\d)(?:{SEP}(?:/|,|and|&|–|-){SEP}"
                r"\d{1,2}(?!\d))*")

Retired = namedtuple("Retired", "shape cite kind")

RETIRED = (
    Retired("Finding N",
            rf"(?i)(?<!\w)findings?{GAP}#?(?P<n>{RETIRED_NUMS})",
            "Finding"),
    Retired("scaling entry N",
            rf"(?i)(?<![\w-])entr(?:y|ies){GAP}\(?#?(?P<n>{RETIRED_NUMS})",
            "entry"),
    Retired("Follow-ups item N",
            rf"(?i)(?<![\w-])follow-ups?{GAP}(?:items?{GAP})?#?"
            rf"(?P<n>{RETIRED_NUMS})", "Follow-ups item"),
)

# Phrases where "entry N" or "item N" numbers something other than a research
# unit: a boot or tool menu, a table, a quirk catalogue, a cross-device
# section's own entries. A match with one of these just before or just after
# it is not a cite.
NOT_A_CITE_BEFORE = re.compile(
    r"(?i)(?:\b(?:menu|boot|table|quirk(?:\s+catalogue)?)|§\d+)\s*\Z")
NOT_A_CITE_AFTER = re.compile(
    r"(?i)\s+(?:of|in)\s+the\s+(?:[\w-]+\s+)?(?:menu|table|quirk)")

Quoted = namedtuple("Quoted", "shape doc side pattern")

QUOTED = (
    Quoted('doc "Section"', "any doc", "after",
           rf"`?(?:\]\([^)\s]*\))?(?:'s)?[,:]?\s+(?:section\s+)?\(?{QUOTE}"),
    Quoted("doc's Section section", "any doc", "after",
           r"'s\s+(?P<name>[A-Z][\w-]*)\s+section\b"),
    Quoted('"Section" in doc', "doc path", "before",
           rf"{QUOTE},?\s+in\s+\[?`?\Z"),
    Quoted('"Section" above/below', "this doc", "anywhere",
           rf"{QUOTE}\)?,?\s+(?:above|below)\b"),
)

TEXT_SUFFIXES = {".md", ".py", ".yml", ".yaml", ".txt", ".sh", ".toml"}


def tracked_files(root):
    listing = subprocess.run(["git", "-C", str(root), "ls-files"],
                             capture_output=True, text=True, check=True)
    return listing.stdout.splitlines()


def is_text(path):
    return (PurePosixPath(path).suffix in TEXT_SUFFIXES
            or path.startswith(".claude/"))


# ---------------------------------------------------------------------------
# Reading a markdown doc: its anchors, its section names, its sections.
# ---------------------------------------------------------------------------

FENCE = re.compile(r"^\s*(`{3,}|~{3,})")
HEADING = re.compile(r"^ {0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$")
CODE_SPAN = re.compile(r"(`+)(.+?)\1")
LIST_MARKER = re.compile(r"^\s*(?:>\s*)*(?:[-*+]|\d+\.)\s+")
HTML_ANCHOR = re.compile(r"<[^>]*\b(?:id|name)=[\"']([^\"']+)[\"']")
SUMMARY = re.compile(r"<summary>(.*?)</summary>")


def fenced_lines(lines):
    """Which lines sit inside a fenced code block, fences included."""
    inside, fence, flags = False, "", []
    for line in lines:
        match = FENCE.match(line)
        if match and (not inside or match.group(1)[0] == fence[0]
                      and len(match.group(1)) >= len(fence)):
            inside, fence = not inside, match.group(1)
            flags.append(True)
        else:
            flags.append(inside)
    return flags


def _plain_prose(text):
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"\[([^\]]*)\]\[[^\]]*\]", r"\1", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = re.sub(r"\*\*|__", "", text)
    text = re.sub(r"(?<!\w)[*_](\S(?:.*?\S)?)[*_](?!\w)", r"\1", text)
    text = re.sub(r"\\(.)", r"\1", text)
    return html.unescape(text)


def plain(text):
    """Markdown inline text as GitHub renders it, formatting dropped."""
    out, last = [], 0
    for match in CODE_SPAN.finditer(text):
        out.append(_plain_prose(text[last:match.start()]))
        out.append(match.group(2).strip())
        last = match.end()
    out.append(_plain_prose(text[last:]))
    return "".join(out)


def github_slug(text):
    """GitHub's heading anchor: lowercase, punctuation dropped, spaces hyphenated."""
    kept = []
    for ch in plain(text).lower():
        category = unicodedata.category(ch)
        if ch in " -_" or category[0] in "LM" or category in ("Nd", "Nl", "Pc"):
            kept.append("-" if ch == " " else ch)
    return "".join(kept)


def norm(text):
    text = re.sub(r"\s*\{#[\w-]+\}\s*$", "", plain(text))
    text = re.sub(r"\s+", " ", text).strip().rstrip(".:,;…").rstrip(". ")
    return text.casefold()


def _starts_with(known, wanted):
    return known == wanted or (known.startswith(wanted)
                               and not known[len(wanted)].isalnum())


class Doc:
    def __init__(self, text):
        self.lines = text.splitlines()
        self.headings = []
        self.names = []
        self.titles = []
        self.anchors = set()
        self._items = {}
        self._sites = {}
        seen = {}
        block = []

        def close_block():
            if block:
                self.names.append(norm(" ".join(block))[:300])
                block.clear()

        fenced = fenced_lines(self.lines)
        for index, line in enumerate(self.lines):
            if fenced[index]:
                close_block()
                continue
            self.anchors.update(a.lower() for a in HTML_ANCHOR.findall(line))
            summaries = [norm(s) for s in SUMMARY.findall(line)]
            self.names += summaries
            self.titles += summaries
            heading = HEADING.match(line)
            if heading:
                close_block()
                slug = base = github_slug(heading.group(2))
                while slug in seen:
                    seen[base] += 1
                    slug = f"{base}-{seen[base]}"
                seen.setdefault(base, 0)
                seen.setdefault(slug, 0)
                self.anchors.add(slug)
                name = norm(heading.group(2))
                self.headings.append((index, len(heading.group(1)), name))
                variants = [name, name.partition(": ")[2],
                            re.sub(r"^\d+\.\s+", "", name)]
                self.names += variants
                self.titles += variants
            elif not line.strip() or line.lstrip().startswith("<"):
                close_block()
            elif line.lstrip().startswith("|"):
                close_block()
                cell = line.strip().strip("|").split("|")[0]
                if cell.strip(" :-"):
                    self.names.append(norm(cell))
            else:
                if LIST_MARKER.match(line):
                    close_block()
                    line = LIST_MARKER.sub("", line)
                block.append(re.sub(r"^\s*(?:>\s*)*", "", line))
        close_block()

    def has_name(self, name, strict=False):
        """Whether a heading, <summary>, paragraph or table row starts `name`.

        `strict` accepts only a heading or a <summary>.
        """
        wanted = norm(name)
        pool = self.titles if strict else self.names
        return bool(wanted) and any(_starts_with(n, wanted) for n in pool)

    def has_path(self, name, strict=False):
        """A quoted "A → B" also names B inside A's section.

        `strict` applies to the whole name and to A, never to B: B is usually
        a bullet label inside A.
        """
        if self.has_name(name, strict):
            return True
        parts = [p for p in re.split(r"\s*(?:→|->)\s*", name) if p.strip()]
        section = self.section(parts[0]) if len(parts) > 1 else None
        if section is None:
            return False
        inner = Doc("\n".join(self.lines[section[0]:section[1]]))
        return all(inner.has_name(part) for part in parts[1:])

    def section(self, name):
        """(start, end) line span of the first heading named `name`."""
        wanted = norm(name)
        for position, (index, level, text) in enumerate(self.headings):
            if _starts_with(text, wanted):
                ends = [i for i, depth, _ in self.headings[position + 1:]
                        if depth <= level]
                return index, (ends or [len(self.lines)])[0]
        return None

    def items(self, section, item):
        """Item numbers, and their text, of a section's numbered items.

        An item's text runs from its line up to the next item or heading.
        """
        key = (section, item)
        if key not in self._items:
            span = self.section(section) if section else (0, len(self.lines))
            found, text, sites = {}, None, {}
            for index, line in enumerate(
                    self.lines[span[0]:span[1]] if span else (),
                    span[0] + 1 if span else 1):
                match = re.match(item, line)
                if match:
                    sites.setdefault(int(match.group(1)), []).append(index)
                    text = found.setdefault(int(match.group(1)), [line])
                elif HEADING.match(line):
                    text = None
                elif text is not None:
                    text.append(line)
            self._items[key] = {n: "\n".join(t) for n, t in found.items()}
            self._sites[key] = sites
        return self._items[key]

    def sites(self, section, item):
        """Item number → every line that opens an item with that number."""
        self.items(section, item)
        return self._sites[(section, item)]


class DocSet:
    """Several docs read as one: a design-notes citation may land in any."""

    def __init__(self, docs):
        self.docs = [(path, doc) for path, doc in docs if doc]

    def has_path(self, name, strict=False):
        return any(doc.has_path(name, strict) for _, doc in self.docs)


# ---------------------------------------------------------------------------
# Reading a citing file: its prose, flattened so a citation may wrap lines.
# ---------------------------------------------------------------------------

Citation = namedtuple("Citation", "shape path line text problem")


def unreleased_only(text):
    """CHANGELOG.md with every released section blanked, line numbers kept."""
    lines, keep = text.splitlines(), False
    for index, line in enumerate(lines):
        if line.startswith("## "):
            keep = bool(re.match(r"## \[?Unreleased\b", line))
        if not keep:
            lines[index] = ""
    return "\n".join(lines)


PROSE_MD = re.compile(r"^[ \t]*(?:>[ \t]*)*", re.MULTILINE)
PROSE_CODE = re.compile(r"^[ \t]*(?:(?:#|//)+[ \t]?)?", re.MULTILINE)


class Source:
    def __init__(self, path, text):
        self.path = path
        marker = PROSE_MD if path.endswith(".md") else PROSE_CODE
        self.lines = text.splitlines()
        self.flat = marker.sub("", "\n".join(self.lines))
        self.starts, self.paragraph, number = [0], [], 0
        for line in self.flat.split("\n"):
            self.starts.append(self.starts[-1] + len(line) + 1)
            number += not line.strip()
            self.paragraph.append(number)

    def line_of(self, offset):
        return bisect_right(self.starts, offset)

    def paragraph_text(self, line):
        wanted = self.paragraph[line - 1]
        return "\n".join(text for text, number in zip(self.lines, self.paragraph)
                         if number == wanted)


def _numbers(spec):
    numbers = []
    for part in re.split(r"\s*(?:/|,|and|&)\s*", spec):
        bounds = [int(n) for n in re.findall(r"\d+", part)]
        if len(bounds) == 2 and bounds[0] < bounds[1] <= bounds[0] + 20:
            numbers.extend(range(bounds[0], bounds[1] + 1))
        else:
            numbers.extend(bounds)
    return numbers


def _one_line(text):
    return re.sub(r"\s+", " ", text).strip()


def _where(target):
    return target + (" or docs/research/" if target == DESIGN_NOTES else "")


def check_numbered(source, docs):
    for row in NUMBERED:
        doc = docs(row.doc)
        items = doc.items(row.section, row.item) if doc else {}
        sites = doc.sites(row.section, row.item) if doc else {}
        where = row.doc + (f' "{row.section}"' if row.section else "")
        for match in re.finditer(row.cite, source.flat):
            line = source.line_of(match.start())
            if (row.context and source.path != row.doc
                    and not re.search(row.context, source.paragraph_text(line))):
                continue
            problem = None
            for number in _numbers(match.group("n")):
                if number not in items:
                    problem = f"no item {number} in {where}"
                    break
                if len(sites[number]) > 1:
                    listed = ", ".join(f"{row.doc}:{n}" for n in sites[number])
                    problem = (row.shape.replace("N", str(number))
                               + f" is defined at {listed}")
                    break
            yield Citation(row.shape, source.path, line,
                           _one_line(match.group(0)), problem)


def _overlaps(match, spans):
    return any(start < match.end() and match.start() < end
               for start, end in spans)


def check_retired(source, legacy, skip, quoted):
    """Each cite of a retired number, which fails naming the tag to use.

    `legacy` maps an old number to its tag, `skip` holds the line numbers of
    the Legacy numbers table's rows, and `quoted` the (start, end) spans of
    the quoted section names in `source`: a number inside one is part of a
    name. A cross-device §N or follow-up #N is not a retired number.
    """
    flat = source.flat
    cross_device = [m.span() for row in NUMBERED
                    for m in re.finditer(row.cite, flat)]
    for row in RETIRED:
        for match in re.finditer(row.cite, flat):
            line = source.line_of(match.start())
            if (line in skip or _overlaps(match, cross_device)
                    or NOT_A_CITE_BEFORE.search(
                        flat, max(0, match.start() - 40), match.start())
                    or NOT_A_CITE_AFTER.match(flat, match.end())
                    or any(start <= match.start() and match.end() <= end
                           for start, end in quoted)):
                continue
            olds = [f"{row.kind} {n}" for n in _numbers(match.group("n"))]
            unknown = [old for old in olds if old not in legacy]
            if unknown:
                problem = (f"a retired number, and design-notes "
                           f'"{LEGACY_SECTION}" has no {", ".join(unknown)}')
            else:
                problem = ("a retired number: cite "
                           + " / ".join(f"`{legacy[old]}`" for old in olds)
                           + f' (design-notes "{LEGACY_SECTION}")')
            yield Citation(row.shape, source.path, line,
                           _one_line(match.group(0)), problem)


DOC_MENTION = re.compile(
    r"\.md(?![\w-])|design-notes|cross-device[- ]findings|README")
PATH_CHARS = set("abcdefghijklmnopqrstuvwxyz"
                 "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_./-")
ALIASES = {"design-notes": DESIGN_NOTES, "cross-device-findings": CROSS_DEVICE,
           "cross-device findings": CROSS_DEVICE, "README": README}


def doc_mentions(flat):
    """(start, end, token) for each doc a text names, by path or by alias."""
    for match in DOC_MENTION.finditer(flat):
        start, end = match.span()
        if match.group(0) == ".md":
            while start and flat[start - 1] in PATH_CHARS:
                start -= 1
            if start < match.start():
                yield start, end, flat[start:end]
            continue
        glued_before = start and flat[start - 1] in PATH_CHARS
        glued_after = end < len(flat) and (flat[end].isalnum() or flat[end] in "_.-")
        if not (glued_before or glued_after):
            yield start, end, match.group(0)


def _collapse(parts):
    out = []
    for part in parts:
        if part == "..":
            if out:
                out.pop()
        elif part != ".":
            out.append(part)
    return "/".join(out)


def resolve_doc(token, source_path, known):
    if token in ALIASES:
        return ALIASES[token]
    here = PurePosixPath(source_path).parent
    for base in (here, PurePosixPath(), PurePosixPath("docs")):
        path = _collapse((base / token).parts)
        if path in known:
            return path
    return None


def _quoted_sites(source, known):
    """(row, doc cited, offset, match) for each quoted name a source cites."""
    patterns = [(row, re.compile(row.pattern)) for row in QUOTED]
    flat = source.flat
    for start, end, token in doc_mentions(flat):
        target = resolve_doc(re.sub(r"\s+", " ", token), source.path, known)
        for row, pattern in patterns if target else ():
            if row.doc == "this doc" or (row.doc == "doc path"
                                         and not token.endswith(".md")):
                continue
            if row.side == "after":
                match = pattern.match(flat, end)
            else:
                match = pattern.search(flat, max(0, start - 300), start)
            yield row, target, start, match
    for row, pattern in patterns if source.path.endswith(".md") else ():
        if row.doc == "this doc":
            for match in pattern.finditer(flat):
                yield row, source.path, match.start(), match


AND_QUOTE = re.compile(rf",?\s+(?:and|or)\s+{QUOTE}")

# A quote into the research log names a heading or a <summary>, because a
# paragraph opening or a table row is not a stable place to cite. These real
# cites predate that rule and keep the looser match: a design-notes paragraph
# opening and a gain-staging table row.
LOOSE_QUOTED = frozenset({"the 47 hz deviation", "mbc upward compression"})


def in_research_log(path):
    return path == DESIGN_NOTES or path.startswith(RESEARCH_DIR)


def quoted_names(source, known):
    """(row, doc cited, offset, name match) for each quoted section name."""
    for row, target, offset, match in _quoted_sites(source, known):
        while match:
            yield row, target, offset, match
            match = row.side == "after" and AND_QUOTE.match(source.flat,
                                                            match.end())


def is_number_cite(name):
    return any(re.fullmatch(row.cite, name) for row in NUMBERED + RETIRED)


def check_quoted(source, docs, cited, known):
    for row, target, offset, match in quoted_names(source, known):
        name = match.group("name")
        if is_number_cite(name):
            continue
        doc = cited(target) if row.doc != "this doc" else DocSet(
            [(target, docs(target))])
        if in_research_log(target) and norm(name) not in LOOSE_QUOTED:
            problem = None if doc.has_path(name, strict=True) else (
                f"no heading or <summary> named that in {_where(target)}")
        else:
            problem = None if doc.has_path(name) else (
                "no heading, <summary>, paragraph or row named that in "
                + target)
        yield Citation(row.shape, source.path, source.line_of(offset),
                       _one_line(f'{target} "{name}"'), problem)


LINK = re.compile(r"\]\((<[^>]*>|[^)\s]*)(?:\s+[\"'][^\"']*[\"'])?\)")


def check_links(source, docs, known, directories, tags):
    fenced = fenced_lines(source.lines)
    for number, line in enumerate(source.lines, 1):
        if fenced[number - 1]:
            continue
        line = CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)
        for match in LINK.finditer(line):
            target = match.group(1).strip("<>")
            if not target or re.match(r"[a-zA-Z][\w+.-]*:|//", target):
                continue
            path, _, anchor = target.partition("#")
            path = unquote(path)
            if not path:
                resolved = source.path
            else:
                here = PurePosixPath(source.path).parent
                base = PurePosixPath() if path.startswith("/") else here
                resolved = _collapse((base / path.lstrip("/")).parts)
            problem = None
            if resolved not in known and resolved not in directories:
                problem = f"{resolved} is not a tracked file or directory"
            elif (anchor and resolved.endswith(".md")
                    and unquote(anchor).lower() not in docs(resolved).anchors):
                problem = f"no heading or anchor #{anchor} in {resolved}"
                tag = unquote(anchor).lower()
                if tag in tags:
                    problem += f"; the tag is in {tags[tag][0][0]}"
            yield Citation("markdown link", source.path, number, target, problem)


TAG_SLUG = re.compile(r"r-[a-z]+(?:-[a-z]+)*")
TAG_ANCHOR = re.compile(r"<[^>]*\b(?:id|name)=[\"'](r-[^\"']*)[\"']")
# A shell string escapes its backticks, so a trailing backslash is not the tag's.
TAG_CITE = re.compile(r"`(r-[^`\s<][^`\s]*?)\\?`")
TAG_FRAGMENT = re.compile(r"\.md#r-")
MALFORMED_TAG = "a tag is r- plus lowercase words joined by hyphens, no digits"


def _blank_code(line):
    return CODE_SPAN.sub(lambda m: " " * len(m.group(0)), line)


def tag_definitions(docs, known):
    """Tag → [(file, line)] of each `r-` id or name in a doc under docs/,
    and a Citation for each one not written as the convention asks."""
    tags, citations = {}, []
    for path in sorted(p for p in known
                       if p.startswith("docs/") and p.endswith(".md")):
        doc = docs(path)
        lines = doc.lines if doc else []
        fenced = fenced_lines(lines)
        for index, line in enumerate(lines):
            if fenced[index]:
                continue
            for tag in TAG_ANCHOR.findall(_blank_code(line)):
                tags.setdefault(tag, []).append((path, index + 1))
                problem = None
                if not TAG_SLUG.fullmatch(tag):
                    problem = MALFORMED_TAG
                elif not (line.strip() == f'<a id="{tag}"></a>'
                          and index + 2 < len(lines)
                          and not lines[index + 1].strip()
                          and HEADING.match(lines[index + 2])):
                    problem = (f'write <a id="{tag}"></a> on its own line, '
                               "then a blank line, then the heading")
                citations.append(
                    Citation("r- tag", path, index + 1, tag, problem))
    return tags, citations


def check_tag_definitions(tags):
    for tag, sites in sorted(tags.items()):
        if len(sites) > 1:
            yield Citation("r- tag", sites[0][0], sites[0][1], tag,
                           f"tag {tag} is defined at "
                           + ", ".join(f"{p}:{n}" for p, n in sites))


def check_tag_cites(source, tags):
    fenced = fenced_lines(source.lines)
    for number, line in enumerate(source.lines, 1):
        if fenced[number - 1]:
            continue
        for match in TAG_CITE.finditer(line):
            tag = match.group(1)
            problem = (MALFORMED_TAG if not TAG_SLUG.fullmatch(tag) else
                       None if tag in tags else
                       f"no <a id=\"{tag}\"> under docs/")
            yield Citation("r- tag cite", source.path, number, tag, problem)
        if not source.path.endswith(".md"):
            for match in TAG_FRAGMENT.finditer(line):
                yield Citation("r- tag cite", source.path, number,
                               _one_line(line.strip()),
                               "outside markdown, cite a tag as the bare "
                               "backticked `r-…` token")


# design-notes "Legacy numbers" resolves each retired number to its unit's tag.
# The table is frozen, so its rows are pinned here too.
LEGACY_SECTION = "Legacy numbers"
LEGACY_ROWS = {
    "Finding 1": "r-dax-lti-behaviour",
    "Finding 2": "r-dax-phase-response",
    "Finding 3": "r-dax-response-vs-xml",
    "Finding 4": "r-ee-response-vs-xml",
    "Finding 5": "r-hf-shaping-block-audit",
    "Finding 6": "r-ao-sign-variant-matrix",
    "Finding 7": "r-xml-interpretation-hypotheses",
    "Finding 8": "r-dax-virtual-bass",
    "Finding 9": "r-ieq-amount-scaling",
    "Finding 10": "r-simplified-schema-ao-units",
    "entry 1": "r-dialog-enhancer-gain-ceiling",
    "entry 2": "r-surround-boost-stereo-base",
    "entry 3": "r-convolver-headroom-restore",
    "entry 4": "r-regulator-slope-ratio",
    "entry 5": "r-regulator-timbre-knee",
    "entry 6": "r-mbc-ratio-time-constants",
    "entry 7": "r-leveler-autogain-window",
    "entry 8": "r-peq-anti-clipping-trim",
    "entry 9": "r-soundwire-bass-enhancer-constants",
    "entry 10": "r-conservative-autogain-offsets",
    "entry 11": "r-fixed-dynamics-constants",
    "Follow-ups item 1": "r-single-block-xml-ab",
    "Follow-ups item 2": "r-hybrid-phase-matching",
    "Follow-ups item 3": "r-dax-leveler-approximation",
    "Follow-ups item 4": "r-fit-to-dax-capture",
    "Follow-ups item 5": "r-regulator-stress-amount",
}
LEGACY_ROW = re.compile(
    r"\|\s*(?P<kind>Finding|entry|Follow-ups item) (?P<n>\d+)\s*"
    r"\|\s*\[[^\]]*\]\([^)#]*#(?P<tag>[^)]*)\)\s*\|(?P<title>[^|]+)\|\s*$")
TABLE_RULE = re.compile(r"\|(?:\s*:?-+:?\s*\|)+")


def legacy_table(doc):
    """The Legacy numbers section's line span, and (line, text, row match or
    None) for each of its table rows, or None without that section."""
    span = doc.section(LEGACY_SECTION) if doc else None
    if span is None:
        return None
    rows = []
    lines = doc.lines[span[0]:span[1]] + [""]
    for offset, line in enumerate(lines[:-1]):
        if (not line.startswith("|") or TABLE_RULE.fullmatch(line.strip())
                or TABLE_RULE.fullmatch(lines[offset + 1].strip())):
            continue
        rows.append((span[0] + offset + 1, line, LEGACY_ROW.match(line)))
    return span, rows


def check_legacy_numbers(table, docs, tags, expected):
    """Each legacy row maps its number to the pinned tag, once, and that tag
    sits directly above the heading the row names."""
    if table is None:
        yield Citation("legacy number", DESIGN_NOTES, 1, LEGACY_SECTION,
                       f'no "{LEGACY_SECTION}" section')
        return
    span, rows = table
    seen = set()
    for number, line, row in rows:
        if not row:
            yield Citation("legacy number", DESIGN_NOTES, number, line,
                           "write | <old number> | [tag](#r-tag) | title |")
            continue
        old = f"{row['kind']} {row['n']}"
        problem = (f"a second row for {old}" if old in seen
                   else _legacy_problem(docs, tags, row, expected))
        seen.add(old)
        yield Citation("legacy number", DESIGN_NOTES, number, old, problem)
    for old in expected:
        if old not in seen:
            yield Citation("legacy number", DESIGN_NOTES, span[0] + 1, old,
                           f'no row for {old} in "{LEGACY_SECTION}"')


def _legacy_problem(docs, tags, row, expected):
    old, tag, title = f"{row['kind']} {row['n']}", row["tag"], row["title"]
    if old not in expected:
        return f'{old} is not a legacy number; "{LEGACY_SECTION}" is frozen'
    if tag != expected[old]:
        return f"{old} is {expected[old]}, not {tag}"
    if tag not in tags:
        return f'no <a id="{tag}"> under docs/'
    path, line = tags[tag][0]
    doc = docs(path)
    heading = HEADING.match(doc.lines[line + 1] if line + 1 < len(doc.lines)
                            else "")
    if not heading or heading.group(2) != title.strip():
        shown = doc.lines[line + 1].strip() if heading else "no heading"
        return f"{tag} sits above {shown!r}, not {title.strip()!r}"
    return None


def _heading_span(doc, index):
    """(start, end) lines of the heading at `index` and its body."""
    for position, (at, level, _) in enumerate(doc.headings):
        if at == index:
            ends = [i for i, depth, _ in doc.headings[position + 1:]
                    if depth <= level]
            return at, (ends or [len(doc.lines)])[0]
    return None


def unit_text(docs, tags, site):
    """A tagged unit's own text: its heading up to the next one as high,
    less any tagged unit nested inside it."""
    path, line = site
    doc = docs(path)
    span = _heading_span(doc, line + 1)
    if span is None:
        return ""
    nested = [(other - 1, _heading_span(doc, other + 1))
              for sites in tags.values() for where, other in sites
              if where == path and span[0] < other - 1 < span[1]]
    return "\n".join(
        text for index, text in enumerate(doc.lines[span[0]:span[1]], span[0])
        if not any(inner and start <= index < inner[1]
                   for start, inner in nested))


# Sub-item letters after a tag cite: "[…](…#r-x) (d)", "`r-x` (d, e)",
# "(d)–(f)", "item (z)".
TAG_LETTERS = re.compile(
    r"(?:\]\([^)\s]*#(r-[a-z-]+)\)|`(r-[a-z-]+)\\?`)\s*(?:items?\s+)?"
    r"(\([a-z]\)(?:\s*(?:,|/|–|-|and|to)\s*\([a-z]\))*"
    r"|\([a-z](?:\s*(?:,|/|–|-|and|to)\s*[a-z])*\))")
LETTER_SEP = re.compile(r"\s*(,|/|–|-|\band\b|\bto\b)\s*")


def _letters(spec):
    """The letters a "(d, e)" or "(d)–(f)" spec names, ranges expanded."""
    parts = LETTER_SEP.split(spec.replace("(", "").replace(")", "").strip())
    letters = [parts[0]]
    for sep, letter in zip(parts[1::2], parts[2::2]):
        if sep in ("–", "-", "to"):
            letters += [chr(c) for c in range(ord(letters[-1]) + 1,
                                              ord(letter) + 1)]
        else:
            letters.append(letter)
    return letters


def check_tag_letters(source, docs, tags):
    for match in TAG_LETTERS.finditer(source.flat):
        tag, spec = match.group(1) or match.group(2), match.group(3)
        if tag not in tags:
            continue
        text = unit_text(docs, tags, tags[tag][0])
        missing = [letter for letter in _letters(spec)
                   if not re.search(rf"(?<![\w)])\({letter}\)", text)]
        problem = (f"{tag} has no " + ", ".join(f"({m})" for m in missing)
                   if missing else None)
        yield Citation("tag sub-letter", source.path,
                       source.line_of(match.start()),
                       _one_line(f"{tag} {spec}"), problem)


# docinv's fixtures there tokenise the retired "Finding N" form on purpose.
RETIRED_EXEMPT = frozenset({"tests/test_docs_tools.py"})


def scan(root, files, legacy=None):
    """Every doc citation in `files`, relative to `root`, resolved or not.

    `legacy` maps each row design-notes "Legacy numbers" must hold to its
    tag; None skips that check.
    """
    root = Path(root)
    known = set(files)
    directories = {parent.as_posix() for p in files
                   for parent in PurePosixPath(p).parents if parent.parts}
    parsed = {}

    def docs(path):
        if path not in parsed:
            readable = path in known and (root / path).is_file()
            parsed[path] = (Doc((root / path).read_text(encoding="utf-8"))
                            if readable else None)
        return parsed[path]

    research = sorted(p for p in known
                      if p.startswith(RESEARCH_DIR) and p.endswith(".md"))

    def cited(path):
        group = [DESIGN_NOTES, *research] if path == DESIGN_NOTES else [path]
        return DocSet((p, docs(p)) for p in group)

    tags, citations = tag_definitions(docs, known)
    citations.extend(check_tag_definitions(tags))
    table = legacy_table(docs(DESIGN_NOTES))
    if legacy is not None:
        citations.extend(check_legacy_numbers(table, docs, tags, legacy))
    retired = {f"{row['kind']} {row['n']}": row["tag"]
               for _, _, row in (table[1] if table else ()) if row}
    for path in sorted(filter(is_text, known)):
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if path == "CHANGELOG.md":
            text = unreleased_only(text)
        source = Source(path, text)
        citations.extend(check_numbered(source, docs))
        citations.extend(check_quoted(source, docs, cited, known))
        citations.extend(check_tag_cites(source, tags))
        citations.extend(check_tag_letters(source, docs, tags))
        if path not in RETIRED_EXEMPT:
            skip = ({number for number, _, _ in table[1]}
                    if table and path == DESIGN_NOTES else ())
            quoted = [match.span("name") for *_, match
                      in quoted_names(source, known)
                      if not is_number_cite(match.group("name"))]
            citations.extend(check_retired(source, retired, skip, quoted))
        if path.endswith(".md"):
            citations.extend(check_links(source, docs, known, directories,
                                         tags))
    return citations


def problems(citations):
    return [f"{c.path}:{c.line}: {c.text} → {c.problem}"
            for c in citations if c.problem]


# ---------------------------------------------------------------------------
# Commit hashes.
# ---------------------------------------------------------------------------

# 7 to 40 hex characters with at least one digit and one letter, not part of a
# path, URL, anchor or longer word.
COMMIT_HASH = re.compile(r"(?<![\w/#.-])(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])"
                         r"[0-9a-f]{7,40}(?![\w/-])")


def _git(root, *args, stdin=None):
    return subprocess.run(["git", "-C", str(root), *args], input=stdin,
                          capture_output=True, text=True, check=True).stdout


def stale_commit_cites(root, files):
    """Each cite of one of this repo's commits that HEAD's history lacks.

    Rebasing unpushed commits gives them new ids, and text written before the
    rebase keeps the old ones, which then resolve nowhere on GitHub. Only a
    hash this clone knows as a commit is checked. Another project's commit, a
    checksum, an old id git has since pruned, and anything in a shallow clone
    such as CI's all pass unchecked.
    """
    sites = {}
    for path in files:
        if not is_text(path) or not (root / path).is_file():
            continue
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        if path == "CHANGELOG.md":
            text = unreleased_only(text)
        for number, line in enumerate(text.splitlines(), 1):
            for match in COMMIT_HASH.finditer(line):
                sites.setdefault(match.group(), []).append(f"{path}:{number}")
    names = sorted(sites)
    kinds = _git(root, "cat-file", "--batch-check=%(objecttype) %(objectname)",
                 stdin="".join(f"{name}\n" for name in names)).splitlines()
    history = set(_git(root, "rev-list", "HEAD").split())
    return [f"{site}: {name} → a commit HEAD's history lacks; cite the commit "
            "that replaced it (same subject)"
            for name, kind in zip(names, kinds)
            if kind.split()[0] == "commit" and kind.split()[1] not in history
            for site in sites[name]]


# ---------------------------------------------------------------------------
# The real tree.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_citations():
    this_file = Path(__file__).resolve().relative_to(ROOT).as_posix()
    return scan(ROOT, [p for p in tracked_files(ROOT) if p != this_file],
                legacy=LEGACY_ROWS)


def test_every_doc_citation_resolves(real_citations):
    """Each § section, quoted name, tag or anchor finds its target doc, and no
    retired number is cited."""
    broken = problems(real_citations)
    assert not broken, (
        "these cite a doc heading, list item or anchor that no longer exists, "
        "or a retired number — fix each as its message says:\n  "
        + "\n  ".join(broken))


def test_every_citation_shape_still_finds_sites(real_citations):
    """Each shape still matches a site, so a dead pattern fails here."""
    found = {c.shape for c in real_citations}
    shapes = ({row.shape for row in NUMBERED + QUOTED}
              | {"markdown link", "legacy number", "tag sub-letter"})
    assert shapes <= found, f"no site left for: {sorted(shapes - found)}"


def test_every_cited_commit_is_in_the_history():
    """No text cites a commit id a rebase left behind."""
    this_file = Path(__file__).resolve().relative_to(ROOT).as_posix()
    stale = stale_commit_cites(
        ROOT, [p for p in tracked_files(ROOT) if p != this_file])
    assert not stale, "\n  ".join(["stale commit ids:", *stale])


def test_a_commit_off_the_history_goes_red(tmp_path, monkeypatch):
    # Pinned identity and dates make every commit id the same on each run. A
    # fresh id's short form is all digits about one run in 26, and
    # COMMIT_HASH does not read that as a hash, so the test would flake.
    for who in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{who}_NAME", "t")
        monkeypatch.setenv(f"GIT_{who}_EMAIL", "t@t")
        monkeypatch.setenv(f"GIT_{who}_DATE", "2026-01-01T00:00:00Z")

    def commit(message):
        _git(tmp_path, "commit", "-q", "--allow-empty", "-m", message)
        short = _git(tmp_path, "rev-parse", "--short=7", "HEAD").strip()
        assert COMMIT_HASH.fullmatch(short), (
            f"{short} is not hash-shaped: change the pinned date or message")
        return short

    _git(tmp_path, "init", "-q", "-b", "main")
    kept = commit("kept")
    _git(tmp_path, "checkout", "-q", "-b", "side")
    left = commit("left behind")
    _git(tmp_path, "checkout", "-q", "main")
    (tmp_path / "notes.md").write_text(
        f"See `{kept}`, `{left}^` and 0123456789abcdef, another project's.\n",
        encoding="utf-8")
    assert stale_commit_cites(tmp_path, ["notes.md"]) == [
        f"notes.md:1: {left} → a commit HEAD's history lacks; cite the commit "
        "that replaced it (same subject)"]


def test_docs_index_links_every_doc():
    """`docs/README.md` links every doc, as CLAUDE.md "Docs are layered" says,
    so a new page cannot go missing from the index."""
    index = (ROOT / "docs/README.md").read_text(encoding="utf-8")
    targets = {unquote(t.split("#")[0]).rstrip("/")
               for t in re.findall(r"\]\(([^)\s]+)\)", index)}
    docs = {path.name for path in (ROOT / "docs").glob("*.md")
            if path.name != "README.md"} | {"research"}
    assert docs <= targets, f"not in docs/README.md: {sorted(docs - targets)}"


def test_packages_doc_sections_exist():
    """The doc sections `lib/packages.py` sends readers to still exist."""
    from lib import packages
    readme = Doc((ROOT / README).read_text(encoding="utf-8"))
    quoted, doc = re.search(r'"([^"]+)" section of (\S+)',
                            packages.PLUGINS_SECTION).groups()
    named = re.search(r"README's (\w+) section",
                      packages.README_INSTALL_SECTION).group(1)
    plugins = Doc((ROOT / doc).read_text(encoding="utf-8"))
    assert plugins.has_name(quoted), packages.PLUGINS_SECTION
    assert readme.has_name(named), packages.README_INSTALL_SECTION


# ---------------------------------------------------------------------------
# The check itself, on a tree small enough to break on purpose.
# ---------------------------------------------------------------------------

DESIGN = """# Design notes

## Rejected approaches

- **Parametric-EQ approximation of the IEQ curve.** Rejected.

### DAX is non-LTI

### Unvalidated converter scaling factors (the class)

<a id="r-dialog"></a>

#### Dialog enhancer

Readings (a) (b).
"""

CITING = '''"""Why: design-notes "Rejected approaches", `r-dialog`
(a). See design-notes "Rejected approaches → Parametric-EQ
approximation"."""
'''

LINKING = ("See [the log](design-notes.md#rejected-approaches) and "
           '[top](#other).\n\nAlso "Other" below.\n\n# Other\n')


def _tree(tmp_path, design=DESIGN, citing=CITING, linking=LINKING,
          files=None, legacy=None, **research):
    tree = {"docs/design-notes.md": design, "lib/x.py": citing,
            "docs/other.md": linking, **(files or {})}
    tree.update({f"docs/research/{name}.md": text
                 for name, text in research.items()})
    for name, text in tree.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    return problems(scan(tmp_path, list(tree), legacy))


def test_an_intact_tree_has_no_problems(tmp_path):
    assert _tree(tmp_path) == []


def test_a_renamed_quoted_section_goes_red(tmp_path):
    renamed = DESIGN.replace("Rejected approaches", "Declined ideas")
    broken = _tree(tmp_path, design=renamed)
    assert ('lib/x.py:1: docs/design-notes.md "Rejected approaches" → no '
            "heading or <summary> named that in docs/design-notes.md or "
            "docs/research/") in broken


def test_a_quote_of_a_research_paragraph_goes_red(tmp_path):
    """Into the research log, a quote names a heading, not a paragraph."""
    citing = CITING + '# design-notes "Readings (a)".\n'
    assert _tree(tmp_path, citing=citing) == [
        'lib/x.py:4: docs/design-notes.md "Readings (a)" → no heading or '
        "<summary> named that in docs/design-notes.md or docs/research/"]


def test_a_broken_anchor_goes_red(tmp_path):
    broken = _tree(tmp_path, linking=LINKING.replace("-approaches", ""))
    assert broken == ["docs/other.md:1: design-notes.md#rejected → "
                      "no heading or anchor #rejected in docs/design-notes.md"]


def test_a_sub_letter_the_unit_lacks_goes_red(tmp_path):
    """A letter after a tag cite must appear as "(a)" in that unit's text."""
    missing = DESIGN.replace("(a) (b)", "(b)")
    assert _tree(tmp_path, design=missing) == [
        "lib/x.py:1: r-dialog (a) → r-dialog has no (a)"]
    later = missing + "\n### Later\n\n(a)\n"
    assert _tree(tmp_path, design=later) == [
        "lib/x.py:1: r-dialog (a) → r-dialog has no (a)"]
    linking = LINKING + "\n[dialog](design-notes.md#r-dialog) (b) (c)\n"
    assert _tree(tmp_path, linking=linking) == []
    linking = LINKING + "\n[dialog](design-notes.md#r-dialog) (c)\n"
    assert _tree(tmp_path, linking=linking) == [
        "docs/other.md:7: r-dialog (c) → r-dialog has no (c)"]


@pytest.mark.parametrize("spec, broken", [
    ("(a, b)", None), ("(a, c)", "(a, c) → r-dialog has no (c)"),
    ("(a)–(b)", None), ("(a)–(c)", "(a)–(c) → r-dialog has no (c)"),
    ("(a-d)", "(a-d) → r-dialog has no (c), (d)"), ("item (b)", None),
    ("items (a) and\n# (c)", "(a) and (c) → r-dialog has no (c)")])
def test_sub_letter_lists_and_ranges_are_each_checked(tmp_path, spec, broken):
    citing = CITING + f"# See `r-dialog` {spec}.\n"
    assert _tree(tmp_path, citing=citing) == (
        [] if broken is None else [f"lib/x.py:4: r-dialog {broken}"])


def test_a_sub_letter_counts_only_in_the_units_own_text(tmp_path):
    """A nested tagged unit's "(c)", or a call like "f(c)", is not the unit's."""
    nested = DESIGN + '\n<a id="r-inner"></a>\n\n##### Inner\n\n(c)\n'
    citing = CITING + "# See `r-dialog` (c).\n"
    assert _tree(tmp_path, design=nested, citing=citing) == [
        "lib/x.py:4: r-dialog (c) → r-dialog has no (c)"]
    untagged = DESIGN + "\n##### Inner\n\n(c)\n"
    assert _tree(tmp_path, design=untagged, citing=citing) == []
    called = DESIGN + "\nIt reads f(c).\n"
    assert _tree(tmp_path, design=called, citing=citing) == [
        "lib/x.py:4: r-dialog (c) → r-dialog has no (c)"]


TAG = '<a id="r-dax-lti"></a>\n\n'
TAGGED = TAG + "### DAX is non-LTI\n"


def test_a_design_notes_citation_resolves_in_a_research_file(tmp_path):
    kept, heading, moved = DESIGN.partition(
        "### Unvalidated converter scaling factors (the class)\n")
    citing = CITING + '# design-notes "Unvalidated converter scaling factors".\n'
    assert _tree(tmp_path, design=kept, citing=citing, k=heading + moved) == []
    assert len(_tree(tmp_path, design=kept, citing=citing)) == 2


def test_a_cross_device_section_defined_twice_goes_red(tmp_path):
    cross = {"docs/cross-device-findings.md": "## 3. Amps\n\n## 3. Pins\n"}
    citing = CITING + "# cross-device-findings §3.\n"
    assert _tree(tmp_path, citing=citing, files=cross) == [
        "lib/x.py:4: §3 → cross-device §3 is defined at "
        "docs/cross-device-findings.md:1, docs/cross-device-findings.md:3"]
    assert _tree(tmp_path, citing=citing.replace("§3", "§4"), files=cross) == [
        "lib/x.py:4: §4 → no item 4 in docs/cross-device-findings.md"]


def test_a_duplicate_tag_goes_red(tmp_path):
    broken = _tree(tmp_path, design=DESIGN + "\n" + TAGGED, k=TAGGED)
    assert broken == ["docs/design-notes.md:17: r-dax-lti → tag r-dax-lti is "
                      "defined at docs/design-notes.md:17, docs/research/k.md:1"]


def test_an_unknown_tag_goes_red(tmp_path):
    broken = _tree(tmp_path, citing=CITING + "# See `r-dax-lti`.\n")
    assert broken == ['lib/x.py:4: r-dax-lti → no <a id="r-dax-lti"> '
                      "under docs/"]
    assert _tree(tmp_path, citing=CITING + "# See `r-dax-lti`.\n",
                 k=TAGGED) == []


@pytest.mark.parametrize("slug", ["r-dax-lti3", "r-Dax-lti", "r-dax_lti",
                                  "r-dax-lti-"])
def test_a_malformed_tag_goes_red(tmp_path, slug):
    malformed = f'<a id="{slug}"></a>\n\n### DAX\n'
    cite = CITING + f"# See `{slug}`.\n"
    assert _tree(tmp_path, citing=cite, k=malformed) == [
        f"docs/research/k.md:1: {slug} → {MALFORMED_TAG}",
        f"lib/x.py:4: {slug} → {MALFORMED_TAG}"]


def test_a_tag_off_its_own_line_before_a_heading_goes_red(tmp_path):
    fix = ('write <a id="r-dax-lti"></a> on its own line, then a blank line, '
           "then the heading")
    for text in ('<a id="r-dax-lti"></a>\n### DAX\n',
                 '<a id="r-dax-lti"></a>\n\nA paragraph.\n',
                 'Text <a id="r-dax-lti"></a>\n\n### DAX\n',
                 '<span name="r-dax-lti"></span>\n\n### DAX\n'):
        assert _tree(tmp_path, k=text) == [
            f"docs/research/k.md:1: r-dax-lti → {fix}"], text


def test_a_tag_in_code_defines_nothing(tmp_path):
    cite = CITING + "# See `r-dax-lti`.\n"
    unknown = ['lib/x.py:4: r-dax-lti → no <a id="r-dax-lti"> under docs/']
    fenced = '```\n<a id="r-dax-lti"></a>\n\n### DAX\n```\n'
    spanned = 'Write `<a id="r-dax-lti"></a>` above the heading.\n'
    assert _tree(tmp_path, citing=cite, k=fenced) == unknown
    assert _tree(tmp_path, citing=cite, k=spanned) == unknown


def test_a_shell_escaped_tag_cite_resolves(tmp_path):
    cite = CITING + '# echo "see \\`r-dax-lti\\`"\n'
    assert _tree(tmp_path, citing=cite, k=TAGGED) == []


def test_a_tag_cite_in_a_fenced_block_is_not_read(tmp_path):
    linking = LINKING + "\n```\n`r-nowhere`\n```\n"
    assert _tree(tmp_path, linking=linking) == []


def test_a_tag_fragment_outside_markdown_goes_red(tmp_path):
    cite = CITING + "# docs/research/k.md#r-dax-lti\n"
    assert _tree(tmp_path, citing=cite, k=TAGGED) == [
        "lib/x.py:4: # docs/research/k.md#r-dax-lti → outside markdown, cite a "
        "tag as the bare backticked `r-…` token"]


def test_a_link_to_a_tag_in_the_wrong_file_goes_red(tmp_path):
    linking = LINKING + "\n[F1](design-notes.md#R-dax-lti)\n"
    broken = _tree(tmp_path, linking=linking, k=TAGGED)
    assert broken == ["docs/other.md:7: design-notes.md#R-dax-lti → no heading "
                      "or anchor #R-dax-lti in docs/design-notes.md; the tag is "
                      "in docs/research/k.md"]
    fixed = linking.replace("(design-notes.md#R-", "(research/k.md#r-")
    assert _tree(tmp_path, linking=fixed, k=TAGGED) == []


def test_released_changelog_sections_are_not_scanned(tmp_path):
    changelog = ("# Changelog\n\n## Unreleased\n\n- See `r-dialog`.\n\n"
                 "## v1\n\n- See Finding 9.\n")
    assert _tree(tmp_path, files={"CHANGELOG.md": changelog}) == []
    unreleased = changelog.replace("`r-dialog`.", "Finding 8.")
    assert _tree(tmp_path, files={"CHANGELOG.md": unreleased}) == [
        "CHANGELOG.md:5: Finding 8 → a retired number, and design-notes "
        '"Legacy numbers" has no Finding 8']


@pytest.mark.parametrize("heading", ["## Unreleased", "## [Unreleased]",
                                     "## Unreleased (next)"])
def test_released_changelog_sections_are_not_read(heading):
    text = f"# Changelog\n\n{heading}\n\nkept\n\n## v1\n\ndropped\n"
    lines = unreleased_only(text).split("\n")
    assert len(lines) == 9 and lines[4] == "kept" and "dropped" not in lines


LEGACY = ("## Where the research lives\n\n### Legacy numbers\n\n"
          "| Old number | Tag | Heading |\n|---|---|---|\n"
          "| Finding 1 | [`r-dax-lti`](#r-dax-lti) | DAX is non-LTI |\n"
          "| entry 1 | [`r-dialog`](#r-dialog) | Dialog enhancer |\n\n")
LEGACY_DESIGN = (DESIGN.replace("## Rejected", LEGACY + "## Rejected")
                 .replace("### DAX is non-LTI", TAG + "### DAX is non-LTI"))
LEGACY_EXPECTED = {"Finding 1": "r-dax-lti", "entry 1": "r-dialog"}
ENTRY_ROW = "| entry 1 | [`r-dialog`](#r-dialog) | Dialog enhancer |\n"


def test_the_real_legacy_table_pins_every_retired_number():
    assert len(LEGACY_ROWS) == 26
    assert len(set(LEGACY_ROWS.values())) == 26


def test_an_intact_legacy_table_has_no_problems(tmp_path):
    assert _tree(tmp_path, design=LEGACY_DESIGN, legacy=LEGACY_EXPECTED) == []


def test_a_missing_legacy_row_goes_red(tmp_path):
    design = LEGACY_DESIGN.replace(ENTRY_ROW, "")
    assert _tree(tmp_path, design=design, legacy=LEGACY_EXPECTED) == [
        'docs/design-notes.md:5: entry 1 → no row for entry 1 in '
        '"Legacy numbers"']


def test_a_duplicate_legacy_row_goes_red(tmp_path):
    design = LEGACY_DESIGN.replace(ENTRY_ROW, ENTRY_ROW * 2)
    assert _tree(tmp_path, design=design, legacy=LEGACY_EXPECTED) == [
        "docs/design-notes.md:11: entry 1 → a second row for entry 1"]


def test_a_legacy_row_mapping_another_tag_goes_red(tmp_path):
    """Swapping two rows' tags and titles keeps each tag above its title."""
    swapped = (LEGACY_DESIGN
               .replace("| Finding 1 | [`r-dax-lti`](#r-dax-lti) | "
                        "DAX is non-LTI |",
                        "| Finding 1 | [`r-dialog`](#r-dialog) | "
                        "Dialog enhancer |")
               .replace(ENTRY_ROW, "| entry 1 | [`r-dax-lti`](#r-dax-lti) | "
                                   "DAX is non-LTI |\n"))
    assert _tree(tmp_path, design=swapped, legacy=LEGACY_EXPECTED) == [
        "docs/design-notes.md:9: Finding 1 → Finding 1 is r-dax-lti, "
        "not r-dialog",
        "docs/design-notes.md:10: entry 1 → entry 1 is r-dialog, not r-dax-lti"]


def test_a_legacy_row_with_an_unknown_tag_goes_red(tmp_path):
    design = LEGACY_DESIGN.replace("[`r-dialog`](#r-dialog)",
                                   "[`r-dialogue`](#r-dialogue)")
    legacy = {**LEGACY_EXPECTED, "entry 1": "r-dialogue"}
    broken = _tree(tmp_path, design=design, legacy=legacy)
    assert ('docs/design-notes.md:10: entry 1 → no <a id="r-dialogue"> '
            "under docs/") in broken


def test_a_legacy_tag_above_another_heading_goes_red(tmp_path):
    renamed = LEGACY_DESIGN.replace("#### Dialog enhancer",
                                    "#### Dialogue enhancer")
    assert ("docs/design-notes.md:10: entry 1 → r-dialog sits above "
            "'#### Dialogue enhancer', not 'Dialog enhancer'") in _tree(
        tmp_path, design=renamed, legacy=LEGACY_EXPECTED)


def _retired(tmp_path, text, design=LEGACY_DESIGN):
    return _tree(tmp_path, design=design, linking=LINKING + "\n" + text + "\n")


def test_a_retired_number_goes_red_naming_its_tag(tmp_path):
    citing = CITING + "# See Finding 1, and scaling entries 1/2.\n"
    assert _tree(tmp_path, design=LEGACY_DESIGN, citing=citing) == [
        "lib/x.py:4: Finding 1 → a retired number: cite `r-dax-lti` "
        '(design-notes "Legacy numbers")',
        "lib/x.py:4: entries 1/2 → a retired number, and design-notes "
        '"Legacy numbers" has no entry 2']
    assert _retired(tmp_path, "Entry 1 and Follow-ups item 1.") == [
        "docs/other.md:7: Entry 1 → a retired number: cite `r-dialog` "
        '(design-notes "Legacy numbers")',
        "docs/other.md:7: Follow-ups item 1 → a retired number, and "
        'design-notes "Legacy numbers" has no Follow-ups item 1']


@pytest.mark.parametrize("text", [
    "entry 1", "(entry 1 (b))", "entry 1(b)", "entries 1–2", "Scaling entry 1",
    "scaling-factor entry 1", "finding 1", "Finding #1", "FINDING 1",
    "Findings-1", "Follow-up 1", "Follow-up #1", "follow-ups item 1",
    "Follow-up item 1", "Finding\n1", "Findings\n1 and 2", "entry-1",
    "entries-1/2", "Entries-1 and 2", "entry-#1", "Follow-up-1",
    "Follow-up-#1", "follow-ups-item-1", "Follow-ups item-1"])
def test_every_past_spelling_of_a_retired_number_goes_red(tmp_path, text):
    broken = _retired(tmp_path, f"See {text}.")
    assert len(broken) == 1 and "a retired number" in broken[0], broken


def test_a_hyphenated_compound_cite_names_every_number(tmp_path):
    assert _retired(tmp_path, "See entry-2 and entries-6/11.") == [
        "docs/other.md:7: entry-2 → a retired number, and design-notes "
        '"Legacy numbers" has no entry 2',
        "docs/other.md:7: entries-6/11 → a retired number, and design-notes "
        '"Legacy numbers" has no entry 6, entry 11']


def test_a_retired_number_never_spans_a_blank_line(tmp_path):
    assert _retired(tmp_path, "The Finding\n\n1 row.") == []
    assert _retired(tmp_path, "Findings 1 and\n\n2 more.") == [
        "docs/other.md:7: Findings 1 → a retired number: cite `r-dax-lti` "
        '(design-notes "Legacy numbers")']


@pytest.mark.parametrize("text", [
    "Entry 2 of the boot menu", "Menu Entry 3", "quirk catalogue entry 3",
    "pick menu entry 2", "the table entry 4", "cross-device §17 entry 3",
    "cross-device §3 / follow-up #2", "cross-device §3 / follow-up-#2",
    "the finding10 anchor", "`finding9()`", "finding10-ieq.png",
    "the entry2 row", "menu-entry-3", "the entry-point script"])
def test_a_number_that_is_not_a_research_unit_is_not_a_cite(tmp_path, text):
    broken = _retired(tmp_path, f"See {text}.")
    assert not [p for p in broken if "retired" in p], broken


def test_only_the_legacy_rows_are_exempt(tmp_path):
    """The Legacy numbers section's prose, and any heading added under it,
    are scanned like the rest of the doc."""
    design = LEGACY_DESIGN.replace(
        "### Legacy numbers\n\n", "### Legacy numbers\n\nFinding 1 moved.\n\n")
    assert _tree(tmp_path, design=design) == [
        "docs/design-notes.md:7: Finding 1 → a retired number: cite "
        '`r-dax-lti` (design-notes "Legacy numbers")']


def test_a_number_inside_a_quoted_section_name_is_not_a_cite(tmp_path):
    changelog = {"CHANGELOG.md": "# Changelog\n\n## Unreleased\n\n## v1\n\n"
                                 "- Finding 2 in context.\n"}
    citing = CITING + '# CHANGELOG.md "Finding 2 in context".\n'
    assert _tree(tmp_path, citing=citing, files=changelog) == []
    citing = CITING + '# design-notes "Finding 1".\n'
    assert _tree(tmp_path, design=LEGACY_DESIGN, citing=citing) == [
        "lib/x.py:4: Finding 1 → a retired number: cite `r-dax-lti` "
        '(design-notes "Legacy numbers")']


@pytest.mark.parametrize("heading, slug", [
    ("Limitations / known gaps", "limitations--known-gaps"),
    ("11. MI steering — dynamic profile", "11-mi-steering--dynamic-profile"),
    ("`volmax-boost` slot: [#23](https://x/23)", "volmax-boost-slot-23"),
    ("Über café_menu?", "über-café_menu"),
])
def test_github_slug(heading, slug):
    assert github_slug(heading) == slug


def test_duplicate_headings_get_numbered_anchors():
    doc = Doc("# Notes\n# Notes\n# Notes\n")
    assert {"notes", "notes-1", "notes-2"} <= doc.anchors
