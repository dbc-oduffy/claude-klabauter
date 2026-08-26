"""Deep (past-one-hop) reachability collector: sibling to `test_no_unbatched_per_item_git_spawn`
(the gate module), reusing its `_FuncIndex` and `find_unbatched_per_item_spawns` seam rather
than re-deriving any of the discriminator or spawn-detection logic.

Spec backlink: `docs/plans/2026-08-25-a-collector-that-sees-past-one-hop.md`, chunk C1.

WHY THIS IS A SEPARATE MODULE, NOT AN EDIT TO THE GATE. The gate's own collector is
one-hop-only by design (`find_unbatched_per_item_spawns`'s module docstring: "restricted to
the high-precision stratum"). This module builds a call-graph CLOSURE over the gate's own
`_FuncIndex` and widens `direct_spawn_funcs` / `same_module_direct_spawn` to every function
within `max_depth` hops of a spawn, then hands the widened index back to the gate's own
`find_unbatched_per_item_spawns` via its `index_transform` seam -- the collector itself stays
UNMODIFIED and unaware that widening happened. There is exactly one parse: `index_transform`
runs against `find_unbatched_per_item_spawns`'s own single-parse build over `roots`, so
`_call_graph`/`_depths`/`widened_index` all derive from that same `base` argument with nothing
threaded in from outside. This is deliberate over one-hop discovery, not a bug: a two-hop
report is only as trustworthy as the routes it still passes through afterward (see
`_call_graph`'s docstring for the bounded over-approximation this implies).

ANTI-SCOPE. No repo-wide bare-name fallback: `_call_graph` only adds a cross-module edge for a
callee present in the CALLING file's `from X import` names. A same-named, unimported function
defined elsewhere never gets an edge from this closure. (The closure's own scoping is not the
whole story downstream -- see `_call_graph`'s docstring, F9 note, for the bounded precision leak
`direct_spawn_funcs`'s bare-name keying reopens at the per-route lookup, after this closure has
decided which functions to add.)

THE COST-RANKED ADVISORY WORKLIST (C4, AC2/AC7/AC10) -- `_advisory_worklist` below, published
by `test_deep_per_item_spawn_advisory_worklist`. Sibling in shape to the gate's own
`designed_red` burn-down worklist (`test_burn_down_known_preexisting_amplification_sites`): it
reports, it never gates, and its failure output is not a red build -- there is no failure output,
by construction (AC7's own "the run emits ... it does not fail").

RANKED BY MEASURED COST, HIGHEST FIRST -- NEVER BY DEPTH. Depth 2 carries the most sites (53)
and the worst precision of the three strata below; a depth-ordered list would put the least
trustworthy stratum on top and train a reader to skip it. Cost is sourced from this module's own
small, honestly-sparse `_KNOWN_SITE_COST_MS` table (seeded from measurements already on record
elsewhere in this repo -- `docs/reference/...` / `CLAUDE.md`'s own cited per-verb costs) rather
than measured per-site here (a live per-site measurement is exactly the O(N) subprocess cost this
plan's own sibling gate exists to forbid). A site with no recorded cost sorts BELOW every site
that has one, and the row says so (`cost_ms: None`) rather than imputing a number. Every row
carries its OWN depth alongside its cost (Review: staff-eng -- F14): because the list is
cost-ranked, not depth-ranked, a reader cannot otherwise tell which published per-depth precision
figure below applies to the row in front of them.

PUBLISHED PRECISION, PER DEPTH, WITH n AND METHOD (Review: staff-eng -- F11, F12, and an EM
correction: the figures below were re-scored after F1 established that a hop memoized on a
PER-ITEM key does not collapse N spawns to one -- four sites originally scored false-positive on
that now-rejected rationale are true positives, so the depth-3/4 and combined figures moved; no
depth-2 site was judged on the memoization rationale, so depth 2 is unaffected). Measured in the
spike this session over 36 hand-judged sites, 12 per stratum, corrected sites: `coverage.py ::
_reviewed_via_graph_walk -> _resolve_endpoint` (depth 3), `handoff_reconcile.py ::
_ancestor_liveness_blocked -> resolve_target` (depth 3), `baton_drift_sweep.py ::
baton_drift_sweep -> _retained_supersede_eligibility` (depth 4), `ceremony/renderers.py ::
_join_plans_to_handoffs -> _resolve_candidate_path` (depth 4):

    depth 2 -- 33% (4/12), 53 new sites
    depth 3 -- 75% (9/12), 39 new sites
    depth 4 -- 83% (10/12), 23 new sites
    combined depths 2-4 -- 64% (23/36); 141 total sites at depth 4, from 26 at depth 1

Point estimates 33%/75%/83% at depths 2/3/4, n=12 per stratum. Intervals are wide (roughly
14-61% at depth 2 by the Wilson method) and overlap across strata at this sample size: this
sample can neither establish a floor nor cleanly distinguish the depths. Read the figures as an
order-of-magnitude sanity check on an advisory worklist, not as a precision guarantee. Do NOT
publish "the defensible claim is the floor (>=X% at every depth)" -- the minimum of three point
estimates is not a floor a 12-per-stratum sample supports, and asserting one is the same class of
overclaim this plan's own Anti-scope forbids under "Do not inherit precision figures."

SUPERSEDED 2026-08-26, not merely re-sample-pending: every edge in this sample was resolved by
route c's pre-fix alias-blind rule (`pln-route-c-resolves-the-imported-name-not-the-local-alias`),
confirmed to admit false edges on aliased imports, so these figures are an upper bound on the
fixed instrument's true precision rather than a measured property of it. No revised figure is
asserted here. The re-sample -- >=30 sites drawn at random from the post-fix worklist, two
independent judges, disagreement rate published -- is tracked at
`state/improvement-queue/2026-08-26-re-sample-the-deep-collector-precision-post-seam-fix.yaml`.

METHOD, not just n: the sample was judged by a SINGLE UNBLINDED JUDGE who authored the
instrument being evaluated (this session, 2026-08-25), against the rubric "a site is a true
positive when the flagged per-item call reaches a real, unbatched spawn through routes a-g,
confirmed by reading the call chain at every hop" -- the same TP rubric the gate module's own
`_KNOWN_SITES` dispositions use. Publishing at all is defensible for an ADVISORY worklist -- the
cost of a wrong figure here is a human dismissing a row, not a red build -- but publishing
without the judge count and blinding status invites a future reader to cite these numbers as a
measured property of the instrument. A re-sampling protocol is owed as a follow-on, NOT in this
plan: >=30 sites drawn at random from the published worklist, two independent judges,
disagreement rate published, these figures superseded on completion. Naming that follow-on here
is what keeps the current figures provisional in the record rather than in someone's memory.

REACH-QUALITY ANNOTATION, NEVER SUPPRESSION (AC10). `cache_in_front` and `terminating_path`
below are the two real quality signals a deleted chunk C3 tried to turn into a SUPPRESSING
discriminator (PM decision 2026-08-25, after its premise was refuted and two reformulations were
measured empty over the 116-site deep stratum -- see each helper's own docstring for the
refutation this module inherits rather than re-derives). Both are emitted as COLUMNS beside a
row's cost and depth, never as a filter: nothing is ever dropped from the worklist on their
account, which is exactly the inversion hazard that killed C3. Because these only annotate, they
CANNOT move the published precision figures above -- those describe which REPORTED rows are true
positives, and annotation changes no row's presence in the report.

PROCESS TIME, AND THE TIER PLACEMENT IT DERIVES (AC5, DR-344's brightline). Re-measured on
this finished instrument via `time.process_time()`, not restated from the earlier spike:
discover + read + parse (1596 files) ~11.2s, `_FuncIndex` build ~6.5s, the NEW call-graph +
depth closure ~0.9s, and the corpus-walk collect itself ~48-61s depending on which collector
runs (one-hop or depth-4 widened) -- so every test in this module that touches
`_gate_scope_paths()` (the real corpus, not a `tmp_path` fixture) runs somewhere in the tens
of seconds, dominated by the collect step both this module's tests and the STANDING one-hop
gate already pay. That is roughly two orders of magnitude over DR-344's 500ms brightline --
never a hot-path instrument, by construction, and every one of those tests below carries
`@pytest.mark.cadence` for exactly that reason: this suite runs at cadence gates, not
per-commit. This is not a defect being excused -- it is an offline advisory worklist and a
pair of standing multi-hop reproducers riding a corpus walk the one-hop gate already pays
separately, and the module says so here rather than leaving a future reader to find the
number unexplained and file it as a brightline violation.
"""

import ast
import copy
import json
import re
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.tests.test_no_unbatched_per_item_git_spawn import (
    AmpSite,
    _FuncIndex,
    _build_func_index,
    _call_callee_name,
    _compute_spawn_bearing_params,
    _discover_scope_files,
    _gate_scope_paths,
    _import_resolves_to,
    _load_file_records,
    _REPO_ROOT,
    find_unbatched_per_item_spawns,
)


