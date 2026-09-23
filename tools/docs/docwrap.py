#!/usr/bin/env python3
"""Check or fix the wrapping of prose paragraphs in markdown files.

  tools/docs/docwrap.py check FILE...        report lines over 80 columns, ragged
                                             paragraphs, and split links or code spans
  tools/docs/docwrap.py fix   FILE[:START]... reflow the paragraphs check reports;
                                             START skips the lines before it

Paragraphs and list items at any depth are wrapped.
List markers and nesting indents are kept.
Fenced code, tables, headings, block quotes, HTML blocks and comments,
frontmatter, images and link definitions are never touched.
A link or a code span is never split; one longer than a line keeps a line.
A block with an unpaired backtick or a hard line break is reported, not wrapped.
`fix` changes whitespace only: it refuses to write a file whose text would
differ once whitespace runs are collapsed.
`check` exits 1 when it reports anything.
"""
import re
import sys
import textwrap

WIDTH = 80
# Stands in for a space inside a link or code span while wrapping.
NB = '\ue000'

SPAN = re.compile(r'!?\[[^\]]*\](?:\([^)]*\)|\[[^\]]*\])|(`+)(?!`).+?(?<!`)\1(?!`)')
ITEM = re.compile(r'( *)([-*+]|\d{1,9}[.)])( +)(?=\S)')
# An HTML block that may interrupt a paragraph (CommonMark types 1 to 6).
HTML = (r'<(?:!|\?|/?(?:address|article|aside|blockquote|body|center|details|dialog|div|'
        r'dl|dd|dt|fieldset|figure|footer|form|h[1-6]|header|hr|html|iframe|li|main|nav|'
        r'ol|p|pre|script|section|style|summary|table|tbody|td|textarea|tfoot|th|thead|'
        r'tr|ul)(?:[\s/>]|$))')
# A lone tag on its line starts an HTML block too, but only after a blank line.
LONE_TAG = re.compile(r'\s{0,3}</?[A-Za-z][A-Za-z0-9-]*(?:\s[^>]*)?/?>\s*$')
# A line that starts some other block, or ends a paragraph.
SPECIAL = re.compile(r'''\s*(?:$|\#{1,6}(?:\s|$)|```|~~~|>|\||!\[|\[[^\]]+\]:\s|
                         (?:[-*_]\s*){3,}$|=+\s*$|''' + HTML + ')', re.X | re.I)
# A word that would start a new block if the wrap put it first on a line.
MARKER = re.compile(r' (?=(?:[-*+]|\d+[.)]|\#{1,6}|=+|-+)(?: |$)|[>|]|<[A-Za-z/!?]|```|~~~)')


def protect(t):
    return SPAN.sub(lambda m: m.group(0).replace(' ', NB), t)


def blocks(lines, start=1):
    """Yield (i, j, first, rest, kind) for each wrappable run lines[i:j].

    FIRST and REST are the prefixes of the first and later wrapped lines.
    KIND is 'para' or 'item'.
    """
    i, n = 0, len(lines)
    if lines and lines[0].strip() == '---':
        i = next((k + 1 for k in range(1, n) if lines[k].strip() == '---'), n)
    fence, comment, html = None, False, False
    cols = []  # content columns of the list items above
    while i < n:
        line = lines[i]
        s = line.strip()
        i += 1
        if fence:
            fence = None if s.startswith(fence) else fence
            continue
        if comment:
            comment = '-->' not in line
            continue
        if html:
            html = bool(s)
            continue
        m = re.match(r'\s*(```|~~~)', line)
        if m:
            fence = m.group(1)
            continue
        if re.match(r'\s{0,3}<!--', line):
            comment = '-->' not in line.split('<!--', 1)[1]
            continue
        if re.match(r'\s{0,3}' + HTML, line, re.I) or LONE_TAG.match(line):
            html = True
            continue
        if not s:
            continue
        indent = len(line) - len(line.lstrip(' '))
        item = ITEM.match(line)
        if indent == 0 and not item:
            cols = []
        if SPECIAL.match(line):
            continue
        if item:
            first, kind = item.group(0), 'item'
            cols = [c for c in cols if c <= indent] + [len(first)]
            rest = ' ' * len(first)
        elif indent == 0 or any(c <= indent < c + 4 for c in cols):
            first = rest = ' ' * indent
            kind = 'para'
        else:
            continue  # an indented code block, or something unknown
        j = i
        while j < n and not SPECIAL.match(lines[j]) and not ITEM.match(lines[j]):
            j += 1
        if i >= start:
            yield i - 1, j, first, rest, kind
        i = j


