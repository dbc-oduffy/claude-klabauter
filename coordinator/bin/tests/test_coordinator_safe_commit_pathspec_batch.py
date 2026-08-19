"""test_coordinator_safe_commit_pathspec_batch.py -- multi-item coverage for
`coordinator-safe-commit.py::_first_invalid_pathspec` (amplification
burn-down, `state/ledgers/wave4-dispositions/c1.md`, keys
`do_scope_from -> _validate_pathspec` and `do_scoped -> _validate_pathspec`).

A single-item test passes identically before and after a batching change --
see the plan's own warning about `_own_frozen_diff_shas`. These tests assert
the CALL COUNT (one batched `git ls-files` call on the all-valid path, never
one per pathspec), not just the returned verdict.

Loaded by file path (`importlib.machinery.SourceFileLoader`), matching this
directory's existing hyphenated-module idiom.
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import types

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def test_all_valid_pathspecs_is_one_subprocess_call(monkeypatch):
    mod = _load_cli_module()
    calls: list[list[str]] = []

    def _fake_run(cmd, **_kwargs):
        calls.append(cmd)
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    result = mod._first_invalid_pathspec(["a/b.py", "c/*.py", "d/e.py"])

    assert result is None
    assert len(calls) == 1
    assert calls[0][:3] == ["git", "ls-files", "--"]
    assert calls[0][3:] == ["a/b.py", "c/*.py", "d/e.py"]


def test_one_invalid_pathspec_falls_back_to_per_item_and_names_it(monkeypatch):
    mod = _load_cli_module()
    calls: list[list[str]] = []

    def _fake_run(cmd, **_kwargs):
        calls.append(cmd)
        pathspecs = cmd[3:]
        # Batch call (3 pathspecs) fails; per-item fallback fails only for
        # the deliberately-bad one.
        if len(pathspecs) > 1:
            return types.SimpleNamespace(returncode=1, stdout="", stderr="bad pathspec")
        return types.SimpleNamespace(
            returncode=0 if pathspecs[0] != "::bad::" else 1, stdout="", stderr=""
        )

    monkeypatch.setattr(mod.subprocess, "run", _fake_run)

    result = mod._first_invalid_pathspec(["a/b.py", "::bad::", "d/e.py"])

    assert result == "::bad::"
    # 1 batched call + up to 3 per-item fallback calls (stops at first bad one).
    assert len(calls) == 3


def test_empty_pathspec_list_is_a_zero_spawn_noop(monkeypatch):
    mod = _load_cli_module()
    calls = []
    monkeypatch.setattr(mod.subprocess, "run", lambda *a, **k: calls.append(1))

    result = mod._first_invalid_pathspec([])

    assert result is None
    assert calls == []
