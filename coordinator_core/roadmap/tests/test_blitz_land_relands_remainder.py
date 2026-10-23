"""A retried 7-plan fire lands only the plans still unlanded and never rewrites the landed ones."""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap import blitz_land as bl
from coordinator_core.roadmap.tests._multi_plan_wave_fixture import build_wave, install_fixture_gate
from coordinator_core.roadmap.tests.test_blitz_land import _status

N = 7


def _tree(root: Path) -> dict:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for sub in ("docs/plans", "state/handoffs")
        for p in sorted((root / sub).rglob("*.md"))
    }


def test_relanding_with_six_of_seven_landed_lands_only_the_seventh(tmp_path, monkeypatch):
    install_fixture_gate(monkeypatch)
    # The fixture's `landed` plans carry no approval stamp, so land the first six for real:
    # that is the state a timed-out fire leaves behind.
    root, wave = build_wave(tmp_path, n=N)
    bl.land_wave(root, {"waveIndex": 0, "ready": wave["ready"][:6]})
    before = _tree(root)

    out = bl.land_wave(root, wave)

    assert out["refused"] == []
    rows = {r["plan"]: r for r in out["approved"]}
    landed_paths = [e["planPath"] for e in wave["ready"][:6]]
    seventh = wave["ready"][6]["planPath"]
    assert len(rows) == N
    for path in landed_paths:
        assert rows[path]["already_in_state"] is True
        assert (root / path).read_bytes() == before[path]
    assert rows[seventh]["already_in_state"] is False
    assert _status(root, seventh) == "approved"
    baton_text = (root / "state/handoffs/w-6.md").read_text(encoding="utf-8")
    plan_text = (root / seventh).read_text(encoding="utf-8")
    assert "deliverable_id: dlv-w-6" in baton_text
    assert "deliverable_id: dlv-w-6" in plan_text


def test_landing_the_same_fire_twice_equals_landing_it_once(tmp_path, monkeypatch):
    install_fixture_gate(monkeypatch)
    for name in ("once", "twice"):
        (tmp_path / name).mkdir()
    root_once, wave_once = build_wave(tmp_path / "once", n=N)
    root_twice, wave_twice = build_wave(tmp_path / "twice", n=N)

    bl.land_wave(root_once, wave_once)
    bl.land_wave(root_twice, wave_twice)
    second = bl.land_wave(root_twice, wave_twice)

    assert second["refused"] == []
    assert all(r["already_in_state"] for r in second["approved"])
    assert _tree(root_twice) == _tree(root_once)
