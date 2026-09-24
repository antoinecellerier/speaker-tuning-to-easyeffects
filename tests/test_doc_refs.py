"""Citations of the docs from anywhere in the repo still land on something.

Finding numbers, cross-device § sections, numbered entries, quoted section
names, `r-` tags and markdown links are all checked against the doc they point
into. A design-notes citation resolves against design-notes.md and the
per-class research files under docs/research/, since units move between them.
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
    Numbered("Finding N",
             rf"Finding(?<!\wFinding)s?[ -](?P<n>{NUMS})",
             DESIGN_NOTES, None, r"#+ Finding (\d+):", None),
    Numbered("cross-device §N",
             rf"§(?P<n>{NUMS})",
             CROSS_DEVICE, None, r"## (\d+)\. ", r"(?i)cross-device"),
    Numbered("scaling entry N",
             r"(?:unvalidated-scaling|scaling|catalogue|design-notes(?:\.md)?`?,?"
             r"|scaling\s+factors[\"”],?)\s+entr(?:y|ies)\s+"
             rf"(?P<n>{NUMS})(?:\s*\((?P<sub>[a-z])\))?",
             DESIGN_NOTES, "Unvalidated converter scaling factors",
             r"#+ Entry (\d+):", None),
    Numbered("Follow-ups item N",
             rf"Follow-ups\s+items?\s+(?P<n>{NUMS})",
             DESIGN_NOTES, "Follow-ups to close the gap to DAX",
             r"(\d+)\.\s", None),
    Numbered("cross-device follow-up #N",
             r"§\d+\s*[/,]\s*follow-ups?\s+#(?P<n>\d+)",
             CROSS_DEVICE, "Open follow-ups", r"(\d+)\.\s", r"(?i)cross-device"),
)

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

    def items(self, section, item):
        merged = {}
        for _, doc in self.docs:
            for number, text in doc.items(section, item).items():
                merged.setdefault(number, text)
        return merged

    def sites(self, section, item):
        merged = {}
        for path, doc in self.docs:
            for number, lines in doc.sites(section, item).items():
                merged.setdefault(number, []).extend(
                    (path, line) for line in lines)
        return merged

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
            keep = line.strip() == "## Unreleased"
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


def check_numbered(source, cited):
    for row in NUMBERED:
        doc = cited(row.doc)
        items = doc.items(row.section, row.item)
        sites = doc.sites(row.section, row.item)
        where = _where(row.doc) + (f' "{row.section}"' if row.section else "")
        for match in re.finditer(row.cite, source.flat):
            line = source.line_of(match.start())
            if (row.context and source.path != row.doc
                    and not re.search(row.context, source.paragraph_text(line))):
                continue
            problem = None
            sub = match.groupdict().get("sub")
            for number in _numbers(match.group("n")):
                if number not in items:
                    problem = f"no item {number} in {where}"
                    break
                if len(sites[number]) > 1:
                    listed = ", ".join(f"{p}:{n}" for p, n in sites[number])
                    problem = (row.shape.replace("N", str(number))
                               + f" is defined at {listed}")
                    break
                if sub and f"({sub})" not in items[number]:
                    problem = f"item {number} in {where} has no ({sub})"
                    break
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


def check_quoted(source, docs, cited, known):
    for row, target, offset, match in _quoted_sites(source, known):
        if not match:
            continue
        names = [match.group("name")]
        while row.side == "after" and (
                more := AND_QUOTE.match(source.flat, match.end())):
            names.append(more.group("name"))
            match = more
        for name in names:
            if any(re.fullmatch(numbered.cite, name) for numbered in NUMBERED):
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


def scan(root, files):
    """Every doc citation in `files`, relative to `root`, resolved or not."""
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
    for path in sorted(filter(is_text, known)):
        try:
            text = (root / path).read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        if path == "CHANGELOG.md":
            text = unreleased_only(text)
        source = Source(path, text)
        citations.extend(check_numbered(source, cited))
        citations.extend(check_quoted(source, docs, cited, known))
        citations.extend(check_tag_cites(source, tags))
        if path.endswith(".md"):
            citations.extend(check_links(source, docs, known, directories,
                                         tags))
    return citations


def problems(citations):
    return [f"{c.path}:{c.line}: {c.text} → {c.problem}"
            for c in citations if c.problem]


# ---------------------------------------------------------------------------
# The real tree.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def real_citations():
    this_file = Path(__file__).resolve().relative_to(ROOT).as_posix()
    return scan(ROOT, [p for p in tracked_files(ROOT) if p != this_file])


def test_every_doc_citation_resolves(real_citations):
    """Each number, § section, quoted name or anchor finds its target doc."""
    broken = problems(real_citations)
    assert not broken, (
        "these cite a doc heading, list item or anchor that no longer exists — "
        "point the citation at the current heading:\n  " + "\n  ".join(broken))


def test_every_citation_shape_still_finds_sites(real_citations):
    """Each shape still matches a site, so a dead pattern fails here."""
    found = {c.shape for c in real_citations}
    shapes = {row.shape for row in NUMBERED + QUOTED} | {"markdown link"}
    assert shapes <= found, f"no site left for: {sorted(shapes - found)}"


def test_packages_readme_sections_exist():
    """The README sections `lib/packages.py` sends readers to still exist."""
    from lib import packages
    readme = Doc((ROOT / README).read_text(encoding="utf-8"))
    quoted = re.search(r'"([^"]+)"', packages.README_SECTION).group(1)
    named = re.search(r"README's (\w+) section",
                      packages.README_INSTALL_SECTION).group(1)
    assert readme.has_name(quoted), packages.README_SECTION
    assert readme.has_name(named), packages.README_INSTALL_SECTION


# ---------------------------------------------------------------------------
# The check itself, on a tree small enough to break on purpose.
# ---------------------------------------------------------------------------

DESIGN = """# Design notes

