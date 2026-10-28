"""A refire's stage baseline never bakes a halted run's withheld hunk into its base."""

from __future__ import annotations

import json
from pathlib import Path

import coordinator_core.git.git_state as git_state
from coordinator_core.ops.dispatch_emit import ask_stage, halted_withheld_index
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT, ManifestRow
from coordinator_core.ops.dispatch_emit.kept_work import blob_sha, fingerprint
from coordinator_core.ops.dispatch_emit.write_baseline import WITHHELD_PRINTS_FILE, unwritten

SIZING_A = "state/sizings/a.yaml"
SIZING_OTHER = "state/sizings/other.yaml"
HEAD_BLOB = blob_sha(b"committed\n")


def _run_a_halts(root: Path, sizing: str) -> None:
    (root / "pkg").mkdir(exist_ok=True)
    (root / "pkg" / "w.py").write_text("withheld hunk\n", encoding="utf-8", newline="\n")
    d = root / RECORD_DIR_ROOT / "runA"
    d.mkdir(parents=True)
    (d / "halted.json").write_text(
        json.dumps({"status": "halted", "sizing_rel": sizing, "run_artifacts": {"withheld": {"pkg/w.py": "why"}}}),
        encoding="utf-8",
    )
    (d / WITHHELD_PRINTS_FILE).write_text(json.dumps({"pkg/w.py": fingerprint(root, "pkg/w.py")}), encoding="utf-8")
    halted_withheld_index.add_run(root, "runA", ["pkg/w.py"])
    (root / halted_withheld_index.INDEX_DIR / halted_withheld_index.SENTINEL).write_bytes(b"")


def _stage_b(root: Path, sizing: str) -> dict:
    rec = root / RECORD_DIR_ROOT / "runB"
    rec.mkdir(parents=True, exist_ok=True)
    rows = [ManifestRow("C1", "executor", "sonnet", "b.md", ("pkg/w.py", "pkg/other.py"), 0)]
    return ask_stage._stage_write_baseline(root, rows, rec, sizing)


def test_same_sizing_hunk_is_credited_to_b_not_its_base(tmp_path, monkeypatch):
    _run_a_halts(tmp_path, SIZING_A)
    (tmp_path / "pkg" / "other.py").write_text("plain dirt\n", encoding="utf-8", newline="\n")
    monkeypatch.setattr(git_state, "head_blobs", lambda root, paths: {p: (0o100644, HEAD_BLOB) for p in paths})
    got = _stage_b(tmp_path, SIZING_A)
    assert got["pkg/w.py"] == HEAD_BLOB != fingerprint(tmp_path, "pkg/w.py")
    assert unwritten(tmp_path, got, ["pkg/w.py"]) == []
    assert got["pkg/other.py"] == fingerprint(tmp_path, "pkg/other.py")


def test_same_sizing_hunk_absent_from_head_baselines_none(tmp_path, monkeypatch):
    _run_a_halts(tmp_path, SIZING_A)
    monkeypatch.setattr(git_state, "head_blobs", lambda root, paths: {})
    assert _stage_b(tmp_path, SIZING_A)["pkg/w.py"] is None


def test_different_sizing_hunk_stays_a_is_neither_base_nor_b_work(tmp_path):
    _run_a_halts(tmp_path, SIZING_OTHER)
    got = _stage_b(tmp_path, SIZING_A)
    assert "pkg/w.py" not in got
    assert unwritten(tmp_path, got, ["pkg/w.py"]) == []
    assert halted_withheld_index.withheld_by_path(tmp_path)["pkg/w.py"] == "runA"


def test_edited_after_halt_is_ordinary_capture(tmp_path):
    _run_a_halts(tmp_path, SIZING_A)
    (tmp_path / "pkg" / "w.py").write_text("foreign edit\n", encoding="utf-8", newline="\n")
    got = _stage_b(tmp_path, SIZING_A)
    assert got["pkg/w.py"] == fingerprint(tmp_path, "pkg/w.py")
