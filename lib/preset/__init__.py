"""Building the EasyEffects preset: the FIR kernel, the filters, the JSON.

Deliberately empty of code, like `lib/__init__.py` and `lib/data/__init__.py`
— a re-export here would drag every sibling in behind any single import and
make cycles reachable (`tests/test_layout.py`). Callers import the submodule
they want by name (`from lib.preset import fir`) — which also keeps numpy
behind the submodules that need it. `fir` and `emit` import it at module scope
(`emit` scipy too); `plugins` reaches it through `fir` and `build` through
`plugins`, so all four arrive in `dolby_to_easyeffects.py` only through the
function-local imports in its `main()`. That leaves `bands` and `autoload`,
which are stdlib-only and so may be imported at the top of the file like any
other module: `autoload` is, and `bands` the generator never names, reaching it
through `plugins` and `build`.
"""
