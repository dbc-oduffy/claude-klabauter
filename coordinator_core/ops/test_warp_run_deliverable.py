"""Tests for the shared warp-run reader."""

from __future__ import annotations

import json
import subprocess
import time

import pytest

from coordinator_core.ops import warp_run_deliverable as wrd
from coordinator_core.ops.dispatch_emit import run_token
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker

SESSION = "11111111-1111-1111-1111-111111111111"
SIZING = "state/sizings/a.yaml"
WRITES = ("src/a.py", "src/b.py")


@pytest.fixture(autouse=True)
def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    return tmp_path


def _sizing(repo, rel=SIZING, did="dlv-run-1", status="routed"):
    path = repo / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'status: {status}  # c\ndeliverable_id: "{did}"  # key\n', encoding="utf-8")


def _run(repo, run_id="r1", writes=WRITES, session=SESSION, sizing_in_submission=SIZING, reconciled=False):
    run_dir = repo / "scratch/warp" / run_id
    run_dir.mkdir(parents=True)
    marker = render_marker(
        CommitRequest(chunks=(ChunkCommit(id="C", title="t", paths=tuple(writes)),), version=1)
    )
    (run_dir / "commit-request.txt").write_text((marker or "") + "\n", encoding="utf-8")
    (run_dir / "submission.json").write_text(
        json.dumps({"session_id": session, "sizing_rel": sizing_in_submission, "submitted_at": 5.5}),
        encoding="utf-8",
    )
    if reconciled:
        (run_dir / "reconciled.json").write_text("{}", encoding="utf-8")
    return run_dir


def _mint(repo, run_id="r1", session=SESSION, sizing=SIZING):
    return run_token.mint(repo, run_id, "s.mjs", session_id=session, sizing_rel=sizing)


def test_live_run_returns_sizing_deliverable(repo):
    _sizing(repo)
    _run(repo)
    _mint(repo)
    assert wrd.live_run_deliverable_id(repo, SESSION, ["src/a.py"]) == "dlv-run-1"
    assert wrd.live_run_deliverable_id(repo, SESSION, ["src\\a.py"]) == "dlv-run-1"


def test_no_token_returns_empty(repo):
    _sizing(repo)
    _run(repo)
    assert wrd.live_run_deliverable_id(repo, SESSION, ["src/a.py"]) == ""


def test_other_session_returns_empty(repo):
    _sizing(repo)
    _run(repo)
    _mint(repo, session="22222222-2222-2222-2222-222222222222")
    assert wrd.live_run_deliverable_id(repo, SESSION, ["src/a.py"]) == ""


def test_raw_ask_token_without_sizing_returns_empty(repo):
    _sizing(repo)
    _run(repo)
    _mint(repo, sizing=None)
    assert wrd.live_run_deliverable_id(repo, SESSION, ["src/a.py"]) == ""


def test_consumed_token_returns_empty(repo):
    _sizing(repo)
    _run(repo)
    _mint(repo)
    run_token.consume(repo, "r1")
    assert wrd.live_run_deliverable_id(repo, SESSION, ["src/a.py"]) == ""


def test_expired_token_returns_empty(repo):
    _sizing(repo)
    _run(repo)
    _mint(repo)
    record = repo / ".git" / "coordinator-warp-run-tokens" / "r1.json"
    data = json.loads(record.read_text(encoding="utf-8"))
    data["minted_at"] = time.time() - run_token.TTL_SECONDS - 10
    record.write_text(json.dumps(data), encoding="utf-8")
    assert wrd.live_run_deliverable_id(repo, SESSION, ["src/a.py"]) == ""


def test_two_distinct_ids_return_empty(repo):
    _sizing(repo, SIZING, "dlv-one")
    _sizing(repo, "state/sizings/b.yaml", "dlv-two")
    _run(repo, "r1")
    _run(repo, "r2", sizing_in_submission="state/sizings/b.yaml")
    _mint(repo, "r1")
    _mint(repo, "r2", sizing="state/sizings/b.yaml")
    assert wrd.live_run_deliverable_id(repo, SESSION, ["src/a.py"]) == ""


def test_two_runs_same_id_return_it(repo):
    _sizing(repo)
    _run(repo, "r1")
    _run(repo, "r2")
    _mint(repo, "r1")
    _mint(repo, "r2")
    assert wrd.live_run_deliverable_id(repo, SESSION, ["src/a.py"]) == "dlv-run-1"


def test_disjoint_staged_paths_return_empty(repo):
    _sizing(repo)
    _run(repo)
    _mint(repo)
    assert wrd.live_run_deliverable_id(repo, SESSION, ["other.py"]) == ""
    assert wrd.live_run_deliverable_id(repo, SESSION, []) == ""


