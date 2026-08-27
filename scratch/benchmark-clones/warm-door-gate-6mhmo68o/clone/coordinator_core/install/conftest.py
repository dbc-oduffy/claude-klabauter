"""
coordinator_core.install.conftest — repo-root litter guard for this
package's own test tree (`coordinator_core/install/` and its `tests/`
subpackage).

Purpose: closes the class of defect fixed 2026-08-26 in
`coordinator_core/install/tests/test_fleet_env_cutover.py` — a test that
flips `os.name` to `"nt"` process-wide makes CPython's `pathlib.Path(...)`
pick `WindowsPath` for every path constructed anywhere in the process for
the rest of the test run, so `str()` of any such path silently turns `/`
into `\\`. That corrupted a lock-file path built via `str(Path(...))` in
`fleet_env.py`, which was then opened relative to the real POSIX cwd and
planted a stray backslash-named file at the REPO ROOT (observed under
xdist, `popen-gw2`). `_host_is_nt` (see `coordinator_core.install.junction`)
closes the CAUSE for the one call site that had it; this fixture closes the
SYMPTOM for ANY cause, present or future, anywhere in this test tree —
a bare `os.name` flip is not the only way a test can leak a file at the
repo root, and this fixture does not need to know the mechanism to catch it.

Deliberately function-scoped and autouse rather than session-scoped: a
session-scoped before/after snapshot would only attribute the litter to
"somewhere in this whole run", forcing a re-run under `-k` to localize it.
Function scope costs one extra `os.listdir()` of the repo root per test
(a single non-recursive directory listing, not a walk) — negligible next to
this suite's own I/O and well inside the repo's 500ms per-op brightline
(this is a `listdir`, not a spawn).

Negative-spec:
    - Does NOT scan recursively, and does NOT flag pre-existing repo-root
      entries (tracked files, an operator's own scratch files already
      present before the test ran) — only entries that appear DURING the
      test are flagged.
    - Does NOT attempt to clean up a flagged entry — a test that leaks one
      is broken and must fail loudly, not have its symptom silently swept.
    - Does NOT replace `junction._host_is_nt` as the fix for the ROOT CAUSE
      this module's tests hit — this is a backstop for the symptom class,
      not a substitute for a named platform seam in the production code
      under test.
"""
from __future__ import annotations

import os

import pytest

_REPO_ROOT_MARKERS = ("pyproject.toml",)


def _find_repo_root(start: str) -> "str | None":
    current = start
    while True:
        if any(os.path.isfile(os.path.join(current, marker)) for marker in _REPO_ROOT_MARKERS):
            return current
        parent = os.path.dirname(current)
        if parent == current:
            return None
        current = parent


@pytest.fixture(autouse=True)
def _no_new_repo_root_entries():
    repo_root = _find_repo_root(os.path.dirname(os.path.abspath(__file__)))
    if repo_root is None:
        yield
        return
    try:
        before = set(os.listdir(repo_root))
    except OSError:
        yield
        return
    yield
    try:
        after = set(os.listdir(repo_root))
    except OSError:
        return
    new_entries = after - before
    assert not new_entries, (
        "test left new untracked entries at the repo root: "
        f"{sorted(new_entries)!r} — a test flipping os.name process-wide "
        "(or any other cause) can make a relative-path write land here "
        "instead of a tmp_path. See this file's module docstring."
    )
