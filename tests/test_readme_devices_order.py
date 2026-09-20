"""README "Supported devices" table order trap.

The table grows one row per confirmed device report, mostly through
`.github/workflows/device-confirmed.yml`, whose prompt tells the bot where a
new row goes. That rule is only prose to a model, so this locks it: rows sort
by device name case-insensitively, with each run of digits compared as a
number — "X1 Carbon Gen 9" before "Gen 11", "X1" before "X13". A plain string
sort put Gen 11 and Gen 13 ahead of Gen 9. If the rule changes, change it in
the workflow prompt too.

That workflow also runs *this file* — by this path, as an exact-match entry in
its tool allowlist — before it opens its PR, so the bot can move a mis-sorted
row itself. Renaming or moving this file breaks that check silently: the run
just loses the ability to verify its row. Grep the workflow first.
"""

import re
from pathlib import Path

README = (Path(__file__).resolve().parent.parent / "README.md").read_text(
    encoding="utf-8")


def _device_names() -> list[str]:
    section = README.split("\n## Supported devices\n", 1)[1].split("\n## ", 1)[0]
    rows = [line for line in section.splitlines() if line.startswith("| ")]
    return [row.split("|")[1].strip() for row in rows[1:]]  # drop the header


def _natural_key(name: str) -> list:
    return [int(part) if part.isdigit() else part.casefold()
            for part in re.split(r"(\d+)", name)]


def test_key_compares_digit_runs_as_numbers():
    names = ["ThinkPad X13 Gen 6", "ThinkPad X1 Carbon Gen 11",
             "ThinkPad X1 Carbon Gen 9"]
    assert sorted(names, key=_natural_key) == [
        "ThinkPad X1 Carbon Gen 9", "ThinkPad X1 Carbon Gen 11",
        "ThinkPad X13 Gen 6"]


def test_supported_devices_are_in_number_aware_order():
    names = _device_names()
    assert len(names) > 30, "table not found — did the section heading change?"
    assert names == sorted(names, key=_natural_key)
