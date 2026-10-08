"""`plan-spine-check --for-execution` promotes UNDECLARED writes to STRUCTURAL; the two read lints
and LEGACY-READS stay advisory always; paths compare backslash-insensitive; the
`## Width rationale` helper reads a non-empty section only.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_MOD = Path(__file__).resolve().parents[1] / "plan-spine-check.py"


def _load():
    spec = importlib.util.spec_from_file_location("plan_spine_check_for_execution", _MOD)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plan_spine_check_for_execution"] = mod
    spec.loader.exec_module(mod)
    return mod


psc = _load()


def _plan(tmp_path: Path, rows: str) -> Path:
    path = tmp_path / "plan.md"
    path.write_text(
        "---\ntitle: t\n---\n\n# A plan\n\n## Tasks\n\n```yaml plan-tasks\n" + rows + "\n```\n",
        encoding="utf-8",
    )
    return path


_UNDECLARED_ROW = '- id: C1\n  title: "no writes"\n  change_kind: doc-edit\n  surface: "docs/a.md"'
_WRITES_EMPTY_ROW = (
    '- id: C1\n  title: "memo"\n  change_kind: doc-edit\n  surface: "docs/a.md"\n  writes: []'
)
_WRITES_UNDER_ROW = (
    '- id: C1\n  title: "grows a dir"\n  change_kind: doc-edit\n  surface: "docs/a.md"\n'
    '  writes_under:\n    - "state/audits/"'
)
_OPERATOR_ROW = (
    '- id: C1\n  title: "human only"\n  change_kind: verification\n  surface: "docs/a.md"\n'
    '  execution_mode: operator'
)


def test_for_execution_exits_1_on_undeclared_writes(tmp_path):
    path = _plan(tmp_path, _UNDECLARED_ROW)
    assert psc.main([str(path), "--for-execution"]) == psc.EXIT_INVALID


def test_without_flag_same_fixture_exits_0_and_reports_advisory(tmp_path):
    path = _plan(tmp_path, _UNDECLARED_ROW)
    assert psc.main([str(path)]) == psc.EXIT_OK
    report = psc.check_plan(path, for_execution=False)
    assert report["verdict"] == "VALID"
    assert any(a["at"] == "writes" for a in report["advisories"])


def test_writes_empty_is_declared_not_undeclared(tmp_path):
    path = _plan(tmp_path, _WRITES_EMPTY_ROW)
    assert psc.main([str(path), "--for-execution"]) == psc.EXIT_OK


def test_writes_under_only_is_not_undeclared(tmp_path):
    path = _plan(tmp_path, _WRITES_UNDER_ROW)
    assert psc.main([str(path), "--for-execution"]) == psc.EXIT_OK


def test_operator_row_is_exempt(tmp_path):
    path = _plan(tmp_path, _OPERATOR_ROW)
    assert psc.main([str(path), "--for-execution"]) == psc.EXIT_OK


def test_reads_at_head_hitting_a_written_path_is_advisory_only(tmp_path):
    rows = (
        '- id: C1\n  title: "writer"\n  change_kind: doc-edit\n  surface: "docs/a.md"\n'
        '  writes:\n    - "docs/a.md"\n'
        '- id: C2\n  title: "reader"\n  change_kind: doc-edit\n  surface: "docs/b.md"\n'
        '  writes:\n    - "docs/b.md"\n'
        '  reads_at_head:\n    - "docs/a.md"'
    )
    path = _plan(tmp_path, rows)
    assert psc.main([str(path), "--for-execution"]) == psc.EXIT_OK
    report = psc.check_plan(path, for_execution=True)
    assert any("reads_at_head" in a["at"] for a in report["advisories"])


def test_consumes_a_path_nobody_writes_is_advisory_only(tmp_path):
    rows = (
        '- id: C1\n  title: "consumer"\n  change_kind: doc-edit\n  surface: "docs/a.md"\n'
        '  writes:\n    - "docs/a.md"\n'
        '  consumes:\n    - "docs/nobody-writes.md"'
    )
    path = _plan(tmp_path, rows)
    assert psc.main([str(path), "--for-execution"]) == psc.EXIT_OK
    report = psc.check_plan(path, for_execution=True)
    assert any(a["at"] == "consumes" for a in report["advisories"])


def test_legacy_reads_is_advisory_notice(tmp_path):
    rows = (
        '- id: C1\n  title: "legacy"\n  change_kind: doc-edit\n  surface: "docs/a.md"\n'
        '  writes:\n    - "docs/a.md"\n'
        '  reads:\n    - "docs/other.md"'
    )
    path = _plan(tmp_path, rows)
    report = psc.check_plan(path, for_execution=True)
    assert report["verdict"] == "VALID"
    assert any("LEGACY-READS" in a["error"] for a in report["advisories"])


def test_backslash_spelled_path_still_matches(tmp_path):
    rows = (
        '- id: C1\n  title: "writer"\n  change_kind: doc-edit\n  surface: "docs/a.md"\n'
        '  writes:\n    - "docs\\\\a.md"\n'
        '- id: C2\n  title: "reader"\n  change_kind: doc-edit\n  surface: "docs/b.md"\n'
        '  writes:\n    - "docs/b.md"\n'
        '  reads_at_head:\n    - "docs/a.md"'
    )
    path = _plan(tmp_path, rows)
    report = psc.check_plan(path, for_execution=True)
    assert any("reads_at_head" in a["at"] for a in report["advisories"])


def test_width_rationale_helper_finds_non_empty_section():
    text = "# A plan\n\n## Width rationale\n\nA narrow spine is fine here because X.\n\n## Next\n"
    assert psc._width_rationale(text) == "A narrow spine is fine here because X."


def test_width_rationale_helper_returns_none_when_absent():
    assert psc._width_rationale("# A plan\n\nno such section\n") is None


def test_width_rationale_helper_returns_none_when_empty():
    text = "# A plan\n\n## Width rationale\n\n## Next\n"
    assert psc._width_rationale(text) is None


def _goal_plan(tmp_path: Path, tshirt: str) -> Path:
    (tmp_path / "sizing.yaml").write_text(f"estimate:\n  tshirt: {tshirt}\n", encoding="utf-8")
    path = tmp_path / "plan.md"
    path.write_text(
        "---\ntitle: t\ncreated: 2026-10-05\nsizing_object: sizing.yaml\n"
        "prime_exit_criterion:\n  statement: s\n  # falsifier: owed\n---\n\n# A plan\n",
        encoding="utf-8",
    )
    return path


def test_for_execution_refuses_owed_falsifier_on_m_plan(tmp_path, capsys):
    path = _goal_plan(tmp_path, "M")
    assert psc.main([str(path), "--for-execution"]) == psc.EXIT_INVALID
    assert "falsifier_exemption" in capsys.readouterr().out
    assert psc.main([str(path)]) == psc.EXIT_OK


def test_for_execution_passes_owed_falsifier_on_s_plan(tmp_path):
    path = _goal_plan(tmp_path, "S")
    assert psc.main([str(path), "--for-execution"]) == psc.EXIT_OK


def test_path_in_both_consumes_and_reads_at_head_is_structural(tmp_path):
    rows = (
        '- id: C1\n  title: "writer"\n  change_kind: doc-edit\n  surface: "docs/a.md"\n'
        '  writes:\n    - "docs/a.md"\n'
        '- id: C2\n  title: "both"\n  change_kind: doc-edit\n  surface: "docs/b.md"\n'
        '  writes: []\n  consumes:\n    - "docs/a.md"\n  reads_at_head:\n    - "docs/a.md"'
    )
    path = _plan(tmp_path, rows)
    report = psc.check_plan(path, for_execution=False)
    assert report["verdict"] == "INVALID"
    assert any("CONTRADICTORY-READS" in f["error"] for f in report["rows"])
    assert psc.main([str(path)]) == psc.EXIT_INVALID


def test_disjoint_consumes_and_reads_at_head_stay_valid(tmp_path):
    rows = (
        '- id: C1\n  title: "writer"\n  change_kind: doc-edit\n  surface: "docs/a.md"\n'
        '  writes:\n    - "docs/a.md"\n'
        '- id: C2\n  title: "ok"\n  change_kind: doc-edit\n  surface: "docs/b.md"\n'
        '  writes: []\n  consumes:\n    - "docs/a.md"\n  reads_at_head:\n    - "docs/c.md"'
    )
    path = _plan(tmp_path, rows)
    assert psc.check_plan(path)["verdict"] == "VALID"


def _plan_with_falsifier(tmp_path: Path, how: str) -> Path:
    path = tmp_path / "plan.md"
    path.write_text(
        "---\ntitle: t\nprime_exit_criterion:\n  falsifier:\n"
        f"    how: \"{how}\"\n    baseline_output: x\n    baseline_ref: abc1234\n"
        "    expected_when_true: y\n---\n\n# A plan\n",
        encoding="utf-8",
    )
    return path


def test_unanchored_falsifier_how_is_advisory_not_fatal(tmp_path):
    path = _plan_with_falsifier(tmp_path, "audit | grep -c prior")
    report = psc.check_plan(path)
    assert any("UNANCHORED-FALSIFIER" in a["error"] for a in report["advisories"])
    assert psc.main([str(path)]) == psc.EXIT_OK


def test_anchored_falsifier_how_raises_no_advisory(tmp_path):
    path = _plan_with_falsifier(tmp_path, "audit | grep -x prior")
    report = psc.check_plan(path)
    assert not any("UNANCHORED-FALSIFIER" in a["error"] for a in report.get("advisories", []))
