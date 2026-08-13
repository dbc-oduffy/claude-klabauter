"""test_learn_lessons_age_sweep — pytest tests for coordinator/bin/learn-lessons-age-sweep.py.

Spec backlink: coordinator-claude coordinator/skills/learn-lessons/SKILL.md
  § Phase 4.5 — Local-Mode Age-Sweep (Bound the File) [cutoff subcommand]
  § Phase 5 — Authorization and Apply § Strip-list orphan-rejection
    [check-strip-orphans subcommand]
  — this CLI is the ported destination for that skill's genuine imperative
  bash/inline-Python logic (COMPLETE-sentinel cutoff scan, routed-id vs.
  strip-list cross-check).

Coverage:
  cutoff:
    T1 latest of multiple COMPLETE-sentinel dirs wins (lexical == date order)
    T2 in-progress/aborted dirs (no COMPLETE) are excluded from candidacy
    T3 no completed run reachable -> None (skip-loud caller path)
    T4 runs_dir itself absent -> None
  check-strip-orphans:
    T5 all strip ids have a routed sibling -> no orphans
    T6 a strip id whose record has change_kind: discard -> orphan
    T7 a strip id with no matching record at all -> orphan
    T8 CLI exit codes: 0 clean, 1 orphans-found (via subprocess, exercises argv/exit path)
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from coordinator_core.win_portability import no_console_creationflags

_BIN_DIR = Path(__file__).parent.parent
_SCRIPT = _BIN_DIR / "learn-lessons-age-sweep.py"


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "learn_lessons_age_sweep",
        _SCRIPT,
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


# ---------------------------------------------------------------------------
# cutoff — derive_cutoff()
# ---------------------------------------------------------------------------


def test_cutoff_latest_completed_wins(tmp_path):
    runs = tmp_path / "tasks"
    runs.mkdir()
    for date in ("2026-06-01", "2026-06-15", "2026-05-20"):
        d = runs / f"learn-lessons-{date}"
        d.mkdir()
        (d / "COMPLETE").write_text("")
    assert _mod.derive_cutoff(runs) == "2026-06-15"


def test_cutoff_excludes_in_progress_dirs(tmp_path):
    runs = tmp_path / "tasks"
    runs.mkdir()
    completed = runs / "learn-lessons-2026-06-01"
    completed.mkdir()
    (completed / "COMPLETE").write_text("")

    in_progress = runs / "learn-lessons-2026-06-20"
    in_progress.mkdir()
    # no COMPLETE sentinel — must not become the cutoff

    assert _mod.derive_cutoff(runs) == "2026-06-01"


def test_cutoff_no_completed_run_returns_none(tmp_path):
    runs = tmp_path / "tasks"
    runs.mkdir()
    d = runs / "learn-lessons-2026-06-01"
    d.mkdir()
    # no COMPLETE sentinel anywhere
    assert _mod.derive_cutoff(runs) is None


def test_cutoff_missing_runs_dir_returns_none(tmp_path):
    assert _mod.derive_cutoff(tmp_path / "does-not-exist") is None


def test_cutoff_cli_skip_loud_exit_code(tmp_path):
    runs = tmp_path / "tasks"
    runs.mkdir()
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "cutoff", str(runs)],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert result.returncode == 1
    assert result.stdout == ""
    assert "no completed central run reachable" in result.stderr


def test_cutoff_cli_prints_date_on_stdout(tmp_path):
    runs = tmp_path / "tasks"
    runs.mkdir()
    d = runs / "learn-lessons-2026-06-01"
    d.mkdir()
    (d / "COMPLETE").write_text("")
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "cutoff", str(runs)],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "2026-06-01"


# ---------------------------------------------------------------------------
# check-strip-orphans — find_strip_orphans()
# ---------------------------------------------------------------------------


def test_strip_orphans_none_when_all_routed():
    records = [
        {"id": "repo-L1", "change_kind": "wiki-append"},
        {"id": "repo-L2", "change_kind": "wiki-new"},
    ]
    strip_list = [{"id": "repo-L1"}, {"id": "repo-L2"}]
    assert _mod.find_strip_orphans(records, strip_list) == []


def test_strip_orphans_discard_change_kind_is_orphan():
    records = [
        {"id": "repo-L1", "change_kind": "discard"},
    ]
    strip_list = [{"id": "repo-L1"}]
    assert _mod.find_strip_orphans(records, strip_list) == ["repo-L1"]


def test_strip_orphans_no_matching_record_is_orphan():
    records = [
        {"id": "repo-L1", "change_kind": "wiki-append"},
    ]
    strip_list = [{"id": "repo-L1"}, {"id": "repo-L2"}]
    assert _mod.find_strip_orphans(records, strip_list) == ["repo-L2"]


def _write_yaml(path: Path, doc: dict) -> None:
    path.write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")


def test_strip_orphans_cli_clean_exit_0(tmp_path):
    records_path = tmp_path / "records.yaml"
    strip_path = tmp_path / "strip-list.yaml"
    _write_yaml(
        records_path,
        {"records": [{"id": "repo-L1", "change_kind": "wiki-append"}]},
    )
    _write_yaml(strip_path, {"strip": [{"id": "repo-L1"}]})

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "check-strip-orphans",
            str(records_path),
            str(strip_path),
        ],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert result.returncode == 0
    assert result.stderr == ""


def test_strip_orphans_cli_rejects_with_exit_1(tmp_path):
    records_path = tmp_path / "records.yaml"
    strip_path = tmp_path / "strip-list.yaml"
    _write_yaml(
        records_path,
        {"records": [{"id": "repo-L1", "change_kind": "discard"}]},
    )
    _write_yaml(strip_path, {"strip": [{"id": "repo-L1"}]})

    result = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "check-strip-orphans",
            str(records_path),
            str(strip_path),
        ],
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert result.returncode == 1
    assert "STRIP-ORPHAN-REJECT:" in result.stderr
    assert "repo-L1" in result.stderr
