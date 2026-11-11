"""roadmap.blitz_land answers through a live-handoff head open that blocks."""
import time

import pytest

pytestmark = pytest.mark.complete

from coordinator_core.ops.roadmap_blitz_land import _handler
from coordinator_core.roadmap import blitz_land as bl
from coordinator_core.roadmap import gate_v2
from coordinator_core.roadmap.tests.test_gate_v2_read_head_bounded import _Block, _write

WALL = 0.3
ENGINE_BUDGET_SECS = 5.0


@pytest.fixture(autouse=True)
def _wall(monkeypatch):
    monkeypatch.setattr(gate_v2, "HEAD_READ_DEADLINE_SECS", WALL)
    monkeypatch.setattr(bl, "shipped_in_refusals", lambda root, shas: {})
    gate_v2._HEAD_CACHE.clear()
    yield
    gate_v2._HEAD_CACHE.clear()


@pytest.fixture
def root(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / "state/handoffs").mkdir(parents=True)
    return tmp_path


PARAMS = {"wave_result": {"waveIndex": 0, "ready": [], "pulled": [], "replan": [], "surfacedToPm": []}}


@pytest.mark.deliberate_wall_clock(reason="bounds the wall a blocked open may hold the landing")
def test_land_answers_from_the_stale_head_while_a_live_open_blocks(root, monkeypatch):
    _write(root, "ok")
    blocked = _write(root, "stuck")
    gate_v2.compute_gate_report(root)
    full = blocked.as_posix()
    old_key = gate_v2._HEAD_CACHE[full][0]
    _write(root, "stuck", body="a longer body so the stat key moves")
    blk = _Block(monkeypatch, full)
    try:
        t0 = time.monotonic()
        out = _handler(dict(PARAMS), repo_root=root)
        assert time.monotonic() - t0 < ENGINE_BUDGET_SECS
        assert blk.entered.is_set()
        assert set(out) >= {"approved", "refused", "minted", "pulled", "surfaced_to_pm", "next_wave"}
        assert gate_v2._HEAD_CACHE[full][0] == old_key
    finally:
        blk.release()


@pytest.mark.deliberate_wall_clock(reason="bounds the wall a blocked open may hold the landing")
def test_land_refuses_naming_the_file_when_the_blocked_open_has_no_stale_head(root, monkeypatch):
    _write(root, "ok")
    blocked = _write(root, "stuck")
    blk = _Block(monkeypatch, blocked.as_posix())
    try:
        t0 = time.monotonic()
        with pytest.raises(gate_v2.GateReadRefused) as err:
            _handler(dict(PARAMS), repo_root=root)
        assert time.monotonic() - t0 < ENGINE_BUDGET_SECS
        assert "state/handoffs/stuck.md" in str(err.value)
    finally:
        blk.release()
