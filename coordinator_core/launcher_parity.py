"""coordinator_core.launcher_parity — where an entrypoint IS, and what makes
one. Production logic, importable without pytest.

WHY THIS MODULE EXISTS. `ScanRoot`/`SCAN_ROOTS`/`_is_entrypoint` are the single
source of truth for which directories hold directly-invoked entrypoints and
which files in them count. They were authored inside
`coordinator_core/test_bin_launcher_parity.py`, and the PUBLISH path consumes
them: `coordinator_core.percolate.engine.enumerate_gate_entrypoints` imports
that test module to enumerate the entrypoints its end-of-run gate checks.

That made `publish.py` — a production CLI — depend on pytest being importable,
because the test module carries `import pytest` and a `pytestmark` at module
scope. pytest is an OPTIONAL extra in `pyproject.toml` (`[project.optional-
dependencies]` "Test tooling"), deliberately NOT a `[project].dependencies`
entry, and `scripts/setup.py` provisions from the runtime list only. So a
correctly-provisioned claude-klabauter install has no pytest, and a publish round on one
died with `ModuleNotFoundError: No module named 'pytest'` AFTER syncing every
row — the rows landed, the end-of-run gate crashed, and the round reported
failure and committed nothing. It went unseen because the authoring workstation
has pytest installed; an ephemeral cloud container does not.

The fix is not to degrade the gate when pytest is missing — a gate that skips
itself on a missing test dependency is worse than the crash, because it is
silent. It is to stop the production path depending on a test module at all:
nothing these symbols do has anything to do with pytest.

WHAT MOVED AND WHAT DID NOT. The definitions moved here verbatim;
`test_bin_launcher_parity` imports them back under their existing names, so its
own ~1700 lines and every sibling guard that consumes `SCAN_ROOTS` or
`_py_entrypoints()` from it keep working against the same objects. This is a
re-home, not a re-design: one definition, two importers, and the test module
stays the place the PARITY RULES are asserted.

Underscore-prefixed names are kept underscore-prefixed, deliberately. They are
re-exported by the test module under those exact names and read as private
there; renaming them here would fork the vocabulary between the two files for
no gain, and this module's docstring is the thing that says they are shared.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple

#: This repo's root. Same derivation as `test_bin_launcher_parity`'s, and it
#: must stay at the same directory depth (`coordinator_core/<file>.py`) for
#: that to hold.
REPO_ROOT = Path(__file__).resolve().parent.parent

_MAIN_GUARD_RE = re.compile(r'^if\s+__name__\s*==\s*[\'"]__main__[\'"]\s*:', re.M)


class ScanRoot(NamedTuple):
    """One directory this guard holds to `.cmd`-twin parity.

    `rel` is the repo-relative POSIX path (used verbatim in failure messages
    and in the `git ls-files` query). `dir` is the on-disk directory, split
    out from `rel` so red-case tests can point a root at a tmp_path fixture
    without monkeypatching module state.

    `require_main_guard` is the entrypoint discriminator, and it differs by
    root for a real reason rather than convenience:
      - False: the directory is entrypoints-by-construction -- every tracked,
        top-level, non-test file in it is spawned, never imported. Any file
        landing there is presumed an entrypoint.
      - True: the directory MIXES entrypoints with importable library
        modules (e.g. `coordinator/lib/release_currency.py`,
        `coordinator/lib/oss-repo-constants.py`), so membership is decided by
        the file's own content: an `if __name__ == "__main__":` block, which
        is the direct-invocation signal. A library module needs no launcher
        and generating one for it is noise. Encoding the distinction here --
        rather than hand-listing the library modules as exemptions -- is what
        keeps `PY_ENTRYPOINT_EXEMPTIONS` empty and keeps the guard correct for
        files that do not exist yet.
    """

    rel: str
    dir: Path
    require_main_guard: bool


def _root(rel: str, *, require_main_guard: bool) -> ScanRoot:
    return ScanRoot(rel, REPO_ROOT / rel, require_main_guard)


# Every directory holding entrypoints a user or the engine invokes directly.
#
# `require_main_guard=False` is verified-safe, not assumed: as of 2026-08-03
# all 344 coordinator/bin entrypoints (67 bare + 277 `.py`) carry an
# `if __name__ == "__main__":` block anyway, so the two policies agree on that
# tree today. The flag stays False there so a future coordinator/bin
# entrypoint that runs at import time cannot silently fall out of coverage --
# widening this guard must never narrow what it already checked.
#
# NEGATIVE SPEC -- `coordinator_core/` is deliberately absent. It is the
# importable engine package (`python -m coordinator_core`), not a launcher
# directory: its modules are imported by dotted name, and the handful of
# `if __name__ == "__main__":` blocks in it (`machine_resolver.py`,
# `pyresolve.py`, `state_root.py`, `dag.py`) are self-test/debug hooks on
# imported modules, not console entrypoints. Adding it would generate ~5
# launchers nothing invokes. `.sh` entrypoints are likewise out of scope --
# this guard has never scanned them, and the naked-Python conversion of the
# remaining `coordinator/lib/*.sh` files is its own workstream.
SCAN_ROOTS: tuple[ScanRoot, ...] = (
    _root("coordinator/bin", require_main_guard=False),
    _root("coordinator/scripts", require_main_guard=False),
    _root("bin", require_main_guard=True),
    _root("coordinator/lib", require_main_guard=True),
    _root("scripts", require_main_guard=True),
)


def _has_main_guard(path: Path) -> bool:
    """True when `path` carries a module-level `if __name__ == "__main__":`.

    The direct-invocation signal that separates an entrypoint from an
    importable library module in the mixed scan roots (see `ScanRoot`).
    Parsed via `ast` so a `__main__` string inside a comment or docstring
    cannot fake membership; falls back to a line-anchored regex only when the
    file does not parse as Python (a non-Python extensionless file cannot be
    a Python entrypoint, and the regex will not match it either).
    """
    try:
        source = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return bool(_MAIN_GUARD_RE.search(source))
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        test = node.test
        if (
            isinstance(test, ast.Compare)
            and isinstance(test.left, ast.Name)
            and test.left.id == "__name__"
            and len(test.comparators) == 1
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value == "__main__"
        ):
            return True
    return False


def _is_entrypoint(root: ScanRoot, name: str) -> bool:
    """Whether `root`'s policy admits `name` as an entrypoint needing a twin."""
    if not root.require_main_guard:
        return True
    return _has_main_guard(root.dir / name)