def contents(block, first):
    """Each line's text without its prefix."""
    return [block[0][len(first):].strip()] + [x.strip() for x in block[1:]]


def hard_break(block):
    return any(re.search(r'(?: {2,}|\\)$', x) for x in block[:-1])


def problems(block, first, kind):
    """(indexes of over-long lines, ragged, split) for one block."""
    texts = contents(block, first)
    over = [k for k, x in enumerate(block)
            if len(x) > WIDTH and len(protect(texts[k]).split()) > 1]
    # A short line is ragged only if the next line's first unit would have fitted on it.
    ragged = (kind != 'item' and len(block) > 1 and all(len(x.split()) >= 3 for x in texts)
              and any(len(a) < WIDTH - 35 and len(a) + 1 + len(protect(t).split()[0]) <= WIDTH
                      for a, t in zip(block, texts[1:])))
    split = any(x.count('`') % 2 or re.search(r'\[[^\]]*$|\]\([^)]*$', x) for x in block)
    return over, ragged, split


def check(path):
    with open(path, encoding='utf-8') as f:
        lines = f.read().split('\n')
    bad = 0
    for i, j, first, _rest, kind in blocks(lines):
        over, ragged, split = problems(lines[i:j], first, kind)
        if ragged:
            print(f'{path}:{i + 1}: ragged paragraph')
        if split:
            print(f'{path}:{i + 1}: link or code span split across lines')
        for k in over:
            print(f'{path}:{i + k + 1}: over {len(lines[i + k])} cols')
        bad += len(over) + ragged + bool(split)
    return bad


def collapse(t):
    return re.sub(r'[ \t\n\r\f\v]+', ' ', t).strip()


def reflow(text, start=1, path='-'):
    """TEXT with the reported blocks rewrapped, and the number rewrapped."""
    lines = text.split('\n')
    out, last, count = [], 0, 0
    for i, j, first, rest, kind in blocks(lines, start):
        block = lines[i:j]
        if not any(problems(block, first, kind)):
            continue
        joined = ' '.join(contents(block, first))
        if joined.count('`') % 2 or hard_break(block):
            print(f'{path}:{i + 1}: unpaired backtick or hard line break, left as is')
            continue
        wrapped = textwrap.wrap(MARKER.sub(NB, protect(joined)), WIDTH,
                                initial_indent=first, subsequent_indent=rest,
                                break_long_words=False, break_on_hyphens=False)
        out += lines[last:i] + [w.replace(NB, ' ') for w in wrapped]
        last = j
        count += 1
    return '\n'.join(out + lines[last:]), count


def fix(path, start=1):
    with open(path, encoding='utf-8') as f:
        text = f.read()
    if NB in text:
        print(f'{path}: holds U+E000, which this tool uses internally; not fixed')
        return None
    new, count = reflow(text, start, path)
    if collapse(new) != collapse(text):
        print(f'{path}: the fix would change more than whitespace; not written')
        return None
    if new != text:
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new)
    return count


def main(argv):
    if len(argv) < 2 or argv[0] not in ('check', 'fix'):
        print(__doc__)
        return 2
    cmd, *paths = argv
    if cmd == 'check':
        return 1 if sum(check(p) for p in paths) else 0
    failed = False
    for spec in paths:
        path, _, start = spec.partition(':')
        count = fix(path, int(start or 1))
        failed |= count is None
        if count is not None:
            print(f'{path}: {count} blocks reflowed')
    return 1 if failed else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
