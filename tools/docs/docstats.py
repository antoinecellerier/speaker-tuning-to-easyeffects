#!/usr/bin/env python3
"""Print prose metrics for markdown files, optionally before and after a change.

  tools/docs/docstats.py FILE...                  the working-tree files
  tools/docs/docstats.py -b REV [-c REV] FILE...  REV -> the working tree, or -> the -c REV

Prose excludes fenced code, tables, headings, HTML comments and frontmatter.
A sentence ends at . ! or ? before a space, and at the end of a paragraph or list item.
"Lines > 80" counts prose lines only.
"Rendered cells > 200" counts the text a reader sees: a link's text without its URL,
and no HTML tags, emphasis markers or backticks.
A path is read relative to the current directory, also at a revision.
"""
import argparse
import re
import subprocess
import sys

WIDTH = 80
ABBREV = re.compile(r'\b(?:e\.g|i\.e|vs|etc|cf|approx|incl|resp|Fig|No)\.', re.I)


def read(path, rev=None):
    if rev is None:
        with open(path, encoding='utf-8') as f:
            return f.read()
    done = subprocess.run(['git', 'show', f'{rev}:./{path}'], capture_output=True, text=True)
    return done.stdout if done.returncode == 0 else ''


def split(text):
    """(prose paragraphs as lists of lines, table cells) of a markdown text."""
    lines = text.split('\n')
    if lines and lines[0].strip() == '---':
        end = next((k for k in range(1, len(lines)) if lines[k].strip() == '---'), 0)
        lines = lines[end + 1:]
    text = re.sub(r'<!--.*?-->', '', '\n'.join(lines), flags=re.S)
    paras, cells, cur, fence = [], [], [], None
    for line in text.split('\n') + ['']:
        s = re.sub(r'^(?:>\s?)+', '', line.strip())
        m = re.match(r'```|~~~', s)
        if fence:
            fence = None if s.startswith(fence) else fence
            continue
        if s.startswith('|') and not re.match(r'\|?\s*:?-{3,}', s):
            cells += [c.strip() for c in s.strip('|').split('|')]
        # Anything but prose, and a new list item, ends the paragraph.
        other = not s or m or re.match(r'#{1,6}(\s|$)|\||<[A-Za-z/!]|!\[', s)
        if other or re.match(r'([-*+]|\d+[.)])\s', s):
            if cur:
                paras.append(cur)
            cur = []
        fence = m.group(0) if m else None
        if not other:
            cur.append(line)
    return paras, cells


def clean(line):
    """LINE without its indent and block-quote markers."""
    return re.sub(r'^\s*(?:>\s?)*', '', line)


def words(text):
    """Whitespace-separated words with a letter or digit, so a dash or marker is none."""
    return sum(1 for w in text.split() if re.search(r'\w', w))


LONG_CELL = 200


def rendered(cell):
    """CELL as a reader sees it: link and image text only, code text without its
    backticks, and no HTML tags or emphasis markers."""
    cell = re.sub(r'!?\[([^\]]*)\](?:\([^)]*\)|\[[^\]]*\])', r'\1', cell)
    parts = re.split(r'`([^`]*)`', cell)
    for k in range(0, len(parts), 2):  # the odd parts are code, kept as is
        p = re.sub(r'<([a-z][\w+.-]*:[^>\s]*)>', r'\1', parts[k])  # an autolink shows its URL
        p = re.sub(r'<[A-Za-z/!][^>]*>', '', p)
        parts[k] = re.sub(r'\*+|(?<!\w)_+|_+(?!\w)', '', p)
    return ''.join(parts)


def stats(text):
    paras, cells = split(text)
    prose = ' '.join(clean(x) for p in paras for x in p)
    sentences = []
    for p in paras:
        body = ABBREV.sub('X', re.sub(r'`[^`]*`', 'X', ' '.join(clean(x) for x in p)))
        body = re.sub(r'^([-*+]|\d+[.)])\s+', '', body)
        sentences += [words(s) for s in re.split(r'(?<=[.!?])[)"\'”’*_]*\s+', body)
                      if words(s)]
    sentences.sort()
    plain = re.sub(r'`[^`]*`|\]\([^)]*\)', '', prose)
    return {
        'lines': len(text.splitlines()),
        'words': words(text),
        'prose words': words(prose),
        'sentences': len(sentences),
        'words/sent': sum(sentences) / len(sentences) if sentences else 0.0,
        'p90 words/sent': sentences[int(0.9 * (len(sentences) - 1))] if sentences else 0,
        'em-dash asides': plain.count(' — '),
        'parentheticals': plain.count('('),
        'bold spans': len(re.findall(r'\*\*[^*]+\*\*', prose + ' '.join(cells))),
        f'rendered cells > {LONG_CELL}': sum(len(rendered(c)) > LONG_CELL for c in cells),
        f'lines > {WIDTH}': sum(len(x) > WIDTH for p in paras for x in p),
    }


def fmt(v):
    return f'{v:.1f}' if isinstance(v, float) else str(v)


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('-b', '--base', help='revision to compare from')
    ap.add_argument('-c', '--compare', help='revision to compare to (default: working tree)')
    ap.add_argument('files', nargs='+')
    a = ap.parse_args(argv)
    cols = []
    for path in a.files:
        after = stats(read(path, a.compare))
        before = stats(read(path, a.base)) if a.base else None
        cols.append((path, {k: fmt(v) if before is None else f'{fmt(before[k])} -> {fmt(v)}'
                            for k, v in after.items()}))
    names = list(cols[0][1])
    left = max(len(n) for n in names)
    widths = [max(len(p), *(len(c[n]) for n in names)) for p, c in cols]
    print(' ' * left + ''.join(f'  {p:>{w}}' for (p, _), w in zip(cols, widths)))
    for n in names:
        print(f'{n:{left}}' + ''.join(f'  {c[n]:>{w}}' for (_, c), w in zip(cols, widths)))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
