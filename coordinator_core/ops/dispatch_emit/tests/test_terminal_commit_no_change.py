"""terminal_commit._no_change_refusal fires only when every non-kept row path is unwritten and the
review recorded no edit to it; _pre_existing_dirt_refusal skips kept and adopting rows."""

from __future__ import annotations

from types import SimpleNamespace

from coordinator_core.ops.dispatch_emit import terminal_commit as tc
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow, StageManifest
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest


def _manifest(root, *, kept="", adopts=False, baseline=True):
    (root / "a.py").write_text("one\n")
    row = ManifestRow(
        id="C1", agent_type="x", model="m", brief_path="b", writes=("a.py",), wave=0,
        register_rows=("R1",), kept_from=kept, adopts_stranded=adopts,
    )
    return StageManifest(
        run_dir="r", rows=(row,), review_declared_paths=(), marker_path="m",
        write_baseline=wb.capture(root, ["a.py"]) if baseline else {},
    )


def _request(review_only=False):
    return CommitRequest(chunks=(ChunkCommit(id="C1", title="t", paths=("a.py",)),), review_only=review_only)


def test_unwritten_path_refuses_and_a_written_one_does_not(tmp_path):
    m = _manifest(tmp_path)
    out = tc._no_change_refusal(tmp_path, m, _request(), None)
    assert out["refused"] == wb.NO_CHANGE_REFUSAL and out[wb.MEANT_TO_LAND_KEY] == ["a.py"]
    (tmp_path / "a.py").write_text("two\n")
    assert tc._no_change_refusal(tmp_path, m, _request(), None) is None


def _crlf_case(tmp_path, monkeypatch, index_sha_of):
    from coordinator_core.git import git_state
    from coordinator_core.ops.dispatch_emit import kept_work

    m = _manifest(tmp_path)
    (tmp_path / "a.py").write_bytes(b"one\r\n")
    base = kept_work.fingerprint(tmp_path, "a.py")
    m = StageManifest(
        run_dir="r", rows=m.rows, review_declared_paths=(), marker_path="m", write_baseline={"a.py": base},
    )
    monkeypatch.setattr(kept_work, "checkin_form", lambda root, rel, data: data.replace(b"\r\n", b"\n"))
    sha = index_sha_of(kept_work)
    monkeypatch.setattr(git_state, "read_index", lambda root: {"a.py": SimpleNamespace(sha=sha)})
    return tc._no_change_refusal(tmp_path, m, _request(), None)


def test_unchanged_crlf_path_routes_revise_the_ask(tmp_path, monkeypatch):
    out = _crlf_case(tmp_path, monkeypatch, lambda kw: kw.blob_sha(b"one\n"))
    assert "revise the ask" in out["error"] and "predates" not in out["error"]


def test_crlf_path_with_a_different_index_blob_still_routes_pre_existing_dirt(tmp_path, monkeypatch):
    out = _crlf_case(tmp_path, monkeypatch, lambda kw: kw.blob_sha(b"other\n"))
    assert "predates the run" in out["error"]


def test_review_recorded_edit_lifts_the_refusal(tmp_path):
    m = _manifest(tmp_path)
    assert tc._no_change_refusal(tmp_path, m, _request(), {"tests": {"edited": ["a.py"]}}) is None


def test_exemptions_keep_todays_behaviour(tmp_path):
    assert tc._no_change_refusal(tmp_path, _manifest(tmp_path, baseline=False), _request(), None) is None
    assert tc._no_change_refusal(tmp_path, _manifest(tmp_path, kept="run0"), _request(), None) is None
    assert tc._no_change_refusal(tmp_path, _manifest(tmp_path), _request(review_only=True), None) is None
    assert tc._no_change_refusal(tmp_path, _manifest(tmp_path), CommitRequest(chunks=()), None) is None


def test_ledger_rule_refuses_unwritten_and_skips_kept_adopting_or_written(tmp_path):
    ledger = SimpleNamespace(items=(1,), all_of_pm_ask=False)
    verdict = {"items": [{"id": "R1", "status": "post_commit"}]}
    out = tc._pre_existing_dirt_refusal(tmp_path, _manifest(tmp_path), verdict, ledger)
    assert out["refused"] == "ledger-unmet" and out["ledger_failures"][0]["reason"] == "pre-existing-dirt: a.py"
    assert tc._pre_existing_dirt_refusal(tmp_path, _manifest(tmp_path, kept="r"), verdict, ledger) is None
    assert tc._pre_existing_dirt_refusal(tmp_path, _manifest(tmp_path, adopts=True), verdict, ledger) is None
    m = _manifest(tmp_path)
    (tmp_path / "a.py").write_text("changed\n")
    assert tc._pre_existing_dirt_refusal(tmp_path, m, verdict, ledger) is None
    assert tc._pre_existing_dirt_refusal(tmp_path, m, {"items": [{"id": "R1", "status": "not_met"}]}, ledger) is None
