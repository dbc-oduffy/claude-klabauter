"""`--only-incomplete` against a spine honours a task output's `incomplete_chunks`; a git-log dump is unchanged."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = "docs/plans/p.md"
_ROWS = ["C1", "C2", "C3", "C4", "C5", "C6"]


def _plan() -> str:
    out = ""
    for rid in _ROWS:
        out += (
            f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: docs/x/{rid}.md\n"
            f"  writes:\n    - docs/x/{rid}.md\n  queue_scope: project\n  disposition: open\n"
            f"  body: |\n    Do {rid}.\n"
        )
    return (
        '---\ntitle: "p"\nsizing_object: null\n---\n\n# p\n\n## Goal\n\ng\n\n'
        f"## Tasks\n\n```yaml plan-tasks\n{out}```\n"
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    r = tmp_path / "repo"
    (r / "docs" / "plans").mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.example")
    _git(r, "config", "user.name", "t")
    (r / _PLAN).write_text(_plan(), encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    monkeypatch.chdir(r)
    return r


def _task_output(repo: Path, incomplete: list, *, nested: bool = False) -> str:
    outcome = {"incomplete_chunks": incomplete, "landed_chunks": ["Z9"]}
    params = {"run_outcome": outcome} if nested else outcome
    p = repo.parent / "task-output.json"
    p.write_text(json.dumps({"next_action": {"kind": "land_run", "params": params}}), encoding="utf-8")
    return str(p)


def _emit(repo: Path, *extra: str) -> tuple:
    out = repo / "p.workflow.mjs"
    out.unlink(missing_ok=True)
    code = cli.main(["--plan", str(repo / _PLAN), "--out", str(out), *extra])
    return code, (out.read_text(encoding="utf-8") if out.exists() else "")


def _registered(script: str) -> set:
    names = re.findall(r"_rows\['([\w-]+)'\] = _runRow\(", script)
    return {rid for name in names for rid in name.split("-")}


@pytest.mark.parametrize("nested", [False, True])
def test_reemit_emits_only_the_task_outputs_incomplete_rows(repo, nested):
    code, script = _emit(repo, "--only-incomplete", _task_output(repo, ["C2", "C5"], nested=nested))

    assert code == cli.EXIT_OK
    assert _registered(script) == {"C2", "C5"}


def test_a_git_log_dump_still_reemits_every_unlanded_row(repo, tmp_path):
    dump = tmp_path / "log.txt"
    dump.write_text("abc checkpoint(wave 1): 1 rows — C1\n", encoding="utf-8")

    code, script = _emit(repo, "--only-incomplete", str(dump))

    assert code == cli.EXIT_OK
    assert _registered(script) == {"C2", "C3", "C4", "C5", "C6"}


def test_an_incomplete_list_naming_no_spine_row_changes_nothing(repo):
    code, script = _emit(repo, "--only-incomplete", _task_output(repo, ["Q1"]))

    assert code == cli.EXIT_OK
    assert _registered(script) == set(_ROWS)
