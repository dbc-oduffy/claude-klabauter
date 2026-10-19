"""A `depends_on_plan` edge whose target plan and chunk are minted in the same
inventory run orders as a `depends_on` edge; an edge outside the run withholds."""

from __future__ import annotations

import textwrap

from coordinator_core.ops.dispatch_emit import inventory_mint as im

_PLAN_A = """\
---
run_id: plan-a
---

## Tasks

```yaml plan-tasks
- id: A1
  title: A step
  writes: [some/a.py]
```
"""

_PLAN_B = """\
---
run_id: plan-b
---

## Tasks

```yaml plan-tasks
- id: B1
  title: B step
  writes: [some/b.py]
  depends_on_plan:
    - plan: docs/plans/plan-a.md
      chunk: A1
      gate_kind: output-consumption-runtime
```
"""

_PLAN_B_OUT_OF_RUN = _PLAN_B.replace("plan-a.md", "plan-z.md").replace("chunk: A1", "chunk: Z1")

_PLAN_Z = _PLAN_A.replace("A1", "Z1")

_HEADER = """\
---
run_id: 20260918T000000-edge
---

## Chunk table

| id | spec path | summary | footprint | deps | verification | complexity | disposition |
|---|---|---|---|---|---|---|---|
"""


def _row(item: str, plan: str) -> str:
    return f"| {item} | `docs/plans/{plan}.md` | whole {plan} | `docs/plans/{plan}.md` | — | scoped pytest | S | in_progress |\n"


def _repo(tmp_path, plans: dict, items: list):
    (tmp_path / ".git").mkdir()
    plan_dir = tmp_path / "docs" / "plans"
    plan_dir.mkdir(parents=True)
    for name, text in plans.items():
        (plan_dir / f"{name}.md").write_text(text, encoding="utf-8")
    inv_dir = tmp_path / "state" / "mise-inventory"
    inv_dir.mkdir(parents=True)
    inv = inv_dir / "20260918T000000-edge.md"
    inv.write_text(_HEADER + "".join(_row(i, p) for i, p in items), encoding="utf-8")
    return inv


def _mint(inv, **kw):
    rows = im.parse_chunk_table(inv.read_text(encoding="utf-8"))
    return im.mint_rows(rows, inventory_path=inv, **kw)


def test_in_run_target_becomes_depends_on_edge_and_does_not_withhold(tmp_path):
    inv = _repo(tmp_path, {"plan-a": _PLAN_A, "plan-b": _PLAN_B}, [("PA", "plan-a"), ("PB", "plan-b")])
    withheld: dict = {}

    by_id = {r["id"]: r for r in _mint(inv, withheld_out=withheld)}

    assert withheld == {}
    assert set(by_id) == {"PA.A1", "PB.B1"}
    assert {"chunk": "PA.A1", "gate_kind": "output-consumption-runtime"} in by_id["PB.B1"]["depends_on"]


def test_edge_orders_when_the_dependent_item_precedes_its_target_in_the_table(tmp_path):
    inv = _repo(tmp_path, {"plan-a": _PLAN_A, "plan-b": _PLAN_B}, [("PB", "plan-b"), ("PA", "plan-a")])

    by_id = {r["id"]: r for r in _mint(inv)}

    assert [e["chunk"] for e in by_id["PB.B1"]["depends_on"]] == ["PA.A1"]


def test_edge_outside_the_run_still_withholds(tmp_path):
    inv = _repo(
        tmp_path,
        {"plan-a": _PLAN_A, "plan-b": _PLAN_B_OUT_OF_RUN, "plan-z": _PLAN_Z},
        [("PA", "plan-a"), ("PB", "plan-b")],
    )
    withheld: dict = {}

    by_id = {r["id"]: r for r in _mint(inv, withheld_out=withheld)}

    assert set(by_id) == {"PA.A1"}
    assert "PB" in withheld


def test_tranche_keeps_the_dependent_with_its_in_run_predecessor(tmp_path):
    inv = _repo(tmp_path, {"plan-a": _PLAN_A, "plan-b": _PLAN_B}, [("PB", "plan-b"), ("PA", "plan-a")])
    minted = _mint(inv)

    taken, _ = im.select_tranche(minted, ["PB", "PA"], 1)

    ids = [r["id"] for r in taken]
    assert "PB.B1" not in ids
    assert ids == ["PA.A1"]
