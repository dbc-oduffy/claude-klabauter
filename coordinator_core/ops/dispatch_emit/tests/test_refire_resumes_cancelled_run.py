"""A run cancelled by workflow.cancel is a halted run a refire resumes; `cancelled` is a classified halt kind."""

from __future__ import annotations

import json

import pytest

from coordinator_core import _settings_home
from coordinator_core.ops.dispatch_emit import halt_remedy_class as hrc
from coordinator_core.ops.dispatch_emit import halted_run as hr
from coordinator_core.ops.dispatch_emit import live_run_index as idx
from coordinator_core.ops.dispatch_emit import run_cancel
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

SIZING = "state/sizings/2026-11/2026-11-15-foo.yaml"
RUN = "warp-2026-11-15-foo-20261115T000000-bbbbbb"


@pytest.fixture(autouse=True)
def _isolated_machine_local(tmp_path, monkeypatch):
    monkeypatch.setattr(_settings_home, "machine_local_dir", lambda: tmp_path / "ml-home")


def _cancelled_run(root):
    (root / ".git").mkdir()
    d = root / RECORD_DIR_ROOT / RUN
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"sizing_rel": SIZING}), encoding="utf-8")
    assert idx.claim(root, RUN, ["a.py"]) == {}
    assert run_cancel._handler({"run_id": RUN}, repo_root=root)["ok"] is True
    return d


def test_refire_resumes_the_cancelled_run(tmp_path):
    d = _cancelled_run(tmp_path)

    assert hr.halted_run_for(tmp_path, SIZING) == RUN
    assert hr.halted_run_to_resume(tmp_path, SIZING) == RUN

    assert hr.mark_resumed(d) is True
    assert run_cancel.cancelled_refusal(d) is None
    assert json.loads((d / hr.HALTED_FILE).read_text(encoding="utf-8"))["status"] == hr.STATUS_RESUMED


def test_a_resumed_cancelled_run_is_not_resumed_twice(tmp_path):
    d = _cancelled_run(tmp_path)
    assert hr.mark_resumed(d) is True
    assert hr.halted_run_for(tmp_path, SIZING) != RUN


def test_cancelled_is_a_mechanical_halt_whose_remedy_is_a_refire():
    assert hrc.is_mechanical(run_cancel.CANCELLED)
    assert hrc.HALT_REMEDY[run_cancel.CANCELLED] == (hrc.MECHANICAL, "refire-resumes-kept-work")
