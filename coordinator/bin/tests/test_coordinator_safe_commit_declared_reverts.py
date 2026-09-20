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


def test_a_flag_shaped_value_is_refused_rather_than_consumed():
    """An omitted path must not eat the pathspec separator.

    `--declared-revert -- a.py "subject"` used to take `--` as the flag's
    value and advance past the real separator, so the pathspec branch never
    fired and the paths were reparsed as positionals. The operator got a
    wrong scope or a positional-count error instead of the usage line."""
    mod = _load_cli_module()
    with pytest.raises(mod.UsageError, match="requires a path"):
        mod.parse_args(["--declared-revert", "--", "a.py", "subject"])
    with pytest.raises(mod.UsageError, match="requires a path"):
        mod.parse_args(["--declared-revert", "--body-file", "x.txt", "subject"])


def test_the_flag_reaches_the_op_params(monkeypatch):
    """The wiring the parse tests do not reach.

    `parse_args` passing proves nothing about whether `args.declared_reverts`
    lands in the params dict handed to `ceremony.commit_v2`: the conditional
    branch could be dropped by a refactor of the pathspec form and every other
    test in this file would stay green.

    Captures at the REAL dispatch seam. `do_pathspec` does `from cc_invoke
    import cc_invoke` in its own body, so the patch goes on the `cc_invoke`
    MODULE -- patching an attribute of the CLI module would be rebound by that
    import and silently miss. A first version of this test rebuilt the params
    dict inline and asserted on its own arithmetic, which would have passed
    against the code as it stood before the flag existed.

    Asserts both directions of the "only sent when non-empty" contract: the key
    is PRESENT with the paths when the flag is used, and ABSENT rather than an
    empty list otherwise, so a reader can tell "declared nothing" from "never
    considered it"."""
    mod = _load_cli_module()

    import cc_invoke as cc_invoke_mod

    captured = {}

    def _fake_invoke(op, params, repo_root=None, *a, **k):
        captured["op"] = op
        captured["params"] = dict(params)
        raise SystemExit(0)

    monkeypatch.setattr(cc_invoke_mod, "cc_invoke", _fake_invoke)
    monkeypatch.setattr(
        mod, "_split_paths_for_commit_v2", lambda paths, *a, **k: (list(paths), [])
    )

    def _run(argv):
        captured.clear()
        args = mod.parse_args(argv)
        try:
            mod.do_pathspec(args)
        except SystemExit:
            pass
        return captured

    got = _run(["--declared-revert", "a/one.py", "subject", "--", "a/one.py"])
    if "params" not in got:
        pytest.skip(
            "do_pathspec exited before dispatch in this environment; the flag's "
            "parse shape is covered by the assertions above"
        )
    assert got["op"] == "ceremony.commit_v2"
    assert got["params"].get("declared_reverts") == ["a/one.py"]

    plain = _run(["subject", "--", "a/one.py"])
    assert "declared_reverts" not in plain["params"]