def test_reconciled_run_returns_empty(repo):
    _sizing(repo)
    _run(repo, reconciled=True)
    _mint(repo)
    assert wrd.live_run_deliverable_id(repo, SESSION, ["src/a.py"]) == ""


def test_run_records_reads_submission(repo):
    _run(repo)
    (rec,) = list(wrd.run_records(repo))
    assert (rec.run_id, rec.session_id, rec.sizing_rel) == ("r1", SESSION, SIZING)
    assert rec.submitted_at == 5.5
    assert rec.writes == WRITES
    assert rec.reconciled is False


def test_run_records_falls_back_to_record_run_json(repo):
    _run(repo, sizing_in_submission=None)
    rec_dir = repo / "state/warp/r1"
    rec_dir.mkdir(parents=True)
    (rec_dir / "run.json").write_text(json.dumps({"sizing_rel": "state\\sizings\\z.yaml"}), encoding="utf-8")
    (rec,) = list(wrd.run_records(repo))
    assert rec.sizing_rel == "state/sizings/z.yaml"


def test_run_records_reconciled_flag_and_no_marker(repo):
    run_dir = _run(repo, reconciled=True)
    (run_dir / "commit-request.txt").unlink()
    (rec,) = list(wrd.run_records(repo))
    assert rec.reconciled is True
    assert rec.writes == ()


def test_run_records_empty_without_scratch(repo):
    assert list(wrd.run_records(repo)) == []


def test_write_reconciled_round_trips(repo):
    run_dir = _run(repo)
    path = wrd.write_reconciled(run_dir, SIZING, "dlv-run-1", ["abc", "def"])
    assert path.name == wrd.RECONCILED_FILE
    assert json.loads(path.read_text(encoding="utf-8"))["shas"] == ["abc", "def"]
    (rec,) = list(wrd.run_records(repo))
    assert rec.reconciled is True


def test_sizing_scalar_forms(repo):
    _sizing(repo)
    assert wrd.sizing_scalar(repo, SIZING, "status") == "routed"
    assert wrd.sizing_scalar(repo, SIZING, "deliverable_id") == "dlv-run-1"
    assert wrd.sizing_scalar(repo, SIZING, "missing") == ""
    assert wrd.sizing_scalar(repo, "nope.yaml", "status") == ""


PEER = "22222222-2222-2222-2222-222222222222"


@pytest.fixture
def live(monkeypatch):
    from coordinator_core.session import touch_record

    dead: set = set()
    monkeypatch.setattr(touch_record, "session_live", lambda sid, cwd=None: sid not in dead)
    return dead


def _manifest(repo, run_id, writes=(), doc_writes=(), raw=None):
    d = repo / "state/warp" / run_id
    d.mkdir(parents=True, exist_ok=True)
    body = raw if raw is not None else json.dumps({"rows": [{"id": "C1", "writes": list(writes)}], "doc_writes": list(doc_writes)})
    (d / "manifest.json").write_text(body, encoding="utf-8")


def _claims(repo, paths, **kw):
    kw.setdefault("exclude_session", SESSION)
    return wrd.live_run_write_claims(repo, paths, **kw)


def test_claims_manifest_row_path(repo, live):
    _manifest(repo, "p1", writes=["pkg/op.py"])
    run_token.mint(repo, "p1", "s.mjs", session_id=PEER)
    assert _claims(repo, ["pkg/op.py", "pkg/other.py"]) == {"pkg/op.py": [PEER]}


def test_claims_doc_writes_path(repo, live):
    _manifest(repo, "p1", doc_writes=["docs/a.md"])
    run_token.mint(repo, "p1", "s.mjs", session_id=PEER)
    assert _claims(repo, ["docs\\a.md"]) == {"docs/a.md": [PEER]}


def test_commit_request_prefix_claims_child(repo, live):
    run_dir = repo / "scratch/warp/p1"
    run_dir.mkdir(parents=True)
    marker = render_marker(
        CommitRequest(chunks=(ChunkCommit(id="C", title="t", prefixes=("pkg/tests/",)),), version=1)
    )
    (run_dir / "commit-request.txt").write_text((marker or "") + "\n", encoding="utf-8")
    run_token.mint(repo, "p1", "s.mjs", session_id=PEER)
    assert _claims(repo, ["pkg/tests/t.py", "pkg/testsx/t.py"]) == {"pkg/tests/t.py": [PEER]}


def test_own_session_excluded(repo, live):
    _manifest(repo, "p1", writes=["a.py"])
    run_token.mint(repo, "p1", "s.mjs", session_id=SESSION)
    assert _claims(repo, ["a.py"]) == {}


