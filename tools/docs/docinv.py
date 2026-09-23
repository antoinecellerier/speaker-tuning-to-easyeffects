#!/usr/bin/env python3
"""Inventory the data tokens in a text file, or diff two inventories.

  tools/docs/docinv.py list  FILE             one token per line, with its count
  tools/docs/docinv.py diff  OLD NEW [NEW...] tokens of OLD missing from the NEW files
  tools/docs/docinv.py lines OLD NEW [NEW...] the same, grouped by OLD line
  --py (any command)                          read only comments and docstrings
  --counts (diff, lines)                      also list tokens, hedges and quantifiers
                                              that occur fewer times

A prose rewrite runs `diff OLD NEW` and then `diff NEW OLD`.
The second direction lists the tokens the rewrite invented.
Several NEW files are read as one text, for a section moved to another file.
A token counts as present when the new text holds it, whitespace aside.
A token repeated elsewhere hides a deleted copy; `--counts` shows those.
Hedges and quantifiers are compared by count only, and only with `--counts`.
A line break inside a paragraph counts as a space, so a rewrap changes no count.

Token classes: numbers with a unit, ranges, dates, commit hashes, issue refs,
URLs, repo paths, SSIDs, codec and amp ids, CLI flags, doc citations, counts,
versions, and the inner text of every code span.
"""
import ast
import bisect
import collections
import dataclasses
import io
import re
import sys
import tokenize

UNIT = (r'(?:dB\s?SPL|dBFS|dBTP|dB/oct|dB|LUFS|kHz|Hz|µs|us|ms|sec|s|min|%|'
        r'samples?|taps?|bands?|ppm|bits?|kB|KB|MB|GB|W|×|x)')
NUM = r'(?:(?<![\w.])[-+−±~≈<>≤≥])?(?<![\w.])\d+(?:[.,]\d+)?(?:/\d+)?'
# A figure may end on a sentence's full stop but not on a dot inside a longer
# figure: "since 8.0.7." holds 8.0.7, and "8.0.7.1" holds no 8.0.7.
END = r'(?!\w|\.\S)'
PATTERNS = [
    ('code', re.compile(r'(`+)(?!`)(.+?)(?<!`)\1(?!`)')),
    ('url', re.compile(r'https?://[^\s<>()\[\]`\'"]+')),
    ('date', re.compile(r'\b20\d\d-[01]\d(?:-[0-3]\d)?\b')),
    ('hash', re.compile(r'\b[0-9a-f]{7,40}\b')),
    ('issue', re.compile(r'(?:\b[\w.-]+/[\w.-]+#|(?<![\w&])#)\d{1,5}\b')),
    ('path', re.compile(r'(?<![\w./-])(?:~|\.{1,2})?/?[\w.][\w./-]*\.(?:py|md|xml|conf|'
                        r'json|irs|png|svg|sh|yml|yaml|txt|toml|wav|csv|ini)\b')),
    ('path', re.compile(r'(?<![\w./-])(?:\.claude|\.github|lib|tools|tests|docs)/'
                        r'(?:[\w.*-]+/?)*')),
    ('ssid', re.compile(r'\b[0-9A-Fa-f]{4}:[0-9A-Fa-f]{4}\b|\b[0-9A-F]{8}\b')),
    ('chip', re.compile(r'\b(?:ALC|CS35L|CS42L|TAS|TAC|RT|SN|MAX|AW|TFA|CX|ES)'
                        r'\d{2,5}[A-Z]?\b')),
    ('flag', re.compile(r'(?<![\w-])--(?:enable|disable)[ =][a-z][\w-]*')),
    ('flag', re.compile(r'(?<![\w-])--[a-z][a-z0-9-]*[a-z0-9]')),
    ('cite', re.compile(r'§\s?\d+(?:\.\d+)*|\b(?:Follow-ups )?(?i:findings?|entry|'
                        r'entries|items?|options?|phases?|rounds?|steps?|gen)\s\d+\b')),
    ('count', re.compile(r'\b\d+ of \d+\b|(?<![\w./])\d+/\d+(?!/)' + END + '|'
                         r'(?<![\w.#/–-])(?!(?:19|20)\d\d )\d[\d,]* (?:XMLs?|devices?|rows?|'
                         r'files?|machines?|laptops?|models?|entries|commits?|tests?|'
                         r'profiles?|voicings?|presets?|reports?|variants?|captures?|'
                         r'packages?)\b')),
    ('version', re.compile(r'\b(?:EE|EasyEffects|[Kk]ernel|Linux|PipeWire|WirePlumber|'
                           r'Python|Debian|Fedora|Ubuntu|SOF|numpy|scipy|Qt|GTK)'
                           r'\s?v?\d+(?:\.\d+)+(?:-rc\d+)?|\bv20\d\d\.\d\d(?:\.\d+)?\b|'
                           r'(?<![\w.])\d+(?:\.\d+){2,}' + END)),
    # A range goes before `num`: the figures inside it are not tokens of their own.
    ('range', re.compile(NUM + r'\s?(?:[-–]|\.\.)\s?' + NUM + r'\s?' + UNIT + r'(?!\w)')),
    ('num', re.compile(NUM + r'(?:\s|-)?' + UNIT + r'(?!\w)')),
    # Words that set how sure or how general a claim is. Compared by count only:
    # a lost hedge or an added universal is a changed claim, not a lost token.
    # Phrases that overlap another hedge get their own pattern, so both count.
    ('hedge', re.compile(r'\b(?:hypothes(?:is|es|i[sz]e|i[sz]ed)|'
                         r'unvalidated|unconfirmed|unverified|untested|unmeasured|'
                         r'not\s+yet|(?:un)?likely|plausib\w*|probabl\w*|suspect\w*|'
                         r'appears\s+to|seems|suggests|estimat\w*|one\s+device|'
                         r'single\s+device|dev\s+device|n\s?=\s?\d+)\b', re.I)),
    ('hedge', re.compile(r'\bleading\s+hypothesis\b|\bon\s+one\b', re.I)),
    ('quantifier', re.compile(r'\b(?:most|many|usually|often|rarely|rare|typical(?:ly)?|'
                              r'every|all|always|never|none|only)\b', re.I)),
]
COUNT_ONLY = ('hedge', 'quantifier')
# A table separator row holds no data.
SEPARATOR = re.compile(r'^\s*\|?\s*:?-{3,}')
UNICODE = str.maketrans({'µ': 'u', '−': '-', '–': '-', '×': 'x', '≈': '~'})


