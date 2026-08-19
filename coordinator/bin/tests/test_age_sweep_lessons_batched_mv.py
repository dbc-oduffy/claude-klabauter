"""test_age_sweep_lessons_batched_mv.py -- multi-item coverage for
`age-sweep-lessons.py::_batched_git_mv_into_dir` (amplification burn-down,
`state/ledgers/wave4-dispositions/c1.md`, key
`age-sweep-lessons.py::main -> run`).

A single-item test passes identically before and after a batching change --
see the plan's own warning about `_own_frozen_diff_shas`. These tests assert
the CALL COUNT (one `subprocess.run` per BATCH, not per source) and the
byte-budget chunk boundary, never just the end-to-end return value.

Loaded by file path (`importlib.machinery.SourceFileLoader`), matching this
directory's existing hyphenated-module idiom (see
test_percolate_liveops_preflight.py::_load_cli_module).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import types

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "age_sweep_lessons", str(_BIN_DIR / "age-sweep-lessons.py")
    )
    spec = importlib.util.spec_from_loader("age_sweep_lessons", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def test_multi_item_batch_is_one_subprocess_call(monkeypatch, tmp_path):
    """N sources sharing one destination directory -> ONE `git mv` call, not N."""
    mod = _load_cli_module()
    calls: list[list[str]] = []

    def _fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    srcs = [tmp_path / f"lesson-{i}.yaml" for i in range(5)]
    dst = tmp_path / "archive"
    moved = mod._batched_git_mv_into_dir(srcs, dst, tmp_path, {})

    assert moved == 5
    assert len(calls) == 1
    cmd = calls[0]
    assert cmd[0:2] == ["git", "mv"]
    assert cmd[-1] == str(dst)
    assert cmd[2:-1] == [str(s) for s in srcs]


def test_byte_budget_chunk_boundary_splits_into_multiple_calls(monkeypatch, tmp_path):
    """A source list whose total length exceeds the argv budget must split into
    multiple `git mv` calls -- never a single call over the Windows
    `CreateProcess` cap, and never silently dropping a source."""
    mod = _load_cli_module()
    monkeypatch.setattr(mod, "_GIT_MV_BATCH_BUDGET", 40)
    calls: list[list[str]] = []

    def _fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    srcs = [tmp_path / f"lesson-{i:03d}.yaml" for i in range(10)]
    dst = tmp_path / "archive"
    moved = mod._batched_git_mv_into_dir(srcs, dst, tmp_path, {})

    assert moved == 10
    assert len(calls) > 1
    seen = []
    for cmd in calls:
        assert cmd[0:2] == ["git", "mv"]
        assert cmd[-1] == str(dst)
        seen.extend(cmd[2:-1])
    assert seen == [str(s) for s in srcs]


def test_batch_failure_aborts_and_reports_none(monkeypatch, tmp_path):
    mod = _load_cli_module()

    def _fake_run(cmd, **_kwargs):
        return types.SimpleNamespace(returncode=1, stderr=b"boom")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    srcs = [tmp_path / "a.yaml", tmp_path / "b.yaml"]
    result = mod._batched_git_mv_into_dir(srcs, tmp_path / "archive", tmp_path, {})

    assert result is None


def test_empty_source_list_is_a_zero_spawn_noop(monkeypatch, tmp_path):
    mod = _load_cli_module()
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: calls.append(1))

    result = mod._batched_git_mv_into_dir([], tmp_path / "archive", tmp_path, {})

    assert result == 0
    assert calls == []
