"""``dispatch.land_credited`` leg budgets: a pre-commit leg never debits the commit leg; a halt records per-leg process ms."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import land_credited as lane
from coordinator_core.ops.dispatch_emit import land_credited_contract as contract
from coordinator_core.ops.dispatch_emit.commit_request import ChunkCommit, CommitRequest, render_marker
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

RUN = "warp-test-run"
SCRIPT = "scratch/run.js"


def _git(args, cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(["init", "-q"], root)
    _git(["config", "user.email", "t@example.com"], root)
    _git(["config", "user.name", "t"], root)
    _git(["config", "core.autocrlf", "false"], root)
    _write(root, "src/a.py", "v = 0\n")
    _git(["add", "."], root)
    _git(["commit", "-q", "-m", "seed"], root)
    marker = render_marker(CommitRequest(chunks=(ChunkCommit(id="C1", title="one", paths=("src/a.py",)),)))
    _write(root, SCRIPT, f"// script\n{marker}\n")
    _write(root, "src/a.py", "v = 1\n")
    return root


def _request() -> contract.LandCreditedRequest:
    return contract.LandCreditedRequest(run_id=RUN, plan_path="docs/plans/p.md", script_path=SCRIPT)


def _burn(secs: float) -> None:
    end = time.process_time() + secs
    while time.process_time() < end:
        pass


def _generous(monkeypatch, **overrides) -> None:
    budgets = {leg: 60.0 for leg in lane.PRECOMMIT_LEG_BUDGET_SECS}
    budgets.update(overrides)
    monkeypatch.setattr(lane, "PRECOMMIT_LEG_BUDGET_SECS", budgets)


def test_a_pre_commit_leg_over_budget_halts_naming_that_leg_not_commit(repo, monkeypatch):
    _generous(monkeypatch, holds=0.02)
    monkeypatch.setattr(lane, "_holds", lambda root, request: (_burn(0.05), {})[1])
    head = _git(["rev-parse", "HEAD"], repo)

    reply = lane.land(repo, _request())

    assert reply["committed"] is False
    assert reply["refused"] == "land-deadline"
    assert reply["leg"] == "holds"
    assert "leg holds" in reply["error"]
    assert lane.COMMIT_LEG not in reply["leg_timings"]
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_a_slow_pre_commit_leg_within_its_budget_still_commits(repo, monkeypatch):
    _generous(monkeypatch, holds=0.5)
    monkeypatch.setattr(lane, "_holds", lambda root, request: (_burn(0.1), {})[1])

    reply = lane.land(repo, _request())

    assert reply["committed"] is True, reply
    assert reply["leg_timings"]["holds"] >= 100
    assert lane.COMMIT_LEG in reply["leg_timings"]


def test_halt_reply_and_run_journal_carry_per_leg_process_ms_for_every_leg_that_ran(repo, monkeypatch):
    _generous(monkeypatch, holds=0.02)
    monkeypatch.setattr(lane, "_holds", lambda root, request: (_burn(0.05), {})[1])

    reply = lane.land(repo, _request())

    assert set(reply["leg_timings"]) == {"credited", "residue", "holds"}
    assert all(isinstance(ms, float) for ms in reply["leg_timings"].values())
    assert reply["leg_timings"]["holds"] >= 50
    journal = json.loads((repo / reply["leg_record"]).read_text(encoding="utf-8"))
    assert journal["outcome"] == "land-deadline"
    assert journal["leg"] == "holds"
    assert {e["leg"]: e["process_ms"] for e in journal["legs"]} == reply["leg_timings"]


def test_a_landing_within_budgets_reports_every_leg_including_commit(repo, monkeypatch):
    _generous(monkeypatch)

    reply = lane.land(repo, _request())

    assert reply["committed"] is True, reply
    assert set(reply["leg_timings"]) == {"credited", "residue", "holds", lane.COMMIT_LEG}


def test_a_slow_commit_leg_never_halts_a_landed_commit(repo, monkeypatch):
    import coordinator_core.git.commit as git_commit

    _generous(monkeypatch)
    real = git_commit.commit_paths

    def slow(*args, **kwargs):
        _burn(0.3)
        return real(*args, **kwargs)

    monkeypatch.setattr(git_commit, "commit_paths", slow)

    reply = lane.land(repo, _request())

    assert reply["committed"] is True
    assert "refused" not in reply
    assert reply["leg_timings"][lane.COMMIT_LEG] >= 300