def norm(t, cls):
    """Display form: unicode folded, spaces collapsed, a figure glued to its unit."""
    t = re.sub(r'\s+', ' ', t.translate(UNICODE).strip())
    if cls == 'code':
        return t
    if cls in COUNT_ONLY:
        return f'[{cls}] {t.lower()}'
    if cls in ('num', 'range'):
        t = re.sub(r'^[~<>≤≥]', '', t.replace(' ', ''))
    return t.rstrip('.,;:')


def key(t):
    return re.sub(r'\s+', '', t)


def keep(t, cls):
    if cls == 'hash':
        return re.search(r'[a-f]', t) and re.search(r'\d', t)
    if cls == 'ssid' and ':' not in t:
        return re.search(r'[A-F]', t) and re.search(r'\d', t)
    return bool(t)


# A line that starts a block of its own rather than continuing a paragraph.
BLOCK = re.compile(r'\s*(?:[-*+]\s|\d+[.)]\s|#{1,6}(?:\s|$)|\||```|~~~)')


def paragraphs(text):
    """Yield (joined text, [(offset, line number, line), ...]) per paragraph.

    Paragraph lines are joined with a space; a table row, a heading and each
    line of fenced code stand alone.
    """
    runs, fence, joinable = [], None, False
    for ln, line in enumerate(text.split('\n'), 1):
        body = re.sub(r'^\s*(?:>\s?)*', '', line)
        s = body.strip()
        fenced = fence or re.match(r'```|~~~', s)
        if fence:
            fence = None if s.startswith(fence) else fence
        elif fenced:
            fence = fenced.group(0)
        if SEPARATOR.match(line):
            joinable = False
            continue
        if joinable and s and not fenced and not BLOCK.match(body):
            text_, parts = runs[-1]
            parts.append((len(text_) + 1, ln, line))
            runs[-1] = (text_ + ' ' + s, parts)
        else:
            runs.append((line if fenced else s, [(0, ln, line)]))
        joinable = bool(s) and not fenced and not re.match(r'\s*(?:#{1,6}(?:\s|$)|\|)', body)
    return runs


@dataclasses.dataclass
class Token:
    text: str
    cls: str
    lines: list  # (line number, line), once per line
    count: int = 0


