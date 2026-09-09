"""coordinator_core/ops/tests/test_roadmap_blitz_land.py — the op's parameter surface.

Subject: `coordinator_core.ops.roadmap_blitz_land`, the JSON-RPC wrapper over
`coordinator_core.roadmap.blitz_land.land_wave`.

The claim under test: every documented wire parameter reaches `land_wave`. That is the whole
job of a thin RPC wrapper, and it is exactly the thing nothing checked — `shipped_in` was
accepted by `land_wave`, required by `close_dispatched`, supplied by the caller, and dropped
here, so every XS baton landed through this op was refused for want of a SHA that had been
passed. The refusal message named the missing SHA, which sent the caller to re-commit rather
than to the wrapper.

`land_wave` itself is tested in `coordinator_core/roadmap/tests/test_blitz_land.py`; these
cases stub it deliberately, because what is under test is the handover and not the landing.
"""

from __future__ import annotations

import asyncio

import pytest

from coordinator_core.ops import roadmap_blitz_land as mod

_EMPTY_LANDING = {
    "approved": [],
    "refused": [],
    "minted": [],
    "pulled": [],
    "surfaced_to_pm": [],
    "next_wave": {"waveIndex": 1, "batons": []},
}


@pytest.fixture
def repo(tmp_path):
    """A worktree root `main_worktree_root` will accept. The handler resolves it before it
    reaches `land_wave`, so a bare tmp_path fails there rather than in what is under test."""
    (tmp_path / ".git").mkdir()
    return tmp_path


def _capturing_land_wave(seen: dict):
    def _fake(root, wave_result, **kwargs):
        seen.update(kwargs)
        return dict(_EMPTY_LANDING)

    return _fake


def test_shipped_in_reaches_land_wave(monkeypatch, repo):
    """The op forwarded `branch` and `limit` and dropped `shipped_in` on the floor.

    That is the recycling defect the skill names: the baton stays open, comes back as a
    candidate in every later wave, and the wave still reports the work as done. Observed on
    example-retrieval-repo's wave 0, where three XS batons were refused with the SHA in the request.
    """
    seen: dict = {}
    monkeypatch.setattr(mod, "land_wave", _capturing_land_wave(seen))

    asyncio.run(
        mod._handler(
            {"wave_result": {"waveIndex": 0}, "shipped_in": "d434d54fb"},
            repo_root=repo,
        )
    )

    assert seen.get("shipped_in") == "d434d54fb"


def test_shipped_in_is_optional_and_absent_stays_none(monkeypatch, repo):
    """A wave that dispatched no XS has no SHA to give, and must not be made to invent one.

    Defaulting to a placeholder here would stamp `shipped` against a commit nobody made, which
    is worse than the refusal: it reads as a landed baton.
    """
    seen: dict = {}
    monkeypatch.setattr(mod, "land_wave", _capturing_land_wave(seen))

    asyncio.run(mod._handler({"wave_result": {"waveIndex": 0}}, repo_root=repo))

    assert seen.get("shipped_in") is None


def test_an_empty_shipped_in_is_refused_at_the_wire(monkeypatch, repo):
    """Whitespace is the shape that would reach `close_dispatched` and fail its own regex,
    reported there as a missing SHA rather than as the malformed parameter it is."""
    monkeypatch.setattr(mod, "land_wave", _capturing_land_wave({}))

    with pytest.raises(ValueError, match="shipped_in"):
        asyncio.run(
            mod._handler(
                {"wave_result": {"waveIndex": 0}, "shipped_in": "  "},
                repo_root=repo,
            )
        )


def test_branch_and_limit_still_reach_land_wave(monkeypatch, repo):
    """The two parameters that always worked, pinned alongside the one that did not — the
    point of the module is that the set is complete, not that one member of it arrives."""
    seen: dict = {}
    monkeypatch.setattr(mod, "land_wave", _capturing_land_wave(seen))

    asyncio.run(
        mod._handler(
            {"wave_result": {"waveIndex": 0}, "branch": "work/x", "limit": 3},
            repo_root=repo,
        )
    )

    assert seen.get("branch") == "work/x"
    assert seen.get("limit") == 3


def test_every_documented_wire_parameter_is_read_by_the_handler():
    """The docstring is the wire contract, and a parameter documented but unread is how this
    defect happened. Derived from the module docstring rather than restated, so a new
    parameter documented and not wired fails here instead of in a live wave."""
    doc = mod.__doc__ or ""
    wire = doc.split("Wire params:", 1)[1].split("Reply fields:", 1)[0]
    documented = {
        line.strip().split()[0]
        for line in wire.splitlines()
        if line.strip() and not line.startswith(" " * 20)
    }
    handler_source = mod._handler.__code__.co_consts
    read = {c for c in handler_source if isinstance(c, str)}
    missing = {name for name in documented if name not in read}
    assert not missing, f"documented but never read from params: {sorted(missing)}"
