"""pipeline_run: resolver and revisions-file contract."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import pipeline_run as pr
from coordinator_core.ops.dispatch_emit.pipeline_contract import RUN_DIR_ROOT

RID = "pipeline-research-20261024T000000-abc123"


def _emit(root, run_id=RID):
    d = root / RUN_DIR_ROOT
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{run_id}.workflow.mjs.emitted.json").write_text("{}", encoding="utf-8")
    return d / f"{run_id}.workflow.mjs"


def test_resolves_emitted_run(tmp_path):
    script = _emit(tmp_path)
    assert pr.emitted_pipeline_run(tmp_path, RID) == script


def test_unemitted_wrong_prefix_and_unsafe_ids_refuse(tmp_path):
    assert pr.emitted_pipeline_run(tmp_path, RID) is None
    _emit(tmp_path, "ask-20261024T000000-abc123")
    assert pr.emitted_pipeline_run(tmp_path, "ask-20261024T000000-abc123") is None
    assert pr.emitted_pipeline_run(tmp_path, "pipeline-../x") is None
    assert pr.emitted_pipeline_run(tmp_path, "pipeline-a/b") is None


def test_revisions_rel_and_marker():
    assert pr.revisions_rel(RID) == f"scratch/warp/{RID}/revisions.md"
    assert pr.reads_revisions(f"x\n{pr.REVISIONS_MARKER}\ny")
    assert not pr.reads_revisions("no marker here")


def test_state_unknown_live_finished(tmp_path):
    assert pr.pipeline_run_state(tmp_path, RID) == "unknown"
    assert pr.pipeline_run_state(tmp_path, "pipeline-../x") == "unknown"
    _emit(tmp_path)
    assert pr.pipeline_run_state(tmp_path, RID) == "live"
    marker = tmp_path / pr.finished_rel(RID)
    assert pr.finished_rel(RID) == f"{RUN_DIR_ROOT}/{RID}/finished.md"
    marker.parent.mkdir(parents=True)
    marker.write_text("last stage\n", encoding="utf-8")
    assert pr.pipeline_run_state(tmp_path, RID) == "finished"


def test_append_creates_dir_and_appends_blocks(tmp_path):
    p1 = pr.append_revision(tmp_path, RID, "first note\n", "em-1", "revision")
    p2 = pr.append_revision(tmp_path, RID, "second note", "em-2", "revision")
    assert p1 == p2 == tmp_path / pr.revisions_rel(RID)
    text = p1.read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text.count("## ") == 2
    assert text.index("first note") < text.index("second note")
    assert "from em-1 (revision)" in text and "from em-2 (revision)" in text
