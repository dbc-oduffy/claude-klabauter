"""linked_handoffs: live handoffs linking a plan whose every linked plan is implemented."""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

from coordinator_core.ops.dispatch_emit.plan_linked_handoffs import (
    linked_handoffs,
    links_and_all_implemented,
)

P = "docs/plans/2026-10-29-p.md"
Q = "docs/plans/2026-10-29-q.md"


def _plan(root: Path, rel: str, status: str, plan_id: str) -> None:
    f = root / rel
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"---\nstatus: {status}\nplan_id: {plan_id}\n---\n# plan\n", encoding="utf-8")


def _handoff(root: Path, name: str, body: str) -> str:
    f = root / "state" / "handoffs" / name
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(f"---\n{body}\n---\nbody\n", encoding="utf-8")
    return f"state/handoffs/{name}"


def test_links_via_governing_plan_when_implemented(tmp_path):
    _plan(tmp_path, P, "implemented", "pln-p")
    h = _handoff(tmp_path, "a.md", f"deployment_state: ready_to_fire\ngoverning_plan: {P}")
    assert linked_handoffs(tmp_path, P) == [h]


def test_unimplemented_plan_means_not_listed(tmp_path):
    _plan(tmp_path, P, "executing", "pln-p")
    _handoff(tmp_path, "a.md", f"deployment_state: ready_to_fire\ngoverning_plan: {P}")
    assert linked_handoffs(tmp_path, P) == []


def test_terminal_handoff_is_skipped(tmp_path):
    _plan(tmp_path, P, "implemented", "pln-p")
    for i, st in enumerate(["shipped", "continued", "closed"]):
        _handoff(tmp_path, f"t{i}.md", f"deployment_state: {st}\ngoverning_plan: {P}")
    assert linked_handoffs(tmp_path, P) == []


def test_every_linked_plan_must_be_implemented(tmp_path):
    _plan(tmp_path, P, "implemented", "pln-p")
    _plan(tmp_path, Q, "executing", "pln-q")
    _handoff(
        tmp_path, "a.md",
        f"deployment_state: ready_to_fire\ngoverning_plan: {P}\nplan_ids: [pln-p, pln-q]",
    )
    assert linked_handoffs(tmp_path, P) == []
    _plan(tmp_path, Q, "implemented", "pln-q")
    assert linked_handoffs(tmp_path, P) == ["state/handoffs/a.md"]


def test_unresolvable_plan_id_means_not_shipped(tmp_path):
    _plan(tmp_path, P, "implemented", "pln-p")
    _handoff(tmp_path, "a.md", "deployment_state: ready_to_fire\nplan_ids: [pln-p, pln-ghost]")
    assert linked_handoffs(tmp_path, P) == []


def test_plan_ids_only_link_and_archived_plan_counts(tmp_path):
    arch = "archive/specs/2026-10/2026-10-29-p.md"
    _plan(tmp_path, arch, "implemented", "pln-p")
    h = _handoff(tmp_path, "a.md", "deployment_state: ready_to_fire\nplan_ids: [pln-p]")
    assert linked_handoffs(tmp_path, P) == [h]


def test_governing_plan_archived_path_matches_origin(tmp_path):
    _plan(tmp_path, "archive/specs/2026-10/2026-10-29-p.md", "implemented", "pln-p")
    h = _handoff(
        tmp_path, "a.md",
        "deployment_state: ready_to_fire\ngoverning_plan: archive/specs/2026-10/2026-10-29-p.md",
    )
    assert linked_handoffs(tmp_path, P) == [h]


def test_predicate_matches_enumeration(tmp_path):
    _plan(tmp_path, P, "implemented", "pln-p")
    fm = {"deployment_state": "ready_to_fire", "governing_plan": P}
    assert links_and_all_implemented(fm, P, tmp_path)
    assert not links_and_all_implemented({**fm, "deployment_state": "closed"}, P, tmp_path)
    assert not links_and_all_implemented({"deployment_state": "ready_to_fire"}, P, tmp_path)


def test_cost_over_1100_records_zero_spawn(tmp_path, monkeypatch):
    _plan(tmp_path, P, "implemented", "pln-p")
    for i in range(1100):
        _handoff(tmp_path, f"h{i}.md", f"deployment_state: closed\ngoverning_plan: docs/plans/other-{i}.md\ntitle: x")
    for i in range(3):
        _handoff(tmp_path, f"live{i}.md", f"deployment_state: ready_to_fire\ngoverning_plan: {P}")
    spawns: list = []
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawns.append(a))
    t0 = time.process_time()
    got = linked_handoffs(tmp_path, P)
    elapsed = time.process_time() - t0
    assert len(got) == 3
    assert spawns == []
    assert elapsed < 0.1, elapsed


def test_frontmatter_ends_at_full_delimiter_line():
    from coordinator_core.ops.dispatch_emit.plan_linked_handoffs import _parse_head

    text = "---\nnote: |\n  a\n  ---not-a-delimiter\nstatus: live\n---\nbody\n"
    assert _parse_head(text).get("status") == "live"
    assert _parse_head("---\r\nstatus: x\r\n---\r\nbody").get("status") == "x"