def _call_graph(
    index: _FuncIndex,
) -> dict[tuple[str, str], set[tuple[str, str]]]:
    """Every top-level function's outgoing call edges, resolved same-module-first then via the
    calling file's `from X import` names, narrowed through the shared route-c match helper
    `_import_resolves_to` (the ORIGINAL imported name and its resolved SOURCE MODULE, not the
    local binding alone) -- the same helper `_resolve_callee_def`/`_is_direct_spawner_name` call
    in the gate module, so this closure does not carry a second copy of route c's resolution
    rule. Still ALL CANDIDATES for an imported name resolving to more than one same-named,
    same-module-matched definition elsewhere, never a single "the" resolution.

    This is a bounded over-approximation exactly where route g's own resolution is: an imported
    name that resolves (module AND name) to several same-named functions across the corpus gets
    an edge to every one of them, not just the "right" one. NO repo-wide bare-name fallback -- a
    same-named, unimported function elsewhere never gets an edge (see module docstring's
    Anti-scope paragraph)."""
    edges: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for (relpath, func_name), fn in index.func_defs.items():
        caller = (relpath, func_name)
        out_edges = edges.setdefault(caller, set())
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            callee = _call_callee_name(node)
            if callee is None:
                continue
            if (relpath, callee) in index.func_defs:
                out_edges.add((relpath, callee))
                continue
            if callee in index.imported_names_by_file.get(relpath, set()):
                for candidate in index.funcs_by_name.get(callee, []):
                    if candidate[0] != relpath and _import_resolves_to(
                        index, relpath, callee, candidate[0], candidate[1]
                    ):
                        out_edges.add(candidate)
    return edges


def _depths(
    index: _FuncIndex,
    edges: dict[tuple[str, str], set[tuple[str, str]]],
    max_depth: int,
) -> dict[tuple[str, str], int]:
    """Backwards BFS from every same-module direct spawner (depth 1), over the REVERSED edge
    set, bounded by `max_depth`. Terminates trivially: the visited set grows monotonically over
    a finite domain (the scoped corpus's own `(relpath, func)` pairs) and the hop counter is
    bounded by `max_depth`."""
    reverse: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for caller, callees in edges.items():
        for callee in callees:
            reverse.setdefault(callee, set()).add(caller)

    depths: dict[tuple[str, str], int] = {}
    queue: deque[tuple[str, str]] = deque()
    for spawner in index.same_module_direct_spawn:
        if spawner not in depths:
            depths[spawner] = 1
            queue.append(spawner)

    while queue:
        current = queue.popleft()
        current_depth = depths[current]
        if current_depth >= max_depth:
            continue
        for caller in reverse.get(current, ()):
            if caller not in depths:
                depths[caller] = current_depth + 1
                queue.append(caller)

    return depths


def widened_index(
    base: _FuncIndex,
    depths: dict[tuple[str, str], int],
    max_depth: int,
) -> _FuncIndex:
    """A SHALLOW COPY of `base` with `direct_spawn_funcs` and `same_module_direct_spawn`
    extended by every `(relpath, func)` whose depth is `<= max_depth`, then
    `spawn_bearing_params` RECOMPUTED over the widened index. `base` is shared with the
    standing gate and comes back untouched: the two widened dicts are copied before mutation,
    never assigned back onto `base`'s own dicts.

    `direct_spawn_funcs` (`dict[str, list[tuple[str, str]]]`) is APPENDED to, and only when the
    `(relpath, func)` entry is not already present -- `find_unbatched_per_item_spawns` iterates
    this list with an any()-style membership check, so a duplicate is not a correctness bug but
    it obscures the diff a future reader takes against `base`. `same_module_direct_spawn`
    (`dict[tuple[str, str], bool]`) is ASSIGNED, keyed by the `(relpath, func)` pair."""
    widened = copy.copy(base)
    widened.direct_spawn_funcs = {
        name: list(entries) for name, entries in base.direct_spawn_funcs.items()
    }
    widened.same_module_direct_spawn = dict(base.same_module_direct_spawn)

    for (relpath, func_name), depth in depths.items():
        if depth > max_depth:
            continue
        entry = (relpath, func_name)
        existing = widened.direct_spawn_funcs.setdefault(func_name, [])
        if entry not in existing:
            existing.append(entry)
        widened.same_module_direct_spawn[entry] = True

    widened.spawn_bearing_params = _compute_spawn_bearing_params(widened)
    return widened


def deep_find_with_site_depths(roots, max_depth: int):
    """One walk at `max_depth`, returning `(sites, depth_of)` -- the per-depth sets without
    a walk per depth.

    WHY THIS EXISTS. A consumer that needs the LOWEST depth at which each site appears used to
    have no route but to call `_deep_find_unbatched_per_item_spawns` once per depth, re-parsing
    the whole corpus each time: the collector parses on every call, and the depth `_depths`
    computes was folded away by `widened_index` before any caller saw it. Measured over the gate
    scope, the per-depth loop costs 206156.2ms of process time against 70546.9ms for one walk.

    `depth_of(site)` returns the depth at which that site first becomes visible, so
    `{s for s in sites if depth_of(s) <= k}` reproduces a walk at `max_depth=k` EXACTLY. That
    equivalence is asserted, not assumed, by
    `test_site_depths_reproduce_the_per_depth_walks` -- it is the whole warrant for this
    function and must stay green.

    THE ATTRIBUTION RULE, and three wrong versions of it, recorded because each looks right:
      - NOT the enclosing function's own depth. A function's BFS distance can be reached through
        a different callee than the one the site names, so that overshoots at low depths and
        undershoots at high ones -- measured wrong in BOTH directions.
      - NOT the minimum over every same-named definition. That ignores route c's import
        resolution and matches unrelated namesakes, yielding a strict superset.
      - The depth of the callee def THIS enclosing function actually calls, read off
        `_call_graph`'s own edge set so route c's resolution is used once rather than copied.
        A nested function's enclosing name is dotted (`outer.inner`) while edges are keyed by
        TOP-LEVEL function -- `_call_graph` walks nested bodies as part of the outer function --
        so the lookup strips to the top-level name. Missing that left exactly two sites
        mis-attributed to depth 1.

    A site whose callee reaches no depth-bearing def is depth 1: a direct spawner the standing
    one-hop gate already reports.

    This does NOT promote the deep collector to gating and does not touch the gate module; it is
    the same advisory result, annotated.
    """
    edges: dict[tuple[str, str], set[tuple[str, str]]] = {}
    depths: dict[tuple[str, str], int] = {}

    def _transform(base):
        built_edges = _call_graph(base)
        built_depths = _depths(base, built_edges, max_depth)
        edges.update(built_edges)
        depths.update(built_depths)
        return widened_index(base, built_depths, max_depth)

    sites = find_unbatched_per_item_spawns(roots, index_transform=_transform)

    def depth_of(site) -> int:
        top_level = site.enclosing.split(".")[0]
        reached = [
            callee_def
            for callee_def in edges.get((site.path, top_level), ())
            if callee_def[1] == site.callee and callee_def in depths
        ]
        return min(depths[d] for d in reached) if reached else 1

    return sites, depth_of


def _deep_find_unbatched_per_item_spawns(roots, max_depth: int):
    """Collection entry point: C0's `index_transform` seam, widening the collector's own
    single-parse `base` index in place of a second copy of the discriminator logic. The
    collector itself (`find_unbatched_per_item_spawns`) is UNMODIFIED."""
    return find_unbatched_per_item_spawns(
        roots,
        index_transform=lambda base: widened_index(
            base, _depths(base, _call_graph(base), max_depth), max_depth
        ),
    )


def test_two_hop_chain_found_at_depth_two_not_at_one_hop(tmp_path):
    """`check` -> `wrapper` -> `_git_add` (the spawner). One hop from a spawner reaches only
    `wrapper`, so the stock (one-hop) collector reports nothing for `check`'s own loop body
    calling `wrapper` -- `wrapper` itself never directly spawns. Widening to depth 2 makes
    `wrapper` a recognized direct spawner, and `check`'s per-item `wrapper(p)` call then
    resolves through route b exactly as an ordinary direct-spawning helper would."""
    fixture = tmp_path / "deep_chain.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _git_add(path):\n"
        "    subprocess.run(['git', 'add', path], cwd='/repo')\n"
        "\n"
        "def wrapper(path):\n"
        "    _git_add(path)\n"
        "\n"
        "def check(paths):\n"
        "    for p in paths:\n"
        "        wrapper(p)\n",
        encoding="utf-8",
    )

    baseline = find_unbatched_per_item_spawns((tmp_path,))
    assert baseline == []

    widened = _deep_find_unbatched_per_item_spawns((tmp_path,), max_depth=2)
    assert len(widened) == 1
    assert widened[0].callee == "wrapper"


def test_call_graph_declines_unimported_homonym(tmp_path):
    """Anti-scope negative: a same-named, unimported function defined in a DIFFERENT file must
    not create an edge -- `_call_graph`'s bare-name-fallback ban."""
    other_mod = tmp_path / "other.py"
    other_mod.write_text(
        "def helper(x):\n"
        "    return x + 1\n",
        encoding="utf-8",
    )
    caller_mod = tmp_path / "caller.py"
    caller_mod.write_text(
        "def helper(x):\n"
        "    return x - 1\n"
        "\n"
        "def check(x):\n"
        "    return helper(x)\n",
        encoding="utf-8",
    )

    from coordinator_core.tests.test_no_unbatched_per_item_git_spawn import (
        _build_func_index,
        _discover_scope_files,
        _load_file_records,
    )

    files = _discover_scope_files((tmp_path,))
    records = _load_file_records(files)
    index = _build_func_index(records)

    edges = _call_graph(index)
    caller_relpath = "caller.py"
    # Same-module resolution wins: `caller.py`'s own `helper` is the target, never `other.py`'s.
    assert edges[(caller_relpath, "check")] == {(caller_relpath, "helper")}


