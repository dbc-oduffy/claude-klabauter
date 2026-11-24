"""Two sibling runs of one parent, one session, one base, identical declared paths: concurrent review.prep keeps each reply on its own run-scoped prefix."""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.review_mint import execute_review as er
from coordinator_core.ops.review_slice_id import run_prep_slice_prefix
from coordinator_core.ops.tests.test_review_prep import _base, _write, repo  # noqa: F401
from coordinator_core.ops.tests.test_review_prep_run_id import _stage

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

SESSION = "sess-own"
RUN_A = "ask-20261124T195842-45c335"
RUN_B = "ask-20261124T195842-bdb8b7"
DECLARED = ["a/one.py", "b/two.py"]
ROUNDS = 4


def _params(repo: Path, run: str) -> dict:
    return {"base": _base(repo), "plan_path": "docs/plans/p.md", "worktree": True, "session_id": SESSION, "run_id": run}


def _concurrent_preps(repo: Path) -> dict:
    barrier = threading.Barrier(2)
    replies: dict = {}

    def go(run: str) -> None:
        barrier.wait(timeout=30)
        try:
            replies[run] = rp._handler(_params(repo, run), repo)
        except Exception as exc:  # surfaced by the assertion on the missing reply
            replies[run] = {"error": f"raised: {exc!r}", "slices": []}

    threads = [threading.Thread(target=go, args=(run,)) for run in (RUN_A, RUN_B)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return replies


def _reply_paths(reply: dict) -> list:
    out = [reply["whole_diff_path"], reply["sidecar_path"]]
    return out + [p for s in reply["slices"] for p in (s["diff_path"], s["sidecar_path"])]


def _basename(path: str) -> str:
    return path.replace("\\", "/").rsplit("/", 1)[-1]


@pytest.fixture
def staged(repo):
    _write(repo, DECLARED, 3)
    for run in (RUN_A, RUN_B):
        _stage(repo, DECLARED, run_id=run)
    return repo


def test_concurrent_identical_path_siblings_each_get_only_their_own_prep(staged):
    """Pins the engine's existing run-scoped isolation; the observed collision lives in the shared reply file, pinned by the named-reply-file test below."""
    repo = staged
    base = _base(repo)
    for _ in range(ROUNDS):
        replies = _concurrent_preps(repo)
        assert set(replies) == {RUN_A, RUN_B}
        for run, other in ((RUN_A, RUN_B), (RUN_B, RUN_A)):
            reply = replies[run]
            assert reply["error"] is None, reply["error"]
            assert reply["slices"]
            own, foreign = run_prep_slice_prefix(run, base), run_prep_slice_prefix(other, base)
            for path in _reply_paths(reply):
                name = _basename(path)
                assert name.startswith(own), f"{run} reply names {path}"
                assert not name.startswith(foreign), f"{run} reply leaked sibling {path}"


_REPLY_FILE_RE = re.compile(r"redirected to `([^`]+)` in your scratchpad")


def _reply_file_named_for(run: str, base: str):
    instruction = er._wave_prep_instruction('"worktree":true', base, "unused-slice-id", None, grouped=False, run_id=run)
    match = _REPLY_FILE_RE.search(instruction)
    return match.group(1) if match else None


def test_concurrent_siblings_each_read_only_their_own_named_reply_file(staged, tmp_path):
    repo = staged
    base = _base(repo)
    scratchpad = tmp_path / "scratchpad"
    scratchpad.mkdir()
    named = {run: _reply_file_named_for(run, base) for run in (RUN_A, RUN_B)}
    assert all(named.values()), "the wave prep instruction names no reply file: the shared unnamed scratchpad path is back"
    assert named[RUN_A] != named[RUN_B]
    names = named
    for _ in range(ROUNDS):
        barrier = threading.Barrier(2)

        def go(run: str) -> None:
            barrier.wait(timeout=30)
            reply = rp._handler(_params(repo, run), repo)
            (scratchpad / names[run]).write_text(json.dumps(reply), encoding="utf-8")

        threads = [threading.Thread(target=go, args=(run,)) for run in (RUN_A, RUN_B)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=120)
        for run in (RUN_A, RUN_B):
            reply = json.loads((scratchpad / names[run]).read_text(encoding="utf-8"))
            assert reply["error"] is None, reply["error"]
            own = run_prep_slice_prefix(run, base)
            for path in _reply_paths(reply):
                assert _basename(path).startswith(own), f"{run} read {path} from {names[run]}"


def test_stored_reply_for_each_sibling_is_its_own_after_the_race(staged):
    repo = staged
    base = _base(repo)
    for _ in range(ROUNDS):
        _concurrent_preps(repo)
        for run, other in ((RUN_A, RUN_B), (RUN_B, RUN_A)):
            stored = rp.stored_reply_for_run(repo, SESSION, base, run_key=run)
            assert stored is not None
            assert _basename(stored["sidecar_path"]).startswith(run_prep_slice_prefix(run, base))
            assert run_prep_slice_prefix(other, base) not in stored["sidecar_path"]
