"""halted_withheld_index: per-run files, replace-not-merge, malformed tolerance, sentinel backfill."""

import json

import pytest

from coordinator_core.ops.dispatch_emit import halted_withheld_index as idx


def _marker(root, run, status, withheld=None):
    d = root / "state" / "warp" / run
    d.mkdir(parents=True)
    body = {"status": status}
    if withheld is not None:
        body["run_artifacts"] = {"withheld": withheld}
    (d / "halted.json").write_text(json.dumps(body), encoding="utf-8")


def test_add_read_remove(tmp_path):
    idx.rebuild(tmp_path)
    idx.add_run(tmp_path, "run-a", ["a.py", ".\\sub\\b.py"])
    assert idx.withheld_by_path(tmp_path) == {"a.py": "run-a", "sub/b.py": "run-a"}
    idx.remove_run(tmp_path, "run-a")
    assert idx.withheld_by_path(tmp_path) == {}
    idx.remove_run(tmp_path, "run-a")


def test_add_replaces_and_empty_removes(tmp_path):
    idx.rebuild(tmp_path)
    idx.add_run(tmp_path, "r", ["old.py"])
    idx.add_run(tmp_path, "r", ["new.py"])
    assert idx.withheld_by_path(tmp_path) == {"new.py": "r"}
    idx.add_run(tmp_path, "r", [])
    assert idx.withheld_by_path(tmp_path) == {}


def test_two_runs_are_separate_files(tmp_path):
    idx.rebuild(tmp_path)
    idx.add_run(tmp_path, "r1", ["a.py"])
    idx.add_run(tmp_path, "r2", ["b.py"])
    idx.remove_run(tmp_path, "r1")
    assert idx.withheld_by_path(tmp_path) == {"b.py": "r2"}


def test_malformed_run_file_reads_empty(tmp_path):
    idx.rebuild(tmp_path)
    idx.add_run(tmp_path, "good", ["g.py"])
    (tmp_path / idx.INDEX_DIR / "bad.json").write_text("{nope", encoding="utf-8")
    assert idx.withheld_by_path(tmp_path) == {"g.py": "good"}


def test_unsafe_run_id_refused(tmp_path):
    with pytest.raises(ValueError):
        idx.add_run(tmp_path, "../x", ["a.py"])


def test_sentinel_absent_triggers_rebuild_once(tmp_path):
    _marker(tmp_path, "h1", "halted", {"a.py": "targeted-tests-failed"})
    _marker(tmp_path, "h2", "halted", None)
    _marker(tmp_path, "r1", "resumed", {"z.py": "x"})
    _marker(tmp_path, "l1", "landed", {"y.py": "x"})
    assert not (tmp_path / idx.INDEX_DIR / idx.SENTINEL).exists()
    assert idx.withheld_by_path(tmp_path) == {"a.py": "h1"}
    assert (tmp_path / idx.INDEX_DIR / idx.SENTINEL).exists()
    assert not (tmp_path / idx.INDEX_DIR / "h2.json").exists()
    _marker(tmp_path, "h3", "halted", {"c.py": "x"})
    assert idx.withheld_by_path(tmp_path) == {"a.py": "h1"}


def test_rebuild_without_warp_dir(tmp_path):
    idx.rebuild(tmp_path)
    assert idx.withheld_by_path(tmp_path) == {}


def test_add_run_records_fields_and_entries_reads_them(tmp_path):
    idx.rebuild(tmp_path)
    idx.add_run(
        tmp_path, "r", ["a.py", "b.py"],
        sizing_rel="state\\sizings\\s.yaml", prints={"a.py": "11", "gone.py": "22"}, declared=["./d.py"], at_ns=5,
    )
    got = idx.entries(tmp_path)
    assert got == {"r": idx.RunEntry(("a.py", "b.py"), "state/sizings/s.yaml", {"a.py": "11"}, ("d.py",), 5)}
    assert idx.withheld_by_path(tmp_path) == {"a.py": "r", "b.py": "r"}


