import hashlib
import json

from coordinator_core.ops.dispatch_emit import kept_work as k


def test_blob_sha_matches_git_ids():
    assert k.blob_sha(b"") == "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391"
    assert k.blob_sha(b"hello\n") == "ce013625030ba8dba906f756967f9e9ca394464a"


def test_crlf_fingerprints_raw_bytes(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"x\r\ny\r\n")
    assert k.fingerprint(tmp_path, "a.txt") == k.blob_sha(b"x\r\ny\r\n")
    assert k.fingerprint(tmp_path, "missing.txt") is None


def test_kept_chunks_omits_chunk_with_other_reason(tmp_path):
    for n in ("a", "b", "c"):
        (tmp_path / n).write_bytes(n.encode())
    owner = {"a": "C1", "b": "C1", "c": "C2"}
    withheld = {"a": "halt-record-only", "b": "outside-footprint", "c": "halt-record-only"}
    out = k.kept_chunks(tmp_path, owner, withheld)
    assert list(out) == ["C2"]
    assert out["C2"]["paths"] == {"c": k.blob_sha(b"c")}


def _halted(tmp_path, **extra):
    (tmp_path / "p.md").write_bytes(b"plan")
    blitz = {
        "pulled": [{"planPath": "p.md", "reviews": ["first"], "coverage": 1}],
        "regate": {"pulled": [{"planPath": "p.md", "reviews": ["regate"], "coverage": 2}]},
    }
    return {"halted": "refusal", "plan": "p.md", "blitz": blitz, **extra}


def test_plan_gate_prefers_regate_entry(tmp_path):
    gate = k.plan_gate(_halted(tmp_path), tmp_path)
    assert gate == {
        "plan": "p.md",
        "plan_sha256": hashlib.sha256(b"plan").hexdigest(),
        "reviews": ["regate"],
        "coverage": 2,
    }


def test_plan_gate_falls_back_and_refuses_other_shapes(tmp_path):
    h = _halted(tmp_path)
    del h["blitz"]["regate"]
    assert k.plan_gate(h, tmp_path)["reviews"] == ["first"]
    assert k.plan_gate({**h, "halted": "commit"}, tmp_path) is None
    assert k.plan_gate({**h, "plan": "other.md"}, tmp_path) is None
    assert k.plan_gate({"halted": "refusal", "plan": "p.md"}, tmp_path) is None


def _entry(tmp_path):
    (tmp_path / "f.py").write_bytes(b"one")
    return {"paths": {"f.py": k.blob_sha(b"one")}}


def test_still_kept_true_when_untouched_and_longer_id_noted(tmp_path):
    e = _entry(tmp_path)
    assert k.still_kept(tmp_path, e, [], "C2")
    assert k.still_kept(tmp_path, e, ["C20 was redone"], "C2")


def test_still_kept_false_after_change_or_deleted_recreated(tmp_path):
    e = _entry(tmp_path)
    (tmp_path / "f.py").write_bytes(b"two")
    assert not k.still_kept(tmp_path, e, [], "C2")
    (tmp_path / "f.py").unlink()
    assert not k.still_kept(tmp_path, e, [], "C2")
    (tmp_path / "f.py").write_bytes(b"two")
    assert not k.still_kept(tmp_path, e, [], "C2")


def test_still_kept_false_when_note_names_row_or_path(tmp_path):
    e = _entry(tmp_path)
    assert not k.still_kept(tmp_path, e, ["redo C2 by hand"], "C2")
    assert not k.still_kept(tmp_path, e, ["f.py is wrong"], "C2")


def test_write_read_round_trip_and_empty_writes_nothing(tmp_path):
    assert k.write(tmp_path, {}, None) is None
    assert not (tmp_path / k.KEPT_FILE).exists()
    path = k.write(tmp_path, {"C1": {"paths": {"a": "x"}}}, None)
    assert path == tmp_path / k.KEPT_FILE
    raw = path.read_bytes()
    assert b"\r" not in raw
    assert k.read(tmp_path) == {"chunks": {"C1": {"paths": {"a": "x"}}}, "plan_gate": None}
    assert json.loads(raw)["chunks"]["C1"]["paths"] == {"a": "x"}


def test_read_malformed_is_empty(tmp_path):
    empty = {"chunks": {}, "plan_gate": None}
    assert k.read(tmp_path) == empty
    (tmp_path / k.KEPT_FILE).write_text("{not json", encoding="utf-8")
    assert k.read(tmp_path) == empty
    (tmp_path / k.KEPT_FILE).write_text("[1]", encoding="utf-8")
    assert k.read(tmp_path) == empty


def _returned(tmp_path, text="DONE: 960449 B"):
    from coordinator_core.ops.dispatch_emit import row_returns as rr

    rr.write(tmp_path, {"C2": text})
    return {"paths": {}, "return": rr.digest(text)}


def test_kept_chunks_records_finished_writes_empty_row_with_return(tmp_path):
    (tmp_path / "a").write_bytes(b"a")
    out = k.kept_chunks(
        tmp_path, {"a": "C1"}, {"a": "halt-record-only"}, returns={"C1": "x", "C2": "sha"}, finished=["C1", "C2", "C3"]
    )
    assert out["C1"] == {"paths": {"a": k.blob_sha(b"a")}}
    assert out["C2"] == {"paths": {}, "return": "sha"}
    assert "C3" not in out


def test_writes_empty_entry_kept_then_unkept_by_note_and_sha_drift(tmp_path):
    e = _returned(tmp_path)
    assert k.still_kept(tmp_path, e, [], "C2", record_dir=tmp_path)
    assert k.still_kept(tmp_path, e, ["C20 redone"], "C2", record_dir=tmp_path)
    assert not k.still_kept(tmp_path, e, ["redo C2"], "C2", record_dir=tmp_path)
    assert not k.still_kept(tmp_path, e, [], "C2")
    _returned(tmp_path, "DONE: other")
    assert not k.still_kept(tmp_path, e, [], "C2", record_dir=tmp_path)


def test_writes_empty_entry_needs_done_return(tmp_path):
    e = _returned(tmp_path, "BLOCKED: no")
    assert not k.still_kept(tmp_path, e, [], "C2", record_dir=tmp_path)
    assert not k.still_kept(tmp_path, {"paths": {}}, [], "C2", record_dir=tmp_path)
