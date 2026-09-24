"""The cross-row grader: `find_union_closure_violations` sees what
`find_import_closure_violations` structurally cannot.

`find_import_closure_violations` only ever walks ONE row's own restricted
tree, so a file shipped by a DIFFERENT row that imports a `coordinator_core`
name absent from the assembled union is invisible to it — the defect
`docs/plans/2026-09-22-publish-integrity-union-and-anchor-spike.md` (P147-C2)
closes: 24 violations over 21 files at authoring, and no gate walked the
union to see them.

Fixtures build a small `assembled_root` directly (never the real mirror —
that is `run_pre_sync_gates`'s wiring, not this row's).
"""

import subprocess
import sys
from pathlib import Path

import pytest

# `coordinator/` and `coordinator/lib/` carry no `__init__.py`, so there is no
# dotted import available from the repo root; `coordinator/lib/percolate/` DOES
# have one. Putting `coordinator/lib` on `sys.path` and importing
# `percolate.import_closure` as an ordinary package member is the route the
# sibling tests in this directory already use.
_COORDINATOR_LIB = Path(__file__).resolve().parents[2]
if str(_COORDINATOR_LIB) not in sys.path:
    sys.path.insert(0, str(_COORDINATOR_LIB))

from percolate.import_closure import find_union_closure_violations  # noqa: E402


def _write(root: Path, rel: str, src: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(src, encoding="utf-8")


def _base_tree(tmp_path: Path) -> Path:
    """An assembled union root: `coordinator_core/` with one present
    top-level package (`present_pkg`, exporting `OP_KEY_SCOPE` via
    `__init__.py`, and one present submodule `present_pkg/leaf.py`), plus a
    sibling non-engine row directory (`bin/`) where the violating imports
    live — the cross-row shape this grader exists to see."""
    root = tmp_path
    _write(
        root,
        "coordinator_core/__init__.py",
        "from os import path as OP_KEY_SCOPE\n",
    )
    _write(root, "coordinator_core/present_pkg/__init__.py", "")
    _write(root, "coordinator_core/present_pkg/leaf.py", "")
    return root


def test_non_engine_module_importing_absent_top_level_package_is_a_violation(tmp_path):
    root = _base_tree(tmp_path)
    _write(root, "bin/mod_a.py", "import coordinator_core.absent_pkg\n")
    examined, violations = find_union_closure_violations(root)
    assert ("bin/mod_a.py", "absent_pkg") in violations
    assert examined >= 1


def test_absent_nested_module_inside_a_present_package_is_a_violation(tmp_path):
    root = _base_tree(tmp_path)
    _write(root, "bin/mod_b.py", "import coordinator_core.present_pkg.absent_submod\n")
    _, violations = find_union_closure_violations(root)
    assert ("bin/mod_b.py", "present_pkg.absent_submod") in violations


def test_bare_import_resolved_only_by_init_is_not_a_violation(tmp_path):
    root = _base_tree(tmp_path)
    _write(root, "bin/mod_c.py", "from coordinator_core import OP_KEY_SCOPE\n")
    _, violations = find_union_closure_violations(root)
    assert violations == []


def test_guarded_import_is_never_reported(tmp_path):
    root = _base_tree(tmp_path)
    _write(
        root,
        "bin/mod_guarded.py",
        "try:\n"
        "    from coordinator_core import missing_thing\n"
        "except ImportError:\n"
        "    missing_thing = None\n",
    )
    _, violations = find_union_closure_violations(root)
    assert violations == []


def test_type_checking_guarded_import_is_never_reported(tmp_path):
    root = _base_tree(tmp_path)
    _write(
        root,
        "bin/mod_typecheck.py",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from coordinator_core import missing_thing\n",
    )
    _, violations = find_union_closure_violations(root)
    assert violations == []


def test_file_under_coordinator_core_is_never_reported(tmp_path):
    root = _base_tree(tmp_path)
    # Self-referential shape (coordinator_core importing its own absent
    # sibling) is the per-row gate's job, never this one's.
    _write(root, "coordinator_core/self_ref.py", "import coordinator_core.absent_pkg\n")
    _, violations = find_union_closure_violations(root)
    assert violations == []


def test_from_present_top_level_package_import_absent_submodule_is_a_violation(tmp_path):
    """The alias form a full-dotted-needle substring prefilter would miss:
    `coordinator_core.present_pkg.absent_submod` never appears contiguous in
    this source text, only split across `from ... import ...`."""
    root = _base_tree(tmp_path)
    _write(root, "bin/mod_d.py", "from coordinator_core.present_pkg import absent_submod\n")
    _, violations = find_union_closure_violations(root)
    assert ("bin/mod_d.py", "present_pkg.absent_submod") in violations


def test_from_present_package_import_present_submodule_is_not_a_violation(tmp_path):
    root = _base_tree(tmp_path)
    _write(root, "bin/mod_d2.py", "from coordinator_core.present_pkg import leaf\n")
    _, violations = find_union_closure_violations(root)
    assert violations == []


def test_from_coordinator_core_import_absent_top_level_package_is_a_violation(tmp_path):
    """The other alias form the same review note names: the bare shape,
    where the full dotted needle again never appears contiguous."""
    root = _base_tree(tmp_path)
    _write(root, "bin/mod_e.py", "from coordinator_core import absent_pkg2\n")
    _, violations = find_union_closure_violations(root)
    assert ("bin/mod_e.py", "absent_pkg2") in violations


def test_spawns_no_subprocess(tmp_path, monkeypatch):
    root = _base_tree(tmp_path)
    _write(root, "bin/mod_f.py", "import coordinator_core.absent_pkg\n")

    def _boom(*args, **kwargs):
        raise AssertionError("find_union_closure_violations must not spawn a subprocess")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    find_union_closure_violations(root)


def test_examined_counts_non_coordinator_core_files_only(tmp_path):
    root = _base_tree(tmp_path)
    _write(root, "bin/mod_g.py", "x = 1\n")
    _write(root, "lib/mod_h.py", "y = 2\n")
    examined, _ = find_union_closure_violations(root)
    # bin/mod_g.py + lib/mod_h.py; the coordinator_core/* fixtures are
    # excluded from the count entirely.
    assert examined == 2


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
