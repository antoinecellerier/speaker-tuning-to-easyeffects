"""Everything that reads a vendor tuning outside the DAX3 XML.

Some OEMs voice the speaker outside Dolby, in their own audio processing
object, with its own per-device config file. Microsoft's Surface APO is read
into an `ApoLayer` the preset builder stacks on top of the Dolby chain
(`surface`, found by `stacked`); HP's Sonitude preset into one that is the
whole tuning (`sonitude`). A Dolby DAX2 preset, kept in a driver's `.inf`, is read into
the Dolby chain itself (`dax2`).

Deliberately empty of code, like `lib/dax/__init__.py`. A re-export here would
drag every sibling in behind any single import and make cycles reachable
(`tests/test_layout.py`). Callers import the submodule they want by name
(`from lib.apo import stacked`).
"""
