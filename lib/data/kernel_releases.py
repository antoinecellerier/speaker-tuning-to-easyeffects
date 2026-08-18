"""When each Linux kernel series was released, for the issue #33 age hint.

Machine-written: ``tools/update_kernel_releases.py`` appends to the literal
below and the weekly ``.github/workflows/kernel-release-table.yml`` opens a PR
per new series. Both find it by a regex pinned to its exact shape, so the
literal's formatting is a contract, not a style choice — see
``tests/test_kernel_releases.py``, which asserts the updater re-renders what
ships here byte-for-byte.

Only the dates live here. What counts as *old* (``_KERNEL_OLD_MONTHS``) and
what the user is told about it stay in ``lib/report/environment.py``, where a
weekly rewrite of this file cannot reach them.

Stdlib-only, and in fact import-free.
"""

# Upstream release month per kernel series (issue #33: a preset can be perfect
# while an *old kernel* mis-configures the speaker path — that report was fixed
# by a 6.12→7.0 kernel upgrade, not a preset change). Month precision is enough for
# an age hint. Dates are historical facts, so an aging copy of this tool still
# ages old kernels correctly; a series newer than the table is assumed recent.
# Each value is that series' `vX.Y` tag date on Linus' tree; new ones are
# appended by tools/update_kernel_releases.py, which the weekly
# .github/workflows/kernel-release-table.yml runs to open a PR per release.
# Edits below the newest entry are never machine-rewritten, so a hand
# correction sticks.
_KERNEL_SERIES_RELEASES = {
    (5, 10): "2020-12", (5, 11): "2021-02", (5, 12): "2021-04",
    (5, 13): "2021-06", (5, 14): "2021-08", (5, 15): "2021-10",
    (5, 16): "2022-01", (5, 17): "2022-03", (5, 18): "2022-05",
    (5, 19): "2022-07", (6, 0): "2022-10", (6, 1): "2022-12",
    (6, 2): "2023-02", (6, 3): "2023-04", (6, 4): "2023-06",
    (6, 5): "2023-08", (6, 6): "2023-10", (6, 7): "2024-01",
    (6, 8): "2024-03", (6, 9): "2024-05", (6, 10): "2024-07",
    (6, 11): "2024-09", (6, 12): "2024-11", (6, 13): "2025-01",
    (6, 14): "2025-03", (6, 15): "2025-05", (6, 16): "2025-07",
    (6, 17): "2025-09", (6, 18): "2025-11", (6, 19): "2026-02",
    (7, 0): "2026-04", (7, 1): "2026-06", (7, 2): "2026-08",
}
