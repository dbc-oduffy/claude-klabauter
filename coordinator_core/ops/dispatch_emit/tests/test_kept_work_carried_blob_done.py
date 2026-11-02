import json

from coordinator_core.ops.dispatch_emit import halted_record_commit as hrc
from coordinator_core.ops.dispatch_emit import kept_work as k
from coordinator_core.ops.dispatch_emit import write_baseline


def _setup(tmp_path):
    (tmp_path / "a").write_bytes(b"a0")
    (tmp_path / "b").write_bytes(b"b0")
    (tmp_path / ".git" / "objects").mkdir(parents=True)
    (tmp_path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    rec = tmp_path / "rec"
    rec.mkdir()
    baseline = {"a": k.blob_sha(b"a0"), "b": k.blob_sha(b"b0")}
    (rec / "manifest.json").write_text(json.dumps({"write_baseline": baseline}), encoding="utf-8")
    owner = {"a": "C1", "b": "C2"}
    withheld = {"a": k.HALT_RECORD_ONLY, "b": k.HALT_RECORD_ONLY}
    return rec, owner, withheld


def test_path_equal_to_earlier_kept_blob_is_credited(tmp_path):
    rec, owner, withheld = _setup(tmp_path)
    k.write(rec, {"C1": {"paths": {"a": k.blob_sha(b"a0")}}}, None)
    out = hrc._record_kept(tmp_path, "rec", owner, withheld, {}, [])
    assert out["kept"]["chunks"] == ["C1"]


def test_path_in_earlier_run_outcome_slice_is_credited(tmp_path):
    rec, owner, withheld = _setup(tmp_path)
    outcome = {"inline_review": {"prep": {"slice_files": ["a"]}}}
    (rec / "run-outcome.json").write_text(json.dumps(outcome), encoding="utf-8")
    out = hrc._record_kept(tmp_path, "rec", owner, withheld, {}, [])
    assert "C1" in out["kept"]["chunks"]


def test_untouched_path_without_carried_evidence_stays_uncredited(tmp_path):
    rec, owner, withheld = _setup(tmp_path)
    out = hrc._record_kept(tmp_path, "rec", owner, withheld, {}, [])
    assert out == {}


def test_own_withheld_map_is_not_carried_evidence(tmp_path):
    rec, owner, withheld = _setup(tmp_path)
    (rec / "halted.json").write_text(
        json.dumps({"run_artifacts": {"withheld": withheld}}), encoding="utf-8"
    )
    got = write_baseline.without_carried(tmp_path, rec, json.loads((rec / "manifest.json").read_text())["write_baseline"])
    assert sorted(got) == ["a", "b"]


def test_changed_kept_blob_is_not_credited(tmp_path):
    rec, owner, withheld = _setup(tmp_path)
    k.write(rec, {"C1": {"paths": {"a": k.blob_sha(b"other")}}}, None)
    out = hrc._record_kept(tmp_path, "rec", owner, withheld, {}, [])
    assert out == {}
