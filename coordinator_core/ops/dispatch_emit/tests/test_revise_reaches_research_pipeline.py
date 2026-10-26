"""workflow.revise on an emitted research pipeline run delivers to its revisions file or refuses by name."""
from __future__ import annotations

import pytest

from coordinator_core.ops.dispatch_emit.pipeline_contract import RUN_DIR_ROOT as DIR
from coordinator_core.ops.dispatch_emit.pipeline_run import REVISIONS_MARKER as MARKER

RID = "pipeline-research-20261024T000000-abc123"


def _emit(root, body, run_id=RID):
    d = root / DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.workflow.mjs").write_text(body, encoding="utf-8")
    (d / f"{run_id}.workflow.mjs.emitted.json").write_text("{}", encoding="utf-8")


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _call(repo, **params):
    from coordinator_core.ops.dispatch_emit import requirements_ledger as rl

    return rl._handler({"run_id": RID, **params}, repo_root=repo)


def test_note_only_revision_delivers(repo):
    _emit(repo, f"// {MARKER}\n")
    reply = _call(repo, note="narrow to cell A")
    assert reply["ok"] is True and reply["delivered"] is True
    assert "held" not in reply and reply["run_live"] is None and reply["item"] is None
    assert reply["revisions"] == f"{DIR}/{RID}/revisions.md"
    assert "narrow to cell A" in (repo / reply["revisions"]).read_text(encoding="utf-8")


def test_verbatim_revision_delivers_with_sender(repo):
    _emit(repo, f"// {MARKER}\n")
    reply = _call(repo, verbatim="also cover B", **{"from": "peer-1"})
    assert reply["delivered"] is True and "held" not in reply
    text = (repo / reply["revisions"]).read_text(encoding="utf-8")
    assert "also cover B" in text and "peer-1" in text


def test_pre_marker_script_refuses(repo):
    _emit(repo, "// old script\n")
    reply = _call(repo, note="x")
    assert "predates revision delivery" in reply["error"] and "held" not in reply
    assert not (repo / DIR / RID).exists()


@pytest.mark.parametrize("flag", [{"all_of_pm_ask": True}, {"build_only": True}, {"stop_after": "plan"}])
def test_ledger_flag_refuses_by_name(repo, flag):
    _emit(repo, f"// {MARKER}\n")
    reply = _call(repo, **flag)
    assert "research pipeline" in reply["error"] and "held" not in reply
    assert not (repo / DIR / RID).exists()


def test_oversize_note_refuses(repo):
    from coordinator_core.ops.dispatch_emit.requirements_ledger import VERBATIM_BYTE_CAP

    _emit(repo, f"// {MARKER}\n")
    reply = _call(repo, note="x" * (VERBATIM_BYTE_CAP + 1))
    assert "exceeds" in reply["error"] and "delivered" not in reply
    assert not (repo / DIR / RID).exists()


def test_finished_run_is_not_delivered(repo):
    _emit(repo, f"// {MARKER}\n")
    (repo / DIR / RID).mkdir(parents=True)
    (repo / DIR / RID / "finished.md").write_text("done", encoding="utf-8")
    reply = _call(repo, note="x")
    assert "finished" in reply["error"] and "not delivered" in reply["error"]
    assert "delivered" not in reply and "held" not in reply
    assert not (repo / DIR / RID / "revisions.md").exists()


def test_unemitted_pipeline_id_is_not_delivered(repo):
    reply = _call(repo, note="x")
    assert "not delivered" in reply["error"]
    assert "delivered" not in reply and "revisions" not in reply and "held" not in reply
    assert not (repo / DIR / RID).exists()
