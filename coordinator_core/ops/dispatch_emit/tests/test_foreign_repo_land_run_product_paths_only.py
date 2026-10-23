"""dispatch.land_run in a repo without coordinator state commits only product paths; the same fixture
with `archive/` present lands the run's artifacts."""

from __future__ import annotations

import pytest

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.process_artifact_policy import WITHHELD_REASON
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (  # noqa: F401
    PLAN,
    RUN,
    SIZING,
    _call,
    _closable_plan,
    _git,
    _MINTING_REVIEW,
    _names,
    _request,
    _run_close_job,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence, pytest.mark.foreign_repo]

BATON = "state/handoffs/2026-01-01-p.md"
NOTE = "docs/delivery/run1.md"
_ARTIFACTS = {PLAN, SIZING, BATON, f"{RECORD_DIR_ROOT}/{RUN}/usage.json"}


def _stage_run(repo) -> None:
    _closable_plan(repo, PLAN)
    _write(repo, SIZING, (repo / SIZING).read_text(encoding="utf-8") + f"baton: {BATON}\n")
    _write(repo, BATON, "---\ntitle: p\nstatus: open\nkind: session-handoff\n---\n\n# handoff\n")
    _write(repo, NOTE, "# delivered\n")
    _request(repo, docs=(NOTE,))


def _exclude_lines(repo) -> list:
    path = resolve_git_common_dir(repo) / "info" / "exclude"
    return path.read_text(encoding="utf-8").splitlines() if path.is_file() else []


def test_land_run_without_coordinator_state_commits_only_product_paths(repo):
    assert not (repo / "archive").exists() and not (repo / "state/workstreams").exists()
    _stage_run(repo)
    out = _call(repo, inline_review=_MINTING_REVIEW)

    assert out["committed"] is True, out
    names = _names(repo, out["sha"])
    assert "a.py" in names
    assert not names & _ARTIFACTS
    assert not any(n.startswith(("state/", "archive/")) for n in names), names

    withheld = out["run_artifacts"]["withheld"]
    for path in (PLAN, SIZING, BATON):
        assert withheld.get(path) == WITHHELD_REASON, withheld

    legs = _run_close_job(repo, out)
    assert not (repo / "archive").exists(), (legs, sorted(p.as_posix() for p in (repo / "archive").rglob("*")))
    assert not any(n.startswith("archive/") for n in _names(repo, "HEAD"))
    # Tracked SIZING stays modified (withheld, not committed) and run.mjs is the harness's own script.
    dirty = {ln[3:] for ln in _git(repo, "status", "--porcelain", "-uall").splitlines()}
    assert dirty <= {SIZING, "run.mjs"}, dirty
    lines = _exclude_lines(repo)
    for path in (PLAN, SIZING, BATON):
        assert f"/{path}" in lines, lines
    assert len(lines) == len(set(lines))


def test_land_run_in_an_onboarded_repo_lands_the_artifacts(repo):
    (repo / "archive").mkdir()
    _write(repo, "archive/.keep", "")
    _stage_run(repo)
    out = _call(repo, inline_review=_MINTING_REVIEW)

    assert out["committed"] is True, out
    names = _names(repo, out["sha"])
    assert BATON in names and "a.py" in names
    assert not any(v == WITHHELD_REASON for v in out["run_artifacts"].get("withheld", {}).values())
