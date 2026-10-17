"""The baton-ship leg is synchronous: no asyncio, one write per baton, bytes equal to the handler sequence."""

from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit.run_close_baton_ship import baton_ship_leg
from coordinator_core.ops.dispatch_emit.tests.test_run_close_baton_ship import BODY, SHA, _baton
from coordinator_core.ops.tests.test_handoff_correct_body import _AUTHOR_SESSION, _make_git_repo

_CHILD = """
import json, os, sys
from pathlib import Path
replaces = []
real = os.replace
def counting(src, dst, *a, **k):
    replaces.append(str(dst))
    return real(src, dst, *a, **k)
os.replace = counting
from coordinator_core.ops.dispatch_emit.run_close_baton_ship import baton_ship_leg
root = Path(sys.argv[1])
first = baton_ship_leg(root, "run-1", [sys.argv[2]], sys.argv[3])
batons = [r for r in replaces if r.replace("\\\\", "/").endswith(sys.argv[2])]
n_first = len(batons)
second = baton_ship_leg(root, "run-1", [sys.argv[2]], sys.argv[3])
n_total = len([r for r in replaces if r.replace("\\\\", "/").endswith(sys.argv[2])])
print(json.dumps({"asyncio": "asyncio" in sys.modules, "first": first.status, "writes": n_first,
                  "second": second.status, "rewrites": n_total - n_first}))
"""


@pytest.fixture
def session_env(monkeypatch):
    monkeypatch.setenv("COORDINATOR_SESSION_ID", _AUTHOR_SESSION)


def _no_clock(text: str) -> str:
    return re.sub(r"\d{4}-\d\d-\d\dT[\d:.]+(\+00:00|Z)?", "<ts>", text)  # abs-path-ok: ISO timestamp regex, not a path


def _golden(repo: Path, rel: str) -> str:
    from coordinator_core.ops.handoff_append_session_ledger import _handler as ledger
    from coordinator_core.ops.handoff_stamp import _handler as stamp
    from coordinator_core.ops.handoff_transition import _handler as transition

    marker = "run run-1 shipped in " + SHA[:8]

    async def go():
        assert (await stamp({"handoff_path": rel, "sha": SHA[:8], "kind": "ship-commit"}, repo_root=repo))["exit_code"] == 0
        assert (await transition({"handoff_path": rel, "verb": "ship"}, repo_root=repo))["exit_code"] == 0
        reply = await ledger(
            {"handoff_path": rel, "summary": marker, "closing_ceremony": "run-close"}, repo_root=repo
        )
        assert reply["exit_code"] == 0, reply

    asyncio.run(go())
    return (repo / rel).read_text(encoding="utf-8")


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_fresh_interpreter_no_asyncio_one_write_golden_bytes_and_silent_rerun(tmp_path, session_env):
    (tmp_path / "gold").mkdir()
    (tmp_path / "new").mkdir()
    gold_repo = _make_git_repo(tmp_path / "gold")
    new_repo = _make_git_repo(tmp_path / "new")
    rel = _baton(gold_repo, "a.md")
    assert _baton(new_repo, "a.md") == rel
    golden = _golden(gold_repo, rel)

    proc = subprocess.run(
        [sys.executable, "-c", _CHILD, str(new_repo), rel, SHA],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[4])},
        cwd=str(tmp_path),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout.strip().splitlines()[-1])

    assert out["asyncio"] is False
    assert out["first"] == "ran"
    assert out["writes"] == 1
    assert out["second"] == "discharged"
    assert out["rewrites"] == 0
    assert _no_clock((new_repo / rel).read_text(encoding="utf-8")) == _no_clock(golden)


def test_refused_ship_leaves_baton_byte_unchanged(tmp_path, session_env, monkeypatch):
    from coordinator_core.locked_write import MutateAbort
    from coordinator_core.ops import handoff_transition

    repo = _make_git_repo(tmp_path)
    rel = _baton(repo, "a.md")
    before = (repo / rel).read_bytes()

    def refuse(text, params, *, repo_root=None):
        raise MutateAbort({"exit_code": 1, "error": "no way"})

    monkeypatch.setattr(handoff_transition, "ship_apply", refuse)

    result = baton_ship_leg(repo, "run-1", [rel], SHA)

    assert result.status == "failed"
    assert result.detail == f"{rel}: ship refused: no way"
    assert (repo / rel).read_bytes() == before
