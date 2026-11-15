"""A sizing read hung forever refuses within the hook budget naming its reason and next step; the hung
read's `_INFLIGHT` entry ages out after `_INFLIGHT_STALE_MS` so a fresh read starts instead of `STALLED`
at once, and a superseded read's finish never discards its successor's entry."""

from __future__ import annotations

import threading
import time

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests.test_submit_engine_ask_resume_record import (  # noqa: F401
    SIZING_REL,
    _hso,
    _payload,
    _repo,
    seams,
)

pytestmark = pytest.mark.complete


@pytest.fixture
def hung_read(tmp_path, seams, monkeypatch):
    """`load_sizing` blocks until the returned event is set; yields (root, emit, calls, release)."""
    release = threading.Event()
    calls: list = []

    def load(root, rel):
        calls.append(rel)
        release.wait(10)
        return {}

    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.sizing_fire.load_sizing", load)
    monkeypatch.setattr(mod, "_READ_DEADLINE_MS", 50.0)
    monkeypatch.setattr(mod, "_INFLIGHT", {})
    root = _repo(tmp_path)
    try:
        yield root, seams[2], calls, release
    finally:
        release.set()


def _key(root) -> tuple:
    return ("sizing", str(root), SIZING_REL)


def _age(root, seconds: float) -> None:
    """Back-date the in-flight entry of the sizing read by `seconds`."""
    started, token = mod._INFLIGHT[_key(root)]
    mod._INFLIGHT[_key(root)] = (started - seconds, token)


def _reason(env: dict) -> str:
    assert _hso(env)["permissionDecision"] == "deny", env
    return _hso(env)["permissionDecisionReason"]


def test_a_hung_sizing_read_refuses_within_the_budget_naming_reason_and_next_step(hung_read):
    root, emit, calls, _ = hung_read
    started = time.perf_counter()
    text = _reason(mod.evaluate(_payload(root), emit=emit))
    assert time.perf_counter() - started < 1.0
    assert SIZING_REL in text and "outran its 50ms budget" in text and "submit again" in text
    assert "Re-submit once" not in text and "marker is unknown" not in text


def test_a_second_submit_while_the_read_is_young_names_the_age_and_the_wait(hung_read):
    root, emit, calls, _ = hung_read
    mod.evaluate(_payload(root), emit=emit)
    started = time.perf_counter()
    text = _reason(mod.evaluate(_payload(root), emit=emit))
    assert time.perf_counter() - started < 1.0
    assert "in flight" in text and "submit again after" in text and "ms" in text
    assert "Re-submit once" not in text
    assert len(calls) == 1, "the young in-flight read is not duplicated"


def test_past_the_stale_age_a_fresh_read_starts(hung_read):
    root, emit, calls, _ = hung_read
    mod.evaluate(_payload(root), emit=emit)
    _age(root, mod._INFLIGHT_STALE_MS / 1000.0 + 1.0)
    text = _reason(mod.evaluate(_payload(root), emit=emit))
    deadline = time.monotonic() + 2.0
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert len(calls) == 2, "an entry older than _INFLIGHT_STALE_MS is superseded, not STALLED at once"
    assert "outran its 50ms budget" in text


def test_the_in_flight_refusal_wait_shrinks_as_the_entry_ages(hung_read):
    root, emit, calls, _ = hung_read
    mod.evaluate(_payload(root), emit=emit)
    _age(root, 1.0)
    text = _reason(mod.evaluate(_payload(root), emit=emit))
    wait = int(text.split("submit again after ")[1].split("ms")[0])
    assert 0 < wait <= mod._INFLIGHT_STALE_MS - 1000.0


def test_a_superseded_reads_finish_leaves_its_successors_entry():
    key = ("sizing", "r", "s")
    first = mod._claim_inflight(key)
    assert first is not None and mod._claim_inflight(key) is None
    started, token = mod._INFLIGHT[key]
    mod._INFLIGHT[key] = (started - mod._INFLIGHT_STALE_MS / 1000.0 - 1.0, token)
    second = mod._claim_inflight(key)
    assert second is not None and second is not first
    mod._release_inflight(key, first)
    assert key in mod._INFLIGHT and mod._INFLIGHT[key][1] is second
    mod._release_inflight(key, second)
    assert key not in mod._INFLIGHT


def test_the_batch_reader_honours_the_same_age_out(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.halt_resume_record import STALLED

    release = threading.Event()
    calls: list = []

    def load(root, rel):
        calls.append(rel)
        release.wait(10)
        return {"k": 1}

    monkeypatch.setattr("coordinator_core.ops.dispatch_emit.sizing_fire.load_sizing", load)
    monkeypatch.setattr(mod, "_SIZING_BATCH_DEADLINE_MS", 50.0)
    monkeypatch.setattr(mod, "_INFLIGHT", {})
    try:
        assert mod._load_sizing_docs(tmp_path, ["a.yaml"]) == {"a.yaml": STALLED}
        assert mod._load_sizing_docs(tmp_path, ["a.yaml"]) == {"a.yaml": STALLED}
        assert len(calls) == 1
        key = ("sizing", str(tmp_path), "a.yaml")
        started, token = mod._INFLIGHT[key]
        mod._INFLIGHT[key] = (started - 100.0, token)
        assert mod._load_sizing_docs(tmp_path, ["a.yaml"]) == {"a.yaml": STALLED}
        deadline = time.monotonic() + 2.0
        while len(calls) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(calls) == 2
    finally:
        release.set()
