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
