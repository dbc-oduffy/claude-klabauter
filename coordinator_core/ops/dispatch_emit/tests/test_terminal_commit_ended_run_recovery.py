"""An ended run's recorded task output recovers its unlanded commit through `--from-task-output`."""

from __future__ import annotations

import json

import pytest

from coordinator_core.invoke.__main__ import _dispatch_argv
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import (  # noqa: F401
    _REVIEWED,
    _SID,
    _git,
    _write,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_CAS_ERROR = (
    "compare-and-swap failed -- refs/heads/main moved since 24fbea48 was captured. "
    "Refusing to retry silently"
)


def _recorded_output(repo) -> str:
    """The shape of an ended run's task output: an envelope whose params carry no run_id or run_token.

    The CAS text rides only in ``halted``; no ref CAS is lost here, so these tests pin the flag's
    landing path, and the retry itself is pinned by ``test_terminal_commit_cas_retry``."""
    params = {
        "script_path": "run.mjs",
        "incomplete_chunks": [],
        "inline_review": _REVIEWED,
        "ledger_verdict": {"items": [{"id": "R1", "status": "met", "wired": True}]},
        "session_id": _SID,
        "held": [],
        "halted": {"halted": "commit", "error": _CAS_ERROR},
    }
    out = repo / "scratch" / "task-output.json"
    _write(repo, "scratch/task-output.json", json.dumps(
        {"summary": "run ended", "logs": [], "result": {"next_action": {"params": params}}}
    ))
    return str(out)


def _fire(repo, *argv: str) -> tuple[dict, str, int]:
    stdout, stderr, code = _dispatch_argv(["dispatch.terminal_commit", *argv], str(repo), allow_warm=False)
    return (json.loads(stdout) if stdout.strip() else {}), stderr, code


def _result(envelope: dict) -> dict:
    return envelope.get("result", envelope)


def test_the_flag_alone_lands_the_product_over_a_peer_commit_made_since_the_run(repo):
    task_output = _recorded_output(repo)
    _write(repo, "peer.txt", "peer\n")
    _git(repo, "add", "--", "peer.txt")
    _git(repo, "commit", "-q", "-m", "peer", "--", "peer.txt")
    peer = _git(repo, "rev-parse", "HEAD").strip()

    envelope, stderr, code = _fire(repo, "--from-task-output", task_output)
    out = _result(envelope)
    assert code == 0 and out.get("committed") is True, (stderr, envelope)
    assert _git(repo, "rev-parse", f"{out['sha']}^").strip() == peer
    assert _git(repo, "show", f"{out['sha']}:a.py") == "a\n"
    assert _git(repo, "show", f"{out['sha']}:peer.txt") == "peer\n"


def test_a_second_identical_call_lands_nothing_new(repo):
    task_output = _recorded_output(repo)
    first = _result(_fire(repo, "--from-task-output", task_output)[0])
    assert first.get("committed") is True, first
    head = _git(repo, "rev-parse", "HEAD").strip()

    second = _result(_fire(repo, "--from-task-output", task_output)[0])
    assert second.get("nothing_to_commit") or second.get("already_landed_sha"), second
    assert _git(repo, "rev-parse", "HEAD").strip() == head


def test_a_stale_run_token_is_refused_and_the_error_names_the_recovery_flag(repo):
    params = json.dumps({
        "script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
        "session_id": _SID, "run_id": "run1", "run_token": "stale-token",
        "ledger_verdict": {"items": [{"id": "R1", "status": "met", "wired": True}]},
    })
    envelope, stderr, code = _fire(repo, params)
    out = _result(envelope)
    assert out.get("refused") == "run-token", (stderr, envelope)
    assert "--from-task-output" in out["error"]
    assert not (repo / "a.py").exists() or "a.py" not in _git(repo, "ls-tree", "-r", "--name-only", "HEAD").split()
