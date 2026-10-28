"""Pins that a path owned by an unkept chunk (a peer co-declares it) is still recorded per path."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import halted_record_commit as hrc
from coordinator_core.ops.dispatch_emit import halted_record_ledger as ledger
from coordinator_core.ops.dispatch_emit import kept_work as k


def _repo(tmp_path):
    """A fake repo layout inside tmp_path only; no git process is spawned."""
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (tmp_path / "rec").mkdir()
    (tmp_path / "co.py").write_bytes(b"run + peer\n")
    (tmp_path / "own.py").write_bytes(b"own\n")
    return tmp_path


def test_codeclared_path_recorded_under_post_checkpoint(tmp_path):
    root = _repo(tmp_path)
    owner = {"co.py": "C1", "own.py": "C2"}
    withheld = {"co.py": "peer-hunks-co-declared", "own.py": k.HALT_RECORD_ONLY}
    out = hrc._record_kept(root, "rec", owner, withheld, {}, [], post_candidates=["co.py", "own.py"])
    assert out["kept"]["chunks"] == ["C2"]
    assert out["kept"][k.POST_CHECKPOINT_KEY] == ["co.py"]
    data = k.read(root / "rec")
    assert data[k.POST_CHECKPOINT_KEY] == {"co.py": k.blob_sha(b"run + peer\n")}
    assert list(data["chunks"]["C2"]["paths"]) == ["own.py"]
    assert k.kept_fingerprints(root / "rec")["co.py"] == frozenset({k.blob_sha(b"run + peer\n")})


def test_unpersistable_codeclared_path_stays_out(tmp_path, monkeypatch):
    root = _repo(tmp_path)
    monkeypatch.setattr(ledger, "_persist_blobs", lambda root, common_dir, post: set())
    out = hrc._record_kept(
        root, "rec", {"co.py": "C1"}, {"co.py": "peer-hunks-co-declared"}, {}, [], post_candidates=["co.py"]
    )
    assert out.get("post_checkpoint_unpersisted") == ["co.py"]
    assert k.read(root / "rec")[k.POST_CHECKPOINT_KEY] == {}
