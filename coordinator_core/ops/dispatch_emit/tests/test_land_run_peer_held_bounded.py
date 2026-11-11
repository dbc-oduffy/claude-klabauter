"""_peer_held's claim lookup runs under a wall deadline well inside the 500ms bar and answers no hold on a stall."""
import time

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.session import claim_index


def test_deadline_is_well_inside_the_brightline():
    assert land_run.PEER_CLAIM_LOOKUP_DEADLINE_S <= 0.35


@pytest.mark.deliberate_wall_clock(reason="bounds a stalled lookup that must answer no-hold within its deadline")
def test_stalled_lookup_answers_no_hold_within_the_deadline(tmp_path, monkeypatch):
    monkeypatch.setattr(claim_index, "lookup", lambda *a, **k: time.sleep(3) or {})
    t0 = time.monotonic()
    held = land_run._peer_held(tmp_path, ["a.py"], "me", "run", None)
    assert held == {}
    assert time.monotonic() - t0 < land_run.PEER_CLAIM_LOOKUP_DEADLINE_S + 0.5


def test_stalled_lookup_marks_every_path_unanswerable(monkeypatch):
    monkeypatch.setattr(claim_index, "lookup", lambda *a, **k: time.sleep(3) or {})
    monkeypatch.setattr(land_run, "PEER_CLAIM_LOOKUP_DEADLINE_S", 0.05)
    got = land_run._bounded_claim_lookup(claim_index, ["a", "b"], "x")
    assert got == {"a": [claim_index.UNANSWERABLE], "b": [claim_index.UNANSWERABLE]}


def test_fast_lookup_passes_through_and_errors_propagate(monkeypatch):
    monkeypatch.setattr(claim_index, "lookup", lambda paths, cwd=None: {"a": ["s"]})
    assert land_run._bounded_claim_lookup(claim_index, ["a"], "x") == {"a": ["s"]}

    def boom(*a, **k):
        raise ValueError("bad")

    monkeypatch.setattr(claim_index, "lookup", boom)
    with pytest.raises(ValueError):
        land_run._bounded_claim_lookup(claim_index, ["a"], "x")