def test_call_graph_all_candidates_for_homonym_across_two_imported_modules(tmp_path):
    """C3 (route-c narrowing, plan `2026-08-26-route-c-resolves-the-imported-name-not-the-local-
    alias.md`): under the OLD all-candidates stance this alias-free fixture (`from mod_a import
    helper`) asserted edges to both `mod_a.helper` and `mod_b.helper`. `_import_resolves_to` now
    requires the candidate's own module to equal the import's resolved SOURCE MODULE, so
    `mod_b.helper` -- a homonym never imported by `caller.py` at all -- no longer qualifies.
    `mod_a.helper` survives because it is both same-named AND the actual resolved source; the
    bounded over-approximation this closure inherits from route g is retained only for genuine
    multi-definition-WITHIN-ONE-MODULE cases, not for an unrelated same-named function in a
    module nothing here imports."""
    mod_a = tmp_path / "mod_a.py"
    mod_a.write_text(
        "def helper(x):\n"
        "    return x + 1\n",
        encoding="utf-8",
    )
    mod_b = tmp_path / "mod_b.py"
    mod_b.write_text(
        "def helper(x):\n"
        "    return x - 1\n",
        encoding="utf-8",
    )
    caller_mod = tmp_path / "caller.py"
    caller_mod.write_text(
        "from mod_a import helper\n"
        "\n"
        "def check(x):\n"
        "    return helper(x)\n",
        encoding="utf-8",
    )

    from coordinator_core.tests.test_no_unbatched_per_item_git_spawn import (
        _build_func_index,
        _discover_scope_files,
        _load_file_records,
    )

    files = _discover_scope_files((tmp_path,))
    records = _load_file_records(files)
    index = _build_func_index(records)

    edges = _call_graph(index)
    assert edges[("caller.py", "check")] == {
        ("mod_a.py", "helper"),
    }


def test_no_remaining_direct_read_of_imported_names_by_file_outside_the_helper():
    """AC6: `_call_graph`, `_site_depth`, and `_reachable_spawn_sites` (this module) plus
    `_resolve_callee_def`/`_is_direct_spawner_name`/`find_unbatched_per_item_spawns`'s own
    `imported_here` leg (the gate module) are the six route-c/route-g resolution sites C1/C3
    repoint at the single shared helper, `_import_resolves_to`. Stronger than "one definition
    site": a surviving THIRD (or Nth) inline transcription of the old
    `funcs_by_name.get(callee)`-then-filter-by-relpath resolution, never routed through the
    helper, would still leave exactly one `_import_resolves_to` definition in the corpus while
    quietly resolving candidates the narrow rule was meant to prune -- this walks every function
    in both modules that reads `imported_names_by_file` (the local-binding gate every route-c
    site still legitimately checks first) and asserts that ANY function ALSO reading
    `funcs_by_name` (the candidate-set lookup only the old all-candidates rule needed unfiltered)
    also references `_import_resolves_to` by name in its own body -- i.e. it hands the candidate
    set through the helper rather than accepting it raw.

    `_resolve_callee_def_wide` (gate module) is excluded the same way `_import_resolves_to`
    itself is: its own docstring (citing AC4b and the 25-vs-26-site measurement) documents a
    DELIBERATE wide resolution for the two suppressor legs alone, never routed through
    `_import_resolves_to` by design -- not a stray transcription of route c/g's narrow rule, so
    it is not one of C3's three legs and flagging it here would be a false positive against a
    named, cited exception."""
    import inspect

    from coordinator_core.tests import test_no_unbatched_per_item_git_spawn as gate_module
    from coordinator_core.tests import (
        test_deep_per_item_spawn_worklist as collector_module,
    )

    def _funcs_reading_both_without_helper(module) -> list[str]:
        source = inspect.getsource(module)
        tree = ast.parse(source)
        offenders = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name in ("_import_resolves_to", "_resolve_callee_def_wide"):
                continue
            attrs_used = {n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)}
            calls_get_on_funcs_by_name = any(
                isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get"
                and isinstance(n.func.value, ast.Attribute)
                and n.func.value.attr == "funcs_by_name"
                for n in ast.walk(node)
            )
            names_used = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
            reads_imported_names = "imported_names_by_file" in attrs_used
            calls_helper = "_import_resolves_to" in names_used
            if reads_imported_names and calls_get_on_funcs_by_name and not calls_helper:
                offenders.append(f"{module.__name__}::{node.name}")
        return offenders

    offenders = _funcs_reading_both_without_helper(
        gate_module
    ) + _funcs_reading_both_without_helper(collector_module)
    assert offenders == []


def test_widened_index_does_not_mutate_base(tmp_path):
    """`base` is shared with the standing gate (C2's non-mutation assertion) -- widening must
    come back with `base`'s own `direct_spawn_funcs` / `same_module_direct_spawn` untouched."""
    fixture = tmp_path / "deep_chain.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def _git_add(path):\n"
        "    subprocess.run(['git', 'add', path], cwd='/repo')\n"
        "\n"
        "def wrapper(path):\n"
        "    _git_add(path)\n"
        "\n"
        "def check(paths):\n"
        "    for p in paths:\n"
        "        wrapper(p)\n",
        encoding="utf-8",
    )

    from coordinator_core.tests.test_no_unbatched_per_item_git_spawn import (
        _build_func_index,
        _discover_scope_files,
        _load_file_records,
    )

    files = _discover_scope_files((tmp_path,))
    records = _load_file_records(files)
    base = _build_func_index(records)

    base_direct_spawn_funcs_before = copy.deepcopy(base.direct_spawn_funcs)
    base_same_module_before = copy.deepcopy(base.same_module_direct_spawn)

    edges = _call_graph(base)
    depths = _depths(base, edges, max_depth=2)
    widened = widened_index(base, depths, max_depth=2)

    assert base.direct_spawn_funcs == base_direct_spawn_funcs_before
    assert base.same_module_direct_spawn == base_same_module_before
    assert widened is not base
    assert ("deep_chain.py", "wrapper") in widened.direct_spawn_funcs["wrapper"]
    assert ("deep_chain.py", "wrapper") not in base.direct_spawn_funcs.get("wrapper", [])


def test_depths_bounded_by_max_depth(tmp_path):
    """A three-hop chain (`a` -> `b` -> `c` -> spawner) is invisible at `max_depth=2`: `c` is
    depth 2 (calls the spawner directly... no -- `c` IS one hop from the spawner, `b` is two,
    `a` is three), so bounding at 2 must exclude `a`."""
    fixture = tmp_path / "three_hop.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def spawner(path):\n"
        "    subprocess.run(['git', 'add', path], cwd='/repo')\n"
        "\n"
        "def c(path):\n"
        "    spawner(path)\n"
        "\n"
        "def b(path):\n"
        "    c(path)\n"
        "\n"
        "def a(path):\n"
        "    b(path)\n",
        encoding="utf-8",
    )

    from coordinator_core.tests.test_no_unbatched_per_item_git_spawn import (
        _build_func_index,
        _discover_scope_files,
        _load_file_records,
    )

    files = _discover_scope_files((tmp_path,))
    records = _load_file_records(files)
    index = _build_func_index(records)
    edges = _call_graph(index)
    depths = _depths(index, edges, max_depth=2)

    assert depths[("three_hop.py", "spawner")] == 1
    assert depths[("three_hop.py", "c")] == 2
    assert ("three_hop.py", "b") not in depths
    assert ("three_hop.py", "a") not in depths


