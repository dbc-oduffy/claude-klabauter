"""commit_act_paths: the paths a post_commit commit row obliges the terminal commit to carry."""
from __future__ import annotations

from coordinator_core.ops.dispatch_emit import requirements_ledger as rl
from coordinator_core.ops.dispatch_emit.post_commit_paths import commit_act_paths


def _verdict(evidence, acts=("commit",), status="post_commit"):
    return {"items": [{"id": "R1", "status": status, "post_commit_acts": list(acts), "evidence": evidence}]}


def test_directory_named_in_evidence(tmp_path):
    (tmp_path / "runs" / "r2").mkdir(parents=True)
    (tmp_path / "runs" / "r2" / "a.txt").write_text("x")
    v = _verdict("present untracked with 10 files at `runs/r2/`.")
    assert commit_act_paths(tmp_path, v) == ("runs/r2",)


def test_file_named_sorted_deduped(tmp_path):
    (tmp_path / "b.md").write_text("x")
    (tmp_path / "a.md").write_text("x")
    assert commit_act_paths(tmp_path, _verdict("b.md a.md b.md")) == ("a.md", "b.md")


def test_nothing_named_yields_no_obligation(tmp_path):
    assert commit_act_paths(tmp_path, _verdict("committed with the run's own writes")) == ()
    assert commit_act_paths(tmp_path, {}) == ()


def test_missing_and_unsafe_paths_dropped(tmp_path):
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("x")
    root = tmp_path / "repo"
    root.mkdir()
    v = _verdict("../outside-secret.txt gone.txt . ..")
    assert commit_act_paths(root, v) == ()


def test_non_commit_act_ignored(tmp_path):
    (tmp_path / "a.md").write_text("x")
    assert commit_act_paths(tmp_path, _verdict("a.md", acts=("sha_reply",))) == ()
    assert commit_act_paths(tmp_path, _verdict("a.md", status="met")) == ()


def test_not_met_remaining_commit_normalised(tmp_path):
    (tmp_path / "d").mkdir()
    v = {"items": [{"id": "R1", "status": "not_met", "remaining": ["commit"], "evidence": "d"}]}
    assert commit_act_paths(tmp_path, v) == ("d",)


def test_paths_named_still_file_only(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "f.md").write_text("x")
    assert rl._paths_named(tmp_path, ["see d and `f.md`."]) == ["f.md"]
