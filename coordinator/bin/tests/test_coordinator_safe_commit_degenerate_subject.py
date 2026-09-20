"""test_coordinator_safe_commit_degenerate_subject.py -- pins the parse_args
refusal for a subject too short to be legible in `git log --oneline`/blame/
bisect output (state/bug-backlog/2026-08-28-an-engine-commit-path-landed-152-
lines-under-the-subject-x.yaml).

Loaded by file path (`importlib.machinery.SourceFileLoader`), matching this
directory's existing hyphenated-module idiom (see
test_coordinator_safe_commit_pathspec_batch.py).
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_safe_commit", str(_BIN_DIR / "coordinator-safe-commit.py")
    )
    spec = importlib.util.spec_from_loader("coordinator_safe_commit", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


def test_one_character_subject_is_refused():
    mod = _load_cli_module()
    with pytest.raises(mod.UsageError, match="too short"):
        mod.parse_args(["x"])


def test_degenerate_subject_is_refused_on_the_pathspec_form():
    mod = _load_cli_module()
    with pytest.raises(mod.UsageError, match="too short"):
        mod.parse_args(["bu", "--", "some/file.py"])


def test_a_normal_length_subject_still_parses():
    mod = _load_cli_module()
    args = mod.parse_args(["fix the frobnicator"])
    assert args.subject == "fix the frobnicator"


def test_a_four_character_subject_is_not_degenerate():
    mod = _load_cli_module()
    args = mod.parse_args(["wip!"])
    assert args.subject == "wip!"