def test_add_run_without_fields_keeps_them_and_replaces_paths(tmp_path):
    idx.rebuild(tmp_path)
    idx.add_run(tmp_path, "r", ["a.py"], sizing_rel="s.yaml", prints={"a.py": "1"}, declared=["d.py"])
    idx.add_run(tmp_path, "r", ["b.py"])
    entry = idx.entries(tmp_path)["r"]
    assert (entry.paths, entry.sizing_rel, entry.prints, entry.declared) == (("b.py",), "s.yaml", {}, ("d.py",))


def test_declared_only_entry_is_not_a_withheld_run(tmp_path):
    idx.rebuild(tmp_path)
    idx.record_declared(tmp_path, "live", ["x.py"], sizing_rel="s.yaml")
    idx.add_run(tmp_path, "held", ["h.py"])
    assert idx.entries(tmp_path)["live"].declared == ("x.py",)
    assert idx.withheld_by_path(tmp_path) == {"h.py": "held"}
    assert idx.run_ids(tmp_path) == ["held"]
    idx.record_declared(tmp_path, "live", [])
    assert "live" not in idx.entries(tmp_path)


def test_record_declared_keeps_paths_and_prints(tmp_path):
    idx.rebuild(tmp_path)
    idx.add_run(tmp_path, "r", ["a.py"], prints={"a.py": "1"})
    idx.record_declared(tmp_path, "r", ["d.py"])
    entry = idx.entries(tmp_path)["r"]
    assert (entry.paths, entry.prints, entry.declared) == (("a.py",), {"a.py": "1"}, ("d.py",))
    idx.add_run(tmp_path, "r", [])
    assert idx.entries(tmp_path)["r"].declared == ("d.py",)
    idx.record_declared(tmp_path, "r", [])
    assert idx.entries(tmp_path) == {}


def test_rebuild_backfills_sizing_prints_and_declared(tmp_path):
    _marker(tmp_path, "h1", "halted", {"a.py": "x"})
    rec = tmp_path / "state" / "warp" / "h1"
    body = json.loads((rec / "halted.json").read_text(encoding="utf-8"))
    (rec / "halted.json").write_text(json.dumps({**body, "sizing_rel": "state/sizings/s.yaml"}), encoding="utf-8")
    (rec / "withheld-prints.json").write_text(json.dumps({"a.py": "abc"}), encoding="utf-8")
    (rec / "manifest.json").write_text(
        json.dumps({"rows": [{"writes": ["w.py"]}], "review_declared_paths": ["r.py"]}), encoding="utf-8"
    )
    _marker(tmp_path, "h2", "halted", None)
    (tmp_path / "state" / "warp" / "h2" / "manifest.json").write_text(
        json.dumps({"rows": [{"writes": ["only.py"]}]}), encoding="utf-8"
    )
    got = idx.entries(tmp_path)
    assert got["h1"].paths == ("a.py",)
    assert got["h1"].sizing_rel == "state/sizings/s.yaml"
    assert got["h1"].prints == {"a.py": "abc"}
    assert got["h1"].declared == ("r.py", "w.py")
    assert got["h1"].at_ns > 0
    assert got["h2"].paths == () and got["h2"].declared == ("only.py",)
    assert idx.withheld_by_path(tmp_path) == {"a.py": "h1"}


def test_record_withheld_mirrors_sizing_and_prints_into_index(tmp_path):
    from coordinator_core.ops.dispatch_emit import halted_run

    (tmp_path / "a.py").write_text("print(1)\n", encoding="utf-8")
    (tmp_path / "state" / "warp").mkdir(parents=True)
    idx.rebuild(tmp_path)
    rec = tmp_path / "state" / "warp" / "h1"
    halted_run.write_halt_marker(rec, halted="x", sizing_rel="state/sizings/s.yaml")
    assert halted_run.record_withheld(rec, {"a.py": "reason", "missing.py": "reason"})
    entry = idx.entries(tmp_path)["h1"]
    assert entry.paths == ("a.py", "missing.py")
    assert entry.sizing_rel == "state/sizings/s.yaml"
    assert list(entry.prints) == ["a.py"] and len(entry.prints["a.py"]) == 40
    halted_run.mark_resumed(rec)
    assert idx.entries(tmp_path) == {}
