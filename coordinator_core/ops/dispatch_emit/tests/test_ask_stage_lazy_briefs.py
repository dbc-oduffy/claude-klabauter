"""A stage above the lazy-brief threshold writes one batch file and no per-row briefs; materialising a wave writes
exactly that wave's files, byte-identical to the eager write."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_stage
from coordinator_core.ops.dispatch_emit.tests._size_l_plan import build_size_l_plan, isolate_launch_dirs
from coordinator_core.ops.dispatch_emit.tests.conftest import write_run_scripts

ROWS = 6
SESSION_ID = "lazy-briefs-session"


def _stage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str, threshold: int) -> tuple[Path, str, object]:
    isolate_launch_dirs(monkeypatch, tmp_path)
    monkeypatch.setattr(ask_stage, "LAZY_BRIEFS_ABOVE_ROWS", threshold)
    root = tmp_path / name
    root.mkdir()
    repo_root, plan_rel, run_id = build_size_l_plan(root, rows=ROWS, live_tokens=0, plan_bytes=6_000, session_id=SESSION_ID)
    write_run_scripts(repo_root, run_id)
    manifest = ask_stage.stage(repo_root, run_id=run_id, plan_rel=plan_rel, session_id=SESSION_ID)
    return repo_root, run_id, manifest


def _briefs(root: Path, run_id: str) -> dict[str, bytes]:
    directory = root / "scratch" / "warp" / run_id / "briefs"
    return {p.name: p.read_bytes() for p in sorted(directory.glob("*.md"))} if directory.is_dir() else {}


def _batch(root: Path, run_id: str) -> list[dict]:
    raw = (root / "scratch" / "warp" / run_id / ask_stage.BRIEFS_BATCH_FILE).read_text(encoding="utf-8")
    return [json.loads(line) for line in raw.splitlines() if line]


def test_below_the_threshold_stages_eager_files_and_no_batch(tmp_path, monkeypatch) -> None:
    root, run_id, manifest = _stage(tmp_path, monkeypatch, "eager", threshold=ROWS)
    assert manifest.rows
    assert len(_briefs(root, run_id)) == ROWS
    assert not (root / "scratch" / "warp" / run_id / ask_stage.BRIEFS_BATCH_FILE).exists()


def test_above_the_threshold_writes_one_batch_file_and_no_row_files(tmp_path, monkeypatch) -> None:
    root, run_id, manifest = _stage(tmp_path, monkeypatch, "lazy", threshold=ROWS - 1)
    assert manifest.rows
    assert _briefs(root, run_id) == {}
    records = _batch(root, run_id)
    assert len(records) == ROWS
    assert {Path(r["path"]).name for r in records} == {f"C{n}.md" for n in range(1, ROWS + 1)}


def test_materialising_each_wave_writes_that_waves_files_with_the_eager_bytes(tmp_path, monkeypatch) -> None:
    eager_root, eager_run, _ = _stage(tmp_path, monkeypatch, "eager", threshold=ROWS)
    lazy_root, lazy_run, _ = _stage(tmp_path, monkeypatch, "lazy", threshold=ROWS - 1)
    assert eager_run == lazy_run
    eager = _briefs(eager_root, eager_run)
    by_wave: dict[int, set[str]] = {}
    for rec in _batch(lazy_root, lazy_run):
        by_wave.setdefault(rec["wave"], set()).add(Path(rec["path"]).name)
    waves = sorted(by_wave)
    assert len(waves) >= 2, f"the fixture must span two waves: {by_wave}"

    written: set[str] = set()
    for wave in waves[:2]:
        assert ask_stage.materialise_briefs(lazy_root, lazy_run, wave) == len(by_wave[wave])
        written |= by_wave[wave]
        present = _briefs(lazy_root, lazy_run)
        assert set(present) == written, "materialising a wave writes that wave's files and no other wave's"
        assert all(present[name] == eager[name] for name in present)
        assert ask_stage.materialise_briefs(lazy_root, lazy_run, wave) == len(by_wave[wave])
        assert _briefs(lazy_root, lazy_run) == present


def test_materialising_without_a_batch_writes_nothing(tmp_path, monkeypatch) -> None:
    root, run_id, _ = _stage(tmp_path, monkeypatch, "eager", threshold=ROWS)
    assert ask_stage.materialise_briefs(root, run_id, 0) == 0
