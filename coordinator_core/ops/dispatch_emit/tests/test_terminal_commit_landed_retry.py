"""A terminal commit that landed is reported as landed on a repeat call: the spent receipt is written
the moment the commit lands, a repeat call answers ``already_landed`` after the HEAD check, and a
call that finds the first one still holding the claim never lands a second commit."""

from __future__ import annotations

import subprocess
import threading
import time
from pathlib import Path

import pytest

from coordinator_core import dispatch_abandon
from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit.ask_contract import HALTED_RECORD_PARAM, RECORD_DIR_ROOT
from coordinator_core.ops.dispatch_emit.tests.test_ledger_halt_commits_delivered_rows import (
    _REVIEWED,
    _SID,
    RUN,
    _git,
    _verdict,
    repo,  # noqa: F401  (fixture)
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SCRIPT = "run.mjs"


def _call(root, token: str) -> dict:
    params = {
        "script_path": _SCRIPT, "incomplete_chunks": ["C9"], "inline_review": _REVIEWED,
        "session_id": _SID, "ledger_verdict": _verdict({"essential": False}), "run_id": RUN, "run_token": token,
    }
    return terminal_commit._handler(params, repo_root=root / ".git")


def _count(root) -> int:
    return int(_git(root, "rev-list", "--count", "HEAD").strip())


def _land(root) -> tuple:
    token = run_token.mint(root, RUN, _SCRIPT)
    first = _call(root, token)
    assert first["committed"] is True, first
    return token, first


def test_a_repeat_call_after_a_landed_commit_answers_already_landed(repo):  # noqa: F811
    token, first = _land(repo)
    head, count = _git(repo, "rev-parse", "HEAD"), _count(repo)

    again = _call(repo, token)

    assert again["committed"] is True and again["already_landed"] is True, again
    assert again["sha"] == first["sha"]
    assert _git(repo, "merge-base", "--is-ancestor", again["sha"], "HEAD") == ""
    assert _git(repo, "rev-parse", "HEAD") == head and _count(repo) == count


def test_the_spent_receipt_exists_before_the_post_commit_steps_run(repo, monkeypatch):  # noqa: F811
    token = run_token.mint(repo, RUN, _SCRIPT)
    seen: dict = {}

    def probe(close_ctx, sha):
        seen["receipt"] = run_token.spent_receipt(repo, RUN, token, _SCRIPT)
        seen["sha"] = sha
        raise RuntimeError("post-commit step failed")

    monkeypatch.setattr(terminal_commit, "_release_siblings_at_landing", probe)
    with pytest.raises(RuntimeError):
        _call(repo, token)
    assert seen["receipt"] is not None and seen["receipt"]["sha"] == seen["sha"]

    again = _call(repo, token)
    assert again["committed"] is True and again["already_landed"] is True and again["sha"] == seen["sha"]


def test_a_receipt_whose_sha_is_off_head_gets_the_run_token_refusal(repo):  # noqa: F811
    token, first = _land(repo)
    _git(repo, "reset", "-q", "--hard", f"{first['sha']}~1")
    count = _count(repo)

    again = _call(repo, token)

    assert again["committed"] is False and again["refused"] == "run-token", again
    assert _count(repo) == count


def test_a_repeat_call_with_a_wrong_token_is_refused(repo):  # noqa: F811
    _land(repo)
    again = _call(repo, "0" * 32)
    assert again["committed"] is False and again["refused"] == "run-token"


class _Deadline:
    def __init__(self, seconds: float):
        self._seconds = seconds

    def __enter__(self):
        self._token = dispatch_abandon.DEADLINE.set(time.monotonic() + self._seconds)

    def __exit__(self, *exc):
        dispatch_abandon.DEADLINE.reset(self._token)


def test_a_repeat_call_while_the_first_holds_the_claim_lands_nothing_and_is_refused_in_flight(repo):  # noqa: F811
    token = run_token.mint(repo, RUN, _SCRIPT)
    assert run_token.redeem(repo, RUN, token, _SCRIPT)
    head, count = _git(repo, "rev-parse", "HEAD"), _count(repo)

    with _Deadline(dispatch_abandon.REPLY_MARGIN_SECS + 0.2):
        again = _call(repo, token)

    assert again["committed"] is False and again["refused"] == "run-token-in-flight", again
    assert "committed nothing" in again["error"]
    assert _git(repo, "rev-parse", "HEAD") == head and _count(repo) == count
    assert run_token.claim_pending(repo, RUN, token, _SCRIPT) is True


def test_a_claim_that_lands_inside_the_deadline_is_answered_already_landed(repo):  # noqa: F811
    token = run_token.mint(repo, RUN, _SCRIPT)
    assert run_token.redeem(repo, RUN, token, _SCRIPT)
    head, count = _git(repo, "rev-parse", "HEAD").strip(), _count(repo)
    timer = threading.Timer(
        0.2, lambda: run_token.consume(repo, RUN, landed={"sha": head, "rows": ["C1"], "paths": ["a.py"]})
    )
    timer.start()
    try:
        with _Deadline(dispatch_abandon.REPLY_MARGIN_SECS + 3.0):
            again = _call(repo, token)
    finally:
        timer.join()

    assert again["committed"] is True and again["already_landed"] is True and again["sha"] == head, again
    assert _count(repo) == count


def test_a_claim_released_inside_the_deadline_lets_the_repeat_call_land(repo):  # noqa: F811
    token = run_token.mint(repo, RUN, _SCRIPT)
    assert run_token.redeem(repo, RUN, token, _SCRIPT)
    count = _count(repo)
    timer = threading.Timer(0.2, lambda: run_token.release(repo, RUN, token, _SCRIPT))
    timer.start()
    try:
        with _Deadline(dispatch_abandon.REPLY_MARGIN_SECS + 3.0):
            again = _call(repo, token)
    finally:
        timer.join()

    assert again["committed"] is True and not again.get("already_landed"), again
    assert _count(repo) > count


HALT_RUN = "ask-20261015T000000-aaaaaa"
_HALT = {"halted": "refusal", "reason": "sizing refused"}


@pytest.fixture
def halted_repo(tmp_path):
    root = tmp_path / "halted"
    root.mkdir()
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"],
                 ["config", "commit.gpgsign", "false"]):
        subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    (root / _SCRIPT).write_text("// script\n", encoding="utf-8")
    for args in (["add", "seed.txt"], ["commit", "-q", "-m", "seed"]):
        subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags())
    record = root / RECORD_DIR_ROOT / HALT_RUN
    record.mkdir(parents=True)
    (record / "ask.md").write_text("ask\n", encoding="utf-8")
    return root