@pytest.mark.cadence
def test_depth_one_widening_is_a_structural_noop_and_base_is_unmutated():
    """Staff-eng F6/F7: the CHEAP STRUCTURAL leg of the neutrality proof, run before the live
    corpus walk below. Pins that depth-1 widening touches nothing -- `widened_index(base,
    depths, max_depth=1)`'s three touched fields (`direct_spawn_funcs`,
    `same_module_direct_spawn`, `spawn_bearing_params`) compare EQUAL to `base`'s own -- and
    that `base` itself comes back untouched across ALL EIGHT `_FuncIndex` fields, not just the
    two widened ones.

    Snapshot is taken via `copy.deepcopy` of each field BEFORE the `widened_index` call.
    Capturing "before" as a reference to the same dict object that later gets mutated would make
    every comparison below trivially true -- the classic form of this bug, and the failure this
    test exists to guard against; a future reader should not simplify this back to a same-object
    capture. `func_defs` is the one exception: its `ast.FunctionDef` values are not
    deep-copyable, so that field is snapshotted as a key set plus per-key object identity
    instead, asserting `base` never rebinds one of its own function-def entries to a different
    node.
    """
    files = _discover_scope_files(_gate_scope_paths())
    records = _load_file_records(files)
    base = _build_func_index(records)

    before_direct_spawn_funcs = copy.deepcopy(base.direct_spawn_funcs)
    before_runner_shaped_funcs = copy.deepcopy(base.runner_shaped_funcs)
    before_same_module_direct_spawn = copy.deepcopy(base.same_module_direct_spawn)
    before_imported_names_by_file = copy.deepcopy(base.imported_names_by_file)
    before_param_runner_defaults = copy.deepcopy(base.param_runner_defaults)
    before_verb_gated_spawn_verbs = copy.deepcopy(base.verb_gated_spawn_verbs)
    before_funcs_by_name = copy.deepcopy(base.funcs_by_name)
    before_func_defs_keys = set(base.func_defs.keys())
    before_func_defs_identity = {k: id(v) for k, v in base.func_defs.items()}

    edges = _call_graph(base)
    depths = _depths(base, edges, max_depth=1)
    widened = widened_index(base, depths, max_depth=1)

    # `base` is shared with the standing gate -- it must come back untouched across all eight
    # non-derived `_FuncIndex` fields, snapshotted before the call above (not after).
    assert base.direct_spawn_funcs == before_direct_spawn_funcs
    assert base.runner_shaped_funcs == before_runner_shaped_funcs
    assert base.same_module_direct_spawn == before_same_module_direct_spawn
    assert base.imported_names_by_file == before_imported_names_by_file
    assert base.param_runner_defaults == before_param_runner_defaults
    assert base.verb_gated_spawn_verbs == before_verb_gated_spawn_verbs
    assert base.funcs_by_name == before_funcs_by_name
    assert set(base.func_defs.keys()) == before_func_defs_keys
    assert {k: id(v) for k, v in base.func_defs.items()} == before_func_defs_identity

    # Structural invariant: at max_depth=1, widening is a no-op on the fields it touches.
    assert widened.direct_spawn_funcs == base.direct_spawn_funcs
    assert widened.same_module_direct_spawn == base.same_module_direct_spawn
    assert widened.spawn_bearing_params == base.spawn_bearing_params


@pytest.mark.cadence
def test_depth_one_key_set_equals_live_gate():
    """AC8, and half of AC3's evidence (C7's closing diff is the other half): the reason the
    instrument can be trusted not to disturb the gate it sits beside. A STANDING test asserting
    that the key set from a depth-1 widened index, collected through C0's `index_transform`
    seam, is EQUAL to the key set from `find_unbatched_per_item_spawns(_gate_scope_paths())`.
    Measured: 26 distinct sites both ways.

    Set equality, not counts -- per staff-eng F6, a count match with different members is the
    failure this test catches, and it is exactly the shape of the cross-parse bug the original
    unsound seam produced. Written as an equality against the LIVE call, never a frozen 26:
    freezing the number turns a neutrality proof into an inventory assertion that goes stale the
    moment the burn-down moves (`_KNOWN_SITES` has already gone 116 -> 27).

    This test owns only the positive leg -- it depends on C0's transform-shape guarantee that
    the collector and the widened index it visits always come from the same parse. C0 owns
    pinning that the cross-parse configuration is unreachable (AC9).
    """
    live = {site.key for site in find_unbatched_per_item_spawns(_gate_scope_paths())}
    depth_one = {
        site.key
        for site in _deep_find_unbatched_per_item_spawns(_gate_scope_paths(), max_depth=1)
    }
    assert depth_one == live


# ==========================================================================
# C4: the cost-ranked advisory worklist, and the reach-quality annotations
# (`cache_in_front`, `terminating_path`) it publishes beside every row.
# See the module docstring's own "THE COST-RANKED ADVISORY WORKLIST" and
# "REACH-QUALITY ANNOTATION" sections for the precision figures, method, and
# the C3 history this absorbs -- not re-derived here.
# ==========================================================================

#: Deliberately SPARSE. A site's absence from this table is the expected, common case, not a
#: gap to fill in later -- per-site cost is a measurement this module explicitly declines to make
#: itself (that measurement is the very O(N) subprocess cost this plan's sibling gate exists to
#: forbid). Every entry here is transcribed from a cost already on record elsewhere in this repo,
#: never guessed: `git --version` at 25.3ms is CLAUDE.md's own cited process-creation baseline
#: (`## The brightline` -- "process creation is the cost ... not the query"), used as the floor
#: estimate for an otherwise-unmeasured single `git` spawn; the two C5 reproducers are each one
#: `git merge-base --is-ancestor` per item, i.e. the same floor. A future session that measures a
#: real per-site cost should ADD an entry here, keyed by `AmpSite.key`, rather than replace this
#: table's shape.
_KNOWN_SITE_COST_MS: dict[tuple[str, str, str], float] = {
    (
        "coordinator_core/reconcile/commitments_recheck.py",
        "recheck_commitments",
        "_evaluate_record",
    ): 25.3,
    (
        "coordinator_core/ops/handoff_transition.py",
        "_read_gate_evidence_resolved",
        "_reresolve_gate_evidence_leg",
    ): 25.3,
}


def _known_cost_ms(site: AmpSite) -> Optional[float]:
    """`None` means "no recorded cost", never 0 -- a row with no data must sort below every row
    that has one (see `_sort_key`), and 0.0 would instead sort it first."""
    return _KNOWN_SITE_COST_MS.get(site.key)


#: Anything whose bare NAME contains "cache" (case-insensitive) -- `_EVER_TRACKED_CACHE`,
#: `_MAX_EVER_TRACKED_CACHE`, an `lru_cache`/`cache` decorator name. Transcribed from the two
#: named exemplars C3's own post-mortem cites (`dag.py :: _git_path_ever_tracked`,
#: `coverage.py :: _resolve_base`) rather than re-derived: both key their cache dict on a
#: reference the function itself receives (a per-item key or a value happening to repeat), so the
#: only mechanically-checkable signal left, once C3's static-taint and control-flow
#: reformulations both measured empty, is "does this hop mention something cache-shaped at all".
_CACHE_NAME_RE = re.compile(r"cache", re.IGNORECASE)
_CACHE_BOUND_RE = re.compile(r"max", re.IGNORECASE)


def _decorator_name(dec: ast.expr) -> Optional[str]:
    if isinstance(dec, ast.Name):
        return dec.id
    if isinstance(dec, ast.Attribute):
        return dec.attr
    if isinstance(dec, ast.Call):
        return _decorator_name(dec.func)
    return None


def _cache_guard_in_function(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> Optional[str]:
    """`None` (no cache guard seen), `"bounded"`, or `"unbounded"` -- a BOUNDED cache (an
    `_MAX_...CACHE...`-shaped name referenced in the same function, `dag.py`'s own capped-and-
    cleared shape) is weaker evidence than an unbounded one, per this chunk's spec: note whether
    the cache is bounded or invalidatable rather than treating every cache hit the same.

    Heuristic, not a proof -- see this module's `_KNOWN_SITE_COST_MS` sibling note and the
    module docstring's REACH-QUALITY section: this ANNOTATES a row, it never suppresses one, and
    a false positive here costs a human one dismissed row, never a missed amplification site."""
    for dec in fn.decorator_list:
        name = _decorator_name(dec)
        if name in ("lru_cache", "cache"):
            return "unbounded"
    referenced = {
        node.id for node in ast.walk(fn) if isinstance(node, ast.Name) and _CACHE_NAME_RE.search(node.id)
    }
    if not referenced:
        return None
    bounded = any(_CACHE_BOUND_RE.search(name) for name in referenced)
    return "bounded" if bounded else "unbounded"


def _forward_chain_to_spawn(
    index: _FuncIndex,
    edges: dict[tuple[str, str], set[tuple[str, str]]],
    start: tuple[str, str],
    max_depth: int,
) -> list[tuple[str, str]]:
    """One WITNESSING forward path from `start` to a same-module direct spawner, within
    `max_depth` hops -- not necessarily the only path a general call graph could offer, but a
    sufficient one for the `cache_in_front` annotation's own job (a reader deciding whether ANY
    hop along a plausible reach carries a cache guard, not enumerating every reach). BFS, so the
    path returned is a SHORTEST witnessing one. Falls back to `[start]` alone if no such path
    exists within `max_depth` (defensive only -- every worklist row's enclosing function was
    reached by exactly this closure in the first place)."""
    queue: deque[tuple[tuple[str, str], list[tuple[str, str]]]] = deque([(start, [start])])
    visited = {start}
    while queue:
        node, path = queue.popleft()
        if node in index.same_module_direct_spawn:
            return path
        if len(path) - 1 >= max_depth:
            continue
        for nxt in edges.get(node, ()):
            if nxt not in visited:
                visited.add(nxt)
                queue.append((nxt, path + [nxt]))
    return [start]


def _cache_in_front(
    index: _FuncIndex,
    edges: dict[tuple[str, str], set[tuple[str, str]]],
    start: tuple[str, str],
    max_depth: int,
) -> Optional[str]:
    chain = _forward_chain_to_spawn(index, edges, start, max_depth)
    for hop in chain:
        fn = index.func_defs.get(hop)
        if fn is None:
            continue
        guard = _cache_guard_in_function(fn)
        if guard is not None:
            return guard
    return None


def _attach_parents(root: ast.AST) -> None:
    for node in ast.walk(root):
        for child in ast.iter_child_nodes(node):
            child.parent = node  # type: ignore[attr-defined]


def _terminating_path(
    fn: ast.FunctionDef | ast.AsyncFunctionDef, lineno: int, callee: str | None = None
) -> bool:
    """True when the per-item call at `lineno` sits inside a block (an `if`/`try`/`except` arm)
    whose LAST statement is `return`/`raise`/`break` -- so however many items the enclosing loop
    is handed, this call fires at most once. `continue` is deliberately excluded (C3's own
    control-flow reformulation, transcribed here): a block ending in `continue` still lets the
    loop iterate again, which is exactly the per-item repetition this annotation is meant to
    flag as ABSENT.

    Heuristic over the nearest enclosing compound-statement body only, walking PARENT pointers
    out from the call's own statement until a `for`/`while` is reached (the loop is the ceiling
    -- a terminating block one level ABOVE the loop says nothing about how many times the loop
    itself runs). Annotation only, per this module's own REACH-QUALITY doctrine: never
    suppresses, only tells a reader that this particular row may be cheaper than its depth
    suggests."""
    _attach_parents(fn)
    # `lineno` alone does not identify a call: `f(x); g(y)` on one physical line, or a call
    # nested as an argument to another (`wrapper(helper(x))`), put several `ast.Call` nodes at
    # the same lineno, and taking whichever `ast.walk` reaches first annotates the wrong one.
    # `AmpSite` carries no col_offset, but it does carry `callee` -- so disambiguate on that and
    # fall back to the first call at the line only when no callee matches (an unparsed callee
    # shape), which keeps this no worse than it was.
    candidates = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Call) and getattr(node, "lineno", None) == lineno
    ]
    if not candidates:
        return False
    call_node = candidates[0]
    if callee:
        for node in candidates:
            if _call_callee_name(node) == callee:
                call_node = node
                break

    current: ast.AST = call_node
    while not isinstance(current, ast.stmt):
        parent = getattr(current, "parent", None)
        if parent is None:
            return False
        current = parent

    while True:
        parent = getattr(current, "parent", None)
        if parent is None:
            return False
        if isinstance(parent, (ast.For, ast.While)):
            return False
        for attr in ("body", "orelse", "finalbody"):
            body = getattr(parent, attr, None)
            if isinstance(body, list) and current in body:
                if isinstance(parent, (ast.If, ast.Try, ast.ExceptHandler)):
                    if body and body[-1] is current and isinstance(
                        body[-1], (ast.Return, ast.Raise, ast.Break)
                    ):
                        return True
                break
        current = parent