## Rejected approaches

- **Parametric-EQ approximation of the IEQ curve.** Rejected.

### Finding 1: DAX is non-LTI

### Unvalidated converter scaling factors (the class)

#### Entry 1: Dialog enhancer

Readings (a) (b).
"""

CITING = '''"""Why: design-notes "Rejected approaches", Finding 1, scaling
entry 1 (a). See design-notes "Rejected approaches → Parametric-EQ
approximation"."""
'''

LINKING = ("See [the log](design-notes.md#rejected-approaches) and "
           '[top](#other).\n\nAlso "Other" below.\n\n# Other\n')


def _tree(tmp_path, design=DESIGN, citing=CITING, linking=LINKING,
          files=None, **research):
    tree = {"docs/design-notes.md": design, "lib/x.py": citing,
            "docs/other.md": linking, **(files or {})}
    tree.update({f"docs/research/{name}.md": text
                 for name, text in research.items()})
    for name, text in tree.items():
        (tmp_path / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / name).write_text(text, encoding="utf-8")
    return problems(scan(tmp_path, list(tree)))


def test_an_intact_tree_has_no_problems(tmp_path):
    assert _tree(tmp_path) == []


def test_a_renamed_finding_heading_goes_red(tmp_path):
    broken = _tree(tmp_path, design=DESIGN.replace("Finding 1:", "Finding 2:"))
    assert broken == ["lib/x.py:1: Finding 1 → no item 1 in docs/design-notes.md"
                      " or docs/research/"]


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


def test_a_missing_entry_letter_goes_red(tmp_path):
    broken = _tree(tmp_path, design=DESIGN.replace("(a) (b)", "(b)"))
    assert broken == [
        "lib/x.py:1: scaling entry 1 (a) → item 1 in docs/design-notes.md "
        'or docs/research/ "Unvalidated converter scaling factors" has no (a)']


TAG = '<a id="r-dax-lti"></a>\n\n'
TAGGED = TAG + "### DAX is non-LTI\n"


def test_a_design_notes_citation_resolves_in_a_research_file(tmp_path):
    kept, finding, moved = DESIGN.partition("### Finding 1: DAX is non-LTI\n")
    citing = CITING + '# design-notes "Unvalidated converter scaling factors".\n'
    research = TAG + finding + moved
    assert _tree(tmp_path, design=kept, citing=citing, k=research) == []
    assert len(_tree(tmp_path, design=kept, citing=citing)) == 3


def test_a_number_defined_twice_across_the_set_goes_red(tmp_path):
    twice = "### Finding 1: DAX again\n"
    defined = ("Finding 1 → Finding 1 is defined at docs/design-notes.md:7, "
               "docs/research/k.md:1")
    assert _tree(tmp_path, k=twice) == [
        f"{site}: {defined}" for site in
        ("docs/design-notes.md:7", "docs/research/k.md:1", "lib/x.py:1")]


def test_a_duplicate_tag_goes_red(tmp_path):
    broken = _tree(tmp_path, design=DESIGN + "\n" + TAGGED, k=TAGGED)
    assert broken == ["docs/design-notes.md:15: r-dax-lti → tag r-dax-lti is "
                      "defined at docs/design-notes.md:15, docs/research/k.md:1"]


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
    changelog = ("# Changelog\n\n## Unreleased\n\n- See Finding 1.\n\n"
                 "## v1\n\n- See Finding 9.\n")
    assert _tree(tmp_path, files={"CHANGELOG.md": changelog}) == []
    unreleased = changelog.replace("Finding 1.", "Finding 8.")
    assert _tree(tmp_path, files={"CHANGELOG.md": unreleased}) == [
        "CHANGELOG.md:5: Finding 8 → no item 8 in docs/design-notes.md or "
        "docs/research/"]


def test_released_changelog_sections_are_not_read():
    text = "# Changelog\n\n## Unreleased\n\nkept\n\n## v1\n\ndropped\n"
    lines = unreleased_only(text).split("\n")
    assert len(lines) == 9 and lines[4] == "kept" and "dropped" not in lines


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
