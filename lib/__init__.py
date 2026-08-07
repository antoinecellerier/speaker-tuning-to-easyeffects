"""Everything the three root scripts are built from, but that none of them *is*.

The root of the repo holds only what a user types: ``dolby_to_easyeffects.py``,
``ee_to_pipewire.py``, ``dolby_to_pipewire.py``. Those paths are load-bearing
far outside the code — the README, the issue template, the argcomplete
registration line, two weekly workflows that rewrite tables inside the
generator — so they stay put, and everything they lean on lives here instead.

This package sits at the repo root rather than under ``src/`` because nothing
here is ever installed. The repo root is already on ``sys.path`` everywhere
that matters (a script's own directory, ``tests/conftest.py``, the
``tools/measure_*`` harnesses), so ``import lib.version`` resolves with no
path juggling at any of those call sites.

Deliberately empty of code: importing ``lib`` must cost nothing and must not
be able to introduce an import cycle, so submodules are imported by name
(``from lib import doctor``), never re-exported from here.
"""