def _halt_call(root: Path, token: str) -> dict:
    params = {"script_path": _SCRIPT, "run_id": HALT_RUN, "run_token": token, HALTED_RECORD_PARAM: dict(_HALT)}
    return terminal_commit._handler(params, repo_root=root / ".git")


def test_a_halt_commit_rerun_answers_the_landed_sha_lands_nothing_and_keeps_the_token_live(halted_repo):
    token = run_token.mint(halted_repo, HALT_RUN, _SCRIPT)
    first = _halt_call(halted_repo, token)
    assert first["committed"] is True, first
    head, count = _git(halted_repo, "rev-parse", "HEAD"), _count(halted_repo)

    again = _halt_call(halted_repo, token)

    assert again["committed"] is True and again["already_landed"] is True, again
    assert again["sha"] == first["sha"]
    assert _git(halted_repo, "rev-parse", "HEAD") == head and _count(halted_repo) == count
    assert run_token.admits(halted_repo, HALT_RUN, token, _SCRIPT) is True


def test_a_halt_commit_rerun_with_a_different_halt_kind_is_not_answered_from_the_receipt(halted_repo):
    token = run_token.mint(halted_repo, HALT_RUN, _SCRIPT)
    first = _halt_call(halted_repo, token)
    assert first["committed"] is True
    params = {
        "script_path": _SCRIPT, "run_id": HALT_RUN, "run_token": token,
        HALTED_RECORD_PARAM: {"halted": "stop_rule", "reason": "other"},
    }
    again = terminal_commit._handler(params, repo_root=halted_repo / ".git")
    assert not again.get("already_landed")
    assert again.get("sha") != first["sha"]
