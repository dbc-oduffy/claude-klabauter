"""C-13 (`docs/plans/2026-08-19-every-budgeted-op-counts-its-own-spawns.md`): the mechanism
decision opro-03 shipped for "every budgeted op counts the spawns it actually makes."

THE PROPERTY. *A spawn reachable from a budgeted op's entrypoint must be visible to that op's
counter.* NOT "a budgeted op may not call `subprocess`" -- `discover_working_repos::_sort_unique`
shells to `sort -u` as a *sanctioned* carve-out (`test_no_bash_dependency.py`), and a literal
prohibition would either delete a ruled-on carve-out or accrete an exemption list to survive it.
A legitimate spawn is fine; an UNCOUNTED one is not.

THIS GATE'S OPERATIONAL DEFINITION OF "COUNTED": an enumerated, inline-sentinel-keyed entry in
`_LEGITIMIZED_SITES` below, added only once a site is proven to route through its op's counter
(C6's job) or is a carve-out whose counting is explicitly wired. NOTHING is pre-populated. A
reachable bare-subprocess call that merely happens to fall under a companion test's *global*
`subprocess.run` monkeypatch is NOT treated as "counted" by this gate -- that visibility is a
test-time accident of how that one companion test happens to patch, not a structural guarantee a
future refactor preserves (see `ceremony.scoped_git_commit`'s own counter, which patches
`git_native._git` by function-OBJECT substitution -- narrow by construction, and the one shape a
global-patch accident cannot be assumed to generalize to). This is the plan's own instruction,
read literally: "each survivor names why it is legitimate AND how it is counted" -- today, none
of the ten confirmed-reachable sites the C-08 audit found do the second half, so this gate is
EXPECTED to be red on arrival. Tuning `_LEGITIMIZED_SITES` to make it pass without C6 actually
routing or sanctioning a site is exactly the failure this plan's anti-scope names.

REUSE FROM `spawn_policy`, UNMODIFIED (pinned API, `tasks/shell-spawn-regrowth-gate/
PINNED-API.md`): `sites_in_source`, `is_test_tree_site`, `DEFAULT_EXCLUDE`, `SpawnParseError`,
`discover_source_files`. Not extending `SpawnSite`.

REUSE FROM `coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py`, UNMODIFIED, its
repo-wide cross-module name index: `_FuncIndex`, `_build_func_index`, `_FileRecord`,
`_load_file_records`, `_discover_scope_files`, `_REPO_ROOT`, `_GATE_SCOPE_ROOTS`. That module's
own docstring names its resolution as ONE-HOP ONLY (routes b/c/d/e/f/g each resolve a callee at
most one indirection deep) and explicitly excludes the transitive deep tail, measured there at
32% true-positive repo-wide with no static discriminator separating true from false at any depth.
This gate's whole reason to exist is that C-13 needs the transitive closure that module declined
to build -- so it is reopened here, deliberately, over a MUCH smaller surface (see "SCOPE
RESTRICTION AND ITS MEASURED EFFECT" below).

THE ONE NEW PREDICATE: is a spawn site's enclosing function TRANSITIVELY reachable, through any
number of hops, from one of the nine live budgeted entrypoints (`_BUDGETED_ENTRYPOINTS`)? This
gate builds that reachable-function set itself (`_reachable_functions`, a plain worklist BFS over
a call graph, fixed-point over a finite domain the same way `_FuncIndex.spawn_bearing_params`
terminates) -- reusing the OTHER module's per-file `func_defs` (every top-level function's own AST
node) as the domain, and adding its OWN edge resolution rather than that module's
`imported_names_by_file` + repo-wide `funcs_by_name` pairing (see `_import_function_aliases`'s own
docstring for why: that pairing is safe for a ONE-HOP route that independently re-verifies each
resolved callee's body, and measured UNUSABLE for a transitive BFS that does not -- a single
common-name collision, e.g. two unrelated modules each defining a `_git` helper, opened the entire
unrelated subtree behind whichever one the repo-wide index happened to list first, inflating one
op's reachable set from 0 to roughly 40 on the first run against the live tree). This gate resolves
three edge kinds instead, all PRECISE (pinned to the specific module a name was imported FROM, not
a repo-wide bare-name search): same-module direct calls; `from X import func` resolved to the
exact `X` (`_import_function_aliases`); and a bare `module.attr(...)` attribute call through a
tracked `import X` / `from pkg import X` / `from . import X` module alias (`_import_module_aliases`),
plus the one further hop the C-08 audit's own trace hit by hand (op 5, hop 4:
`_claude_klabauter_root = cli_shared.claude_klabauter_root` -- a module-level local name bound to an imported module's
attribute, then called bare -- `_local_module_attr_aliases`). Deeper relative-import levels
(`from ...pkg import X`, level >= 2) and any alias chain longer than the hops above are accepted
false-negative gaps, not traced.

FUNCTION GRANULARITY, NOT LITERAL MODULE GRANULARITY. The plan phrases the predicate as "is this
MODULE reachable" -- this gate answers a tighter question, "is this specific ENCLOSING FUNCTION
reachable," and treats a spawn site as on-path only when its own top-level enclosing function (not
merely some other function in the same file) is in the reached set. A literal module-level
predicate was tried by hand first and rejected: `ceremony.scoped_git_commit` imports
`git_native` for its one counted seam (`git_native._git`), which would make the WHOLE of
`git_native.py` "reachable" under a module-level reading -- including
`cat_file_batch_objects`/`_hash_object_stdin_bytes`, two sites the C-08 audit traced BY HAND as
unreachable from this op (they belong to a different commit form entirely). Function-granularity
does not report either of those two, because neither function is in the transitively-reached set;
a literal module-level predicate would have reported both as false positives on the very first op
it was tried against. This is a deliberate tightening past the plan's literal wording, not a
loophole -- see the measurement section below for what it does and does not fix.

SCOPE RESTRICTION AND ITS MEASURED EFFECT (per the plan's own instruction: "whether that holds is
itself a measurement, not an assumption"). Restricted to the nine live budgeted entrypoints'
reachable sets, this predicate flags 18 sites on the live tree at HEAD (not 10 -- see below for
why that is a finding, not a bug). HAND-TRACED classification of every one of the 18 (full per-site
call chains in this file's own run-report sidecar):

  - 9 sites match the C-08 audit's confirmed-reachable table exactly, op-for-op, site-for-site:
    `changelog.cited_in_range_count` (1), `percolate.functional_identifier_output_drift_in_tree`
    (1), `bin.coordinator_harvest_deferrals_dedup_scan_root_resolution` (1, its `repo_root.py` hop),
    `bin.reap_integrated_review_findings.tracked_untracked_split` (1),
    `bin.workday_complete_step2_5_dirty_tree.classify_main_pass` (1),
    `execute_plan_assemble.dispatch_ledger_delivered` (2), `ops.discover_working_repos` (1), plus
    one of `execute_plan_assemble.sibling_committed_chunk_ids_memo`'s two (its `_run_git` site).
  - 1 site (`sibling_committed_chunk_ids_memo`'s second hit, `git/repo_root.py::_spawn_rev_parse`)
    was NOT in the audit's own op-9 section but is a real hop the audit's own limits section
    predicted ("a different session re-running this same method could plausibly find 1-3 more
    sites") -- `_sibling_committed_chunk_ids` -> `_committed_chunk_ids` -> `_committed_chunk_shas`
    reaches it 3-4 hops deep, past where the by-hand trace stopped.
  - 8 sites belong to `ceremony.scoped_git_commit`, which the C-08 audit called CLEAN (0 sites) --
    verified by hand-tracing every one back to `_handler`: `_handler` calls
    `run_commit_pipeline` (`commit_pipeline.py`, imported), which directly imports and calls
    `git_native._git` (the op's own declared, intentionally-counted seam -- reachable and correct,
    simply not yet `_LEGITIMIZED_SITES`-entered) and `diverging_paths` (`git/divergence.py`,
    confirmed: its body calls `_run_git` directly), and separately -- past where `run_commit_pipeline`
    itself hands off to a best-effort post-sync helper -- `auto_push.drain_pending_push` (a real,
    confirmed one-hop `module.attr(...)` call at `commit_pipeline.py`'s own local `from
    coordinator_core.hooks import auto_push`), whose body calls `run_push_with_retry`,
    `_branch_resolves_locally`, and `_drain_dead_ref_record`, which in turn reach `push_once`,
    `_run_git`, and `_is_ancestor` (all confirmed by reading each function's own body); plus
    `_handler` ALSO calls `session_scope.release_committed_claims` /
    `release_phantom_claims` directly (both confirmed imported, both confirmed to call git-shaped
    helpers in `session/scope.py` reaching `_git_run`). Every one of these 8 was traced to an
    actual `ast.Call` node, not inferred -- ZERO of them are collision artifacts of the precise
    (per-import-pinned) resolution this gate uses.

MEASURED FALSE-POSITIVE RATE: 0 of 18 (every flagged site traced to a real call chain). This IS a
genuine result, not a tuned one, and it is a NARROWER claim than "the technique has no false
positives": it is a measurement of THIS run, over nine entrypoints' traced reachable sets, using
the precise (not repo-wide-by-name) resolution this file ships. The finding worth carrying forward
is not the zero -- it is that transitive analysis found MORE real reachability than the manual
one-hop-per-op audit did, on the op the audit was most confident about (`ceremony.scoped_git_commit`,
called "CLEAN" there). That is the mechanism working as designed, not a defect in the C-08
baseline -- a by-hand DFS that stopped at "no direct site in `scoped_git_commit.py` itself, and
`git_native.py`'s two OTHER sites are unreachable" never followed `run_commit_pipeline`'s own
fan-out into `auto_push.py`. Restated per the plan's own instruction: scope restriction was not
assumed to fix the false-positive problem; it was traced, hop by hop, and on this run every flagged
site held up.

WHAT THIS GATE DELIBERATELY DOES NOT CATCH (negative spec):
  - A callee reached only through an alias chain longer than two hops (module import ->
    module-level local-name-to-attribute binding), a `lambda`, a `getattr`/dynamic dispatch, or a
    relative import at level >= 2. Matches the reused module's own bare-`Name`-only, false-
    negative-biased restriction.
  - A spawn inside a function that is itself reachable, but where the spawn call is gated behind a
    literal condition this gate does not evaluate (e.g. discriminator 5's verb-gated chokepoints in
    the sibling module) -- this gate reports every spawn site whose ENCLOSING function is reached,
    with no attempt to determine whether the specific branch containing the spawn is live on any
    particular call. A verb-gated chokepoint that never spawns for the verbs a budgeted op actually
    uses will still be reported; narrowing that is left to a future pass, matching this module's
    stated false-negative-over-false-positive preference in the other direction (it would rather
    over-report a site than silently drop one whose gating it cannot prove).
  - Whether a flagged site is ACTUALLY invisible to its op's counter at runtime -- this gate does
    not read or execute any companion test's patch target. It answers a structural reachability
    question and lets `_LEGITIMIZED_SITES` carry the human judgment call (mirroring
    `test_no_unbatched_per_item_git_spawn.py`'s own `_EXEMPT_SITES` model) for a site once C6 has
    made it legitimately-and-provably counted. Until then, a global-`subprocess.run`-patched
    companion test's incidental coverage is NOT treated as "counted" -- see "THIS GATE'S
    OPERATIONAL DEFINITION" above for why.
  - The `coverage.diagnose_open_review_loop_dag_mode` manifest row. Its subject
    (`coverage._diagnose_open_review_loop`) is unreachable dead code (EM-verified addendum,
    `state/audits/2026-08-19-opro-03-c08-budgeted-op-spawn-trace.md`) with no registered op and no
    live caller -- it is not one of the nine LIVE entrypoints this gate enforces, and removing that
    row/function is a different chunk's (C1/C2/C4) job. `_BUDGETED_ENTRYPOINTS` omits it on
    purpose, not by oversight.

ENTRYPOINT REGISTRY ROT GUARD. `_BUDGETED_ENTRYPOINTS` is a hardcoded (relpath, function-name)
mapping -- explicit by necessity, since a manifest key names a call SHAPE
(`bin.workday_complete_step2_5_dirty_tree.classify_main_pass`), not always an importable symbol.
"Do not hardcode a list of entrypoints that silently rots" (the brief's own words) is answered by
`test_budgeted_entrypoints_resolve_to_live_functions`: every entry must resolve to an actual
top-level function definition in the reused index's `func_defs`, checked fresh on every run, never
trusted from a comment. A row whose entrypoint no longer exists fails LOUDLY, the same defect class
C2 closes for the manifest's own orphan-row gap (a different file, not touched here).

EXEMPTION MODEL. `_LEGITIMIZED_SITES`, keyed on `(relpath, enclosing, argv0, ordinal)` -- the
SAME four-tuple `spawn_policy.site_key` uses for site identity, deliberately excluding `lineno`
(a `file:line` pin rots on the next edit above it, exactly the defect class
`test_no_unbatched_per_item_git_spawn.py`'s own exemption register calls out by name). No pragma
comment escape hatch, matching that module's own stance: a structural exemption a call site could
opt out of by comment would let this class regrow behind a comment, the inverse of what this gate
exists to end.

RE-ENTRANCY. This file's own path (`coordinator_core/tests/test_no_uncounted_spawn_on_budgeted_
path.py`) is excluded from every real scan by the REUSED `is_test_tree_site` (a `test_*` basename,
same rule that excludes every other gate module in this tree) -- inherited, not re-derived; this
module does not repeat `test_no_unbatched_per_item_git_spawn.py`'s own loud self-scan sentinel
because it does not re-implement `_discover_scope_files`, it imports the one copy that already
carries that guard.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

from coordinator_core.tests.test_no_unbatched_per_item_git_spawn import (
    _GATE_SCOPE_ROOTS,
    _REPO_ROOT,
    _FileRecord,
    _FuncIndex,
    _build_func_index,
    _discover_scope_files,
    _load_file_records,
)

#: The nine LIVE `spawn_count_budget` rows in `coordinator_core/benchmarks/budget-manifest.json`,
#: manifest key -> (relpath, tuple-of-top-level-entrypoint-function-names). Resolved and verified
#: against `state/audits/2026-08-19-opro-03-c08-budgeted-op-spawn-trace.md`'s per-op sections and
#: cross-checked against each row's own companion test (which function it actually exercises, not
#: the manifest key's prose shape) -- see this file's own run-report sidecar for the per-row
#: verification. The tenth manifest row, `coverage.diagnose_open_review_loop_dag_mode`, is
#: deliberately omitted -- see module docstring's negative spec.
#:
#: `ops.discover_working_repos` carries TWO entrypoints (`main()` calls both) -- the audit traced
#: both as independently reachable-with-a-spawn.
_BUDGETED_ENTRYPOINTS: dict[str, tuple[str, tuple[str, ...]]] = {
    "ceremony.scoped_git_commit": (
        "coordinator_core/ops/ceremony/scoped_git_commit.py",
        ("_handler",),
    ),
    "changelog.cited_in_range_count": (
        "coordinator_core/ops/changelog_ops.py",
        ("_cited_in_range_count",),
    ),
    "percolate.functional_identifier_output_drift_in_tree": (
        "coordinator_core/percolate/store.py",
        ("find_functional_identifier_output_drift_in_tree",),
    ),
    "bin.coordinator_harvest_deferrals_dedup_scan_root_resolution": (
        "coordinator/bin/coordinator-harvest-deferrals.py",
        ("_candidate_search_dirs",),
    ),
    "bin.reap_integrated_review_findings.tracked_untracked_split": (
        "coordinator/bin/reap-integrated-review-findings.py",
        ("_reap_integrated_legacy",),
    ),
    "bin.workday_complete_step2_5_dirty_tree.classify_main_pass": (
        "coordinator_core/ops/workday_complete_step2_5_dirty_tree.py",
        ("_classify_main_pass",),
    ),
    "execute_plan_assemble.dispatch_ledger_delivered": (
        "coordinator_core/execute_plan_assemble/close_out_and_stamp.py",
        ("_dispatch_ledger_delivered",),
    ),
    "execute_plan_assemble.sibling_committed_chunk_ids_memo": (
        "coordinator_core/execute_plan_assemble/close_out_and_stamp.py",
        ("_sibling_committed_chunk_ids",),
    ),
    "ops.discover_working_repos": (
        "coordinator_core/ops/discover_working_repos.py",
        ("_tier_a5", "_publish_mirror_keys"),
    ),
}

#: Sites PROVEN legitimately counted -- see module docstring's "EXEMPTION MODEL". C6's worklist
#: (opro-03, `docs/plans/2026-08-19-every-budgeted-op-counts-its-own-spawns.md`) drains this set
#: one entry at a time, each earning its place by ROUTING evidence, not by a companion test's
#: incidental visibility. Do NOT add an entry here because a companion test's global
#: `subprocess.run` patch happens to also see the call -- that is not this gate's definition of
#: counted (module docstring, "THIS GATE'S OPERATIONAL DEFINITION"); the two entries below were
#: each checked against that bar and hold up. 13 of 15 distinct sites remain unlegitimized (16 of
#: 18 (op, site) pairs) -- see `state/subagent-share/7a4959ac-9247-439f-b7e2-d462e0608725/
#: coordinatorexecutor-3400b29c.md` for the full per-site drain evidence, run 2026-08-19.
_LEGITIMIZED_SITES: set[tuple[str, str, str, int]] = {
    (
        "coordinator_core/ops/ceremony/git_native.py",
        "_git._invoke",
        "<dynamic>",
        0,
    ),
    # `ceremony.scoped_git_commit`'s counting seam itself. `test_commit_e2e_spawn_budget.py::
    # _count_op_git_calls` substitutes the FUNCTION OBJECT `git_native._git` (`git_native._git =
    # _wrapper`, not a `subprocess.run` module-attribute patch) before invoking the op -- every
    # call to `git_native._git(...)`, whatever `_invoke`'s own body does internally, routes
    # through the substituted name first. This is not a call INTO the seam that the seam then
    # hides from the counter; it IS the seam, so it is counted by construction. Budget shape:
    # `overrides["ceremony.scoped_git_commit"].spawn_count_budget.green_path` (and siblings) in
    # `budget-manifest.json`.
    (
        "coordinator/bin/reap-integrated-review-findings.py",
        "_git",
        "git",
        0,
    ),
    # `bin.reap_integrated_review_findings.tracked_untracked_split`'s sole spawn seam.
    # `coordinator/tests/test_reap_integrated_review_findings_spawn_budget.py` substitutes the
    # module's own FUNCTION OBJECT (`mod._git = _counting_git`, wrapping `orig_git = mod._git`),
    # narrow by construction like the seam above -- not a `subprocess.run` module-attribute
    # patch. `spawn_policy.sites_in_source` finds exactly one spawn site in this whole file
    # (`_git`), and every git operation the module makes (`ls-files`, `rm`, `commit`) routes
    # through it, so wrapping the name wraps the site. Budget shape:
    # `overrides["bin.reap_integrated_review_findings.tracked_untracked_split"].spawn_count_budget.
    # per_reap_call` in `budget-manifest.json`.
}


def _module_dotted_name(relpath: str) -> str | None:
    """`coordinator_core/a/b.py` -> `"coordinator_core.a.b"`. `None` for anything outside
    `coordinator_core` (in particular every `coordinator/bin/*.py` script, which is never a real
    importable dotted module in this repo -- those files are loaded via `SourceFileLoader`, not
    `import`, so they can never be the TARGET of a resolved import alias below; they can still be
    a BFS start point, since `func_defs` indexes them regardless of importability)."""
    if not relpath.startswith("coordinator_core/") or not relpath.endswith(".py"):
        return None
    return relpath[:-3].replace("/", ".")


def _module_index(records: list[_FileRecord]) -> dict[str, str]:
    """dotted module name -> relpath, over every in-scope `coordinator_core` file."""
    out: dict[str, str] = {}
    for record in records:
        dotted = _module_dotted_name(record.relpath)
        if dotted:
            out[dotted] = record.relpath
    return out


def _package_dotted(relpath: str) -> str | None:
    dotted = _module_dotted_name(relpath)
    if dotted is None:
        return None
    parts = dotted.split(".")
    return ".".join(parts[:-1])


def _import_module_aliases(record: _FileRecord, module_index: dict[str, str]) -> dict[str, str]:
    """alias name -> relpath of an in-scope module, for `import X`, `from pkg import X`, and
    `from . import X` (level-0 and level-1 relative only -- deeper relative levels are a named,
    accepted false-negative gap, see module docstring)."""
    out: dict[str, str] = {}
    pkg = _package_dotted(record.relpath)
    for node in ast.walk(record.tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 0:
                base = node.module or ""
            elif node.level == 1 and pkg is not None:
                base = pkg if not node.module else f"{pkg}.{node.module}"
            else:
                continue
            for alias in node.names:
                dotted = f"{base}.{alias.name}" if base else alias.name
                target = module_index.get(dotted)
                if target:
                    out[alias.asname or alias.name] = target
        elif isinstance(node, ast.Import):
            for alias in node.names:
                target = module_index.get(alias.name)
                if target:
                    out[alias.asname or alias.name.split(".")[0]] = target
    return out


def _import_function_aliases(
    record: _FileRecord,
    module_index: dict[str, str],
    func_defs: dict[tuple[str, str], object],
) -> dict[str, tuple[str, str]]:
    """alias name -> `(relpath, func_name)`, for `from X import func` where `func` is a
    top-level FUNCTION defined in X (not a submodule -- that shape is `_import_module_aliases`'s
    job). PRECISE by construction: pinned to the specific module named in the import statement,
    never a repo-wide bare-name lookup.

    This is deliberately NOT the reused index's `imported_names_by_file` +
    repo-wide `funcs_by_name` combination -- that pairing answers "was this name imported from
    SOME module" and "does ANY same-named function anywhere directly spawn," which the one-hop
    routes can afford (each hop still independently re-verifies the resolved function's own body
    via `sites_in_source` before counting a route). This gate does NOT re-verify per hop -- it
    walks the resolved callee's own body for FURTHER calls -- so a same-named-function collision
    at one hop would silently open up that function's ENTIRE unrelated reachable subtree.
    Measured on the first run against the live tree: using the imprecise pairing here inflated
    `ceremony.scoped_git_commit` alone from 0 sites (matching the audit's own CLEAN finding) to
    roughly 40, by resolving common generic names (`_git`, `_run`) to whichever of the dozens of
    unrelated modules defining a same-named helper the repo-wide index happened to list first.
    Pinning resolution to the specific import statement's own named module removed that collision
    class entirely -- see module docstring's measured false-positive section."""
    out: dict[str, tuple[str, str]] = {}
    pkg = _package_dotted(record.relpath)
    for node in ast.walk(record.tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level == 0:
            base = node.module or ""
        elif node.level == 1 and pkg is not None:
            base = pkg if not node.module else f"{pkg}.{node.module}"
        else:
            continue
        target_relpath = module_index.get(base)
        if not target_relpath:
            continue
        for alias in node.names:
            if (target_relpath, alias.name) in func_defs:
                out[alias.asname or alias.name] = (target_relpath, alias.name)
    return out


def _local_module_attr_aliases(
    record: _FileRecord,
    import_aliases: dict[str, str],
    func_defs: dict[tuple[str, str], object],
) -> dict[str, tuple[str, str]]:
    """Module-level `name = alias.attr` bindings (C-08 audit op 5, hop 4:
    `_claude_klabauter_root = cli_shared.claude_klabauter_root`), where `alias` resolves to an in-scope module and
    `attr` is one of that module's own top-level functions. One further hop past
    `_import_module_aliases` alone, matching what the audit's own hand trace needed."""
    out: dict[str, tuple[str, str]] = {}
    for node in record.tree.body:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
        ):
            continue
        target_relpath = import_aliases.get(node.value.value.id)
        attr = node.value.attr
        if target_relpath and (target_relpath, attr) in func_defs:
            out[node.targets[0].id] = (target_relpath, attr)
    return out


def _direct_call_targets(
    func_node,
    relpath: str,
    index: _FuncIndex,
    import_aliases_by_file: dict[str, dict[str, str]],
    func_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
    local_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
) -> set[tuple[str, str]]:
    """Every top-level `(relpath, func_name)` `func_node`'s body calls, one hop: same-module
    direct call, a PRECISE cross-module function import (`_import_function_aliases` -- pinned to
    the specific module named in the `from X import name` statement, never a repo-wide bare-name
    lookup; see that function's own docstring for why the imprecise version is unusable here),
    a module-level local-name-to-attribute alias (`_local_module_attr_aliases`), or a direct
    `module.attr(...)` attribute call through a tracked module-import alias
    (`_import_module_aliases`)."""
    out: set[tuple[str, str]] = set()
    aliases_here = import_aliases_by_file.get(relpath, {})
    func_aliases_here = func_aliases_by_file.get(relpath, {})
    local_here = local_aliases_by_file.get(relpath, {})
    for node in ast.walk(func_node):
        if node is func_node or not isinstance(node, ast.Call):
            continue
        callee = node.func
        if isinstance(callee, ast.Name):
            name = callee.id
            if (relpath, name) in index.func_defs:
                out.add((relpath, name))
            elif name in local_here:
                out.add(local_here[name])
            elif name in func_aliases_here:
                out.add(func_aliases_here[name])
        elif isinstance(callee, ast.Attribute) and isinstance(callee.value, ast.Name):
            target_relpath = aliases_here.get(callee.value.id)
            if target_relpath and (target_relpath, callee.attr) in index.func_defs:
                out.add((target_relpath, callee.attr))
    return out


def _reachable_functions(
    entry_funcs: set[tuple[str, str]],
    index: _FuncIndex,
    import_aliases_by_file: dict[str, dict[str, str]],
    func_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
    local_aliases_by_file: dict[str, dict[str, tuple[str, str]]],
) -> set[tuple[str, str]]:
    """Transitive closure (plain worklist BFS) over `_direct_call_targets`'s one-hop edges,
    seeded at `entry_funcs`. Terminates: `seen` grows monotonically over the finite domain of
    `(relpath, func_name)` pairs `index.func_defs` defines, so a round that adds nothing halts
    the loop -- same termination argument the reused module's own route-g fixed point makes."""
    seen: set[tuple[str, str]] = set(entry_funcs)
    queue: list[tuple[str, str]] = list(entry_funcs)
    while queue:
        current = queue.pop()
        node = index.func_defs.get(current)
        if node is None:
            continue
        for target in _direct_call_targets(
            node,
            current[0],
            index,
            import_aliases_by_file,
            func_aliases_by_file,
            local_aliases_by_file,
        ):
            if target not in seen:
                seen.add(target)
                queue.append(target)
    return seen


def _on_path_spawn_sites(
    reached_funcs: set[tuple[str, str]],
    spawn_sites_by_file: dict[str, list],
    exempt: set[tuple[str, str, str, int]],
) -> list:
    """Every `spawn_policy` site whose own top-level enclosing function is in `reached_funcs`,
    minus `_LEGITIMIZED_SITES`. Function granularity, not whole-file -- see module docstring's
    "FUNCTION GRANULARITY" section for why."""
    reached_names_by_file: dict[str, set[str]] = {}
    for relpath, name in reached_funcs:
        reached_names_by_file.setdefault(relpath, set()).add(name)
    out = []
    for relpath, names in reached_names_by_file.items():
        for site in spawn_sites_by_file.get(relpath, []):
            top_enclosing = site.enclosing.split(".")[0]
            if top_enclosing not in names:
                continue
            key = (relpath, site.enclosing, site.argv0, site.ordinal)
            if key in exempt:
                continue
            out.append(site)
    return out


def _scope_roots() -> tuple[pathlib.Path, ...]:
    return tuple(_REPO_ROOT / root for root in _GATE_SCOPE_ROOTS)


def _build_corpus():
    """One shared corpus build: scope files, `_FileRecord`s, the reused repo-wide `_FuncIndex`,
    and this gate's own import/local-alias indexes, each computed exactly once. Returns
    `(index, spawn_sites_by_file, import_aliases_by_file, func_aliases_by_file,
    local_aliases_by_file)`."""
    files = _discover_scope_files(_scope_roots())
    records = _load_file_records(files)
    index = _build_func_index(records)
    module_index = _module_index(records)

    import_aliases_by_file: dict[str, dict[str, str]] = {}
    for record in records:
        import_aliases_by_file[record.relpath] = _import_module_aliases(record, module_index)

    func_aliases_by_file: dict[str, dict[str, tuple[str, str]]] = {}
    for record in records:
        func_aliases_by_file[record.relpath] = _import_function_aliases(
            record, module_index, index.func_defs
        )

    local_aliases_by_file: dict[str, dict[str, tuple[str, str]]] = {}
    for record in records:
        local_aliases_by_file[record.relpath] = _local_module_attr_aliases(
            record, import_aliases_by_file[record.relpath], index.func_defs
        )

    spawn_sites_by_file = {record.relpath: record.spawn_sites for record in records}
    return (
        index,
        spawn_sites_by_file,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    )


def _format_violation(op_key: str, site) -> str:
    return (
        f"  [{op_key}] {site.path}:{site.lineno} enclosing={site.enclosing!r} "
        f"argv0={site.argv0!r} ordinal={site.ordinal}"
    )


def test_budgeted_entrypoints_resolve_to_live_functions():
    """Registry-rot guard: every `_BUDGETED_ENTRYPOINTS` row must resolve to an actual top-level
    function definition, checked fresh against the live tree -- never trusted from a comment. A
    row whose subject no longer exists fails loudly here, the same defect class C2 closes for the
    manifest's own orphan-row gap on a different file."""
    index, _sites, _imp, _func_imp, _loc = _build_corpus()
    missing = []
    for op_key, (relpath, func_names) in _BUDGETED_ENTRYPOINTS.items():
        for func_name in func_names:
            if (relpath, func_name) not in index.func_defs:
                missing.append(f"{op_key}: {relpath}::{func_name}")
    assert not missing, (
        "budgeted entrypoint(s) no longer resolve to a live top-level function -- "
        "the manifest row's subject moved, was renamed, or was deleted:\n"
        + "\n".join(missing)
    )


@pytest.mark.designed_red
def test_no_uncounted_spawn_reachable_from_a_budgeted_entrypoint():
    """The C-13 gate. For each of the nine live budgeted entrypoints, every `spawn_policy`-
    detected spawn site whose enclosing function is transitively reachable from that entrypoint
    must carry a `_LEGITIMIZED_SITES` entry. EXPECTED RED on arrival -- 18 reachable sites exist
    with none legitimized yet (module docstring, "THIS GATE'S OPERATIONAL DEFINITION"); this is
    deliberate, not a bug in the gate.

    `designed_red` BY EM DECISION, not by the authoring agent: this lands as a standing,
    non-gating worklist rather than a tier-breaking failure, exactly as
    `test_no_unbatched_per_item_git_spawn.py` split its own collector (G1) from its assertions
    (G2) across waves. The 18 sites are opro-03 C6's work-list; each one leaves this list by
    being routed through its op's counter or by earning a `_LEGITIMIZED_SITES` entry that says
    how it IS counted. When the list empties, this marker comes off and the gate becomes
    standing -- that removal is the definition of C6 being done, and it must not be done by
    populating `_LEGITIMIZED_SITES` wholesale to buy a green.

    The three tests around this one are NOT `designed_red` and gate normally: the entrypoint
    registry must resolve, and the planted-fixture RED/GREEN pair must hold. Those are what
    prove this module still works while its live-tree list is non-empty."""
    (
        index,
        spawn_sites_by_file,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    ) = _build_corpus()

    all_violations: list[tuple[str, object]] = []
    for op_key, (relpath, func_names) in _BUDGETED_ENTRYPOINTS.items():
        entry_funcs = {(relpath, func_name) for func_name in func_names}
        reached = _reachable_functions(
            entry_funcs,
            index,
            import_aliases_by_file,
            func_aliases_by_file,
            local_aliases_by_file,
        )
        for site in _on_path_spawn_sites(reached, spawn_sites_by_file, _LEGITIMIZED_SITES):
            all_violations.append((op_key, site))

    assert not all_violations, (
        f"{len(all_violations)} spawn site(s) reachable from a budgeted entrypoint are not "
        "visible to that op's counter (no _LEGITIMIZED_SITES entry):\n"
        + "\n".join(_format_violation(op_key, site) for op_key, site in all_violations)
    )


# --------------------------------------------------------------------------
# Planted-fixture self-tests -- prove the transitive predicate on a synthetic
# corpus, independent of the live tree's own current pass/fail state.
# --------------------------------------------------------------------------


def test_plant_multi_hop_spawn_is_flagged_red_then_removed_is_green(tmp_path):
    """Proves the ONE thing this gate does that the reused one-hop index cannot: a spawn reached
    only through a THREE-hop chain (entry -> local helper -> imported cross-module function ->
    module-import-alias attribute call) is flagged. RED with the spawn planted, GREEN once
    removed -- the module docstring's own non-negotiable ("prove the gate bites")."""
    entry_mod = tmp_path / "entry.py"
    helper_mod = tmp_path / "helper.py"
    wrapper_mod = tmp_path / "wrapper.py"

    entry_mod.write_text(
        "import helper\n"
        "\n"
        "def _entry(rows):\n"
        "    return helper.stage(rows)\n",
        encoding="utf-8",
    )
    helper_mod.write_text(
        "import wrapper\n"
        "\n"
        "def stage(rows):\n"
        "    return _local_hop(rows)\n"
        "\n"
        "def _local_hop(rows):\n"
        "    return wrapper.run_git(rows)\n",
        encoding="utf-8",
    )
    # PLANT: a bare spawn three hops from `_entry` (entry -> stage -> _local_hop -> run_git).
    wrapper_mod.write_text(
        "import subprocess\n"
        "\n"
        "def run_git(rows):\n"
        "    return subprocess.run(['git', 'status'], cwd='/repo')\n",
        encoding="utf-8",
    )

    files = _discover_scope_files((tmp_path,))
    records = _load_file_records(files)
    index = _build_func_index(records)
    module_index = _module_index_for_test(records)
    import_aliases_by_file = {
        r.relpath: _import_module_aliases(r, module_index) for r in records
    }
    func_aliases_by_file = {
        r.relpath: _import_function_aliases(r, module_index, index.func_defs) for r in records
    }
    local_aliases_by_file = {
        r.relpath: _local_module_attr_aliases(r, import_aliases_by_file[r.relpath], index.func_defs)
        for r in records
    }
    spawn_sites_by_file = {r.relpath: r.spawn_sites for r in records}

    reached = _reachable_functions(
        {("entry.py", "_entry")},
        index,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    )
    assert ("wrapper.py", "run_git") in reached, (
        "transitive BFS did not reach the 3-hop callee -- the predicate this gate exists to add "
        "is not working"
    )
    violations = _on_path_spawn_sites(reached, spawn_sites_by_file, set())
    assert len(violations) == 1
    assert violations[0].path == "wrapper.py"
    assert violations[0].enclosing == "run_git"

    # REMOVE: neutralize the spawn, prove the same predicate goes green.
    wrapper_mod.write_text(
        "def run_git(rows):\n"
        "    return len(rows)\n",
        encoding="utf-8",
    )
    files2 = _discover_scope_files((tmp_path,))
    records2 = _load_file_records(files2)
    index2 = _build_func_index(records2)
    module_index2 = _module_index_for_test(records2)
    import_aliases_by_file2 = {
        r.relpath: _import_module_aliases(r, module_index2) for r in records2
    }
    func_aliases_by_file2 = {
        r.relpath: _import_function_aliases(r, module_index2, index2.func_defs) for r in records2
    }
    local_aliases_by_file2 = {
        r.relpath: _local_module_attr_aliases(r, import_aliases_by_file2[r.relpath], index2.func_defs)
        for r in records2
    }
    spawn_sites_by_file2 = {r.relpath: r.spawn_sites for r in records2}
    reached2 = _reachable_functions(
        {("entry.py", "_entry")},
        index2,
        import_aliases_by_file2,
        func_aliases_by_file2,
        local_aliases_by_file2,
    )
    violations2 = _on_path_spawn_sites(reached2, spawn_sites_by_file2, set())
    assert violations2 == []


def _module_index_for_test(records: list[_FileRecord]) -> dict[str, str]:
    """`_module_index` is restricted to `coordinator_core/...` relpaths (module docstring's
    `_module_dotted_name`), which a bare `tmp_path` fixture (relpaths like `"entry.py"`,
    `"wrapper.py"`) never matches. This test-only variant maps every discovered file's own
    basename-minus-`.py` as its dotted name, matching the flat single-directory fixture shape the
    self-test above builds (`import helper` / `import wrapper` at top level, no package)."""
    out: dict[str, str] = {}
    for record in records:
        if record.relpath.endswith(".py"):
            out[record.relpath[:-3]] = record.relpath
    return out


def test_legitimized_site_suppresses_a_reachable_violation(tmp_path):
    """`_LEGITIMIZED_SITES` (module docstring's "EXEMPTION MODEL") suppresses a specific,
    key-matched site without weakening the predicate for anything else."""
    entry_mod = tmp_path / "entry2.py"
    entry_mod.write_text(
        "import subprocess\n"
        "\n"
        "def _entry(rows):\n"
        "    return subprocess.run(['git', 'status'])\n",
        encoding="utf-8",
    )
    files = _discover_scope_files((tmp_path,))
    records = _load_file_records(files)
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
        {("entry2.py", "_entry")},
        index,
        import_aliases_by_file,
        func_aliases_by_file,
        local_aliases_by_file,
    )
    unfiltered = _on_path_spawn_sites(reached, spawn_sites_by_file, set())
    assert len(unfiltered) == 1
    site = unfiltered[0]
    key = (site.path, site.enclosing, site.argv0, site.ordinal)
    filtered = _on_path_spawn_sites(reached, spawn_sites_by_file, {key})
    assert filtered == []
