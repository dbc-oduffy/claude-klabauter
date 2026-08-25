"""C3 (`docs/plans/2026-08-22-the-composition-gate-counts-processes-across-the-op-graph.md` §C3,
`state/dispatch-briefs/2026-08-22-the-composition-gate-counts-processes-across-the-op-graph/C3.md`)
-- AC7, AC8: the composition spawn-count instrument the plan's Measurement 1 says does not exist
yet, proven against planted fixtures rather than the live tree.

WHY THIS GATE EXISTS. `test_no_unbatched_per_item_git_spawn.py::find_unbatched_per_item_spawns` is
a per-item-LOOP collector: it scores a site zero unless a spawn sits inside a loop over a
qualifying sequence. Composition amplification -- N sequential, unrolled call sites doing the same
per-item work with no loop anywhere -- is invisible to it by construction (plan's Measurement 1,
`memo.send`: 15 spawns, p90 3461ms, no loop). This module measures that blindness directly rather
than asserting it from prose: AC7's fixture is scored by `find_unbatched_per_item_spawns` and the
result MUST be empty, or this test's own premise is wrong.

WHAT THIS MODULE COUNTS. Per-op composition cost, summed over `_reachable_functions`'
call-graph closure (`test_no_uncounted_spawn_on_budgeted_path.py`'s own transitive-BFS reachability
predicate, reused here unmodified per that plan's anti-scope -- "do not rebuild what op_census/
already has"). NOT an op-graph closure (`cartography.op_edges`): the first draft of this plan
proposed that route and the staff-eng review record
(`docs/plans/2026-08-22-the-composition-gate-counts-processes-across-the-op-graph.staff-eng-review.md`)
is why it was wrong -- `op_edges` endpoints are file paths with no op->op relation formable, and the
cost this gate needs is transitive over the CALL graph, which `_reachable_functions` already walks.

FIXTURES, NOT THE LIVE TREE. AC7/AC8 are planted, `tmp_path`-rooted fixtures -- this module makes
no claim about any real op's cost, live-registry enrolment (C1/C2a) is exercised elsewhere. A
`tmp_path` fixture satisfies `find_unbatched_per_item_spawns`'s own `roots: tuple[pathlib.Path,
...]` contract; it runs `_discover_scope_files` plus `_assert_not_self_scanned` internally, so no
extra wiring is needed to call it directly against a planted root.

NON-GATING, PENDING AC11. This module proves the instrument DISCRIMINATES -- the red fixture's
summed cost is higher than the green fixture's, and only the red fixture trips
`_FIXTURE_SPAWN_BUDGET` -- using a bare fixture-local budget picked to separate "six unrolled
spawns" from "one batched spawn", never a repo-wide, ratified process-time threshold. AC11 is the
chunk that prices a real gate against DR-344's 500ms budget; wiring THAT number in here ahead of
its own ratification would be exactly the "threshold constant chosen freehand" AC11 rules out.
`pytestmark` below keeps this demonstration out of the enforced tiers for that reason, named by the
marker itself rather than by a placeholder number that would need silent updating when AC11 lands.

Negative-spec:
    - No live-tree assertion anywhere in this module. A caller wanting real enrolled-op coverage
      wants `op_census/spawn_bearing_ops.py` (C1) or `test_no_uncounted_spawn_on_budgeted_path.py`
      (C2a), not this file.
    - `_FIXTURE_SPAWN_BUDGET` is not, and must never become, AC11's ratified threshold -- it is
      sized only to discriminate this module's own two planted fixtures.
"""

from __future__ import annotations

import pathlib

import pytest

from coordinator_core.tests.test_no_uncounted_spawn_on_budgeted_path import (
    _FileRecord,
    _build_func_index,
    _discover_scope_files as _reachability_discover_scope_files,
    _import_function_aliases,
    _import_module_aliases,
    _load_file_records,
    _local_module_attr_aliases,
    _module_index_for_test,
    _on_path_spawn_sites,
    _reachable_functions,
)
from coordinator_core.tests.test_no_unbatched_per_item_git_spawn import (
    find_unbatched_per_item_spawns,
)

#: Sized only to separate this module's own two planted fixtures (six unrolled spawns vs. one
#: batched spawn) -- see module docstring's "NON-GATING, PENDING AC11" section. Never AC11's
#: ratified process-time threshold.
_FIXTURE_SPAWN_BUDGET = 2

pytestmark = pytest.mark.pending_fix


