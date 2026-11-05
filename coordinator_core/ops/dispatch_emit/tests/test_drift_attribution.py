"""drift_attribution.classify: run-owned, outside-declared, foreign and foreign-after-own-write.

The session ledger is written through `touch_record.append_touch_claims`, the production writer;
the ledger layout is documented in `drift_attribution`'s module docstring."""

import json
import subprocess

import pytest

from coordinator_core.git import commit
from coordinator_core.ops.dispatch_emit import drift_attribution as da
from coordinator_core.session import touch_record

SID = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
def repo(tmp_path):
    (tmp_path / ".git").mkdir()
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "run-stage.json").write_text(json.dumps({"session_id": SID}))
    return tmp_path


def _own_write(repo, rel, text):
    (repo / rel).parent.mkdir(parents=True, exist_ok=True)
    (repo / rel).write_text(text)
    touch_record.append_touch_claims(
        [rel], SID, repo, content_hashes={rel: touch_record.compute_content_hash(repo / rel)}
    )


def _classify(repo, paths, declared):
    return da.classify(repo, repo / "run", paths, declared)


def test_run_owned_blob_equals_ledger_last_write(repo):
    _own_write(repo, "p.py", "one\n")
    _own_write(repo, "p.py", "two\n")
    assert _classify(repo, ["p.py"], ["p.py"]) == {"run_owned": ["p.py"], "outside_declared": [], "foreign": []}


def test_run_owned_wins_over_undeclared(repo):
    _own_write(repo, "q.md", "note\n")
    assert _classify(repo, ["q.md"], ["p.py"])["run_owned"] == ["q.md"]


def test_outside_declared_unrecorded_path_is_withheld_not_foreign(repo):
    (repo / "doc.md").write_text("em edit\n")
    assert _classify(repo, ["doc.md"], ["p.py"]) == {
        "run_owned": [], "outside_declared": ["doc.md"], "foreign": [],
    }


def test_foreign_declared_path_with_no_run_write(repo):
    (repo / "p.py").write_text("peer\n")
    assert _classify(repo, ["p.py"], ["p.py"])["foreign"] == ["p.py"]


def test_foreign_after_own_write_refuses(repo):
    _own_write(repo, "p.py", "mine\n")
    (repo / "p.py").write_text("peer rewrote\n")
    assert _classify(repo, ["p.py"], ["p.py"])["foreign"] == ["p.py"]


def test_touched_earlier_but_bytes_differ_is_not_exempt_when_undeclared(repo):
    _own_write(repo, "doc.md", "mine\n")
    (repo / "doc.md").write_text("em later\n")
    assert _classify(repo, ["doc.md"], ["p.py"])["outside_declared"] == ["doc.md"]


def test_ledger_event_older_than_run_start_is_another_runs(repo):
    _own_write(repo, "p.py", "old run\n")
    out = da.classify(repo, repo / "run", ["p.py"], ["p.py"], run_started_at=4102444800.0)
    assert out["foreign"] == ["p.py"]


def test_deleted_path_is_never_run_owned(repo):
    _own_write(repo, "p.py", "mine\n")
    (repo / "p.py").unlink()
    assert _classify(repo, ["p.py"], ["p.py"])["foreign"] == ["p.py"]


def test_other_sessions_write_is_no_expectation(repo):
    (repo / "p.py").write_text("peer\n")
    touch_record.append_touch_claims(
        ["p.py"], "99999999-0000-0000-0000-000000000000", repo,
        content_hashes={"p.py": touch_record.compute_content_hash(repo / "p.py")},
    )
    assert _classify(repo, ["p.py"], ["p.py"])["foreign"] == ["p.py"]


def test_own_hashes_and_own_blobs_supply_ownership(repo):
    (repo / "a.py").write_text("a\n")
    (repo / "b.py").write_text("b\n")
    out = da.classify(
        repo, repo / "run", ["a.py", "b.py"], ["a.py", "b.py"],
        own_hashes={"a.py": [touch_record.compute_content_hash(repo / "a.py")]},
        own_blobs={"b.py": [commit.checkin_blob_sha(repo, "b.py")]},
    )
    assert out["run_owned"] == ["a.py", "b.py"]


def test_declared_dir_prefix_and_glob(repo):
    for rel in ("src/x.py", "t/y.py", "z.txt"):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text("peer\n")
    out = _classify(repo, ["src/x.py", "t/y.py", "z.txt"], ["src/", "t/*.py"])
    assert out["foreign"] == ["src/x.py", "t/y.py"] and out["outside_declared"] == ["z.txt"]


def test_every_path_in_exactly_one_list_and_dupes_collapse(repo):
    _own_write(repo, "a.py", "a\n")
    (repo / "b.py").write_text("b\n")
    (repo / "c.md").write_text("c\n")
    out = _classify(repo, ["a.py", "b.py", "c.md", "a.py"], ["a.py", "b.py"])
    assert sorted(out["run_owned"] + out["outside_declared"] + out["foreign"]) == ["a.py", "b.py", "c.md"]


def test_spawns_nothing(repo, monkeypatch):
    _own_write(repo, "p.py", "mine\n")

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", boom)
    assert _classify(repo, ["p.py", "x.md"], ["p.py"])["run_owned"] == ["p.py"]
