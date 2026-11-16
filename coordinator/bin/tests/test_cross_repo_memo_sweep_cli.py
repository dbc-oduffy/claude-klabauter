"""`cross-repo-memo sweep` forwards onto memo.sweep_ref_only: a dry run unless `--apply`."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import pathlib
import sys
import types

import pytest

_BIN_DIR = pathlib.Path(__file__).resolve().parent.parent

_RESULT = {
    "exit_code": 0,
    "dry_run": True,
    "repos": {
        "alpha": {
            "refs": 1181,
            "inbox_files": 25,
            "ref_only": {"open": 7, "terminal": 1100, "unreadable": 0},
            "age": {
                "open": {"<7d": 3, "7-30d": 2, "30-90d": 1, ">90d": 1, "unknown": 0},
                "terminal": {"<7d": 0, "7-30d": 0, "30-90d": 100, ">90d": 1000, "unknown": 0},
            },
        },
    },
    "not_local": ["gone"],
}


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader("cross_repo_memo_sweep_cli", str(_BIN_DIR / "cross-repo-memo.py"))
    spec = importlib.util.spec_from_loader("cross_repo_memo_sweep_cli", loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


@pytest.fixture
def cli(monkeypatch):
    mod = _load_cli_module()
    calls: list = []

    def route_mutation(op, params, sender_root, legacy):
        calls.append((op, dict(params)))
        return {**_RESULT, "dry_run": params["dry_run"], "materialized": 3, "remaining": 1100}

    monkeypatch.setitem(sys.modules, "cc_invoke", types.SimpleNamespace(route_mutation=route_mutation))
    monkeypatch.setattr(mod, "_current_repo_root", lambda: str(_BIN_DIR.parent.parent))
    return mod, calls


def test_no_flag_is_a_dry_run_that_prints_the_per_repo_table(cli, capsys):
    mod, calls = cli

    rc = mod.main(["sweep"])

    out = capsys.readouterr().out
    assert rc == 0
    assert calls == [("memo.sweep_ref_only", {"dry_run": True})]
    assert "alpha: refs=1181 inbox_files=25 ref_only.open=7 ref_only.terminal=1100" in out
    assert "age.terminal: <7d=0 7-30d=0 30-90d=100 >90d=1000" in out
    assert "gone: not_local" in out
    assert "materialized=" not in out


def test_apply_turns_the_dry_run_off_and_forwards_repo_and_limit(cli, capsys):
    mod, calls = cli

    rc = mod.main(["sweep", "--apply", "--repo", "alpha", "--repo", "beta", "--limit", "4"])

    assert rc == 0
    assert calls == [("memo.sweep_ref_only", {"dry_run": False, "repos": ["alpha", "beta"], "limit": 4})]
    assert "materialized=3 remaining=1100" in capsys.readouterr().out


def test_a_refused_apply_exits_nonzero_and_still_prints_the_table(cli, monkeypatch, capsys):
    mod, _calls = cli

    def refuse(op, params, sender_root, legacy):
        exc = RuntimeError("route_mutation: op='memo.sweep_ref_only' refused: materialize_commit_declined")
        exc.result = {**_RESULT, "dry_run": False}
        raise exc

    monkeypatch.setitem(sys.modules, "cc_invoke", types.SimpleNamespace(route_mutation=refuse))

    rc = mod.main(["sweep", "--apply"])

    captured = capsys.readouterr()
    assert rc == 1
    assert "materialize_commit_declined" in captured.err
    assert "alpha: refs=1181" in captured.out


def test_sweep_outside_a_git_tree_is_refused(cli, monkeypatch, capsys):
    mod, calls = cli
    monkeypatch.setattr(mod, "_current_repo_root", lambda: None)

    assert mod.main(["sweep"]) == 2
    assert calls == []
    assert "not a git working tree" in capsys.readouterr().err
