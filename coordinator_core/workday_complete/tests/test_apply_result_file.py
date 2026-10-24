"""`apply --result-file` writes the run report on every exit path; a failed pass names its partial state.

Run: python -m pytest coordinator_core/workday_complete/tests/test_apply_result_file.py -q
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

import pytest

from coordinator_core.workday_complete import apply as wc_apply


def _directive(did: str) -> dict[str, Any]:
    return {
        "id": did, "cli": "fake-cli", "args": [], "depends_on": None,
        "already_satisfied": False, "stdin_from": None,
    }


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    sessions = str(tmp_path / ".git" / "coordinator-sessions")
    from coordinator_core.ceremony_common import apply_checkpoint
    from coordinator_core.session import core

    monkeypatch.setattr(core, "sessions_dir", lambda cwd=None: sessions)
    monkeypatch.setattr(apply_checkpoint, "sessions_dir", lambda cwd=None: sessions)
    monkeypatch.setattr(wc_apply, "_resolve_cli", lambda name: Path(name))
    return tmp_path


def _execute(monkeypatch: pytest.MonkeyPatch, root: Path, directives, fail: frozenset = frozenset()):
    def fake(directive: dict[str, Any], *, stdin_text: Optional[str] = None) -> dict[str, Any]:
        code = 1 if directive["id"] in fail else 0
        return {
            "id": directive["id"], "cli": "fake-cli", "args": [], "exit_code": code,
            "stdout": "CHILD-STDOUT\n", "stderr": "", "exit_class": "returned",
        }

    monkeypatch.setattr(wc_apply, "_dispatch_directive", fake)
    from coordinator_core.ceremony_common.apply_checkpoint import open_checkpoint

    return wc_apply._execute_directives(
        directives, [], {}, checkpoint=open_checkpoint(root, "workday-complete")
    )


def test_success_writes_the_report_without_child_stdout(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    report = {"landed": ["a"], "failed": []}
    monkeypatch.setattr(wc_apply, "apply", lambda **kw: (0, report))
    out = tmp_path / "r.json"
    assert wc_apply.main(["--result-file", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8")) == report
    assert "CHILD-STDOUT" not in out.read_text(encoding="utf-8")
    assert capsys.readouterr().out == json.dumps(report, indent=2, sort_keys=True) + "\n"


def test_unrecognized_arg_writes_error_and_exit_code_3(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    assert wc_apply.main(["--bogus", "--result-file", str(out)]) == 3
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["exit_code"] == 3
    assert "unrecognized argument" in written["error"]


def test_conflicting_channels_refusal_still_writes_the_file(tmp_path: Path) -> None:
    out = tmp_path / "r.json"
    argv = ["--decisions", "{}", "--decisions-file", str(tmp_path / "d.json"), "--result-file", str(out)]
    assert wc_apply.main(argv) == 3
    assert json.loads(out.read_text(encoding="utf-8"))["exit_code"] == 3


def test_result_file_without_a_value_is_refused(capsys: pytest.CaptureFixture) -> None:
    assert wc_apply.main(["--result-file"]) == 3
    assert "--result-file requires a value" in capsys.readouterr().err


def test_exception_from_apply_writes_null_exit_code_and_reraises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    def boom(**kw: Any):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(wc_apply, "apply", boom)
    out = tmp_path / "r.json"
    with pytest.raises(RuntimeError):
        wc_apply.main(["--result-file", str(out)])
    assert json.loads(out.read_text(encoding="utf-8")) == {
        "error": "RuntimeError: kaboom", "exit_code": None,
    }


def test_partial_mutation_carries_partial_state_with_push_landed(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    directives = [_directive("d_step3_consolidate"), _directive("d_after")]
    code, report = _execute(monkeypatch, root, directives, fail=frozenset({"d_after"}))
    assert code == int(wc_apply.WorkdayApplyExitCode.PARTIAL_MUTATION)
    state = report["partial_state"]
    assert state["landed_this_pass"] == ["d_step3_consolidate"]
    assert state["push_landed"] is True
    assert "Rerun the same apply" in state["resume"]


def test_push_landed_in_an_earlier_pass_still_reports_push_landed(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    directives = [_directive("d_step3_consolidate"), _directive("d_b")]
    _execute(monkeypatch, root, directives, fail=frozenset({"d_b"}))
    _, report = _execute(monkeypatch, root, directives, fail=frozenset({"d_b"}))
    assert report["partial_state"]["landed_this_pass"] == []
    assert report["partial_state"]["push_landed"] is True


def test_failed_pass_without_the_push_reports_push_not_landed(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    directives = [_directive("d_a"), _directive("d_b")]
    _, report = _execute(monkeypatch, root, directives, fail=frozenset({"d_b"}))
    assert report["partial_state"]["push_landed"] is False


def test_a_resumed_directive_is_not_in_landed_this_pass(
    monkeypatch: pytest.MonkeyPatch, root: Path
) -> None:
    directives = [_directive("d_a"), _directive("d_b")]
    _execute(monkeypatch, root, directives, fail=frozenset({"d_b"}))
    _, report = _execute(monkeypatch, root, directives, fail=frozenset({"d_b"}))
    assert report["resumed"] == ["d_a"]
    assert report["partial_state"]["landed_this_pass"] == []


def test_a_clean_run_has_no_partial_state(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    _, report = _execute(monkeypatch, root, [_directive("d_a")])
    assert "partial_state" not in report


def test_without_the_flag_no_file_is_created(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture,
) -> None:
    report = {"landed": []}
    monkeypatch.setattr(wc_apply, "apply", lambda **kw: (0, report))
    monkeypatch.chdir(tmp_path)
    assert wc_apply.main([]) == 0
    assert list(tmp_path.iterdir()) == []
    assert capsys.readouterr().out == json.dumps(report, indent=2, sort_keys=True) + "\n"
