"""Two runs dirty in one tree: run A's halt commit withholds the run artifact holding B's hunks and lands the rest."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops.dispatch_emit import hunk_provenance, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_halted_record import (  # noqa: F401
    RUN,
    _git,
    _landed,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PLAN = "docs/plans/2026-11-04-peer-plan.md"
PEER_BYTES = "# plan\nline one\nPEER WIRING calling uncommitted functions\n"
OWN_PRODUCT = "src/own.py"


def _write(root, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _halt(root) -> dict:
    return terminal_commit._handler(
        {"run_id": RUN, HALTED_RECORD_PARAM: {"halted": "refusal", "reason": "sizing refused", "plan": PLAN}},
        repo_root=root / ".git",
    )


def _two_runs_dirty(root) -> None:
    _write(root, PLAN, "# plan\nline one\n")
    _git(["add", PLAN], root)
    _git(["commit", "-q", "-m", "plan"], root)
    # Run B (not the halting run) is mid-flight in the plan; run A declares nothing there.
    _write(root, PLAN, PEER_BYTES)
    record = root / RECORD_DIR_ROOT / RUN
    record.mkdir(parents=True, exist_ok=True)
    (record / "ask.md").write_text("ask\n", encoding="utf-8")
    _write(root, f"{RECORD_DIR_ROOT}/ask-20261015T000001-bbbbbb/ask.md", "peer ask\n")


def test_halt_commit_withholds_the_artifact_holding_the_peer_hunk_and_leaves_it_untouched(repo):
    _two_runs_dirty(repo)

    out = _halt(repo)

    assert out["committed"] is True
    assert out["withheld"][PLAN] == hunk_provenance.FOREIGN_HUNK_UNPRODUCED
    assert PLAN not in _landed(repo, out, "--name-only")
    assert (repo / PLAN).read_text(encoding="utf-8") == PEER_BYTES
    assert _git(["show", f"HEAD:{PLAN}"], repo) == "# plan\nline one\n"


def test_halt_commit_withholding_one_path_still_commits_and_skips_the_peers_record(repo):
    _two_runs_dirty(repo)

    out = _halt(repo)

    landed = _landed(repo, out, "--name-only")
    assert landed
    assert f"{RECORD_DIR_ROOT}/ask-20261015T000001-bbbbbb/ask.md" not in landed
    assert (repo / RECORD_DIR_ROOT / "ask-20261015T000001-bbbbbb" / "ask.md").read_text(encoding="utf-8") == "peer ask\n"


def test_halt_commit_lands_a_tracked_artifact_whose_every_hunk_the_run_produced(repo):
    _write(repo, PLAN, "# plan\nline one\n")
    _git(["add", PLAN], repo)
    _git(["commit", "-q", "-m", "plan"], repo)
    record = repo / RECORD_DIR_ROOT / RUN
    record.mkdir(parents=True, exist_ok=True)
    (record / "ask.md").write_text("ask\n", encoding="utf-8")
    _write(repo, PLAN, PEER_BYTES)
    (record / "manifest.json").write_text(json.dumps({
        "rows": [{"id": "C1", "writes": [PLAN]}], "write_baseline": {PLAN: _git(["rev-parse", f"HEAD:{PLAN}"], repo).strip()},
    }), encoding="utf-8")

    out = _halt(repo)

    assert PLAN not in out.get("withheld", {})
    assert PLAN in _landed(repo, out, "--name-only")


def test_halt_commit_without_a_write_baseline_withholds_a_dirty_tracked_artifact(repo):
    _write(repo, PLAN, "# plan\nline one\n")
    _git(["add", PLAN], repo)
    _git(["commit", "-q", "-m", "plan"], repo)
    record = repo / RECORD_DIR_ROOT / RUN
    record.mkdir(parents=True, exist_ok=True)
    (record / "ask.md").write_text("ask\n", encoding="utf-8")
    _write(repo, PLAN, "# plan\nline one\nthe run's own edit\n")

    out = _halt(repo)

    assert out["committed"] is True
    assert out["withheld"][PLAN] == hunk_provenance.FOREIGN_HUNK_UNPRODUCED
    assert PLAN not in _landed(repo, out, "--name-only")
