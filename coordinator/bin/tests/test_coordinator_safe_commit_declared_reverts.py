"""test_coordinator_safe_commit_declared_reverts.py -- `--declared-reverts`
parses, and reaches `ceremony.commit_v2`'s params.

WHY THIS EXISTS: `commit_v2`'s staged-rollback gate refuses a commit that
restores a path to an older state and tells the caller to "pass the reverted
path(s) in `declared_reverts` if this is intentional". The op accepted the
parameter; the sanctioned CLI had no flag that could send it, so the only
route the guard permits a caller to commit through could not comply with the
guard's own instruction. An intentional revert was unlandable -- not refused
on its merits, just unreachable.

Loaded by file path, matching this directory's hyphenated-module idiom.
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


def test_the_flag_survives_the_pathspec_form():
    """The subject sits immediately BEFORE `--` on this form, not last. A
    variadic flag sweeps it up as a path; a repeatable single-value one
    cannot, which is why the flag is shaped this way."""
    mod = _load_cli_module()
    args = mod.parse_args(
        [
            "--declared-revert", "a/one.py",
            "--declared-revert", "a/two.py",
            "subject", "--", "a/one.py",
        ]
    )
    assert args.declared_reverts == ["a/one.py", "a/two.py"]
    assert args.subject == "subject"
    assert args.paths == ["a/one.py"]


def test_the_flag_requires_a_path():
    mod = _load_cli_module()
    with pytest.raises(mod.UsageError, match="requires a path"):
        mod.parse_args(["--declared-revert"])


def test_the_refusal_message_names_a_flag_this_cli_accepts():
    """The gate's remediation text and the CLI's flag set must not drift
    apart again. Whatever the op tells a caller to pass, `parse_args` has to
    be able to accept."""
    mod = _load_cli_module()
    gate = (
        _BIN_DIR.parent.parent
        / "coordinator_core" / "ops" / "ceremony" / "git_native.py"
    ).read_text()
    assert "declared_reverts" in gate

    args = mod.parse_args(["--declared-revert", "x.py", "subject"])
    assert args.declared_reverts == ["x.py"]


def test_an_ordinary_commit_sends_no_declared_reverts_key():
    mod = _load_cli_module()
    args = mod.parse_args(["subject", "--", "a/one.py"])
    assert args.declared_reverts == []
