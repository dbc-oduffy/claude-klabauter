"""A granted ROW-LOCAL-BLOCK widen adds the path to the row's own commit scope (`paths`, `declared`),
not only to the re-dispatch prompt, so the widened file is landable and dependents are not left unstarted."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.emit import _run_row_helper_js


def test_granted_widen_extends_the_rows_commit_paths_and_declared() -> None:
    block = _run_row_helper_js()
    granted = block.split("if (_cls.widen) {", 1)[1].split("} else {", 1)[0]
    assert "_widenCommit(commit, _cls.widen)" in granted
    helper = block.split("function _widenCommit(", 1)[1].split("\n  }\n", 1)[0]
    assert "'paths'" in helper and "'declared'" in helper and "includes(" in helper
