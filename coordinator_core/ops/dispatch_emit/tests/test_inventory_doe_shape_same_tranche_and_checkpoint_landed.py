"""The DoE inventory shape: a `routed-out: next tranche` dependent orders after its
in-run predecessor in one tranche, and a chunk a family checkpoint landed is not minted."""

from __future__ import annotations

import warnings

import pytest

from coordinator_core.ops.dispatch_emit import inventory_mint as im
from coordinator_core.ops.dispatch_emit.tests.test_checkpoint_rows_landed import _commit, _cp, _git

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

BASE = "20261021T182452-4b88be1d"
DIR = "state/mise-inventory"

_PLAN_I10 = """\
---
run_id: plan-i10
---

## Tasks

```yaml plan-tasks
- id: C2
  title: Landed by a checkpoint, never flipped
  writes: [src/i10_c2.py]
- id: C4
  title: Open chunk the dependent waits on
  writes: [src/i10_c4.py]
  depends_on:
    - chunk: C2
      gate_kind: output-consumption-runtime
```
"""

_PLAN_I12 = """\
---
run_id: plan-i12
---

## Tasks

```yaml plan-tasks
- id: S1
  title: Waits on I10.C4
  writes: [src/i12.py]
  depends_on_plan:
    - plan: docs/plans/plan-i10.md
      chunk: C4
      gate_kind: output-consumption-runtime
```
"""

_PLAN_I13 = """\
---
run_id: plan-i13
---

## Tasks

```yaml plan-tasks
- id: T1
  title: Waits on I12
  writes: [src/i13.py]
```
"""

_PLAN_I20 = _PLAN_I13.replace("I13", "I20").replace("T1", "X1").replace("i13", "i20")

_HEADER = f"""\
---
run_id: {BASE}
start_sha: {{start}}
---

## Chunk table

| id | spec path | summary | footprint | deps | verification | complexity | disposition |
|---|---|---|---|---|---|---|---|
"""


def _tr(item, plan, deps, disposition):
    return (
        f"| {item} | `docs/plans/{plan}.md` | whole {plan} | `docs/plans/{plan}.md` | {deps} "
        f"| scoped pytest | S | {disposition} |\n"
    )


@pytest.fixture
def inv(tmp_path):
    _git(tmp_path, "init", "-q")
    plans = tmp_path / "docs" / "plans"
    plans.mkdir(parents=True)
    for name, text in (
        ("plan-i10", _PLAN_I10),
        ("plan-i12", _PLAN_I12),
        ("plan-i13", _PLAN_I13),
        ("plan-i20", _PLAN_I20),
    ):
        (plans / f"{name}.md").write_text(text, encoding="utf-8")
    start = _commit(tmp_path, "seed")
    _cp(
        tmp_path,
        ["I10.C2"],
        plan=f"{DIR}/{BASE}-t2-p2.spine.md",
        base=start,
    )
    inv_dir = tmp_path / DIR
    inv_dir.mkdir(parents=True)
    path = inv_dir / f"{BASE}.md"
    path.write_text(
        _HEADER.format(start=start)
        + _tr("I10", "plan-i10", "—", "pending")
        + _tr("I12", "plan-i12", "—", "routed-out: next tranche, waits on uncoded I10.C4")
        + _tr("I13", "plan-i13", "I12", "routed-out: next tranche, waits on I12")
        + _tr("I20", "plan-i20", "—", "routed-out: writes outside this repo"),
        encoding="utf-8",
    )
    return path


def _mint(path, **kw):
    rows = im.parse_chunk_table(path.read_text(encoding="utf-8"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return im.mint_rows(rows, inventory_path=path, **kw)


def _wave(by_id, rid, memo=None):
    memo = {} if memo is None else memo
    if rid not in memo:
        deps = [e["chunk"] for e in by_id[rid].get("depends_on", []) if e["chunk"] in by_id]
        memo[rid] = 1 + max((_wave(by_id, d, memo) for d in deps), default=-1)
    return memo[rid]


def test_dependent_orders_after_predecessor_and_landed_chunk_is_not_minted(inv):
    accounting: dict = {}
    landed_chunks: dict = {}
    withheld: dict = {}
    by_id = {
        r["id"]: r
        for r in _mint(inv, accounting_out=accounting, landed_chunks_out=landed_chunks, withheld_out=withheld)
    }

    assert "I10.C2" not in by_id
    assert landed_chunks["I10.C2"].startswith("checkpoint-landed")
    assert {"I10.C4", "I12.S1", "I13.T1"} <= set(by_id)
    assert _wave(by_id, "I12.S1") > _wave(by_id, "I10.C4")
    assert _wave(by_id, "I13.T1") > _wave(by_id, "I12.S1")
    assert withheld == {}
    for item in ("I12", "I13"):
        assert item not in accounting


def test_cross_repo_routed_out_row_stays_out(inv):
    accounting: dict = {}
    by_id = {r["id"]: r for r in _mint(inv, accounting_out=accounting)}

    assert not any(i.startswith("I20") for i in by_id)
    assert "I20" in accounting


def test_one_tranche_holds_the_dependent_with_its_predecessor(inv):
    bound: dict = {}
    minted = _mint(inv, bound_out=bound)
    taken, report = im.select_tranche(minted, ["I10", "I12", "I13"], 100, bound=bound)

    ids = {r["id"] for r in taken}
    assert {"I10.C4", "I12.S1", "I13.T1"} <= ids
    assert not report["deferred"]


def test_no_head_means_no_landed_map(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / DIR).mkdir(parents=True)
    path = tmp_path / DIR / f"{BASE}.md"
    path.write_text(_HEADER.format(start="deadbeef") + _tr("I10", "plan-i10", "—", "pending"), encoding="utf-8")
    rows = im.parse_chunk_table(path.read_text(encoding="utf-8"))
    assert im._family_landed_map(rows, path) == {}
