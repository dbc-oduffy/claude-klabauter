"""A row adding a new .py module while editing an existing one warns and orders the new module first."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import emit
from .conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.emit import emit_script
from coordinator_core.ops.dispatch_emit.tests.test_emit import _plan_with_row
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow
from coordinator_core.ops._workflow_contract import Severity

_CLAUSE = "before editing any file that imports them"


def _row(writes) -> WaveRow:
    return WaveRow(
        id="C1", title="t", surface="pkg", writes=writes, reads=[], depends_on=[],
        change_kind="code-edit",
    )


def _spine(*writes: str, kind: str = "code-edit") -> str:
    return (
        f"- id: C1\n  title: row C1\n  change_kind: {kind}\n  surface: pkg\n  writes:\n"
        + "".join(f"    - {w}\n" for w in writes)
    )


def _tree(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "existing.py").write_text("x = 1\n", encoding="utf-8")


def test_new_module_plus_existing_py_warns(tmp_path):
    _tree(tmp_path)
    findings = emit.find_new_module_with_importer(
        [_row(["pkg/new.py", "pkg/existing.py"])], tmp_path
    )
    assert len(findings) == 1
    assert findings[0].severity is Severity.WARN
    assert findings[0].code == emit.NEW_MODULE_WITH_IMPORTER_CODE
    assert "pkg/new.py" in findings[0].message


def test_no_finding_for_other_shapes(tmp_path):
    _tree(tmp_path)
    for writes in (
        ["pkg/new.py"],
        ["pkg/existing.py"],
        ["pkg/new.py", "pkg/test_new.py", "pkg/tests/test_x.py"],
        ["pkg/new.py", "pkg/doc.md"],
    ):
        assert emit.find_new_module_with_importer([_row(writes)], tmp_path) == []


def test_new_test_file_beside_existing_module_is_not_a_new_module(tmp_path):
    _tree(tmp_path)
    assert emit.find_new_module_with_importer(
        [_row(["pkg/existing.py", "pkg/tests/test_new.py"])], tmp_path
    ) == []


def test_prompt_clause_only_on_matching_rows(tmp_path):
    _tree(tmp_path)
    plan = _plan_with_row(tmp_path, _spine("pkg/new.py", "pkg/existing.py"))
    out: list = []
    script = emit_script(plan, repo_root=tmp_path, findings_out=out, **REVIEW_KW)
    assert _CLAUSE in script
    assert any(f.code == emit.NEW_MODULE_WITH_IMPORTER_CODE for f in out)

    plain = _plan_with_row(tmp_path, _spine("pkg/existing.py"))
    out = []
    script = emit_script(plain, repo_root=tmp_path, findings_out=out, **REVIEW_KW)
    assert _CLAUSE not in script
    assert not any(f.code == emit.NEW_MODULE_WITH_IMPORTER_CODE for f in out)