def _reachable_spawn_sites(
    base: _FuncIndex,
    edges: dict[tuple[str, str], set[tuple[str, str]]],
    site: AmpSite,
    max_hops: int,
) -> Optional[int]:
    """C9: a REACHABILITY COUNT, not a per-iteration cost. Counts distinct spawn-bearing
    `(relpath, func)` nodes reachable, via the SAME `edges`/`same_module_direct_spawn` data this
    module already built for `_depths`/`_site_depth` (no second corpus walk), forward from the
    per-item `site.callee` within `max_hops` hops -- `max_hops` is the ROW'S OWN resolved depth,
    never a fixed constant, so a row's count is bounded by exactly the chain the collector already
    confirmed reaches a spawn for that row.

    This is a property of the CALL GRAPH'S SHAPE, not of an invocation: reachable spawn sites are
    frequently mutually exclusive branches, error paths, or simply never taken on a given call.
    C9 corrected an earlier defect (C8) that scaled this count by a process-creation constant and
    published the result as milliseconds -- measured top-ranked row `close_out_and_stamp.py ::
    _disposition_ref_evidence -> _verify_disposition_ref` scored 44 reachable sites (rendered
    1113.2ms), while the function it names (`_verify_disposition_ref`, lines 815-852) contains
    exactly 2 `_run_git` calls: the published figure overstated the real per-iteration cost by
    ~22x. No millisecond figure is derivable from this count; do not multiply it into one.

    Callee resolution mirrors `_site_depth`'s own same-module-first-then-imported-narrowed
    order, both routed through the shared `_import_resolves_to` match helper rather than a
    separate transcription, so a homonym-imported callee starts the walk only from the
    candidate definition(s) whose module and name the import actually resolves to, exactly as
    `_call_graph`'s own bounded over-approximation does elsewhere in this module.

    `None` when the callee cannot be resolved into any starting node at all -- same honesty rule
    as `_known_cost_ms`/`_site_depth`: an unresolvable chain gets no fabricated count, not a 0."""
    starts: set[tuple[str, str]] = set()
    same_module_key = (site.path, site.callee)
    if same_module_key in base.func_defs:
        starts.add(same_module_key)
    elif site.callee in base.imported_names_by_file.get(site.path, set()):
        starts = {
            candidate
            for candidate in base.funcs_by_name.get(site.callee, [])
            if candidate[0] != site.path
            and _import_resolves_to(base, site.path, site.callee, candidate[0], candidate[1])
        }
    if not starts:
        return None

    visited: set[tuple[str, str]] = set(starts)
    reachable_spawns: set[tuple[str, str]] = {
        node for node in starts if node in base.same_module_direct_spawn
    }
    frontier = set(starts)
    hops = 0
    while frontier and hops < max_hops:
        next_frontier: set[tuple[str, str]] = set()
        for node in frontier:
            for nxt in edges.get(node, ()):
                if nxt not in visited:
                    visited.add(nxt)
                    next_frontier.add(nxt)
                    if nxt in base.same_module_direct_spawn:
                        reachable_spawns.add(nxt)
        frontier = next_frontier
        hops += 1
    return len(reachable_spawns)


def _sort_key(entry: dict) -> tuple[int, float, tuple[str, str, str]]:
    """C9: THREE-TIER ranking, split back apart by UNIT rather than merged by magnitude. C8 had
    ranked a measured `cost_ms` row against a derived `est_floor_ms` row on raw magnitude alone --
    defensible only while both columns were milliseconds. C9 deletes the derived-milliseconds
    column entirely (`reachable_spawn_sites` is a COUNT, not a duration), so a count and a
    duration are no longer comparable in any unit and pretending otherwise would be the same
    class of error one level up. Tiers, in order: measured `cost_ms` rows (tier 0, highest first),
    then rows with a `reachable_spawn_sites` count (tier 1, highest first), then genuinely
    unrankable rows (tier 2, no cost and no count). `None` is never imputed as 0 in either column
    (see `_known_cost_ms`/`_reachable_spawn_sites`), so a row only falls into a lower tier when
    the module truly has nothing to say about it.

    Stable on `site.key` among ties/no-data rows so the worklist's own row order is reproducible
    across runs of an otherwise-unordered corpus walk.
    """
    cost = entry["cost_ms"]
    count = entry["reachable_spawn_sites"]
    if cost is not None:
        tier = 0
        magnitude = cost
    elif count is not None:
        tier = 1
        magnitude = float(count)
    else:
        tier = 2
        magnitude = 0.0
    return (tier, -magnitude, entry["site"].key)


def _site_depth(
    base: _FuncIndex,
    depths: dict[tuple[str, str], int],
    site: AmpSite,
) -> Optional[int]:
    """The site's own depth is the hop count from its `callee` (the per-item call target) to a
    real spawn -- NOT the depth of `site.enclosing` (the loop-bearing caller), which the
    C4-FIX defect conflated: a function can carry more than one per-item call, at different
    depths, while itself also containing an unrelated direct spawn that would fabricate depth 1
    for every site inside it. `depths` is already seeded at 1 for exactly the functions in
    `base.same_module_direct_spawn` (`_depths`'s own BFS seed, itself built off
    `base.direct_spawn_funcs`), so keying the lookup on the callee -- resolved same-module-first
    then via the shared `_import_resolves_to` match helper (the same route-c resolution
    `_call_graph` uses, not a separate transcription of it) -- reads depth-1 off exactly the
    index the fix brief names, with no second corpus walk. `None` when no candidate resolves
    within `depths` at all."""
    same_module_key = (site.path, site.callee)
    if same_module_key in base.func_defs:
        return depths.get(same_module_key)
    if site.callee in base.imported_names_by_file.get(site.path, set()):
        candidate_depths = [
            depths[candidate]
            for candidate in base.funcs_by_name.get(site.callee, [])
            if candidate[0] != site.path
            and candidate in depths
            and _import_resolves_to(base, site.path, site.callee, candidate[0], candidate[1])
        ]
        if candidate_depths:
            return min(candidate_depths)
    return None