def test_ownerless_token_excluded(repo, live):
    _manifest(repo, "p1", writes=["a.py"])
    run_token.mint(repo, "p1", "s.mjs")
    assert _claims(repo, ["a.py"]) == {}


def test_dead_session_excluded(repo, live):
    _manifest(repo, "p1", writes=["a.py"])
    run_token.mint(repo, "p1", "s.mjs", session_id=PEER)
    live.add(PEER)
    assert _claims(repo, ["a.py"]) == {}


def test_claimed_token_counts(repo, live):
    _manifest(repo, "p1", writes=["a.py"])
    token = run_token.mint(repo, "p1", "s.mjs", session_id=PEER)
    assert run_token.redeem(repo, "p1", token, "s.mjs")
    assert _claims(repo, ["a.py"]) == {"a.py": [PEER]}


def test_expired_token_does_not_count(repo, live, monkeypatch):
    _manifest(repo, "p1", writes=["a.py"])
    run_token.mint(repo, "p1", "s.mjs", session_id=PEER)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + run_token.TTL_SECONDS + 5)
    assert _claims(repo, ["a.py"]) == {}


def test_malformed_manifest_without_commit_request_is_unknown(repo, live):
    _manifest(repo, "p1", raw="{not json")
    run_token.mint(repo, "p1", "s.mjs", session_id=PEER)
    assert _claims(repo, ["a.py"]) is None


def test_malformed_manifest_with_commit_request_falls_back(repo, live):
    _manifest(repo, "p1", raw="{not json")
    _run(repo, run_id="p1", writes=("a.py",))
    run_token.mint(repo, "p1", "s.mjs", session_id=PEER)
    assert _claims(repo, ["a.py"]) == {"a.py": [PEER]}


def test_no_declaration_files_claims_nothing(repo, live):
    run_token.mint(repo, "p1", "s.mjs", session_id=PEER)
    assert _claims(repo, ["a.py"]) == {}


def test_minted_before_keeps_only_senior_runs(repo, live):
    _manifest(repo, "p1", writes=["a.py"])
    run_token.mint(repo, "p1", "s.mjs", session_id=PEER)
    minted = next(iter(run_token.live_records(repo)))["minted_at"]
    assert _claims(repo, ["a.py"], minted_before=(minted + 1, "z")) == {"a.py": [PEER]}
    assert _claims(repo, ["a.py"], minted_before=(minted, "p1")) == {}
    assert _claims(repo, ["a.py"], minted_before=(minted, "p2")) == {"a.py": [PEER]}


def test_many_runs_many_paths_no_spawn_and_one_listing(repo, live, monkeypatch):
    from coordinator_core.git import run as git_run

    monkeypatch.setattr(git_run, "run_git", lambda *a, **k: (_ for _ in ()).throw(AssertionError("git")))
    for i in range(3):
        _manifest(repo, f"p{i}", writes=[f"f{j}.py" for j in range(0, 50, i + 1)])
        run_token.mint(repo, f"p{i}", "s.mjs", session_id=f"{i}" * 8)
    calls = []
    real = run_token.held_records
    monkeypatch.setattr(run_token, "held_records", lambda r: calls.append(1) or real(r))
    out = _claims(repo, [f"f{j}.py" for j in range(50)])
    assert len(calls) == 1
    assert len(out) == 50


def test_exclude_run_skips_record_whatever_its_session(repo, live):
    _manifest(repo, "p1", writes=["a.py"])
    _manifest(repo, "p2", writes=["b.py"])
    run_token.mint(repo, "p1", "s.mjs", session_id=PEER)
    run_token.mint(repo, "p2", "s.mjs", session_id=PEER)
    assert _claims(repo, ["a.py", "b.py"], exclude_run="p1") == {"b.py": [PEER]}
    assert _claims(repo, ["a.py", "b.py"], exclude_session="", exclude_run="p1") == {"b.py": [PEER]}


def test_exclude_run_skips_malformed_manifest_of_that_run(repo, live):
    _manifest(repo, "p1", raw="{not json")
    run_token.mint(repo, "p1", "s.mjs", session_id=PEER)
    assert _claims(repo, ["a.py"], exclude_run="p1") == {}


def test_unknown_result_logs_offending_run_id(repo, live, caplog):
    _manifest(repo, "p-bad", raw="{not json")
    run_token.mint(repo, "p-bad", "s.mjs", session_id=PEER)
    with caplog.at_level("WARNING"):
        assert _claims(repo, ["a.py"]) is None
    assert "p-bad" in caplog.text
