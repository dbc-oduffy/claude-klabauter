"""kept.json blobs absent from the object store are reported and recoverable."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import doctor_kept_blobs as dkb
from coordinator_core.ops.tests._kept_blob_fixture import commit_at, git_repo, kept, write_transcript

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(repo: Path, *args: str, data: bytes | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        input=data,
        capture_output=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.decode().strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    return repo


def _kept(repo: Path, run: str, paths: dict, status: str | None = None, post: dict | None = None) -> None:
    d = repo / "state" / "warp" / run
    d.mkdir(parents=True)
    (d / "kept.json").write_text(json.dumps({"chunks": {"C1": {"paths": paths}}, "post_checkpoint": post or {}}))
    if status:
        (d / "halted.json").write_text(json.dumps({"status": status}))


def test_absent_blob_is_reported_present_is_not(tmp_path):
    repo = _repo(tmp_path)
    present = _git(repo, "hash-object", "-w", "--stdin", data=b"kept\n")
    absent = dkb.blob_sha(b"never stored\n")
    _kept(repo, "run-a", {"a.txt": present, "b.txt": absent}, post={"c.txt": absent})
    missing = dkb.sweep(repo)
    assert {(m.path, m.landed) for m in missing} == {("b.txt", False), ("c.txt", False)}
    layer = dkb.kept_blobs_layer(repo)
    assert layer.status == "broken" and "NOT LANDED" in layer.findings[0].message


def test_all_present_is_ok_and_landed_flagged(tmp_path):
    repo = _repo(tmp_path)
    sha = _git(repo, "hash-object", "-w", "--stdin", data=b"x")
    _kept(repo, "run-a", {"a.txt": sha}, status="landed")
    assert dkb.kept_blobs_layer(repo).status == "ok"
    _kept(repo, "run-b", {"z.txt": dkb.blob_sha(b"zz")}, status="landed")
    layer = dkb.kept_blobs_layer(repo)
    assert layer.status == "broken" and "(landed)" in layer.findings[0].message


def test_worktree_bytes_make_blob_rescuable_and_recover_writes_it(tmp_path):
    repo = _repo(tmp_path)
    (repo / "f.txt").write_bytes(b"hello\n")
    sha = dkb.blob_sha(b"hello\n")
    _kept(repo, "run-a", {"f.txt": sha})
    assert dkb.sweep(repo)[0].worktree_holds
    res = dkb.recover(repo, transcript_roots=[])
    assert [r.outcome for r in res] == ["rescued-worktree"]
    assert _git(repo, "cat-file", "-t", sha) == "blob"
    assert dkb.sweep(repo) == []


def _transcript(root: Path, run: str, calls: list, name: str = "agent-1.jsonl") -> None:
    write_transcript(
        root / name,
        [("use", f"{name}-{i}", n, inp, f"2026-10-25T00:00:{i:02d}Z") for i, (n, inp) in enumerate(calls)],
    )


def test_replay_reproducing_kept_sha_is_written(tmp_path):
    repo = _repo(tmp_path)
    final = b"alpha\nBETA\n"
    sha = dkb.blob_sha(final)
    _kept(repo, "run-r", {"n.txt": sha})
    tr = tmp_path / "tr"
    _transcript(tr, "run-r", [
        ("Write", {"file_path": str(repo / "n.txt"), "content": "alpha\nbeta\n"}),
        ("Edit", {"file_path": str(repo / "n.txt"), "old_string": "beta", "new_string": "BETA"}),
    ])
    res = dkb.recover(repo, transcript_roots=[tr])
    assert [r.outcome for r in res] == ["rescued-replay"]
    assert _git(repo, "cat-file", "-p", sha) == "alpha\nBETA"
    assert not (repo / "n.txt").exists()


def test_replay_not_matching_is_lost_and_nothing_written(tmp_path):
    repo = _repo(tmp_path)
    sha = dkb.blob_sha(b"the real bytes\n")
    _kept(repo, "run-r", {"n.txt": sha})
    tr = tmp_path / "tr"
    _transcript(tr, "run-r", [("Write", {"file_path": str(repo / "n.txt"), "content": "other\n"})])
    res = dkb.recover(repo, transcript_roots=[tr])
    assert [r.outcome for r in res] == ["lost"]
    assert len(dkb.sweep(repo)) == 1
    assert not list((repo / ".git" / "objects").glob(f"{sha[:2]}/*"))


def test_crlf_file_under_autocrlf_is_rescuable_raw(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "config", "core.autocrlf", "true")
    (repo / "c.txt").write_bytes(b"one\r\ntwo\r\n")
    sha = dkb.blob_sha(b"one\r\ntwo\r\n")
    _kept(repo, "run-c", {"c.txt": sha})
    [m] = dkb.sweep(repo)
    assert m.worktree_holds and m.mode == "raw"
    [r] = dkb.recover(repo, transcript_roots=[])
    assert (r.outcome, r.mode) == ("rescued-worktree", "raw")
    assert _git(repo, "cat-file", "-t", sha) == "blob"


def _commit(repo: Path, rel: str, data: bytes) -> None:
    (repo / rel).write_bytes(data)
    _git(repo, "add", rel)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "x")


def test_kept_raw_sha_equal_to_head_checked_out_crlf_bytes_is_landed(tmp_path):
    repo = _repo(tmp_path)
    _git(repo, "config", "core.autocrlf", "true")
    _commit(repo, "l.txt", b"a\r\nb\r\n")
    _kept(repo, "run-l", {"l.txt": dkb.blob_sha(b"a\r\nb\r\n")})
    assert dkb.sweep(repo) == []
    assert dkb.kept_blobs_layer(repo).status == "ok"


def test_worktree_reset_to_differing_head_content_is_still_missing(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, "t.txt", b"head content\n")
    kept = dkb.blob_sha(b"kept content, lost\n")
    _kept(repo, "run-t", {"t.txt": kept})
    (repo / "t.txt").write_bytes(b"head content\n")
    [m] = dkb.sweep(repo)
    assert (m.path, m.sha, m.worktree_holds) == ("t.txt", kept, False)
    assert dkb.kept_blobs_layer(repo).status == "broken"


def test_recover_op_is_registered_and_takes_repo_root(tmp_path):
    import coordinator_core.ipc as ipc
    from coordinator_core.ops import _registry_map

    assert _registry_map.OP_MODULE_MAP["doctor.recover_kept_blobs"] == "coordinator_core.ops.doctor_kept_blobs"
    assert "doctor.recover_kept_blobs" in ipc._REGISTRY
    repo = _repo(tmp_path)
    (repo / "f.txt").write_bytes(b"hello\n")
    _kept(repo, "run-a", {"f.txt": dkb.blob_sha(b"hello\n")})
    out = dkb._recover_op({"repo_root": str(repo)})
    assert out["counts"] == {"rescued-worktree": 1}
    assert out["results"][0]["path"] == "f.txt"
    import pytest

    with pytest.raises(ipc.CallerFacingValidationError):
        dkb._recover_op({"repo_root": str(tmp_path / "nowhere")})


_BASE = b"a\nb\nc\nd\ne\nf\ng\n"
_KEPT = b"a\nB\nc\nd\ne\nf\ng\n"


def _edit_b(repo: Path) -> tuple:
    return ("Edit", {"file_path": str(repo / "a.txt"), "old_string": "\nb\n", "new_string": "\nB\n"})


def test_blob_built_only_from_another_sessions_transcript_is_rescued(tmp_path):
    repo = _repo(tmp_path)
    sha = dkb.blob_sha(b"alpha\nBETA\n")
    _kept(repo, "run-r", {"n.txt": sha})
    tr = tmp_path / "tr"
    _transcript(tr / "session-a", "run-r", [("Write", {"file_path": str(repo / "n.txt"), "content": "alpha\nbeta\n"})])
    _transcript(
        tr / "session-b" / "subagents", "other-run",
        [("Edit", {"file_path": str(repo / "n.txt"), "old_string": "beta", "new_string": "BETA"})],
        name="agent-9.jsonl",
    )
    [r] = dkb.recover(repo, transcript_roots=[tr])
    assert (r.outcome, r.mode, r.base) == ("rescued-replay", "raw", "empty")
    assert _git(repo, "cat-file", "-t", sha) == "blob"


def test_multiedit_built_blob_is_rescued(tmp_path):
    repo = _repo(tmp_path)
    sha = dkb.blob_sha(b"X\nY\n")
    _kept(repo, "run-m", {"m.txt": sha})
    tr = tmp_path / "tr"
    fp = str(repo / "m.txt")
    _transcript(tr, "run-m", [
        ("Write", {"file_path": fp, "content": "x\ny\n"}),
        ("MultiEdit", {"file_path": fp, "edits": [
            {"old_string": "x", "new_string": "X"}, {"old_string": "y", "new_string": "Y"}]}),
    ])
    [r] = dkb.recover(repo, transcript_roots=[tr])
    assert r.outcome == "rescued-replay"
    assert _git(repo, "cat-file", "-p", sha) == "X\nY"


def test_recovered_blob_already_in_head_is_superseded_with_landing_commit(tmp_path):
    repo = git_repo(tmp_path)
    commit_at(repo, "a.txt", _BASE, "2026-01-01T00:00:00Z")
    c1, _ = commit_at(repo, "a.txt", b"a\nB\nc\nd\ne\nF\ng\n", "2026-01-02T00:00:00Z")
    sha = dkb.blob_sha(_KEPT)
    kept(repo, "run-s", {"a.txt": sha})
    tr = tmp_path / "tr"
    _transcript(tr, "run-s", [_edit_b(repo)])
    [r] = dkb.recover(repo, transcript_roots=[tr])
    assert (r.outcome, r.verdict, r.commit) == ("rescued-replay", "SUPERSEDED", c1[:10])
    assert r.base is not None and r.base != "empty"


def test_recovered_blob_whose_hunk_is_missing_from_head_is_not_landed(tmp_path):
    repo = git_repo(tmp_path)
    commit_at(repo, "a.txt", _BASE, "2026-01-01T00:00:00Z")
    sha = dkb.blob_sha(_KEPT)
    kept(repo, "run-n", {"a.txt": sha})
    tr = tmp_path / "tr"
    _transcript(tr, "run-n", [_edit_b(repo)])
    [r] = dkb.recover(repo, transcript_roots=[tr])
    assert (r.outcome, r.verdict, r.commit) == ("rescued-replay", "NOT-LANDED", None)
    assert "missing from HEAD" in r.why


def test_lost_blob_has_no_verdict(tmp_path):
    repo = git_repo(tmp_path)
    commit_at(repo, "a.txt", _BASE, "2026-01-01T00:00:00Z")
    kept(repo, "run-x", {"a.txt": dkb.blob_sha(b"unreachable\n")})
    tr = tmp_path / "tr"
    _transcript(tr, "run-x", [_edit_b(repo)])
    [r] = dkb.recover(repo, transcript_roots=[tr])
    assert (r.outcome, r.verdict, r.base, r.why) == ("lost", None, None, "no replay base")


def test_op_result_row_carries_verdict_commit_base_and_why(tmp_path, monkeypatch):
    from coordinator_core.ops import kept_blob_transcripts as kbt

    repo = git_repo(tmp_path)
    commit_at(repo, "a.txt", _BASE, "2026-01-01T00:00:00Z")
    kept(repo, "run-o", {"a.txt": dkb.blob_sha(_KEPT)})
    tr = tmp_path / "tr"
    _transcript(tr, "run-o", [_edit_b(repo)])
    monkeypatch.setattr(kbt, "default_transcript_root", lambda: tr)
    out = dkb._recover_op({"repo_root": str(repo)})
    assert out["counts"] == {"rescued-replay": 1, "verdict:NOT-LANDED": 1}
    row = out["results"][0]
    assert row["verdict"] == "NOT-LANDED" and row["commit"] is None
    assert row["base"] and row["why"]


def _budget_repo(tmp_path: Path) -> tuple:
    tmp_path.mkdir(parents=True)
    repo = git_repo(tmp_path)
    c0, _ = commit_at(repo, "a.txt", _BASE, "2026-01-01T00:00:00Z")
    commit_at(repo, "b.txt", _BASE, "2026-01-02T00:00:00Z")
    kept(repo, "run-b", {"a.txt": dkb.blob_sha(_KEPT), "b.txt": dkb.blob_sha(b"a\nB\nc\nd\ne\nf\ng\nh\n")})
    tr = tmp_path / "tr"
    _transcript(tr, "run-b", [
        _edit_b(repo),
        ("Edit", {"file_path": str(repo / "b.txt"), "old_string": "g\n", "new_string": "g\nh\n"}),
        ("Edit", {"file_path": str(repo / "b.txt"), "old_string": "\nb\n", "new_string": "\nB\n"}),
    ])
    return repo, c0, tr


def test_recover_spawn_budget_is_sweep_plus_history_log(tmp_path, monkeypatch):
    # run_git spawns through Popen, so that is the seam to count; spying subprocess.run sees nothing.
    real = subprocess.Popen
    calls: list = []

    def spy(cmd, *a, **kw):
        calls.append(cmd)
        return real(cmd, *a, **kw)

    repo, c0, tr = _budget_repo(tmp_path / "plain")
    monkeypatch.setattr(subprocess, "Popen", spy)
    dkb.sweep(repo)
    sweep_n, calls[:] = len(calls), []
    assert sweep_n > 0
    res = dkb.recover(repo, transcript_roots=[tr])
    assert [r.outcome for r in res] == ["rescued-replay", "rescued-replay"]
    assert len(calls) <= sweep_n + 2

    monkeypatch.setattr(subprocess, "Popen", real)
    repo, c0, tr = _budget_repo(tmp_path / "based")
    monkeypatch.setattr(subprocess, "Popen", spy)
    calls[:] = []
    dkb.recover(repo, base_sha=c0, transcript_roots=[tr])
    assert len(calls) <= sweep_n + 3