def _advisory_worklist(max_depth: int) -> list[dict]:
    """One shared parse (`find_unbatched_per_item_spawns`'s own `index_transform` seam, exactly
    as `_deep_find_unbatched_per_item_spawns` uses it) feeds both the reported sites AND the
    `_call_graph`/`_depths` this function needs for the `depth`/`cache_in_front` columns -- no
    second walk of the corpus for THOSE. `depth1_confirmed_keys` below is the one deliberate,
    documented exception (C4-FIX) -- see its own note just above its use."""
    # The transform's OWN `base` argument is what feeds `_call_graph`/`_depths`/
    # `widened_index`, and is captured here for the annotation columns below -- exactly as
    # `_deep_find_unbatched_per_item_spawns` threads it. Building an index from a separate
    # `_load_file_records` walk and returning it while DISCARDING `base` is the cross-parse
    # configuration C0 exists to eliminate: the `ast.Call` nodes the collector visits would
    # come from its own internal parse while the `func_defs` node identities in the returned
    # index came from ours, so `_enclosing_loop_of` and discriminators 13/14/15 behind it
    # silently fail their lookups and stop suppressing (the plan's Problem section measured
    # exactly that: 31 sites reported against the live gate's 26, five spurious, none lost).
    captured: dict = {}

    def _widen(base):
        edges = _call_graph(base)
        depths = _depths(base, edges, max_depth)
        captured["base"] = base
        captured["edges"] = edges
        captured["depths"] = depths
        return widened_index(base, depths, max_depth)

    sites = find_unbatched_per_item_spawns(_gate_scope_paths(), index_transform=_widen)
    base = captured["base"]
    edges = captured["edges"]
    depths = captured["depths"]

    # C4-FIX, DOCUMENTED DEVIATION FROM "no second walk": widening at `max_depth` can change an
    # EARLIER discriminator's verdict (one that reads wider index state, e.g. discriminator 13's
    # batched-primary-fallback exemption) for a call whose own callee was ALREADY a depth-1
    # direct spawner in `base` -- so a small number of sites structurally computed as depth 1 by
    # `_site_depth` are not actually sites the live, unwidened gate would ever report.
    #
    # HISTORICAL NOTE, kept because it explains a figure earlier revisions recorded: this
    # divergence was once measured at "up to 5 such sites, all route `b-local-helper`" and
    # attributed to reasons internal to the gate module. That attribution was wrong. Those five
    # were the cross-parse artefact -- `_advisory_worklist` was discarding the transform's `base`
    # and substituting an index from a separate parse, the exact 31-vs-26 five-spurious-site
    # shape the plan's Problem section measured. The seam above is sound now, so re-measure
    # before citing any figure here; the check itself stays because C4-FIX requirement 4 is
    # unconditional, not because a known divergence still needs absorbing.
    # Static analysis over `base`/`depths` alone cannot detect this without re-running the
    # discriminator -- it depends on suppression logic this module deliberately does not
    # re-derive (module docstring's own Anti-scope). A fully independent, freshly-built call to
    # `find_unbatched_per_item_spawns(_gate_scope_paths())` -- handed no index at all, so the
    # comparison is against the gate's genuine unwidened verdict rather than against another
    # widened view of it -- is the only way to confirm a depth-1 label is real, and the requirement this closes (C4-FIX
    # requirement 4: no row may be labelled depth 1 unless its key is in the live gate's own key
    # set) is unconditional -- worth the one extra corpus walk this function otherwise avoids.
    # This walk is NOT per-item and NOT a subprocess spawn; this test already runs in "minutes,
    # not seconds" by design (advisory, non-gating, `designed_red`).
    depth1_confirmed_keys = {
        site.key for site in find_unbatched_per_item_spawns(_gate_scope_paths())
    }

    rows = []
    for site in sites:
        # `index.func_defs`/`_call_graph`/`_depths` are all keyed by TOP-LEVEL function only
        # (route g's own `top_level_enclosing = enclosing.split(".")[0]` precedent) -- a nested
        # def's dotted `enclosing` string never appears there directly.
        top_level_enclosing = site.enclosing.split(".")[0]
        top_level_key = (site.path, top_level_enclosing)
        # NO DEFAULT, AND KEYED ON THE CALLEE, NOT THE ENCLOSING FUNCTION (C4-FIX): see
        # `_site_depth`'s own docstring. A key this closure cannot resolve at all genuinely has
        # no depth, and defaulting it to 1 fabricates the single most trusted label in the wrong
        # direction (measured: 26 of 42 `depth 1` rows were not depth-1 gate sites at all).
        # `None` here means "unknown", rendered honestly by `_render_worklist_report`.
        depth = _site_depth(base, depths, site)
        if depth == 1 and site.key not in depth1_confirmed_keys:
            # The site's callee is structurally a depth-1 spawner, but the live gate does not
            # confirm this exact call as a reachable amplification site (see
            # `depth1_confirmed_keys`'s own note) -- rendering it "unknown" rather than a
            # depth-1 label this closure cannot actually stand behind.
            depth = None
        fn = base.func_defs.get(top_level_key)
        # C9: the rankable second column. Bounded by the ROW'S OWN resolved depth, never
        # `max_depth`, and `None` whenever `depth` itself is `None` -- a count bounded by an
        # unresolved hop budget is not a count this closure can stand behind.
        reachable_spawn_sites = (
            _reachable_spawn_sites(base, edges, site, depth) if depth is not None else None
        )
        rows.append(
            {
                "site": site,
                "depth": depth,
                "cost_ms": _known_cost_ms(site),
                "reachable_spawn_sites": reachable_spawn_sites,
                "cache_in_front": _cache_in_front(base, edges, top_level_key, max_depth),
                "terminating_path": (
                    _terminating_path(fn, site.lineno, site.callee) if fn is not None else False
                ),
            }
        )
    rows.sort(key=_sort_key)
    return rows


_PRECISION_BY_DEPTH = {
    2: "33% (4/12), 53 new sites",
    3: "75% (9/12), 39 new sites",
    4: "83% (10/12), 23 new sites",
}

_PRECISION_FRAMING = (
    "Point estimates 33%/75%/83% at depths 2/3/4, n=12 per stratum. Intervals are wide "
    "(roughly 14-61% at depth 2 by the Wilson method) and overlap across strata at this sample "
    "size: this sample can neither establish a floor nor cleanly distinguish the depths. Read "
    "the figures as an order-of-magnitude sanity check on an advisory worklist, not as a "
    "precision guarantee. They were also scored under THIS module's call-graph resolution "
    "rule (`_call_graph`, route c: a cross-module callee is looked up by the name bound in "
    "the calling file, alias included), transcribed verbatim from the one-hop gate. That rule is "
    "CONFIRMED DEFECTIVE on aliased imports, measured 2026-08-26: an aliased import resolves by "
    "its LOCAL name against `funcs_by_name`, so `from x import y as _z` can match any unrelated "
    "`_z` in scope. Reproduced at `promote_shipped_in_flight_stubs.py :: _run_promotions`, where "
    "a walk-only callee is reported as a per-item spawn because the alias collides with a "
    "genuinely-spawning script in `coordinator/bin/` that the file does not import "
    "(`state/bug-backlog/2026-08-25-the-amplification-gate-resolves-an-alias-bf22411daeda.yaml`). "
    "The rule is frozen here precisely BECAUSE these figures were measured under it: fixing it "
    "SUPERSEDES them rather than improving them, and the figures should be read as an upper "
    "bound on precision, since every edge in the sample was resolved by a rule now known to "
    "admit false edges."
)


def _render_worklist_report(rows: list[dict], max_depth: int) -> str:
    lines = [
        "# Deep per-item spawn worklist (advisory, non-gating)",
        "",
        "Spec backlink: `docs/plans/2026-08-25-a-collector-that-sees-past-one-hop.md`, chunk C4.",
        "",
        "`reachable_spawn_sites` counts spawn sites reachable downstream of a per-item call; it "
        "is an upper bound on distinct spawn sites, NOT a count of spawns executed per "
        "iteration -- a high value means \"spawn-dense downstream, worth a look\", not \"this "
        "costs N spawns per item\" (C9).",
        "",
        "## Per-depth precision",
        "",
        _PRECISION_FRAMING,
        "",
    ]
    for depth in sorted(_PRECISION_BY_DEPTH):
        lines.append(f"- depth {depth} -- {_PRECISION_BY_DEPTH[depth]}")
    lines.append(
        "- combined depths 2-4 -- 64% (23/36); 141 total sites at depth 4, from 26 at depth 1"
    )
    lines.append("")
    lines.append(
        "Method: single unblinded judge (the author of this instrument), 2026-08-25, rubric "
        "\"a site is a true positive when the flagged per-item call reaches a real, unbatched "
        "spawn through routes a-g, confirmed by reading the call chain at every hop.\" A "
        "re-sampling protocol is owed as a follow-on, not in this plan: >=30 sites drawn at "
        "random from this worklist, two independent judges, disagreement rate published, these "
        "figures superseded on completion."
    )
    lines.append("")
    lines.append(
        "`cache_in_front`/`terminating_path` below ANNOTATE a row; they never move the "
        "precision figures above, and they never remove a row from this worklist."
    )
    lines.append("")
    lines.append("## Ranking columns (C9)")
    lines.append("")
    lines.append(
        "`cost_ms` is MEASURED and on-record -- a real, previously-observed cost for this exact "
        "site, transcribed into `_KNOWN_SITE_COST_MS` and cited to its source audit."
    )
    lines.append(
        "`reachable_spawn_sites` is a REACHABILITY COUNT of distinct spawn-bearing functions "
        "reachable downstream of the per-item call, within the row's own resolved depth -- an "
        "UPPER BOUND on distinct spawn sites, and NOT a count of spawns executed per iteration. "
        "Reachable spawn sites are frequently mutually exclusive branches, error paths, or "
        "simply never taken on a given call. A high value means \"spawn-dense downstream, worth "
        "a look\", never \"this costs N spawns per item\" -- no millisecond figure is derivable "
        "from this count. (C8 had scaled this count by a process-creation constant and published "
        "the result as milliseconds; measured top-ranked row `close_out_and_stamp.py :: "
        "_disposition_ref_evidence -> _verify_disposition_ref` scored 44 reachable sites "
        "rendered as 1113.2ms, while the named function contains exactly 2 `_run_git` calls -- "
        "an ~22x overstatement. C9 deletes that column rather than adjusting its constant.)"
    )
    lines.append(
        "Ranking is TWO-TIER, on units that are no longer comparable: measured `cost_ms` rows "
        "first (highest first), then `reachable_spawn_sites` count rows (highest first), then "
        "rows this module cannot rank at all last. See `_sort_key`."
    )
    lines.append("")
    lines.append(f"## Worklist (max_depth={max_depth}), ranked by tier then magnitude, highest first")
    lines.append("")
    lines.append(
        "| cost_ms | reachable_spawn_sites | depth | site | cache_in_front | terminating_path |"
    )
    lines.append("|---|---|---|---|---|---|")
    for row in rows:
        site: AmpSite = row["site"]
        cost = "n/a (no recorded cost)" if row["cost_ms"] is None else f"{row['cost_ms']:.1f}"
        count = (
            "n/a (unresolved depth)"
            if row["reachable_spawn_sites"] is None
            else str(row["reachable_spawn_sites"])
        )
        # Unknown depth renders honestly (C4-FIX), mirroring `cost_ms: None` -> "n/a (no recorded
        # cost)" above -- never a blank cell that a reader could misread as a resolved number.
        depth = "unknown" if row["depth"] is None else str(row["depth"])
        cache = row["cache_in_front"] or "none"
        lines.append(
            f"| {cost} | {count} | {depth} | `{site.path}:{site.lineno} ({site.enclosing} -> "
            f"{site.callee})` | {cache} | {row['terminating_path']} |"
        )
    lines.append("")
    return "\n".join(lines)


