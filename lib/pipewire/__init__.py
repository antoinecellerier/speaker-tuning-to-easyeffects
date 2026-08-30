"""Turning an EasyEffects preset into a PipeWire filter-chain conf.

Four layers, each importing only the one below it: ``plugins`` translates an EE
plugin block into an LV2 node, ``conf`` wires those nodes into a chain and
renders the SPA-JSON, ``install`` decides where it goes and what to say
afterwards, and ``checks`` is the ``--doctor`` that reads the result back off
the disk and out of the live graph. Beside them, ``clock`` is a dependency-free
leaf: the PipeWire clock and xrun reader both doctors print their
`=== PipeWire ===` section from, through `lib/report/doctor_layout.py`.

Deliberately empty of code, like `lib/__init__.py` and `lib/data/__init__.py`
— a re-export here would drag every sibling in behind any single import and
make cycles reachable (`tests/test_layout.py`). Callers import the submodule
they want by name (`from lib.pipewire import conf`).
"""
