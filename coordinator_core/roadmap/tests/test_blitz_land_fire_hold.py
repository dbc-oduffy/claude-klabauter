"""Landing clears the plan-blitz fire hold it was the fire's job to lift, and only that hold.

Zero spawns; every case builds its corpus in `tmp_path`.
"""

from __future__ import annotations

from pathlib import Path

from coordinator_core.roadmap import blitz_land as bl

FIRE = bl.FIRE_IN_FLIGHT_HOLD_REASON
HUMAN = 'plan_blitz_hold_reason: "the PM ruled it does not fire"'


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    return tmp_path


def _baton(root: Path, stub_id: str, *extra: str, handoff_id: str | None = None) -> str:
    lines = [
        "kind: roadmap-baton", f"title: {stub_id}", f"stub_id: {stub_id}",
        "status: open", "deployment_state: ready_to_fire", "baton_role: work",
    ]
    if handoff_id:
        lines.append(f"handoff_id: {handoff_id}")
    lines += list(extra)
    p = root / "state" / "handoffs" / f"{stub_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
    return f"state/handoffs/{stub_id}.md"


def _fire_hold() -> tuple[str, str, str]:
    return (
        f'plan_blitz_hold_reason: "{FIRE}"',
        "plan_blitz_hold_cite: state/x.md",
        "plan_blitz_hold_until: 2026-10-02",
    )


def _plan(root: Path, slug: str) -> str:
    p = root / "docs" / "plans" / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"---\ntitle: {slug}\nstatus: draft\n---\n\nbody\n", encoding="utf-8")
    return f"docs/plans/{slug}.md"


def _text(root: Path, rel: str) -> str:
    return (root / rel).read_text(encoding="utf-8")


def test_a_ready_entry_naming_a_minted_hnd_id_links_and_clears_the_fire_hold(tmp_path):
    root = _repo(tmp_path)
    rel = _baton(root, "b-1", *_fire_hold(), handoff_id="hnd-spin-abc123")
    plan = _plan(root, "the-plan")

    out = bl.land_wave(
        root, {"waveIndex": 0, "ready": [{"batonId": "hnd-spin-abc123", "planPath": plan}]}
    )

    assert out["refused"] == []
    text = _text(root, rel)
    assert f"governing_plan: {plan}" in text
    assert "plan_blitz_hold" not in text


def test_a_human_hold_survives_landing_byte_identical(tmp_path):
    root = _repo(tmp_path)
    rel = _baton(root, "b-1", HUMAN, "plan_blitz_hold_cite: state/y.md")
    plan = _plan(root, "the-plan")
    before = _text(root, rel)

    bl.land_wave(root, {"waveIndex": 0, "ready": [{"batonId": "b-1", "planPath": plan}]})

    after = _text(root, rel)
    assert after == before.replace("---\n\nbody", f"governing_plan: {plan}\n---\n\nbody")


def test_relanding_is_a_no_op(tmp_path):
    root = _repo(tmp_path)
    rel = _baton(root, "b-1", *_fire_hold())
    plan = _plan(root, "the-plan")
    wave = {"waveIndex": 0, "pulled": [{"batonId": "b-1", "planPath": plan}]}

    bl.land_wave(root, wave)
    first = _text(root, rel)
    bl.land_wave(root, wave)

    assert _text(root, rel) == first
    assert "plan_blitz_hold" not in first


def test_an_already_linked_baton_still_loses_its_fire_hold(tmp_path):
    root = _repo(tmp_path)
    plan = _plan(root, "the-plan")
    rel = _baton(root, "b-1", f"governing_plan: {plan}", *_fire_hold())

    bl.land_wave(root, {"waveIndex": 0, "pulled": [{"batonId": "b-1", "planPath": plan}]})

    assert "plan_blitz_hold" not in _text(root, rel)


def test_lanes_that_link_no_plan_clear_the_fire_hold_but_not_a_human_one(tmp_path):
    root = _repo(tmp_path)
    surfaced = _baton(root, "b-surf", *_fire_hold())
    pulled = _baton(root, "b-pull", *_fire_hold())
    human = _baton(root, "b-human", HUMAN)
    human_before = _text(root, human)

    out = bl.land_wave(
        root,
        {
            "waveIndex": 0,
            "surfacedToPm": [{"batonId": "b-surf"}],
            "pulled": [{"batonId": "b-pull"}],
            "routedElsewhere": [{"batonId": "b-human"}],
        },
    )

    assert out["refused"] == []
    assert "plan_blitz_hold" not in _text(root, surfaced)
    assert "plan_blitz_hold" not in _text(root, pulled)
    assert _text(root, human) == human_before
