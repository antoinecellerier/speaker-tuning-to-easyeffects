"""Building the EasyEffects preset: the FIR kernel, the filters, the JSON.

Deliberately empty of code, like `lib/__init__.py` and `lib/data/__init__.py`
— a re-export here would drag every sibling in behind any single import and
make cycles reachable (`tests/test_layout.py`). Callers import the submodule
they want by name (`from lib.preset import fir`) — which also keeps numpy
behind the submodules that need it. `fir` is the only one that imports it
directly; `plugins` reaches it through `fir` and `build` through `plugins`, so
those three are what `dolby_to_easyeffects.py` binds in `_load_dsp`. `bands`
and `autoload` are stdlib-only and imported at the top of the file like any
other module.
"""