# ==========================================================================
# C1: the baseline the routine_signals reader consumes. Written by the SAME
# run that regenerates the audit above -- never hand-maintained, and absorbed
# into the next run rather than accumulating a second, hand-frozen inventory
# (see `docs/dispatch-briefs/.../C1.md` § "Why this cannot rot").
# ==========================================================================

#: Repo-relative, resolved once against this module's own file location --
#: matches `_REPO_ROOT`'s own resolution (this module's `parents[2]`), so the
#: baseline lands beside the audit this same test writes.
_BASELINE_PATH = _REPO_ROOT / "state" / "baselines" / "deep-per-item-spawn-worklist.json"


def _site_key_as_list(key: tuple[str, str, str]) -> list[str]:
    """`AmpSite.key` is a tuple; JSON has no tuple type, so every persisted key round-trips
    as a 3-element list -- comparisons below (`_compute_baseline`) re-tuple on read rather
    than comparing lists to tuples."""
    return list(key)


def _compute_baseline(rows: list[dict], previous: Optional[dict]) -> dict:
    """Build one baseline payload from the worklist's own rows, reading `previous` (the
    prior run's baseline, or `None` on a first run) to compute `new_since_last`.

    `new_since_last` is `None`, not `[]`, when `previous` is `None` -- absent evidence and
    "nothing new" are different facts (per the brief), and the routine_signals reader keys
    its `unknown` vs `quiet` states directly off that distinction.
    """
    site_keys = sorted(_site_key_as_list(row["site"].key) for row in rows)

    by_depth: dict[str, int] = {}
    for row in rows:
        depth_key = "unknown" if row["depth"] is None else str(row["depth"])
        by_depth[depth_key] = by_depth.get(depth_key, 0) + 1

    if previous is None:
        new_since_last: Optional[list[list[str]]] = None
    else:
        previous_keys = {tuple(key) for key in previous.get("site_keys", [])}
        current_keys = {tuple(key) for key in site_keys}
        new_since_last = sorted(
            _site_key_as_list(key) for key in (current_keys - previous_keys)
        )

    ranked = sorted(
        (row for row in rows if row["reachable_spawn_sites"] is not None),
        key=lambda row: row["reachable_spawn_sites"],
        reverse=True,
    )
    top = [
        {
            "key": _site_key_as_list(row["site"].key),
            "depth": row["depth"],
            "reachable_spawn_sites": row["reachable_spawn_sites"],
        }
        for row in ranked[:3]
    ]

    return {
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "total_sites": len(rows),
        "by_depth": by_depth,
        "site_keys": site_keys,
        "new_since_last": new_since_last,
        "top": top,
    }


def _write_baseline(rows: list[dict], path: Path) -> dict:
    """Read the PREVIOUS baseline (if any) before overwriting it, then write the new one.
    Never hand-maintained: this is the only writer, called from the same run that
    regenerates the audit."""
    previous = None
    if path.exists():
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = None
    baseline = _compute_baseline(rows, previous)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return baseline


def test_baseline_absorbs_new_sites_once(tmp_path):
    """Second run with an added synthetic site reports it in `new_since_last`; third run
    (same sites again) reports it empty -- the absorb-once behaviour the baseline exists to
    guarantee. A `tmp_path` fixture, not the real corpus, per the brief's own validation
    note (this must stay fast)."""
    baseline_path = tmp_path / "deep-per-item-spawn-worklist.json"

    site_a = AmpSite(path="a.py", lineno=1, enclosing="check", route="a-direct", callee="wrapper")
    site_b = AmpSite(path="b.py", lineno=2, enclosing="check", route="a-direct", callee="wrapper")

    rows_first = [
        {"site": site_a, "depth": 1, "reachable_spawn_sites": 3},
    ]
    first = _write_baseline(rows_first, baseline_path)
    assert first["new_since_last"] is None
    assert first["total_sites"] == 1

    rows_second = [
        {"site": site_a, "depth": 1, "reachable_spawn_sites": 3},
        {"site": site_b, "depth": 2, "reachable_spawn_sites": 1},
    ]
    second = _write_baseline(rows_second, baseline_path)
    assert second["new_since_last"] == [_site_key_as_list(site_b.key)]
    assert second["total_sites"] == 2

    rows_third = list(rows_second)
    third = _write_baseline(rows_third, baseline_path)
    assert third["new_since_last"] == []
    assert third["total_sites"] == 2


def test_baseline_top_capped_at_three_highest_reachable():
    """`top` carries AT MOST 3 rows, highest `reachable_spawn_sites` first."""
    sites = [
        AmpSite(path=f"m{i}.py", lineno=i, enclosing="check", route="a-direct", callee="wrapper")
        for i in range(5)
    ]
    rows = [
        {"site": sites[0], "depth": 1, "reachable_spawn_sites": 10},
        {"site": sites[1], "depth": 1, "reachable_spawn_sites": 40},
        {"site": sites[2], "depth": 1, "reachable_spawn_sites": None},
        {"site": sites[3], "depth": 1, "reachable_spawn_sites": 20},
        {"site": sites[4], "depth": 1, "reachable_spawn_sites": 30},
    ]
    baseline = _compute_baseline(rows, previous=None)
    assert [row["reachable_spawn_sites"] for row in baseline["top"]] == [40, 30, 20]


@pytest.mark.designed_red
@pytest.mark.cadence
def test_deep_per_item_spawn_advisory_worklist():
    """AC2, AC7, AC10. ADVISORY OUTPUT -- modeled on the gate's own `designed_red` burn-down
    worklist (`test_burn_down_known_preexisting_amplification_sites`), never on a gating subset
    assertion: this test emits a cost-ranked worklist and writes the audit file below, and it
    does not fail on the worklist's own contents. See the module docstring's "THE COST-RANKED
    ADVISORY WORKLIST" / "REACH-QUALITY ANNOTATION" sections for the ranking rule, the published
    per-depth precision, and the C3 history this absorbs.

    The only assertions here are STRUCTURAL invariants of the worklist's own shape -- every row
    carries its depth, cost-descending with `None` sorted last is honored, and the report was
    actually written -- never a claim about which or how many sites are TRUE amplification.
    """
    rows = _advisory_worklist(max_depth=4)

    assert all(
        "depth" in row and (row["depth"] is None or row["depth"] >= 1) for row in rows
    ), (
        "every worklist row must carry its own depth column alongside its cost (staff-eng F14) "
        "-- a cost-ranked list is otherwise unreadable against the per-depth precision figures. "
        "`None` (unresolved / unknown depth, C4-FIX) is a valid value here; a fabricated integer "
        "is not."
    )

    costed = [row["cost_ms"] for row in rows if row["cost_ms"] is not None]
    uncosted_seen = any(row["cost_ms"] is None for row in rows)
    if costed and uncosted_seen:
        first_uncosted = next(i for i, row in enumerate(rows) if row["cost_ms"] is None)
        assert all(row["cost_ms"] is not None for row in rows[:first_uncosted]), (
            "a site with no recorded cost must sort below every site that has one, never "
            "interleaved -- imputing an implicit 0 would rank it as free instead"
        )
    for earlier, later in zip(costed, costed[1:]):
        assert earlier >= later, "costed rows must be cost-descending"

    report = _render_worklist_report(rows, max_depth=4)
    audit_path = _REPO_ROOT / "state" / "audits" / "2026-08-25-deep-per-item-spawn-worklist.md"
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit_path.write_text(report, encoding="utf-8")

    # C1: the routine_signals reader's baseline, written by this same run -- see
    # `_write_baseline`'s own docstring for the absorb-once contract.
    _write_baseline(rows, _BASELINE_PATH)


