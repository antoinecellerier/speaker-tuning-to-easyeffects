"""Building the EasyEffects preset: the FIR kernel, the filters, the JSON.

Deliberately empty of code, like `lib/__init__.py` and `lib/data/__init__.py`
— a re-export here would drag every sibling in behind any single import and
make cycles reachable (`tests/test_layout.py`). Callers import the submodule
they want by name (`from lib.preset import fir`). That also keeps numpy
behind the submodules that need it. `fir` and `emit` import it at module
scope, and `emit` imports scipy too. `plugins` reaches it through `fir`, and
`build` through `plugins`. So all four arrive in `dolby_to_easyeffects.py`
only through the function-local imports in its `main()`. `bands` and
`autoload` are stdlib-only, so they may be imported at the top of the file
like any other module. The generator imports `autoload` there. It never names
`bands`, and reaches it through `plugins` and `build`.
"""
