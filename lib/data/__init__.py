"""Tables a machine writes, kept out of the code a human edits.

Two weekly workflows rewrite the contents of this package unattended —
``.github/workflows/kernel-release-table.yml`` and
``.github/workflows/speaker-quirks.yml`` (which writes both quirk tables) —
each running a deterministic ``tools/update_*.py`` script that locates a dict
literal by regex, splices a re-rendered body in, and opens a PR. That is a safe thing to do to a file
whose whole content is the table, and an uncomfortable one to do inside a
script somebody is editing by hand the same week.

Each module here is data plus, at most, the record type its rows are written
in: no probing, no policy, no decisions about what to tell the user. The
thresholds that turn a row into advice stay with the code that reads it, so a
machine rewrite can never move one.

Deliberately empty of code, like ``lib/__init__.py``: callers import the
submodule they want by name (``from lib.data import kernel_releases``).
"""
