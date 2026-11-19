"""A land_run whose blocking step cannot finish answers parked not-started naming that step.

Each site is forced to block through the default `dispatch.land_run` product entry: the byte-gate
flock held by a second landing, the detached re-verify wait, and the reach-claims definition grep.
"""

from __future__ import annotations

import threading
import time

import pytest

from coordinator_core import ipc
from coordinator_core.git import run as git_run
from coordinator_core.ops.ceremony import detached_spawn
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import land_byte_gate, land_deadline, land_run, reach_claims_tree, run_token
from coordinator_core.ops.dispatch_emit.tests import test_land_run_product as prod
from coordinator_core.ops.dispatch_emit.tests.test_land_run_clean_overlay_verify import _dirty_land, _no_spawn
from coordinator_core.ops.dispatch_emit.tests.test_land_run_clean_overlay_verify import halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_clean_overlay_verify import _git as git_in
from coordinator_core.ops.dispatch_emit.tests.test_land_run_clean_overlay_verify import _isolated_system_temp  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import repo  # noqa: F401  (halted's repo)
from coordinator_core.ops.dispatch_emit.tests.test_land_step_deadline_not_started import (  # noqa: F401
    _fire, _head, landing,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BUDGET = ipc._timeout_for("dispatch.land_run")


@pytest.fixture
def product_repo(tmp_path, monkeypatch):
    """`test_land_run_product`'s landing repo under a name `halted`'s `repo` does not shadow."""
    return prod.repo.__wrapped__(tmp_path, monkeypatch)


def _parked(reply: dict, step: str) -> None:
    assert reply["status"] == "not-started" and reply["parked"] is True, reply
    assert reply["step"] == step and step in reply["error"], reply
    assert reply["committed"] is False and reply["sha"] is None
    assert reply["refused"] == land_deadline.REFUSED


@pytest.fixture
def other_landing(landing):  # noqa: F811
    """A second landing holding the repo's byte gate until the test releases it."""
    root, _ = landing
    held, release = threading.Event(), threading.Event()

    def hold() -> None:
        with land_byte_gate.admit(root, 30, holder_label="other-landing"):
            held.set()
            release.wait(30)

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert held.wait(10), "the second landing never took the gate"
    try:
        yield release
    finally:
        release.set()
        thread.join(10)


@pytest.mark.deliberate_wall_clock(reason="parking inside the step deadline while the gate is held is the behaviour under test")
def test_a_gate_held_by_another_landing_parks_the_landing_at_the_gate_step(landing, other_landing, monkeypatch):  # noqa: F811
    root, base = landing
    monkeypatch.setattr(land_byte_gate, "GATE_STEP_SECS", 0.6)
    before = _head(root)
    t0 = time.monotonic()

    reply = _fire(root, base)

    assert time.monotonic() - t0 < BUDGET
    assert _head(root) == before
    _parked(reply, land_byte_gate.GATE_STEP)


def test_a_parked_landing_proceeds_on_the_re_send_once_the_gate_frees(landing, other_landing, monkeypatch):  # noqa: F811
    root, base = landing
    monkeypatch.setattr(land_byte_gate, "GATE_STEP_SECS", 0.4)
    assert _fire(root, base)["status"] == "not-started"
    other_landing.set()
    time.sleep(0.5)

    reply = _fire(root, base)

    assert reply["committed"] is True and "parked" not in reply, reply


def test_an_explicit_wait_keeps_its_queued_refusal_not_a_park(landing, other_landing):  # noqa: F811
    root, _ = landing
    with pytest.raises(land_byte_gate.LandAdmissionTimeout):
        with land_byte_gate.admit(root, 0.3, holder_label="third-landing"):
            pass  # pragma: no cover


def _force_foreign_mismatch(monkeypatch, step_secs: float) -> None:
    _no_spawn(monkeypatch)
    monkeypatch.setattr(detached_spawn, "spawn_detached", lambda *a, **k: True)
    monkeypatch.setattr(cov, "REVERIFY_STEP_SECS", step_secs)


@pytest.mark.deliberate_wall_clock(reason="parking inside the step deadline while a reverify never answers is the behaviour under test")
def test_a_reverify_that_never_answers_parks_at_the_reverify_step(halted, monkeypatch):  # noqa: F811
    _force_foreign_mismatch(monkeypatch, 0.5)
    t0 = time.monotonic()

    _, reply = _dirty_land(halted, "green", mutate={"blobs": {"x": "0" * 40}})

    assert time.monotonic() - t0 < BUDGET
    _parked(reply, cov.REVERIFY_STEP)
    assert git_in(halted, "log", "-1", "--format=%s").strip() == "fixture"


def test_a_verdict_read_that_blocks_past_the_step_parks_at_the_reverify_step(halted, monkeypatch):  # noqa: F811
    _force_foreign_mismatch(monkeypatch, 0.3)
    real = cov.check_verdict
    calls = []

    def slow(root, run_id, measured, git_root=None):
        calls.append(1)
        if len(calls) > 1:
            time.sleep(0.6)
            return cov.OverlayVerdict("missing", "still unwritten")
        return real(root, run_id, measured, git_root)

    monkeypatch.setattr(cov, "check_verdict", slow)

    _, reply = _dirty_land(halted, "green", mutate={"blobs": {"x": "0" * 40}})

    _parked(reply, cov.REVERIFY_STEP)


def test_a_reverify_inside_its_step_still_lands(halted, monkeypatch):  # noqa: F811
    calls = []

    def fake(root, run, measured, stale, **_):
        calls.append(run)
        return cov.OverlayVerdict("green", "reverified", tuple(stale.seeds))

    _no_spawn(monkeypatch)
    monkeypatch.setattr(cov, "reverify_on_mismatch", fake)

    _, reply = _dirty_land(halted, "green", mutate={"blobs": {"x": "0" * 40}})

    assert reply["committed"] is True and "parked" not in reply and len(calls) == 1


_TREE = {
    "pkg/__init__.py": "",
    "pkg/lib.py": "def load_hookimpl(q):\n    return q\n",
    "pkg/loose.py": "def project_trace(q):\n    return q\n",
}


def _stage_claim(root) -> None:
    from coordinator_core.ops.dispatch_emit.ask_contract import DELIVERY_NOTE_DIR

    for rel, text in _TREE.items():
        prod._author(root, rel, text)
    prod._ask_script(root, *_TREE)
    prod._write(root, f"{DELIVERY_NOTE_DIR}/{prod.RUN}.md", "`project_trace` reaches `load_hookimpl`\n")


def _block_the_definition_grep(monkeypatch, secs: float) -> None:
    real = git_run.run_git

    def run_git(args, **kw):
        if "grep" in args and "--cached" in args:
            time.sleep(secs)
        return real(args, **kw)

    monkeypatch.setattr(git_run, "run_git", run_git)


@pytest.mark.deliberate_wall_clock(reason="owing the step inside the deadline while a git blocks is the behaviour under test")
def test_a_definition_grep_that_blocks_lands_with_the_reach_claims_step_owed(product_repo, monkeypatch):
    repo = product_repo  # noqa: F811
    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (None, None, []))
    monkeypatch.setattr(reach_claims_tree, "GREP_STEP_SECS", 0.4)
    _stage_claim(repo)
    _block_the_definition_grep(monkeypatch, 3.0)
    t0 = time.monotonic()

    reply = prod._land(repo)

    assert time.monotonic() - t0 < 2.5, "the landing waited on the blocked git instead of owing the step"
    assert "parked" not in reply and reply["committed"] is True, reply
    assert reply["reach_claims"]["owed"] == [reach_claims_tree.GREP_STEP], reply


def test_the_same_grep_inside_its_step_still_judges_the_claim(product_repo, monkeypatch):
    repo = product_repo  # noqa: F811
    monkeypatch.setattr(land_run, "_ledger_gate", lambda *a, **k: (None, None, []))
    _stage_claim(repo)

    reply = prod._land(repo)

    assert "parked" not in reply and reply["reach_claims"]["checked"] == 1, reply


def test_outside_a_land_scope_the_grep_is_not_bounded_by_a_step(monkeypatch):
    monkeypatch.setattr(reach_claims_tree, "GREP_STEP_SECS", 0.05)

    def run_git(args, **kw):
        time.sleep(0.2)
        return "late result"

    assert reach_claims_tree._run_git_in_step(run_git, ["grep"], b"") == "late result"


def test_inside_a_land_scope_a_git_error_reaches_the_caller_unchanged(monkeypatch):
    def run_git(args, **kw):
        raise OSError("git vanished")

    with land_deadline.scope():
        with pytest.raises(OSError, match="git vanished"):
            reach_claims_tree._run_git_in_step(run_git, ["grep"], b"")
