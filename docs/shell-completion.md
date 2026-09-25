# Shell tab-completion

All three scripts support tab-completion through the optional
[argcomplete](https://github.com/kislyuk/argcomplete) package. They complete
their flags, the `--disable` / `--enable` / `--variant` value lists, file and
directory paths, and your live PipeWire sink names for `--autoload-sink` and
`--target-sink`. Install `python3-argcomplete` on Debian/Ubuntu/Fedora/openSUSE,
`python-argcomplete` on Arch, or `py3-argcomplete` on Alpine.

Argcomplete is off until you register it. Add this line to `~/.bashrc`, or to
`~/.zshrc` *after* its `compinit` line:

```bash
eval "$(activate-global-python-argcomplete --dest=-)"
```

Run the scripts **directly** to get completion: `./dolby_to_easyeffects.py …`.
In bash, that hook also covers the `python3 dolby_to_easyeffects.py …` form used
elsewhere in these docs. In zsh it does not, because zsh's own `python`
completion takes precedence over it.

To scope completion to these three scripts rather than every argcomplete-enabled
program, run `eval "$(register-python-argcomplete dolby_to_easyeffects.py)"`
once per script instead. That form covers `./dolby_to_easyeffects.py` only, in
both shells.
