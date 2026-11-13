"""A halt carrying the `widened` list a ROW-LOCAL-BLOCK computed records it in the run dir when its record
commits, so the plain refire's row footprint and brief seed it; a denied path records nothing."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import emit, in_run_widen, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "warp-2026-11-13-x-20261113T000000-aaaaaa"
SIZING = "state/sizings/2026-11/2026-11-13-x.yaml"
WIDE = "coordinator_core/hooks/cater_subagent_start.py"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    rec = root / RECORD_DIR_ROOT / RUN
    rec.mkdir(parents=True)
    (rec / "run.json").write_text(json.dumps({"sizing_rel": SIZING}) + "\n", encoding="utf-8")
    (root / "run.mjs").write_text("// script\n", encoding="utf-8")
    return root


def _halt(repo, widened):
    return terminal_commit._handler(
        {
            "script_path": "run.mjs",
            "run_id": RUN,
            HALTED_RECORD_PARAM: {"halted": "dependents_unstarted", "reason": "r", "widened": widened},
        },
        repo_root=repo / ".git",
    )


def test_halt_widening_reaches_the_refire_seed(repo):
    out = _halt(repo, [{"row": "C2", "path": WIDE}])

    assert out["committed"] is True, out
    run_dir = repo / RECORD_DIR_ROOT / RUN
    assert in_run_widen.widened_by_row(run_dir) == {"C2": [WIDE]}
    assert emit._widened_seed(None, repo, RUN) == {"C2": WIDE}


def test_denied_path_records_nothing(repo):
    out = _halt(repo, [{"row": "C2", "path": "state/x.yaml"}, {"row": "C2", "path": "../escape.py"}])

    assert out["committed"] is True, out
    assert in_run_widen.widened_by_row(repo / RECORD_DIR_ROOT / RUN) == {}
    assert emit._widened_seed(None, repo, RUN) == {}


def test_halt_without_widening_records_nothing(repo):
    out = _halt(repo, None)

    assert out["committed"] is True, out
    assert not (repo / RECORD_DIR_ROOT / RUN / in_run_widen.WIDEN_RECORD_NAME).exists()
