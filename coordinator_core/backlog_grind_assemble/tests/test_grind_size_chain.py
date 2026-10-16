"""grind-row size-chain: the superseded-predecessor skip and the three-step chain, spawn-free.

The runner seam `_run_tool` is substituted; settings-home shims are plain files under tmp_path.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from coordinator_core.backlog_grind_assemble import grind_rows, grind_size_chain

PROFILE = "p"
OLD = "2026-10-10-fix-the-thing-aaaaaaaaaaaa"
NEW = "2026-10-16-fix-the-thing-bbbbbbbbbbbb"
SIZING = "state/sizings/x.yaml"


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    for name in ("sizing-assemble", "coordinator-doc-new"):
        (home / "bin" / name).write_text("#!/bin/sh\n")
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    calls: list[list[str]] = []
    codes: dict[str, int] = {}

    def fake(argv, cwd):
        calls.append(list(argv))
        assert cwd == tmp_path / "repo"
        return codes.get(Path(argv[0]).name, 0)

    monkeypatch.setattr(grind_size_chain, "_run_tool", fake)
    repo = tmp_path / "repo"
    (repo / "state" / "queue-grind" / PROFILE).mkdir(parents=True)
    return repo, home, calls, codes


def _ledger(repo: Path, row_id: str, outcome: str) -> Path:
    path = repo / "state" / "queue-grind" / PROFILE / f"{row_id}.jsonl"
    path.write_text(json.dumps({"outcome": outcome, "row_id": row_id}, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _argv(repo: Path, row_id: str, *extra: str) -> list[str]:
    return [
        "--profile", PROFILE, "--row-id", row_id, "--digest", "d", "--sizing", SIZING,
        "--run-stamp", "T", "--repo-root", str(repo), "--", "--tshirt", "S", *extra,
    ]


def _lines(repo: Path, row_id: str) -> list[str]:
    path = repo / "state" / "queue-grind" / PROFILE / f"{row_id}.jsonl"
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def _tools(calls):
    return [Path(c[0]).name for c in calls]


def test_superseded_predecessor_skips(env, capsys):
    repo, _, calls, _ = env
    _ledger(repo, OLD, "route-to-plan")
    assert grind_size_chain.cmd_size_chain(_argv(repo, NEW)) == 0
    assert "size skipped" in capsys.readouterr().out
    assert calls == [] and _lines(repo, NEW) == []


def test_non_route_sibling_or_other_slug_runs_chain(env):
    repo, _, calls, _ = env
    _ledger(repo, OLD, "hand-back")
    _ledger(repo, "2026-10-10-other-slug-cccccccccccc", "route-to-plan")
    assert grind_size_chain.cmd_size_chain(_argv(repo, NEW)) == 0
    assert _tools(calls) == ["sizing-assemble", "coordinator-doc-new"]
    assert json.loads(_lines(repo, NEW)[0])["outcome"] == "route-to-plan"


def test_own_ledger_marked_does_not_skip(env):
    repo, _, calls, _ = env
    _ledger(repo, NEW, "route-to-plan")
    assert grind_size_chain.cmd_size_chain(_argv(repo, NEW)) == 0
    assert len(calls) == 2


def test_plain_row_id_reads_no_directory(env, monkeypatch):
    repo, _, calls, _ = env
    import types

    monkeypatch.setattr(
        grind_size_chain, "os", types.SimpleNamespace(listdir=lambda *_: pytest.fail("directory read"))
    )
    assert grind_size_chain.superseded_predecessor(repo, PROFILE, "plain-row") is None
    assert grind_size_chain.cmd_size_chain(_argv(repo, "plain-row")) == 0
    assert len(calls) == 2


def test_assemble_failure_stops_chain(env):
    repo, _, calls, codes = env
    codes["sizing-assemble"] = 7
    assert grind_size_chain.cmd_size_chain(_argv(repo, NEW)) == 7
    assert _tools(calls) == ["sizing-assemble"] and _lines(repo, NEW) == []


def test_doc_new_failure_stops_before_append(env):
    repo, _, calls, codes = env
    codes["coordinator-doc-new"] = 5
    assert grind_size_chain.cmd_size_chain(_argv(repo, NEW)) == 5
    assert _lines(repo, NEW) == []


def test_assemble_argv_carries_passthrough_and_fixed_flags(env):
    repo, _, calls, _ = env
    grind_size_chain.cmd_size_chain(_argv(repo, NEW, "--intent", "do it"))
    argv = calls[0][1:]
    assert argv[:4] == ["--tshirt", "S", "--intent", "do it"]
    assert "--no-verbatims" in argv and "--no-plan-hand-off" in argv
    assert argv[argv.index("--intent-source") + 1] == "em-elaborated"
    write = Path(argv[argv.index("--write") + 1])
    assert write.is_absolute() and write == repo / SIZING
    assert calls[1][1:] == ["--type", "spinoff", "--from-sizing", SIZING]


def test_missing_flag_exits_2(env):
    repo, *_ = env
    assert grind_size_chain.cmd_size_chain(["--profile", PROFILE, "--"]) == 2
    assert grind_size_chain.cmd_size_chain(["--profile", PROFILE]) == 2


def test_cmd_only_resolution_refuses_unsafe_argument(env):
    repo, home, calls, _ = env
    for name in ("sizing-assemble", "coordinator-doc-new"):
        (home / "bin" / name).unlink()
        (home / "bin" / f"{name}.cmd").write_text("@echo off\n")
    assert grind_size_chain.cmd_size_chain(_argv(repo, NEW, "--intent", "a & b")) != 0
    assert calls == []


def test_exe_probe_precedes_bare_name(env):
    _, home, _, _ = env
    (home / "bin" / "sizing-assemble.exe").write_text("")
    assert grind_size_chain._resolve_tool("sizing-assemble").name == "sizing-assemble.exe"


def test_module_has_no_shell_and_no_banned_pattern():
    text = Path(grind_size_chain.__file__).read_text(encoding="utf-8")
    assert "shell=True" not in text and "shlex" not in text
    banned = re.compile(r"/Users/|/home/|\b[A-Za-z]:\\|~/\.claude/projects|/private/tmp|gettempdir|machine_local")
    assert not banned.search(text)
