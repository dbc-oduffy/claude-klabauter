"""workflow.revise adding a verbatim row to a live run on an S or XS arm is accepted: no refusal, no resize route."""

from __future__ import annotations

import subprocess

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.tests.test_ask_stage import SIZING_REL, repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process]


def _live(repo, run_id, tshirt, monkeypatch):  # noqa: F811
    doc = {
        "schema": "sizing-object", "name": "fixture", "intent": "Fix the thing",
        "estimate": {"tshirt": tshirt, "provisional": True}, "route": "spec-dispatch",
        "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 0, "text": "fix the thing"}],
        "exit_criterion": {"statement": "it works", "accepted": None},
        "deliverable_id": "dlv-xs-abc123",
    }
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q"], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())
    rl.start(repo, run_id)
    rl.bind(repo, run_id, SIZING_REL)
    monkeypatch.setattr(run_token, "is_live", lambda *_a, **_k: True)


@pytest.mark.parametrize("tshirt", ["XS", "S"])
def test_no_replan_arm_revise_is_accepted_with_no_resize_route(repo, monkeypatch, tshirt):  # noqa: F811
    _live(repo, "rw1", tshirt, monkeypatch)
    out = rl._handler({"run_id": "rw1", "verbatim": "also refuse undeclared writes"}, repo_root=repo)
    assert out["ok"] is True and out["run_live"] is True
    assert "resize" not in out and "sizing.supersede" not in out.get("warning", "")
    assert "no re-plan leg" not in out.get("warning", "")
    assert out["refused"] == []


def test_m_plus_arm_gets_no_resize_warning(repo, monkeypatch):  # noqa: F811
    _live(repo, "rw2", "M", monkeypatch)
    out = rl._handler({"run_id": "rw2", "verbatim": "also refuse undeclared writes"}, repo_root=repo)
    assert out["ok"] is True
    assert "resize" not in out and "sizing.supersede" not in out.get("warning", "")
    assert out["refused"] == []


def test_note_only_revision_on_no_replan_arm_adds_no_row_so_no_warning(repo, monkeypatch):  # noqa: F811
    _live(repo, "rw3", "S", monkeypatch)
    out = rl._handler({"run_id": "rw3", "note": "context only"}, repo_root=repo)
    assert out["ok"] is True and "resize" not in out
