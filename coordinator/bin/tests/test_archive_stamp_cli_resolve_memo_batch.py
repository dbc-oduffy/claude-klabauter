"""resolve-memo: per-subcommand help, the --realized-by decision default, and N-memo one-commit batching."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import unittest.mock
from pathlib import Path
from types import SimpleNamespace

from coordinator_core import archive_stamp
from coordinator_core.ops.ceremony import git_native

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli():
    loader = importlib.machinery.SourceFileLoader(
        "archive_stamp_cli_resolve_batch_test", str(_BIN_DIR / "archive-stamp-cli.py")
    )
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli()


def _help_text(subcmd: str) -> str:
    with unittest.mock.patch("sys.stdout") as out:
        rc = _cli.main([subcmd, "--help"])
    assert rc == 0
    return "".join(c.args[0] for c in out.write.call_args_list if c.args)


def test_resolve_memo_help_describes_each_flag_and_the_decision_rule():
    printed = _help_text("resolve-memo")
    assert "resolve-memo <memo_path>..." in printed
    assert "ONE commit" in printed
    assert "--realized-by <value>         commit/artifact" in printed
    assert "defaults to 'accepted'" in printed
    assert "Required with" in printed and "--decision-note" in printed


def test_action_memo_help_shares_the_flag_descriptions():
    assert "--decision <accepted|partial|declined>" in _help_text("action-memo")


def test_realized_by_without_decision_defaults_to_accepted():
    params = archive_stamp._parse_disposition_args(("--realized-by", "abc1234"))
    assert params["decision"] == "accepted"


def test_explicit_decision_wins_and_decision_note_alone_gets_no_default():
    explicit = archive_stamp._parse_disposition_args(
        ("--realized-by", "abc1234", "--decision", "partial")
    )
    assert explicit["decision"] == "partial"
    note_only = archive_stamp._parse_disposition_args(("--decision-note", "why"))
    assert "decision" not in note_only


def test_superseded_by_gets_no_decision_default():
    params = archive_stamp._parse_disposition_args(
        ("--superseded-by", "m.md", "--realized-by", "abc1234")
    )
    assert "decision" not in params


def test_batch_of_three_memos_lands_one_commit(tmp_path, monkeypatch):
    memos = [tmp_path / f"m{i}.md" for i in range(3)]
    for m in memos:
        m.write_text("x", encoding="utf-8")
    transition_calls: list[dict] = []

    def fake_transition(memo_path, params):
        transition_calls.append(params)
        return {"exit_code": 0, "applied": True}

    commits: list[list[str]] = []

    def fake_commit_scoped(paths, msg_file, cwd, **kwargs):
        commits.append(list(paths))
        return SimpleNamespace(ok=True, stdout="deadbeef\n", stderr="")

    monkeypatch.setattr(archive_stamp, "_call_memo_transition", fake_transition)
    monkeypatch.setattr(archive_stamp, "_worktree_root", lambda p: tmp_path)
    monkeypatch.setattr(archive_stamp, "resolve_current_session_id", lambda worktree_root=None: "sess-1")
    monkeypatch.setattr(git_native, "commit_scoped", fake_commit_scoped)
    monkeypatch.setattr(
        "coordinator_core.session.scope.release_committed_claims_or_retain",
        lambda *a, **k: None,
    )

    rc = archive_stamp.cs_resolve_memos(
        [str(m) for m in memos], "--realized-by", "abc1234"
    )

    assert rc == 0
    assert len(commits) == 1
    assert sorted(commits[0]) == sorted(m.name for m in memos)
    assert len(transition_calls) == 3
    assert all(c["defer_commit"] and c["decision"] == "accepted" for c in transition_calls)


def test_cli_routes_multiple_paths_to_the_batch_function():
    mock_mod = unittest.mock.Mock()
    mock_mod.cs_resolve_memos.return_value = 0
    with unittest.mock.patch.object(_cli, "_import_module", lambda: mock_mod):
        rc = _cli.main(["resolve-memo", "a.md", "b.md", "c.md", "--realized-by", "abc1234"])
    assert rc == 0
    mock_mod.cs_resolve_memos.assert_called_once_with(
        ["a.md", "b.md", "c.md"], "--realized-by", "abc1234"
    )


def test_action_memo_still_refuses_extra_paths():
    mock_mod = unittest.mock.Mock()
    with unittest.mock.patch.object(_cli, "_import_module", lambda: mock_mod):
        with unittest.mock.patch("sys.stderr"):
            rc = _cli.main(["action-memo", "a.md", "b.md", "--decision", "accepted"])
    assert rc == 2
