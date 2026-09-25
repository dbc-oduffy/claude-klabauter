"""
coordinator_core/roadmap/tests/test_blitz_land_reconciles_sizing.py — issue #87 item 2.

Subject: `coordinator_core.roadmap.blitz_land._reconcile_sizing_object`, wired into
`land_wave`'s `ready` lane.

The claim under test: a sizing-object minted at scout time (`XS`/`dispatch` scaffold
defaults) that a blitz-em later re-sizes at plan time must not stay stale on disk
forever — the landing that approves the plan is what brings the two back into
agreement, reading the wave's own planning-report `size:` field and the verdict
row's `route`.

Zero spawns; every case builds its corpus in `tmp_path`.
"""

from __future__ import annotations

from pathlib import Path

import yaml

from coordinator_core.roadmap import blitz_land as bl


def _repo(tmp_path: Path) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    return tmp_path


def _baton(root: Path, stub_id: str, **fields) -> str:
    lines = [
        "kind: roadmap-baton", f"title: {stub_id}", f"stub_id: {stub_id}",
        "status: open", "deployment_state: ready_to_fire", "baton_role: work",
    ]
    for k, v in fields.items():
        lines.append(f"{k}: {v}")
    p = root / "state" / "handoffs" / f"{stub_id}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
    return f"state/handoffs/{stub_id}.md"


def _sizing(root: Path, name: str, *, tshirt: str, route: str) -> str:
    body = (
        "deliverable_id: null\n"
        "schema: sizing-object\n"
        'intent: "test intent"\n'
        "appetite: small\n"
        "estimate:\n"
        f"  tshirt: {tshirt}  # trailing comment preserved\n"
        "  provisional: true\n"
        f"route: {route}\n"
        "detents: []\n"
        "fork: null\n"
        "xl_exit: null\n"
        "status: sized\n"
        "premise:\n"
        "  provenance: unrecorded\n"
    )
    p = root / "state" / "sizings" / f"{name}.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return f"state/sizings/{name}.yaml"


def _plan(root: Path, slug: str, status: str, sizing_object: str) -> str:
    lines = [
        f"title: {slug}",
        f"status: {status}",
        f"sizing_object: {sizing_object}",
    ]
    p = root / "docs" / "plans" / f"{slug}.md"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("---\n" + "\n".join(lines) + "\n---\n\nbody\n", encoding="utf-8")
    return f"docs/plans/{slug}.md"


def _planning_report(root: Path, slot: Path, baton_id: str, *, plan: str, size: str, route: str) -> None:
    slot.mkdir(parents=True, exist_ok=True)
    p = slot / f"{baton_id}.planning-report.md"
    p.write_text(
        "---\n"
        f"agent_type: planning-report\nbaton: {baton_id}\nplan: {plan}\n"
        f"size: {size}\nroute: {route}\n---\n\nbody\n",
        encoding="utf-8",
    )


def _sizing_yaml(root: Path, rel: str) -> dict:
    return yaml.safe_load((root / rel).read_text(encoding="utf-8"))


def test_a_resized_plan_reconciles_its_stale_sizing_object(tmp_path):
    """The defect: a wave authors a plan at M/plan while its sizing-object still
    reads the XS/dispatch scaffold default. Landing must rewrite the sizing-object
    to agree."""
    root = _repo(tmp_path)
    baton = _baton(root, "b-1")
    sizing = _sizing(root, "s-1", tshirt="XS", route="dispatch")
    plan = _plan(root, "the-plan", "draft", sizing_object=sizing)
    slot = root / "state" / "plan-blitz" / "trail" / "wave-0"
    _planning_report(root, slot, "b-1", plan=plan, size="M", route="plan")

    out = bl.land_wave(
        root,
        {
            "waveIndex": 0,
            "trailDir": str(slot),
            "ready": [{"batonId": "b-1", "planPath": plan, "route": "plan"}],
        },
    )

    row = out["approved"][0]
    assert row["stamped"] is True
    assert row["sizing_reconciled"] == {"sizing_path": sizing, "reconciled": True}

    after = _sizing_yaml(root, sizing)
    assert after["estimate"]["tshirt"] == "M"
    assert after["route"] == "plan"
    # Untouched fields survive the surgical rewrite.
    assert after["estimate"]["provisional"] is True
    assert after["status"] == "sized"


def test_an_already_agreeing_sizing_object_is_a_byte_identical_no_op(tmp_path):
    root = _repo(tmp_path)
    baton = _baton(root, "b-2")
    sizing = _sizing(root, "s-2", tshirt="M", route="plan")
    plan = _plan(root, "the-plan-2", "draft", sizing_object=sizing)
    slot = root / "state" / "plan-blitz" / "trail" / "wave-0"
    _planning_report(root, slot, "b-2", plan=plan, size="M", route="plan")
    before_bytes = (root / sizing).read_bytes()

    out = bl.land_wave(
        root,
        {
            "waveIndex": 0,
            "trailDir": str(slot),
            "ready": [{"batonId": "b-2", "planPath": plan, "route": "plan"}],
        },
    )

    row = out["approved"][0]
    assert row["sizing_reconciled"] == {"sizing_path": sizing, "reconciled": False}
    assert (root / sizing).read_bytes() == before_bytes


def test_no_planning_report_size_reconciles_nothing(tmp_path):
    """No `size:` on the planning report (or no report at all) is "nothing to
    reconcile", never a refusal — the ordinary approval still lands."""
    root = _repo(tmp_path)
    baton = _baton(root, "b-3")
    sizing = _sizing(root, "s-3", tshirt="XS", route="dispatch")
    plan = _plan(root, "the-plan-3", "draft", sizing_object=sizing)

    out = bl.land_wave(
        root,
        {
            "waveIndex": 0,
            "ready": [{"batonId": "b-3", "planPath": plan, "route": "plan"}],
        },
    )

    row = out["approved"][0]
    assert row["stamped"] is True
    assert "sizing_reconciled" not in row
    after = _sizing_yaml(root, sizing)
    assert after["estimate"]["tshirt"] == "XS"
