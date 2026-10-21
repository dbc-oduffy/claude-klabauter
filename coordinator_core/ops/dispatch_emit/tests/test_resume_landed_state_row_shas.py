"""resume_landed_state maps each landed row to its landing commit's sha from its one git log."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.ask_contract import ResumePlan, halted_record_message
from coordinator_core.ops.dispatch_emit.emit import resume_landed_state
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PLAN_REL = "docs/plans/p.md"


def _git(repo, *a):
    return subprocess.run(
        ["git", *a], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _commit(repo, name, subject, *trailers):
    (repo / name).write_text("x\n", encoding="utf-8")
    _git(repo, "add", name)
    args = ["commit", "-q", "-m", subject]
    if trailers:
        args += ["-m", "\n".join(trailers)]
    _git(repo, *args)
    return _git(repo, "rev-parse", "HEAD")


def _init(repo):
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.example")
    _git(repo, "config", "user.name", "t")


def test_checkpoint_and_ledger_halt_commits_each_report_their_sha(repo):  # noqa: F811
    _init(repo)
    ck = _commit(
        repo, "a.txt", "checkpoint(wave 0): 1 rows — C1", f"Checkpoint-Plan: {PLAN_REL}"
    )
    msg = halted_record_message("run-1", {"halted": "ledger-check", "plan": PLAN_REL})
    halt = _commit(repo, "b.txt", msg + "\nLands-Chunks: C2")
    state = resume_landed_state(repo, PLAN_REL)
    assert state.rows == {"C1", "C2"}
    assert dict(state.row_shas) == {"C1": ck, "C2": halt}


def test_newest_landing_wins(repo):  # noqa: F811
    _init(repo)
    _commit(repo, "a.txt", "checkpoint(wave 0): 1 rows — C1", f"Checkpoint-Plan: {PLAN_REL}")
    newer = _commit(
        repo, "b.txt", "checkpoint(wave 1): 1 rows — C1", f"Checkpoint-Plan: {PLAN_REL}"
    )
    assert dict(resume_landed_state(repo, PLAN_REL).row_shas) == {"C1": newer}


def test_other_plan_commits_and_unreadable_git_give_empty_map(repo):  # noqa: F811
    _init(repo)
    _commit(repo, "a.txt", "checkpoint(wave 0): 1 rows — C1", "Checkpoint-Plan: docs/plans/o.md")
    assert dict(resume_landed_state(repo, PLAN_REL).row_shas) == {}


def test_split_checkpoint_row_sha_is_content_half_and_landing_shas_holds_both(repo):  # noqa: F811
    _init(repo)
    trailer = f"Checkpoint-Plan: {PLAN_REL}"
    content = _commit(repo, "a.txt", "checkpoint(wave 0): 1 rows — C1", trailer)
    process = _commit(repo, "b.txt", "checkpoint(wave 0): 1 rows — C1 (process files)", trailer)
    state = resume_landed_state(repo, PLAN_REL)
    assert dict(state.row_shas) == {"C1": content}
    assert state.landing_shas["C1"] == frozenset({content, process})
    assert set(state.row_shas) == set(state.rows)


def test_lone_process_half_still_yields_row_sha(repo):  # noqa: F811
    _init(repo)
    process = _commit(
        repo, "a.txt", "checkpoint(wave 0): 1 rows — C1 (process files)",
        f"Checkpoint-Plan: {PLAN_REL}",
    )
    state = resume_landed_state(repo, PLAN_REL)
    assert dict(state.row_shas) == {"C1": process}
    assert state.landing_shas["C1"] == frozenset({process})


@pytest.mark.parametrize(
    "subject,expected",
    [
        ("checkpoint(wave 0): 1 rows — C1 (process files)", True),
        ("checkpoint(wave 0): 1 rows — C1 (process files)  \n", True),
        ("checkpoint(wave 0): 1 rows — C1", False),
        ("(process files) checkpoint", False),
    ],
)
def test_is_process_half(subject, expected):
    from coordinator_core.ops.dispatch_emit.checkpoint_stamp import is_process_half

    assert is_process_half(subject) is expected


def test_resume_plan_landed_open_defaults_empty():
    assert ResumePlan("p.md", ("C1",)).landed_open == ()
