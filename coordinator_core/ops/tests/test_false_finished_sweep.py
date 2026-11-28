"""run.false_finished_sweep lists committed runs whose sizing still owes register rows."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops import false_finished_sweep as ffs

_OPEN = "state/sizings/open.yaml"
_SETTLED = "state/sizings/settled.yaml"
_NOREG = "state/sizings/noreg.yaml"

_OPEN_TEXT = "status: draft\ninteraction_mode: autonomous\nrequirement_register:\n  rows:\n  - id: R1\n    surface: s\n    status: open\n"
_SETTLED_TEXT = (
    "status: draft\ninteraction_mode: autonomous\nrequirement_register:\n  rows:\n"
    "  - id: R1\n    surface: s\n    status: met\n    wired: true\n"
)


def _sizing(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _run(root: Path, run_id: str, *, sizing: str, halted: dict | None = None, stage: str | None = None) -> None:
    d = root / "state" / "warp" / run_id
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    if halted is not None:
        (d / "halted.json").write_text(json.dumps(halted), encoding="utf-8")
    if stage is not None:
        (d / "run-stage.json").write_text(json.dumps({"stage": stage}), encoding="utf-8")


def _build(root: Path) -> None:
    _sizing(root, _OPEN, _OPEN_TEXT)
    _sizing(root, _SETTLED, _SETTLED_TEXT)
    _sizing(root, _NOREG, "status: draft\ninteraction_mode: autonomous\n")
    _run(root, "ask-landed-open", sizing=_OPEN, halted={"status": "landed"})
    _run(root, "ask-stage-landed-open", sizing=_OPEN, stage="landed")
    _run(root, "ask-landed-settled", sizing=_SETTLED, halted={"status": "landed"})
    _run(root, "ask-landed-noreg", sizing=_NOREG, halted={"status": "landed"})
    _run(root, "ask-halted-open", sizing=_OPEN, halted={"status": "halted"})
    _run(root, "ask-closed-open", sizing=_OPEN, halted={"status": "closed"})
    _run(root, "ask-live-open", sizing=_OPEN, stage="staged")
    _run(root, "ask-landed-missing", sizing="state/sizings/gone.yaml", halted={"status": "landed"})
    (root / "state" / "warp" / ".coordinator-local").mkdir()


def test_lists_only_committed_runs_with_open_rows(tmp_path: Path) -> None:
    _build(tmp_path)
    reply = ffs._handler({"repo_roots": [str(tmp_path)]})
    repo = reply["repos"][str(tmp_path)]
    assert [f["run_id"] for f in repo["false_finished"]] == ["ask-landed-open", "ask-stage-landed-open"]
    first = repo["false_finished"][0]
    assert first["sizing"] == _OPEN
    assert [r["id"] for r in first["open_rows"]] == ["R1"]
    assert repo["committed"] == 5
    assert repo["unresolved"] == 1
    assert repo["runs_scanned"] == 8
    assert reply["total"] == 2


def test_halted_closed_and_live_runs_are_never_listed(tmp_path: Path) -> None:
    _build(tmp_path)
    listed = {f["run_id"] for f in ffs.sweep_repo(tmp_path)["false_finished"]}
    assert not listed & {"ask-halted-open", "ask-closed-open", "ask-live-open"}


def test_each_repo_is_listed_under_its_own_root(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    _build(a)
    (b / "state" / "warp").mkdir(parents=True)
    reply = ffs._handler({"repo_roots": [str(a), str(b)]})
    assert reply["repos"][str(a)]["false_finished"]
    assert reply["repos"][str(b)]["false_finished"] == []


def test_unreadable_warp_dir_reports_error_not_clean(tmp_path: Path) -> None:
    assert "error" in ffs.sweep_repo(tmp_path / "nowhere")


@pytest.mark.parametrize("params", [{}, {"repo_roots": []}, {"repo_roots": [""]}, {"repo_roots": "x"}, {"repo_roots": ["rel/repo"]}])
def test_missing_repo_roots_is_a_typed_refusal_naming_the_param(params: dict) -> None:
    with pytest.raises(CallerFacingValidationError, match="repo_roots"):
        ffs._handler(params)
