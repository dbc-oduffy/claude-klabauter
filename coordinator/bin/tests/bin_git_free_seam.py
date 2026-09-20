"""
coordinator.bin.tests.bin_git_free_seam

C7 of docs/plans/2026-08-13-archive-family-coverage-restoration.md (AC3).
Rule-setting only — this module ports no test. It extends C1's
git-free-helper pattern (coordinator_core/ops/fleet/tests/
archive_git_free_seam.py) to Class 1's other expression named in
state/audits/2026-08-07-spawn-heavy-test-excision-ledger.md: "~100
duplicated module-local `def _git(...)` helpers with no shared
chokepoint" — the shape `test_reap_orphaned_in_flight_handoffs.py` used
(`_git(cwd, *args)` / `_init_repo(root)` / `_commit(root, relpath)`,
recovered verbatim from the excision ledger's parent sha via
`git show 6f0e89044:coordinator/bin/tests/
test_reap_orphaned_in_flight_handoffs.py`), reproduced across ~100
`coordinator/bin/tests/` modules with no shared module to import instead.

This is `coordinator/bin`'s own copy of the seam, NOT a re-export of
`coordinator_core.ops.fleet.tests.archive_git_free_seam` or
`coordinator_core.ops.session.tests.session_git_free_seam`: per this
row's body, `coordinator/bin` does not import `coordinator_core`, and a
bin-package helper that reached into `coordinator_core` for its fixtures
would create exactly the import-shape coupling C7's coupling note warns
against for a different pair of packages. Plain, explicitly-imported
helper module — NEVER a conftest.py, NEVER autouse — same reason as C1:
the 2026-08-07 incident was a conftest fixture running real git ambiently,
per test, on a machine running 50-70 concurrent LLM sessions. Nothing here
may be discovered or applied without a test explicitly writing
`from coordinator.bin.tests.bin_git_free_seam import ...`.

AC3 — the written discriminator, restated for this package
-------------------------------------------------------------
`coordinator/bin` scripts are loaded ad hoc via
`importlib.util.spec_from_file_location`, not imported as a package, so
there is no single "op module" to `patch.object` three names on the way
C1's `patched_disposition_seam` does for `coordinator_core.ops.fleet.*`.
What the ~100 module-local `_git()` helpers actually bought each test
falls into the same two classes C1 names, applied per call site rather
than per module:

- Calls that only ever produce a directory/file layout on disk for a
  script's own path-and-frontmatter logic to walk (e.g. `_init_repo`
  creating an empty `.git/`, `_write_handoff`/`_write_valid_handoff`
  writing frontmatter files under that tree) never touch git's object
  database, index, or commit graph. A script whose logic under test reads
  files by path (`_fm_field`, frontmatter parsing, directory scans,
  argument validation) is exercised identically whether `.git/` was
  built by `git init` or by `Path.mkdir`.
- Calls whose result the script's logic actually inspects — a resolved
  commit SHA (`_shipped_orphan_sha`'s `git cat-file -e <sha>` check), `git
  rev-parse HEAD` output consumed as a real object id, or any assertion
  about what got committed — depend on git's own state and stay real-git;
  no fake in this module resolves an object id, because doing so would
  make the test self-consistent rather than honest against git's actual
  object database (same caution C1's AC12 states for the fleet seam).

Every helper below is the first class only: it builds the directory shape
a `_git()`-based fixture built, and records what a script's own commit
wrapper was asked to do, without spawning `git` anywhere. A test that
needs the second class keeps its real `_git()`/`subprocess` call — this
module does not attempt to fake git's object database.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from unittest.mock import patch


def make_fake_repo_root(tmp_path: Path) -> Path:
    """Build a directory shaped like a git repo root — a plain directory
    plus an empty `.git/` — without spawning `git init`.

    Use in place of a module-local `_init_repo(root)` call for any test
    whose script logic only checks for `.git/`'s existence or walks
    tracked-looking files by path; never for a test whose logic resolves
    an actual git object.
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir(exist_ok=True)
    (repo_root / ".git").mkdir(exist_ok=True)
    return repo_root


def make_recording_git(
    *, response: Optional[str] = None,
) -> Callable:
    """Build a fake replacement for a module-local `_git(cwd, *args)` (or
    `_commit(root, relpath)`-shaped) helper.

    Never spawns a process. Records every call's `(cwd, args)` on
    `.captured`, as a plain list, for a test to assert against.
    `response`, when given, is returned verbatim on every call — use it to
    stand in for a fixed, non-resolved string (e.g. a synthetic SHA-shaped
    placeholder) in the rare case a script's logic wants "some string back"
    without caring that it resolves to a real object; do NOT use this to
    fake a script's own `git cat-file`/object-existence check — that stays
    real-git per this module's docstring.
    """

    def _fake_git(cwd, *args):
        _fake_git.captured.append((str(cwd), tuple(args)))
        return response

    _fake_git.captured: List[Tuple[str, tuple]] = []
    return _fake_git


@contextmanager
def patched_git_seam(op_module, *, git_name: str = "_git", fake=None):
    """Patch `op_module.<git_name>` (default `_git`, matching the ~100
    module-local helpers' usual name) with a git-free fake, so a loaded
    bin script's own logic runs against `make_fake_repo_root`-built
    directories with no git subprocess anywhere in the call.

    `op_module` is whatever a test's own `importlib.util.spec_from_file_
    location` loader produced (see `_load_module()` in
    `test_reap_orphaned_in_flight_handoffs.py`'s recovered shape, this
    module's docstring) — not a `coordinator_core` package module.

    `fake`, when given, replaces the default `make_recording_git()`.
    Yields the fake in use, so a test can read `.captured` after the
    `with` block.
    """
    if fake is None:
        fake = make_recording_git()
    with patch.object(op_module, git_name, fake):
        yield fake
