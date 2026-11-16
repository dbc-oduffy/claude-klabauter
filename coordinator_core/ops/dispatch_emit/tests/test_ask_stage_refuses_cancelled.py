"""dispatch.ask_stage refuses a run whose halted.json reads cancelled, and lets a resumed or otherwise-halted run stage."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_stage, halted_run, run_cancel
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.ask_stage import _handler
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import PLAN, PLAN_REL
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

_RUN = "cancelrun"
_SIZING = "state/sizings/2026-11/2026-11-15-foo.yaml"


def _seed(root) -> None:
    (root / ".git").mkdir(exist_ok=True)
    write_run_scripts(root, _RUN)
    (root / PLAN_REL).parent.mkdir(parents=True, exist_ok=True)
    (root / PLAN_REL).write_text(PLAN, encoding="utf-8", newline="\n")


def _params() -> dict:
    return {"run_id": _RUN, "plan_path": PLAN_REL, "writes": []}


def _halt(root, kind: str) -> None:
    halted_run.write_halt_marker(record_dir(root, _RUN), halted=kind, sizing_rel=_SIZING)


def test_cancelled_run_is_refused_and_stages_nothing(tmp_path, monkeypatch):
    _seed(tmp_path)
    _halt(tmp_path, run_cancel.CANCELLED)
    monkeypatch.setattr(ask_stage, "stage", lambda *a, **k: (_ for _ in ()).throw(AssertionError("staged")))

    reply = _handler(_params(), repo_root=tmp_path)

    assert set(reply) == {"error"} and "cancelled" in reply["error"] and _RUN in reply["error"]
    assert not (record_dir(tmp_path, _RUN) / "manifest.json").exists()


def test_resumed_run_stages(tmp_path):
    _seed(tmp_path)
    _halt(tmp_path, run_cancel.CANCELLED)
    assert halted_run.mark_resumed(record_dir(tmp_path, _RUN)) is True

    reply = _handler(_params(), repo_root=tmp_path)

    assert "error" not in reply and "manifest_handle" in reply


def test_malformed_marker_fails_open_to_ask_stage_own_handling(tmp_path):
    _seed(tmp_path)
    d = record_dir(tmp_path, _RUN)
    d.mkdir(parents=True, exist_ok=True)
    (d / halted_run.HALTED_FILE).write_text("{not json", encoding="utf-8")

    reply = _handler(_params(), repo_root=tmp_path)

    assert "cancelled" not in str(reply.get("error", ""))


def test_other_halt_kind_is_not_refused_as_cancelled(tmp_path):
    _seed(tmp_path)
    _halt(tmp_path, "criterion_not_met")

    reply = _handler(_params(), repo_root=tmp_path)

    assert "manifest_handle" in reply
