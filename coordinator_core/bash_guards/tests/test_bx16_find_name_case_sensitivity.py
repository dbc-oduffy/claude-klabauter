"""Regression coverage for the `find -name` glob-to-`fnmatch` case-
sensitivity fidelity gap in `coordinator_core.bash_guards.dispatch_checks`'s
`-exec`/head-tail rewrites (`_bt_find_exec_python_rewrite`,
`_bt_build_generator_lines`'s `"find"` kind).

The defect: both call sites matched `find -name PATTERN` via
`fnmatch.fnmatch(fn, PATTERN)`, which normalizes case through
`os.path.normcase` -- a no-op on POSIX, but a LOWER-CASE fold on Windows.
`find -name` (unlike `-iname`) is case-SENSITIVE on every platform GNU
findutils ships on, so the rewrite was silently more permissive than the
command it replaced specifically on Windows. Fixed by switching to
`fnmatch.fnmatchcase`, which never normalizes case regardless of host OS.

This suite proves the FIX rather than merely the platform-specific defect
(this test runs on whatever OS pytest is invoked on, and `fnmatchcase`'s
whole point is that its behavior does not depend on host OS) -- it asserts
the translated match expression rejects a differently-cased name that a
naively-`fnmatch.fnmatch`'d version would have accepted on Windows.

Spec backlink: docs/plans/2026-07-29-windows-viability-stop-the-spawn-
storms.md row BX-16 (coordinator-claude); sibling fidelity fix to this same
dispatch's grep-dialect-conflation correction
(`test_bx16_grep_dialect_fidelity.py`).
"""
from __future__ import annotations

import fnmatch

from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.bash_guards import guard_head_tail_rewrite as ht


def test_find_exec_rewrite_uses_fnmatchcase_not_fnmatch():
    parsed = {
        "path": ".",
        "name_pattern": "*.PY",
        "only_files": True,
        "exec_argv": ["rm"],
    }
    rewrite = dc._bt_find_exec_python_rewrite(parsed)
    assert "fnmatch.fnmatchcase(fn," in rewrite
    assert "fnmatch.fnmatch(fn," not in rewrite


def test_build_generator_lines_find_uses_fnmatchcase_not_fnmatch():
    parsed = {"path": ".", "name_pattern": "*.PY", "only_files": True}
    lines = ht._bt_build_generator_lines("find", parsed)
    body = "\n".join(lines)
    assert "fnmatch.fnmatchcase(fn," in body
    assert "fnmatch.fnmatch(fn," not in body


def test_fnmatchcase_is_case_sensitive_unlike_fnmatch_on_a_folding_platform():
    """`fnmatchcase` never case-folds, regardless of host OS -- proves the
    fix's mechanism directly rather than requiring a Windows host to
    observe the divergence `fnmatch.fnmatch` would have introduced there."""
    assert fnmatch.fnmatchcase("script.py", "*.PY") is False
    assert fnmatch.fnmatchcase("script.PY", "*.PY") is True
