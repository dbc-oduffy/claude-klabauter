"""ask.stage records write_baseline for each dispatched row's declared file writes."""

from __future__ import annotations

import json
import subprocess

import yaml

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.ask_contract import ManifestRow
from coordinator_core.ops.dispatch_emit.ask_stage import stage
from coordinator_core.ops.dispatch_emit.kept_work import fingerprint

SIZING_REL = "state/sizings/2026-10-23-fixture.yaml"


def _sizing(repo):
    doc = {"intent": "Fix pkg/a.py and pkg/new.py.", "exit_criterion": {"statement": "ok"}, "estimate": {"tshirt": "XS"}}
    (repo / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    (repo / SIZING_REL).write_text(yaml.safe_dump(doc), encoding="utf-8", newline="\n")


def test_stage_records_dirty_blob_and_null_for_absent(tmp_path, monkeypatch):
    _sizing(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("dirty\n", encoding="utf-8", newline="\n")

    def boom(*a, **k):
        raise AssertionError("spawn")

    real = ask_stage._write_baseline.capture

    def guarded(root, paths):
        monkeypatch.setattr(subprocess, "Popen", boom)
        try:
            return real(root, paths)
        finally:
            monkeypatch.undo()

    monkeypatch.setattr(ask_stage._write_baseline, "capture", guarded)
    m = stage(tmp_path, run_id="r1", sizing_rel=SIZING_REL, writes=["pkg/a.py", "pkg/new.py"])
    data = json.loads(json.dumps(m.to_json()))
    assert data["write_baseline"]["pkg/a.py"] == fingerprint(tmp_path, "pkg/a.py")
    assert data["write_baseline"]["pkg/a.py"] is not None
    assert data["write_baseline"]["pkg/new.py"] is None


def test_directories_and_globs_are_never_baselined(tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "f.py").write_text("x\n", encoding="utf-8", newline="\n")
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("d", "d/", "g/*.py", "f.py"), 0)]
    assert set(ask_stage._stage_write_baseline(tmp_path, rows)) == {"f.py"}


def test_adopting_row_paths_are_absent(tmp_path):
    (tmp_path / "s.py").write_text("x\n", encoding="utf-8", newline="\n")
    rows = [
        ManifestRow("S1", "executor", "sonnet", "b.md", ("s.py",), 0, adopts_stranded=True),
        ManifestRow("C1", "executor", "sonnet", "b.md", ("c.py",), 0),
    ]
    assert ask_stage._stage_write_baseline(tmp_path, rows) == {"c.py": None}


def _halt(tmp_path, run_id, baseline, base_sha="a" * 40):
    rec = ask_stage.record_dir(tmp_path, run_id)
    rec.mkdir(parents=True, exist_ok=True)
    (rec / "halted.json").write_text(
        json.dumps({"status": "halted", "halted": "x", "run_base_sha": base_sha}), encoding="utf-8", newline="\n"
    )
    (rec / "manifest.json").write_text(json.dumps({"write_baseline": baseline}), encoding="utf-8", newline="\n")
    return rec


def test_refire_baseline_is_the_halted_run_base_not_the_live_tree(tmp_path):
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("first fire output\n", encoding="utf-8", newline="\n")
    (tmp_path / "pkg" / "extra.py").write_text("live\n", encoding="utf-8", newline="\n")
    base_a = "0" * 40
    rec = _halt(tmp_path, "r1", {"pkg/a.py": base_a, "pkg/new.py": None})
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("pkg/a.py", "pkg/new.py", "pkg/extra.py"), 0)]
    got = ask_stage._stage_write_baseline(tmp_path, rows, rec)
    assert got["pkg/a.py"] == base_a
    assert got["pkg/new.py"] is None
    assert got["pkg/extra.py"] == fingerprint(tmp_path, "pkg/extra.py")
    # the earlier fire's output differs from that baseline, so it counts as written
    assert ask_stage._write_baseline.unwritten(tmp_path, got, ["pkg/a.py"]) == []


def test_non_refire_record_still_captures_the_live_tree(tmp_path):
    (tmp_path / "a.py").write_text("x\n", encoding="utf-8", newline="\n")
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("a.py",), 0)]
    rec = ask_stage.record_dir(tmp_path, "r2")
    rec.mkdir(parents=True)
    (rec / "manifest.json").write_text(json.dumps({"write_baseline": {"a.py": "0" * 40}}), encoding="utf-8")
    assert ask_stage._stage_write_baseline(tmp_path, rows, rec) == {"a.py": fingerprint(tmp_path, "a.py")}
    assert ask_stage._stage_write_baseline(tmp_path, rows) == {"a.py": fingerprint(tmp_path, "a.py")}


def test_stage_of_a_halted_run_keeps_the_prior_baseline(tmp_path):
    _sizing(tmp_path)
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "a.py").write_text("first fire output\n", encoding="utf-8", newline="\n")
    _halt(tmp_path, "r3", {"pkg/a.py": "0" * 40, "pkg/new.py": None})
    m = stage(tmp_path, run_id="r3", sizing_rel=SIZING_REL, writes=["pkg/a.py", "pkg/new.py"])
    assert m.write_baseline["pkg/a.py"] == "0" * 40
    assert m.write_baseline["pkg/new.py"] is None


def test_adopts_stranded_flag_is_deterministic():
    f = ask_stage._adopts_stranded
    assert f({"authorization": {"source": "em-inferred", "rationale": "the stranded 9 files"}})
    assert not f({"authorization": {"source": "em-inferred", "rationale": "other"}})
    assert not f({"authorization": {"source": "pm", "rationale": "stranded"}})
    assert not f({"title": "adopt stranded work"})
    assert not f(None)
