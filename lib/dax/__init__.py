"""Everything that reads Dolby's DAX3 tuning XML.

Deliberately empty of code, like `lib/__init__.py` and `lib/data/__init__.py`
— a re-export here would drag every sibling in behind any single import and
make cycles reachable (`tests/test_layout.py`). Callers import the submodule
they want by name (`from lib.dax import parse`).
"""