def _composition_spawn_count(tmp_path: pathlib.Path, entry_relpath: str, entry_func: str) -> int:
    """Total spawn sites reachable from `(entry_relpath, entry_func)`'s call-graph closure,
    computed over the planted fixture rooted at `tmp_path` -- the same corpus-build steps
    `test_no_uncounted_spawn_on_budgeted_path.py`'s own planted self-tests use
    (`test_plant_multi_hop_spawn_is_flagged_red_then_removed_is_green`), reused rather than
    re-derived (plan anti-scope: "do not rebuild what op_census/ already has")."""
    files = _reachability_discover_scope_files((tmp_path,))
    records: list[_FileRecord] = _load_file_records(files)
    index = _build_func_index(records)
    module_index = _module_index_for_test(records)
    import_aliases_by_file = {r.relpath: _import_module_aliases(r, module_index) for r in records}
    func_aliases_by_file = {
        r.relpath: _import_function_aliases(r, module_index, index.func_defs) for r in records
    }
    local_aliases_by_file = {
        r.relpath: _local_module_attr_aliases(r, import_aliases_by_file[r.relpath], index.func_defs)
        for r in records
    }
    spawn_sites_by_file = {r.relpath: r.spawn_sites for r in records}

    reached = _reachable_functions(
        {(entry_relpath, entry_func)},
        index,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    )
    sites = _on_path_spawn_sites(reached, spawn_sites_by_file, set())
    return len(sites)


def _plant_loop_free_amplification(tmp_path: pathlib.Path) -> None:
    """RED fixture: six sequential, unrolled `subprocess.run` call sites in one function, doing
    the same per-item work `memo.send`'s six-spawn composition does (plan Measurement 1) --
    with NO loop anywhere in the file. AC7's premise."""
    entry_mod = tmp_path / "entry.py"
    entry_mod.write_text(
        "import subprocess\n"
        "\n"
        "def _dispatch_all():\n"
        "    subprocess.run(['git', 'status'])\n"
        "    subprocess.run(['git', 'status'])\n"
        "    subprocess.run(['git', 'status'])\n"
        "    subprocess.run(['git', 'status'])\n"
        "    subprocess.run(['git', 'status'])\n"
        "    subprocess.run(['git', 'status'])\n"
        "    return None\n",
        encoding="utf-8",
    )


def _plant_batched_form(tmp_path: pathlib.Path) -> None:
    """GREEN fixture: AC8's negative control -- identical work, one batched invocation instead
    of six unrolled ones."""
    entry_mod = tmp_path / "entry.py"
    entry_mod.write_text(
        "import subprocess\n"
        "\n"
        "def _dispatch_all():\n"
        "    subprocess.run(['git', 'status', '--porcelain=v2'])\n"
        "    return None\n",
        encoding="utf-8",
    )


def test_loop_free_amplification_exceeds_budget_and_the_loop_gate_is_blind_to_it(tmp_path):
    """AC7. The red fixture's summed composition cost exceeds `_FIXTURE_SPAWN_BUDGET` with no
    loop anywhere in `entry.py` -- and `find_unbatched_per_item_spawns`, the shipped per-item-loop
    gate, returns EMPTY for the same fixture. That single assertion is what converts "the shipped
    gate is blind to this" from the plan's claim into a measurement."""
    _plant_loop_free_amplification(tmp_path)

    cost = _composition_spawn_count(tmp_path, "entry.py", "_dispatch_all")
    assert cost > _FIXTURE_SPAWN_BUDGET, (
        f"composition cost {cost} did not exceed the fixture budget "
        f"{_FIXTURE_SPAWN_BUDGET} -- the red fixture is not amplified enough to prove the gate"
    )

    loop_violations = find_unbatched_per_item_spawns((tmp_path,))
    assert loop_violations == [], (
        "find_unbatched_per_item_spawns flagged the loop-free amplification fixture -- it is a "
        "per-item-loop collector and should have scored this composition zero by construction; "
        "a non-empty result here means the fixture accidentally planted a loop and no longer "
        "measures the blindness this test exists to prove"
    )


def test_batched_form_clears_the_same_budget(tmp_path):
    """AC8. The negative control: identical work, batched into one invocation, clears
    `_FIXTURE_SPAWN_BUDGET` -- proving the instrument discriminates rather than merely firing on
    any spawn-bearing fixture."""
    _plant_batched_form(tmp_path)

    cost = _composition_spawn_count(tmp_path, "entry.py", "_dispatch_all")
    assert cost <= _FIXTURE_SPAWN_BUDGET, (
        f"composition cost {cost} exceeded the fixture budget {_FIXTURE_SPAWN_BUDGET} for the "
        "batched form -- the negative control should clear it"
    )

    loop_violations = find_unbatched_per_item_spawns((tmp_path,))
    assert loop_violations == []
