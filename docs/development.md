# Running the tests

[README](../README.md) · [All docs](README.md)

The `pytest` suite under `tests/` covers the converter without requiring any
proprietary Dolby tuning data as input.

```bash
pytest tests/
```

The bulk of the suite needs no setup. It covers
DSP math, output schema, `--disable`/argparse behavior, and a dedicated
regression suite for every shipped-bug "trap". It uses synthetic, hand-built
inputs only. No real DAX3 XML is shipped or checked in.

The corpus tier under `tests/corpus/` runs the full pipeline against a corpus of
real DAX3 XMLs: parse → FIR → preset → IRS. It auto-discovers them the same way
the main script does. That means NTFS-family mountpoints whose DriverStore
contains `dax3_ext_*.inf_*`, plus a bounded walk of the current working
directory for any folder containing Dolby-shaped XMLs. To override, point it at
a specific directory:

```bash
ATMOS_CORPUS_DIR=/path/to/dax3/xmls pytest tests/corpus/
```

The corpus tier skips cleanly if no corpus is reachable and `ATMOS_CORPUS_DIR`
is unset.

The heaviest tiers walk every endpoint/profile/curve combination and validate
every distinct discovered XML's generated PipeWire conf through `lv2info`. Both
are gated behind `--run-slow` or `ATMOS_RUN_SLOW=1`. Every run fans across your
cores via [`pytest-xdist`](https://pypi.org/project/pytest-xdist/), which turns
the heavy tiers from tens of minutes into a few. `pyproject.toml` sets
`-n auto --dist worksteal`. Pass `-n 0` to force a serial run, which is what you
want alongside `-x`, `-s` or `--pdb`.

The suite catches structural regressions, such as a FIR that isn't
minimum-phase, convolver autogain accidentally re-enabled, MBC compression-mode
flipped to upward, or enums emitted as integers. It does not substitute for
listening tests after any change to the output path.

`requirements.txt` also includes `pytest` and `pytest-xdist`, so you can run the
test suite with `pytest tests/` from the same venv as the
[README](../README.md#install)'s Install section. pytest won't start without
xdist 3.2 or newer, because `pyproject.toml` passes `-n auto --dist worksteal`.