# ==========================================================================
# C5: standing multi-hop reproducers -- named, live corpus sites the deep
# collector reaches at depth 4 that the one-hop gate cannot see at all. Both
# verified (this session) to sit OUTSIDE the write scope of
# `docs/plans/2026-08-25-the-touched-files-record-gets-a-designed-shape.md`'s
# C4/C7, so neither is deleted out from under these assertions by that plan.
#
# `compute_scope -> normalize_historical_touch_entry` (cited in the module
# docstring's "THE COST-RANKED ADVISORY WORKLIST" measurement) is the
# originating P1 measurement this whole line of work rests on, but it is
# NEVER the standing reproducer here: that chain's own functions
# (`normalize_touch_path`, `classify_touch_entry`, `normalize_historical_
# touch_entry`) are named for deletion by that plan's own C4, and a green run
# over a deleted chain would prove nothing.
# ==========================================================================


@pytest.mark.cadence
def test_deep_reproducer_commitments_recheck_reaches_git_ancestry_at_depth_four():
    """Standing multi-hop reproducer #1 (AC4). `recheck_commitments ->
    _evaluate_record -> sibling_fact.resolve_leg -> git_ancestry.is_ancestor ->
    git/run.run_git` -- one `merge-base --is-ancestor` per commitment record,
    four hops from `recheck_commitments`'s own per-record loop to the spawn.

    Written against the LIVE, real corpus (not a fixture) precisely because
    this is the property the one-hop gate structurally cannot have: the
    one-hop `find_unbatched_per_item_spawns` call below must come back empty
    for this exact site, and the depth-4 widened collector must find it, in
    the same test -- so a future deletion of the chain (this module's `import
    ast` walk finding nothing to walk) fails LOUDLY via the second assertion
    rather than silently passing over an empty set.
    """
    roots = _gate_scope_paths()
    target_path = "coordinator_core/reconcile/commitments_recheck.py"
    target_enclosing = "recheck_commitments"
    target_callee = "_evaluate_record"

    one_hop = find_unbatched_per_item_spawns(roots)
    one_hop_hit = any(
        site.path == target_path
        and site.enclosing == target_enclosing
        and site.callee == target_callee
        for site in one_hop
    )
    assert not one_hop_hit, (
        "this reproducer's whole point is a chain the one-hop gate cannot see -- if it now "
        "shows up at one hop the chain has been shortened and this is no longer a multi-hop "
        "reproducer"
    )

    deep = _deep_find_unbatched_per_item_spawns(roots, max_depth=4)
    deep_hit = any(
        site.path == target_path
        and site.enclosing == target_enclosing
        and site.callee == target_callee
        for site in deep
    )
    assert deep_hit, (
        f"expected the depth-4 collector to reach {target_path}:{target_enclosing} -> "
        f"{target_callee} -- if this chain was deleted or restructured, this reproducer must "
        "fail loudly rather than silently pass over an empty set (see module note above)"
    )


@pytest.mark.cadence
def test_deep_reproducer_handoff_transition_reaches_gate_evidence_leg_at_depth_four():
    """Standing multi-hop reproducer #2 (AC4). `_read_gate_evidence_resolved ->
    _reresolve_gate_evidence_leg` -- same three-hop tail as reproducer #1
    (`sibling_fact.resolve_leg -> git_ancestry.is_ancestor -> git/run.run_git`),
    one spawn per gate-evidence leg.

    Same shape as the sibling reproducer above: asserted absent at one hop and
    present at depth 4 in the same test, so a future deletion of this chain
    fails loudly instead of the assertion silently passing over an empty set.
    """
    roots = _gate_scope_paths()
    target_path = "coordinator_core/ops/handoff_transition.py"
    target_enclosing = "_read_gate_evidence_resolved"
    target_callee = "_reresolve_gate_evidence_leg"

    one_hop = find_unbatched_per_item_spawns(roots)
    one_hop_hit = any(
        site.path == target_path
        and site.enclosing == target_enclosing
        and site.callee == target_callee
        for site in one_hop
    )
    assert not one_hop_hit, (
        "this reproducer's whole point is a chain the one-hop gate cannot see -- if it now "
        "shows up at one hop the chain has been shortened and this is no longer a multi-hop "
        "reproducer"
    )

    deep = _deep_find_unbatched_per_item_spawns(roots, max_depth=4)
    deep_hit = any(
        site.path == target_path
        and site.enclosing == target_enclosing
        and site.callee == target_callee
        for site in deep
    )
    assert deep_hit, (
        f"expected the depth-4 collector to reach {target_path}:{target_enclosing} -> "
        f"{target_callee} -- if this chain was deleted or restructured, this reproducer must "
        "fail loudly rather than silently pass over an empty set (see module note above)"
    )


@pytest.mark.cadence
def test_worklist_depth_one_rows_are_all_live_gate_sites():
    """C4-FIX, requirement 4: a STANDING (never `designed_red`) pin on the defect this chunk
    fixes -- no row in the advisory worklist may be labelled depth 1 unless its `AmpSite.key` is
    actually in the live gate's own key set (`find_unbatched_per_item_spawns(_gate_scope_paths())`
    with no widening at all). Asserts the SET RELATIONSHIP (subset), never a frozen count: the
    live gate's own site count moves as the corpus does (measured 26 at spike time, already 27
    by the time this fix landed -- see `test_depth_one_key_set_equals_live_gate`'s own note on
    the same drift), so a count assertion here would rot the moment a peer commits. This is the
    test that matters in a year, per the fix brief.
    """
    live_keys = {site.key for site in find_unbatched_per_item_spawns(_gate_scope_paths())}
    rows = _advisory_worklist(max_depth=4)
    depth_one_keys = {row["site"].key for row in rows if row["depth"] == 1}
    assert depth_one_keys <= live_keys, (
        "every depth-1-labelled worklist row must be a real depth-1 gate site -- a row here not "
        "in the live gate's own key set is exactly the fabricated-depth defect this test pins"
    )


@pytest.mark.cadence
def test_worklist_rows_never_carry_a_fabricated_time_unit():
    """C9: a STANDING pin against the defect this chunk fixes -- `reachable_spawn_sites` is a
    reachability COUNT, not milliseconds, and must never be multiplied into a time unit again.
    Measured case: `close_out_and_stamp.py :: _disposition_ref_evidence -> _verify_disposition_ref`
    scored `reachable_spawn_sites = 44` (C8 had rendered this `1113.2ms (floor)`), while the named
    function (`_verify_disposition_ref`, lines 815-852) contains exactly 2 `_run_git` calls -- the
    published millisecond figure overstated the real per-iteration cost by ~22x. This test asserts
    no row dict carries any key matching `.*_ms$` other than `cost_ms`, and that `cost_ms` is
    either `None` or sourced from `_KNOWN_SITE_COST_MS` (never derived from a count).
    """
    rows = _advisory_worklist(max_depth=4)
    known_costs = set(_KNOWN_SITE_COST_MS.values())
    ms_key_re = re.compile(r".*_ms$")
    for row in rows:
        fabricated_ms_keys = [
            key for key in row if key != "cost_ms" and ms_key_re.match(key)
        ]
        assert not fabricated_ms_keys, (
            f"row for {row['site'].key} carries a time-unit-shaped key derived from a "
            f"reachability count: {fabricated_ms_keys} -- see C9's 44-reachable-sites-vs-2-"
            "real-calls measurement in this test's own docstring"
        )
        assert row["cost_ms"] is None or row["cost_ms"] in known_costs, (
            f"row for {row['site'].key} carries a cost_ms not sourced from _KNOWN_SITE_COST_MS"
        )


@pytest.mark.cadence
def test_site_depths_reproduce_the_per_depth_walks():
    """`deep_find_with_site_depths` must reproduce a walk per depth, EXACTLY.

    This is the entire warrant for collapsing the per-depth loop into one walk. If it ever goes
    red, the one-walk shape is not equivalent any more and its consumers are recording depths
    that a real walk at that depth would not agree with -- which, for `_KNOWN_SITES`, means rows
    silently moving between PAST_HORIZON and CLOSURE_CANDIDATE. Fix the attribution or revert the
    consumers to the loop; do NOT relax this to a subset check.

    Deliberately expensive: it pays BOTH shapes (a walk per depth plus the one-walk) so the
    comparison is real rather than a re-derivation of the thing under test. That cost is why it
    is cadence-marked, and why it is the only place that pays it -- the consumers get the cheap
    shape precisely because this test stands behind it.

    Three attribution rules were measured wrong before this one passed; `deep_find_with_site_
    depths`' own docstring records them so the next reader does not re-derive a refuted shape.
    """
    roots = _gate_scope_paths()
    max_depth = 4

    sites, depth_of = deep_find_with_site_depths(roots, max_depth)

    for depth in range(2, max_depth + 1):
        from_loop = frozenset(
            site.key for site in _deep_find_unbatched_per_item_spawns(roots, max_depth=depth)
        )
        from_depths = frozenset(site.key for site in sites if depth_of(site) <= depth)

        assert from_depths == from_loop, (
            f"one-walk depth attribution diverges from a real walk at max_depth={depth}: "
            f"{len(from_depths - from_loop)} site(s) only in the attribution "
            f"({sorted(from_depths - from_loop)[:4]}), "
            f"{len(from_loop - from_depths)} only in the walk "
            f"({sorted(from_loop - from_depths)[:4]})"
        )
