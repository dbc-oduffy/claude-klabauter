import json

from coordinator_core.ops.dispatch_emit import halted_record_commit as hrc
from coordinator_core.ops.dispatch_emit import kept_work as k


def _setup(tmp_path):
    (tmp_path / "a").write_bytes(b"a0")
    (tmp_path / "b").write_bytes(b"b0")
    owner = {"a": "C1", "b": "C2"}
    withheld = {"a": k.HALT_RECORD_ONLY, "b": k.HALT_RECORD_ONLY}
    baseline = {"a": k.blob_sha(b"a0"), "b": k.blob_sha(b"b0")}
    return owner, withheld, baseline


def test_kept_work_unwritten(tmp_path):
    owner, withheld, baseline = _setup(tmp_path)
    (tmp_path / "b").write_bytes(b"b1")
    out = k.kept_chunks(tmp_path, owner, withheld, baseline)
    assert list(out) == ["C2"]


def test_absent_baseline_keeps_both(tmp_path):
    owner, withheld, _ = _setup(tmp_path)
    assert sorted(k.kept_chunks(tmp_path, owner, withheld)) == ["C1", "C2"]
    assert sorted(k.kept_chunks(tmp_path, owner, withheld, {})) == ["C1", "C2"]


def test_path_missing_from_baseline_is_kept(tmp_path):
    owner, withheld, baseline = _setup(tmp_path)
    del baseline["a"]
    assert list(k.kept_chunks(tmp_path, owner, withheld, baseline)) == ["C1"]


def test_record_kept_reads_manifest_baseline(tmp_path):
    owner, withheld, baseline = _setup(tmp_path)
    (tmp_path / "b").write_bytes(b"b1")
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "manifest.json").write_text(json.dumps({"write_baseline": baseline}), encoding="utf-8")
    out = hrc._record_kept(tmp_path, "rec", owner, withheld, {}, [])
    assert out["kept"]["chunks"] == ["C2"]
    assert hrc._manifest_baseline(tmp_path / "nope") is None
