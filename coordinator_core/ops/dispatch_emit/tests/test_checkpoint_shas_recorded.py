"""The checkpoint JS records committed shas and resume_landed_state records plan checkpoint shas."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.ask_contract import halted_record_message
from coordinator_core.ops.dispatch_emit.emit import SeamLeg, _checkpoint_commit_js, resume_landed_state
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import repo  # noqa: F401
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

PLAN_REL = "docs/plans/p.md"
PUSH = "_checkpointShas.push(r.sha)"


def _js(seam=None):
    return _checkpoint_commit_js(
        None, repo_root=None, session_id=None, plan_path=PLAN_REL, run_base_sha="a" * 40, seam=seam,
    )


@pytest.mark.parametrize("seam", [None, SeamLeg((PLAN_REL,), (), {}, {})], ids=["no-seam", "seam"])
def test_checkpoint_js_declares_and_pushes_commit_sha(seam):
    js = _js(seam)
    assert "const _checkpointShas = [];" in js
    assert PUSH in js
    assert js.index("_carried.length = 0;") < js.index(PUSH)
    assert js.index("r.outcome !== 'committed'") < js.index(PUSH)


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


def _walk_repo(repo):
    _init(repo)
    c1 = _commit(repo, "a.txt", "checkpoint(wave 0): 1 rows — C1", f"Checkpoint-Plan: {PLAN_REL}")
    _commit(repo, "o.txt", "checkpoint(wave 0): 1 rows — C9", "Checkpoint-Plan: docs/plans/other.md")
    msg = halted_record_message("run-1", {"halted": "ledger-check", "plan": PLAN_REL})
    halt = _commit(repo, "h.txt", msg + "\nLands-Chunks: C2")
    c2 = _commit(repo, "b.txt", "checkpoint(wave 1): 1 rows — C7", f"Checkpoint-Plan: {PLAN_REL}")
    return c1, halt, c2


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_resume_landed_state_records_plan_checkpoint_shas_and_ordinals(repo):  # noqa: F811
    c1, halt, c2 = _walk_repo(repo)
    state = resume_landed_state(repo, PLAN_REL)
    assert state.checkpoint_shas == (c2, c1)
    o = state.walk_ordinals
    assert o[c2] < o[halt] < o[c1]


@pytest.mark.cadence
@pytest.mark.spawns_process
def test_resume_landed_state_still_one_git_call(repo, monkeypatch):  # noqa: F811
    _walk_repo(repo)
    from coordinator_core.git import run as git_run

    calls = []
    real = git_run.run_git

    def counting(*a, **k):
        calls.append(a)
        return real(*a, **k)

    monkeypatch.setattr(git_run, "run_git", counting)
    resume_landed_state(repo, PLAN_REL)
    assert len(calls) == 1


def test_resume_landed_defaults_keep_positional_construction():
    s = emit.ResumeLanded(frozenset(), None)
    assert s.checkpoint_shas == () and dict(s.walk_ordinals) == {}