def tokens(text):
    """Map each token key to its Token."""
    out = {}
    for run, parts in paragraphs(text):
        starts = [p[0] for p in parts]
        seen, ranges = set(), []
        for cls, rx in PATTERNS:
            for m in rx.finditer(run):
                group = 2 if cls == 'code' else 0
                start, end = m.span(group)
                t = norm(m.group(group), cls)
                k = key(t)
                if (not keep(t, cls) or (k, start) in seen
                        or any(a <= start and end <= b for a, b in ranges)):
                    continue
                seen.add((k, start))
                if cls == 'range':
                    ranges.append((start, end))
                tok = out.setdefault(k, Token(t, cls, []))
                tok.count += 1
                _, ln, line = parts[bisect.bisect_right(starts, start) - 1]
                if not tok.lines or tok.lines[-1][0] != ln:
                    tok.lines.append((ln, line.rstrip()))
    return out


def present(k, text, glued):
    """True when TEXT holds K verbatim, whitespace aside, not inside a longer word."""
    if k not in glued:
        return False
    rx = r'\s*'.join(re.escape(c) for c in k)
    if re.match(r'\w', k):
        rx = r'(?<![\w.])' + rx
    if re.search(r'\w$', k):
        rx += r'(?!\w)'
    return re.search(rx, text) is not None


def missing(old_text, new_text, counts=False):
    """Map each token of OLD_TEXT that NEW_TEXT lacks to a note.

    The note is empty for a token that is gone.
    With COUNTS, a token, hedge or quantifier that occurs fewer times is listed
    too, noted "old->new".
    """
    old, new = tokens(old_text), tokens(new_text)
    text = new_text.translate(UNICODE)
    glued = key(text)
    out = {}
    for k, tok in old.items():
        now = new[k].count if k in new else 0
        if tok.cls in COUNT_ONLY:
            if counts and now < tok.count:
                out[k] = f'{tok.count}->{now}'
        elif k not in new:
            if not present(k, text, glued):
                out[k] = ''
        elif counts and now < tok.count:
            out[k] = f'{tok.count}->{now}'
    return out


def py_text(source):
    """The comments and docstrings of SOURCE, each on its original line."""
    lines = source.split('\n')
    kept = [''] * len(lines)
    for tok in tokenize.generate_tokens(io.StringIO(source).readline):
        if tok.type == tokenize.COMMENT:
            kept[tok.start[0] - 1] += tok.string
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) or not node.body:
            continue
        first = node.body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            kept[first.lineno - 1:first.end_lineno] = lines[first.lineno - 1:first.end_lineno]
    # A comment's own "#" would read as a heading and stop the paragraph.
    return '\n'.join(re.sub(r'^#+(?=\s|$)', ' ', x) for x in kept)


def read(path, py=False):
    with open(path, encoding='utf-8') as f:
        text = f.read()
    return py_text(text) if py else text


def main(argv):
    args = [a for a in argv if a not in ('--py', '--counts')]
    if len(args) < 2 or args[0] not in ('list', 'diff', 'lines') or (
            args[0] != 'list' and len(args) < 3):
        print(__doc__)
        return 2
    cmd, old_path, *new_paths = args
    old_text = read(old_path, '--py' in argv)
    old = tokens(old_text)
    if cmd == 'list':
        for k, tok in sorted(old.items()):
            print(f'{tok.count:3d}  {tok.text}')
        return 0
    new_text = '\n'.join(read(p, '--py' in argv) for p in new_paths)
    gone = missing(old_text, new_text, '--counts' in argv)
    print(f'# old tokens {len(old)}, new tokens {len(tokens(new_text))}, missing {len(gone)}')
    label = {k: f'{old[k].text} {note}'.strip() for k, note in gone.items()}
    if cmd == 'diff':
        # Point at the first OLD line the new text no longer holds as is.
        kept = {line.strip() for line in new_text.split('\n')}
        for k in sorted(gone):
            ln, line = next((o for o in old[k].lines if o[1].strip() not in kept),
                            old[k].lines[0])
            print(f'{label[k]:28s}  L{ln}: {line.strip()[:110]}')
    else:
        byline = collections.defaultdict(list)
        for k in gone:
            for ln, _ in old[k].lines:
                byline[ln].append(label[k])
        for ln in sorted(byline):
            print(f'L{ln}: {", ".join(sorted(byline[ln]))}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
