"""The shared-git-runner gate (G7): no NEW module may define its own git
runner, and no NEW module-private number may bound a git spawn.

WHAT THIS GATE IS FOR. `docs/problems/2026-08-21-the-over-budget-timeout-
hitlist.md` groups every over-budget timeout in this tree into twelve root
causes and then names G7 as the one that is not a cost at all: it is the
mechanism by which the numbers the other eleven fix GROW BACK. 60+ non-test
modules each define a private `_run_git`, and ~90 module-private git-timeout
constants exist across the values 2.0, 5, 10, 15, 20, 30, 60, 120, 180, 300,
600, 900, 1800 and 3600. Fix G1-G6 without this gate and the 61st module to
need a git read types 30 again -- not out of carelessness, but because there
is nothing to reach for and nothing that objects.

`coordinator_core/git/run.py` is the thing to reach for. THIS is the thing
that objects.

THE TWO SITE KINDS, and why they are one gate. A module that spawns git
itself and a module that carries its own number for that spawn are the same
defect seen from two angles: the private runner is where the number gets a
home, and the number is what makes the private runner look justified. They
ratchet together -- a module migrated onto `git.run` loses both at once,
which is why the dial register is DERIVED from the runner register (a dial
only counts when its module also spawns git) rather than being an
independent sweep of every `TIMEOUT` constant in the tree.

  - RUNNER (`_GRANDFATHERED_RUNNER_MODULES`) -- a non-test module that
    reaches a spawn API with a `["git", ...]` argv, directly or through a
    generic local runner that spawns one of its own parameters (the
    `_git(args) -> _run(["git", *args]) -> subprocess.run(cmd)` shape
    `bash_guards/_branch_set.py` shipped until this gate landed).
  - DIAL (`_GRANDFATHERED_DIALS`) -- a number that BOUNDS one of those
    spawns: a module-level numeric constant passed as `timeout=` to a git
    spawn or to a local function that reaches one, or a numeric-literal
    `timeout` parameter default on such a function.

HOW IT RATCHETS. Both registers are frozen inventories and the assertion is
a SUBSET check (`observed <= frozen`), not `observed == []` -- blocked on
volume, exactly as `test_no_unbatched_per_item_git_spawn.py :: _KNOWN_SITES`
was, and green at land for the same reason. It bites immediately on any site
outside the inventory. The property that makes it a RATCHET rather than a
snapshot is `_PINNED_RUNNER_CEILING` / `_PINNED_DIAL_CEILING`: independent
second copies of each register's size, deliberately literals rather than
`len()` of the register they guard, since importing the value under test
would make this file agree with any register whatsoever and assert nothing.
That is `test_ceremony_budget_ratchet.py :: PINNED_CEILING_SECS`'s shape,
chosen so that ADDING a grandfather entry costs exactly what RAISING A
BUDGET costs: two edits, visible in one diff, arguing for regrowth in
writing. Removing entries needs neither.

The amplification gate's inventory went 149 -> 94 -> 14 exactly this way.
This one starts far larger because it counts modules, not loops.

KNOWN BLIND SPOTS, false-negative-biased, matching every sibling gate's
stated preference -- a gate that over-fires gets disabled, and a gate that
under-fires still holds the line it does see:
  - `shell=True` with a `"git ..."` STRING is invisible. The argv-list form
    is what the collector keys on. A shell string is separately forbidden by
    the shell-out carve-out list, so this is a gap in two gates at once
    rather than an escape hatch in one.
  - A git binary reached through a variable (`git_exe = shutil.which("git")`,
    `[git_exe, "status"]`) is invisible: the first element is not the
    literal `"git"`.
  - Cross-MODULE indirection is invisible. A module importing another
    module's private `_run_git` is not counted here; the DEFINING module
    already is, and counting the importer too would double-charge one defect.
  - A dial reaching its spawn through more than one hop of local binding
    (`bound = _GIT_TIMEOUT; _run_git(args, timeout=bound)`) is invisible.
    One-hop resolution only, matching `spawn_policy`'s own scope.

Negative-spec -- what this module does NOT assert:
  - It does NOT assert any module's git call COMPLETES inside the bound.
    That is a latency property with its own per-op measurements; this file
    is a structural gate over source text and spawns no processes.
  - It does NOT forbid spawning git. `coordinator_core/git/run.py` spawns
    git and is exempt BY NAME (`_PRIMITIVE_MODULE`) -- a seam has to be
    allowed to do the thing it is a seam for.
  - It does NOT forbid passing `timeout=` to `run_git`. That argument is
    narrow-only by construction (`git.run._resolve_budget`), so a call site
    can ask for less time and cannot ask for more; it is not a dial and is
    not counted as one.
  - It does NOT police non-git timeouts. A module that spawns `mypy` for 120
    seconds is G9's problem, not this gate's, and widening here would make
    the registers meaningless.
  - It does NOT scan test trees. Fixtures build synthetic git argv on
    purpose, and a gate that flags its own siblings' fixtures teaches
    everyone to suppress it.

Spec backlink: docs/problems/2026-08-21-the-over-budget-timeout-hitlist.md § G7
Decision backlink: docs/decisions/DR-349-one-budget-governs-every-constructed-op.md
Decision backlink: docs/decisions/DR-348-the-ceremony-budget-is-a-ratchet.md
Model: coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from coordinator_core.git import run as git_run
from coordinator_core.spawn_policy import is_test_tree_site
from coordinator_core.spawn_policy.detect import DEFAULT_EXCLUDE, discover_source_files
from coordinator_core.spawn_policy.detect import _RECOGNIZED as _SPAWN_TARGETS

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_THIS_FILE = pathlib.Path(__file__).resolve()

#: Leaf function names of every spawn call `spawn_policy.detect` recognizes.
#: DERIVED from that module's own table rather than copied into a literal
#: here, which is the opposite choice to the sibling amplification gate's
#: `_SPAWN_API_NAMES`. The difference is deliberate and narrow: that gate
#: keeps a copy because it pins the copy with its own test, and this
#: collector consumes only the LEAF NAME half of each entry, where a stale
#: copy would not fail loudly -- it would silently stop seeing a spawn verb,
#: and a gate that quietly under-detects is worse than one that reaches for a
#: private name in the module it already imports two public helpers from.
_SPAWN_API_NAMES: frozenset[str] = frozenset(attr for _module, attr in _SPAWN_TARGETS)

#: The seam itself. Exempt by name, not by pattern -- see the negative spec.
_PRIMITIVE_MODULE = "coordinator_core/git/run.py"

#: Same roots the amplification gate scans, plus `coordinator/lib`: the G7
#: census found the identical defect on both sides of the core/CLI seam
#: (`percolate-round.py`'s inherited 600 and `workday_ceremony_lib.py :: git()`'s
#: inherited 300 are the CLI half), so scoping this to `coordinator_core`
#: would gate the smaller side of one problem.
_GATE_SCOPE_ROOTS: tuple[str, ...] = ("coordinator_core", "coordinator/bin", "coordinator/lib")

#: Keyword names that carry an argv at a spawn call site.
_ARGV_KEYWORDS: frozenset[str] = frozenset({"args", "argv", "cmd", "program_args"})


@dataclasses.dataclass(frozen=True)
class GitSpawnSite:
    """One module's git-spawn site. `enclosing` is carried for the failure
    message only -- the register keys on MODULE, because a module is the unit
    a migration moves: it stops defining a runner all at once, or it has not
    migrated. Keying on the function would churn the register on every
    rename and would let a module retire one of five sites and look like
    progress."""

    module: str
    enclosing: str


def _leaf_name(func: ast.expr) -> "str | None":
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def _carries_git_argv(expr: ast.expr) -> bool:
    """True if `expr` contains a `["git", ...]` (or tuple) literal anywhere.

    Walks rather than matching the node directly, because the argv is
    routinely a composition: `["git", "-C", root] + args`, `["git", *args]`,
    `["git", "log"] + list(paths)`. All three carry the same literal head."""
    for node in ast.walk(expr):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            head = node.elts[0]
            if isinstance(head, ast.Constant) and head.value == "git":
                return True
    return False


def _argv_exprs(call: ast.Call) -> list:
    out = []
    if call.args:
        out.append(call.args[0])
    for kw in call.keywords:
        if kw.arg in _ARGV_KEYWORDS:
            out.append(kw.value)
    return out


def _enclosing_names(tree: ast.Module) -> dict:
    """Map every AST node's id to the name of its nearest enclosing function
    (`"<module>"` at module scope). Built by an explicit descent rather than
    `ast.walk`, because `walk` loses the parent relationship this needs."""
    out: dict = {}

    def descend(node: ast.AST, name: str) -> None:
        for child in ast.iter_child_nodes(node):
            out[id(child)] = name
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                descend(child, child.name)
            else:
                descend(child, name)

    descend(tree, "<module>")
    return out


def _func_param_names(fn) -> set:
    args = fn.args
    names = {a.arg for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)}
    if args.vararg is not None:
        names.add(args.vararg.arg)
    return names


def _generic_runner_names(tree: ast.Module) -> set:
    """Local functions that spawn one of their OWN parameters as argv.

    This is the leg that catches the split shape: `_git` builds
    `["git", *args]` and hands it to a sibling `_run(cmd)` that does the
    actual `subprocess.run`. Neither function alone looks like a git runner
    -- one has the argv and no spawn, the other has the spawn and no git --
    and `bash_guards/_branch_set.py` shipped exactly that pair until the G7
    migration. Without this, the module is invisible to the gate."""
    out: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = _func_param_names(node)
        for call in ast.walk(node):
            if not isinstance(call, ast.Call) or _leaf_name(call.func) not in _SPAWN_API_NAMES:
                continue
            for argv in _argv_exprs(call):
                if any(isinstance(n, ast.Name) and n.id in params for n in ast.walk(argv)):
                    out.add(node.name)
    return out


def _module_level_numeric_names(tree: ast.Module) -> set:
    """Module-level names bound to a numeric literal. Bools are excluded --
    `True` is an `int` to `isinstance`, and a module-level flag is not a
    dial."""
    out: set = set()
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        else:
            continue
        value = node.value
        if not isinstance(value, ast.Constant):
            continue
        if isinstance(value.value, bool) or not isinstance(value.value, (int, float)):
            continue
        out.update(targets)
    return out


def _collect_module(relpath: str, source: str) -> "tuple[list, list]":
    """Return `(git_spawn_sites, dial_keys)` for one module's source."""
    tree = ast.parse(source)
    enclosing = _enclosing_names(tree)
    generics = _generic_runner_names(tree)

    def is_git_spawn(call: ast.Call) -> bool:
        name = _leaf_name(call.func)
        if name is None or (name not in _SPAWN_API_NAMES and name not in generics):
            return False
        return any(_carries_git_argv(argv) for argv in _argv_exprs(call))

    sites = {
        GitSpawnSite(module=relpath, enclosing=enclosing.get(id(node), "<module>"))
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and is_git_spawn(node)
    }
    if not sites:
        # A dial is only a GIT dial when its module spawns git. This is what
        # keeps the two registers ratcheting together and keeps the gate off
        # G9's external-tool timeouts.
        return [], []

    git_funcs = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(isinstance(c, ast.Call) and is_git_spawn(c) for c in ast.walk(node))
    }
    numeric_names = _module_level_numeric_names(tree)
    dials: set = set()

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not (is_git_spawn(node) or _leaf_name(node.func) in git_funcs):
            continue
        for kw in node.keywords:
            if kw.arg is None or "timeout" not in kw.arg.lower():
                continue
            for name in ast.walk(kw.value):
                if isinstance(name, ast.Name) and name.id in numeric_names:
                    dials.add((relpath, name.id))

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name not in git_funcs:
            continue
        args = node.args
        positional = list(args.posonlyargs) + list(args.args)
        defaults = list(args.defaults)
        pairs = list(zip(positional[len(positional) - len(defaults):], defaults)) if defaults else []
        pairs += [(k, d) for k, d in zip(args.kwonlyargs, args.kw_defaults) if d is not None]
        for arg, default in pairs:
            if "timeout" not in arg.arg.lower():
                continue
            if isinstance(default, ast.Name) and default.id in numeric_names:
                dials.add((relpath, default.id))
            elif isinstance(default, ast.Constant) and not isinstance(default.value, bool) and isinstance(default.value, (int, float)):
                dials.add((relpath, f"{node.name}({arg.arg})"))

    return sorted(sites, key=lambda s: (s.module, s.enclosing)), sorted(dials)


def _scope_files() -> list:
    """Every non-test source file under the gate's roots, as
    `(repo-relative posix path, absolute path)`. Reuses `spawn_policy`'s
    traversal and test-tree partition rather than mirroring them -- the
    amplification gate's own docstring records the census and the gate
    disagreeing twice on scope when each walked the tree its own way."""
    out: list = []
    for root_name in _GATE_SCOPE_ROOTS:
        root = _REPO_ROOT / root_name
        if not root.exists():
            continue
        discovered, _excluded = discover_source_files(root, exclude=DEFAULT_EXCLUDE)
        for rel, path in discovered:
            try:
                relpath = path.resolve().relative_to(_REPO_ROOT).as_posix()
            except ValueError:
                # A discovered path that does not sit under the repo root
                # (a symlinked tree) still needs a stable key; fall back to
                # the root-relative one `discover_source_files` returned.
                relpath = f"{root_name}/{pathlib.PurePosixPath(rel).as_posix()}"
            if is_test_tree_site(relpath) or relpath == _PRIMITIVE_MODULE:
                continue
            if path.resolve() == _THIS_FILE:
                raise RuntimeError(
                    "re-entrancy: the shared-git-runner gate scanned its own file, "
                    "which would make it pass vacuously. is_test_tree_site's "
                    "test-tree filtering was bypassed or misconfigured."
                )
            out.append((relpath, path))
    return out


def collect_private_git_runners() -> "tuple[list, list]":
    """Sweep the gate's scope. Returns `(git_spawn_sites, dial_keys)`.

    A module that fails to parse is SKIPPED, not raised on: this scope holds
    extensionless shebang scripts and, on a shared tree, files a peer session
    is mid-write. A parse error here is not a finding about git runners, and
    turning one into a gate failure would make the gate fail for reasons it
    has no opinion about."""
    sites: list = []
    dials: list = []
    for relpath, path in _scope_files():
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
            module_sites, module_dials = _collect_module(relpath, source)
        except (SyntaxError, ValueError, OSError):
            continue
        sites.extend(module_sites)
        dials.extend(module_dials)
    return sites, dials


#: Frozen inventory of modules that spawn git without going through
#: `coordinator_core.git.run`. FROZEN 2026-08-21 over a full run of
#: `collect_private_git_runners()` across `coordinator_core`,
#: `coordinator/bin` and `coordinator/lib`: 211 modules on the first sweep,
#: minus the 5 migrated in the same change that landed this gate
#: (`git/remote_url.py`, `git_ancestry.py`, `engine_version.py`,
#: `bash_guards/_branch_set.py`, `bash_guards/commit_tripwires.py`), minus
#: `ops/emit/sections/routine_signals.py`, which a concurrent session
#: migrated while this gate was being written. 205 rows.
#:
#: That last row is worth naming rather than silently absorbing: it is the
#: register doing what it is for. It was frozen from a sweep, went dead
#: within the hour because a peer fixed the module, and
#: `test_every_grandfathered_runner_still_spawns_git` said so. The tree is
#: edited by 50-70 concurrent sessions; a register frozen here and reviewed
#: an hour later WILL carry rows that have since been fixed, and the correct
#: response to that failure is always a delete plus a lower ceiling, never a
#: re-key.
#:
#: SHRINK-ONLY. Do NOT add a row to silence a new violation -- route the new
#: module through `coordinator_core.git.run` instead, which is a smaller
#: change than the two-line edit adding a row costs. Remove a row when its
#: module migrates; `test_every_grandfathered_runner_still_spawns_git` fails
#: on a row whose module no longer qualifies, so a stale row cannot sit here
#: pre-approving whatever next takes that path.
_GRANDFATHERED_RUNNER_MODULES: frozenset[str] = frozenset(
    {
        "coordinator/bin/age-sweep-lessons.py",
        "coordinator/bin/assert-cwd.py",
        "coordinator/bin/break_glass.py",
        "coordinator/bin/bug-sweep-probes.py",
        "coordinator/bin/check-bin-sh-polyglot.py",
        "coordinator/bin/check-install-divergence.py",
        "coordinator/bin/check-machine-path-leak.py",
        "coordinator/bin/check-no-illegal-paths.py",
        "coordinator/bin/check-schema-version-bump.py",
        "coordinator/bin/check-sh-suffix-polyglot.py",
        "coordinator/bin/classify-engine-root-residue.py",
        "coordinator/bin/coordinator-current-branch.py",
        "coordinator/bin/coordinator-doc-new.py",
        "coordinator/bin/coordinator-prepare-commit-msg",
        "coordinator/bin/coordinator-prepare-commit-msg.py",
        "coordinator/bin/coordinator-safe-commit.py",
        "coordinator/bin/coordinator-write-review-trail.py",
        "coordinator/bin/cross-repo-memo.py",
        "coordinator/bin/derive-file-attribution.py",
        "coordinator/bin/emit-goal-from-artifact.py",
        "coordinator/bin/fan-out-dispatch.py",
        "coordinator/bin/handoff-loe-summary.py",
        "coordinator/bin/install-sentinel-write.py",
        "coordinator/bin/klabauter-channel.py",
        "coordinator/bin/lib/emit-lesson-summaries.py",
        "coordinator/bin/lib/workday_ceremony_lib.py",
        "coordinator/bin/merge-gate-and-pr.py",
        "coordinator/bin/merge-recovery-and-tag-cut.py",
        "coordinator/bin/merge-release-notes-derive.py",
        "coordinator/bin/percolate-full-payload-proof.py",
        "coordinator/bin/percolate-gate.py",
        "coordinator/bin/percolate-push.py",
        "coordinator/bin/percolate-round.py",
        "coordinator/bin/probe-prereq.py",
        "coordinator/bin/publish-allowlist-generate.py",
        "coordinator/bin/publish.py",
        "coordinator/bin/reap-integrated-review-findings.py",
        "coordinator/bin/reap-orphaned-in-flight-handoffs.py",
        "coordinator/bin/reap-stale-subagent-sidecars.py",
        "coordinator/bin/red-set-report.py",
        "coordinator/bin/refresh-plugin-live-install.py",
        "coordinator/bin/regen-cockpit-schema.py",
        "coordinator/bin/repair-empty-review-trail-ranges.py",
        "coordinator/bin/repo-setup-args-and-register.py",
        "coordinator/bin/repomap/generate-repomap.py",
        "coordinator/bin/spinoff-deliverable-and-commit.py",
        "coordinator/bin/standup.py",
        "coordinator/bin/test-fixtures/check-workstream-complete-deletion-blocks/run-smoke.py",
        "coordinator/bin/workday-complete-reconcile.py",
        "coordinator/bin/workday-complete-step1-validate.py",
        "coordinator/bin/workday-complete-step9-append-changelog.py",
        "coordinator/bin/workday-start-advisory-counters.py",
        "coordinator/bin/workday-start-day-branch-resolve.py",
        "coordinator/bin/workday-start-handoff-triage.py",
        "coordinator/bin/workweek-complete-close.py",
        "coordinator/bin/workweek-complete-drift-guards.py",
        "coordinator/bin/workweek-start-goal-and-priorities.py",
        "coordinator/bin/wsc-session-disposition.py",
        "coordinator/lib/coordinator-is-meta-repo.py",
        "coordinator/lib/percolate/publish_sync.py",
        "coordinator/lib/release_currency.py",
        "coordinator/lib/session_ensure_branch.py",
        "coordinator_core/archive_stamp.py",
        "coordinator_core/backlog_grind_assemble/apply.py",
        "coordinator_core/backlog_grind_assemble/readers_mise.py",
        "coordinator_core/bash_guards/_alternative_liveness.py",
        "coordinator_core/bash_guards/dispatch_checks.py",
        "coordinator_core/baton_assemble/__init__.py",
        "coordinator_core/baton_assemble/apply.py",
        "coordinator_core/benchmarks/harness.py",
        "coordinator_core/benchmarks/interleave.py",
        "coordinator_core/benchmarks/op_fixtures.py",
        "coordinator_core/chain_attribution.py",
        "coordinator_core/consolidate_assemble/__init__.py",
        "coordinator_core/consolidate_assemble/apply.py",
        "coordinator_core/coverage.py",
        "coordinator_core/dag.py",
        "coordinator_core/diff_scoped_tests.py",
        "coordinator_core/distill/delete_guard.py",
        "coordinator_core/execute_plan_assemble/close_out_and_stamp.py",
        "coordinator_core/frontmatter/schema_validate.py",
        "coordinator_core/git/divergence.py",
        "coordinator_core/git/repo_root.py",
        "coordinator_core/git_scope.py",
        "coordinator_core/hooks/auto_push.py",
        "coordinator_core/hooks/context_pressure_precompact.py",
        "coordinator_core/hooks/day_branch_assert.py",
        "coordinator_core/hooks/example_retrieval_repo_detect.py",
        "coordinator_core/hooks/subagent_fabrication_check.py",
        "coordinator_core/hooks/track_touched_files.py",
        "coordinator_core/install/clone_sibling_repo.py",
        "coordinator_core/install/first_run.py",
        "coordinator_core/install/prereq_probe.py",
        "coordinator_core/install/uninstall_legs.py",
        "coordinator_core/machine_resolver.py",
        "coordinator_core/merge_assemble/__init__.py",
        "coordinator_core/ops/agent_worktree_sweep.py",
        "coordinator_core/ops/assert_no_dangling_plan_backlinks.py",
        "coordinator_core/ops/bootstrap_orchestrate.py",
        "coordinator_core/ops/bootstrap_repo.py",
        "coordinator_core/ops/cascade_retract.py",
        "coordinator_core/ops/ceremony/branch_resolution.py",
        "coordinator_core/ops/ceremony/detached_render_commit.py",
        "coordinator_core/ops/ceremony/git_native.py",
        "coordinator_core/ops/ceremony/resolver.py",
        "coordinator_core/ops/ceremony/update_docs_scan.py",
        "coordinator_core/ops/changelog_ops.py",
        "coordinator_core/ops/check_import_budget_staleness.py",
        "coordinator_core/ops/check_posix_exec_assumptions.py",
        "coordinator_core/ops/check_posix_tmpdir_fallback.py",
        "coordinator_core/ops/check_version_consistency.py",
        "coordinator_core/ops/check_weekly_staleness.py",
        "coordinator_core/ops/check_windows_ssh_binary.py",
        "coordinator_core/ops/commit_anchors.py",
        "coordinator_core/ops/completion_ops.py",
        "coordinator_core/ops/configure_git.py",
        "coordinator_core/ops/create_github_remote.py",
        "coordinator_core/ops/cruft_sweep.py",
        "coordinator_core/ops/cutover_gate.py",
        "coordinator_core/ops/detect_changed_dependency_manifests.py",
        "coordinator_core/ops/detect_project_runtime.py",
        "coordinator_core/ops/detect_staged_rollback.py",
        "coordinator_core/ops/dirty_tree_gate.py",
        "coordinator_core/ops/doc_staleness.py",
        "coordinator_core/ops/dod_floor_ratchet.py",
        "coordinator_core/ops/draft_plan_aging.py",
        "coordinator_core/ops/emit/context.py",
        "coordinator_core/ops/emit/doe_drift.py",
        "coordinator_core/ops/emit/envelope.py",
        "coordinator_core/ops/emit/lma_cache.py",
        "coordinator_core/ops/emit/sections/_shared.py",
        "coordinator_core/ops/emit/sections/handoff_columns.py",
        "coordinator_core/ops/ensure_doe_clone.py",
        "coordinator_core/ops/fan_out_integrator.py",
        "coordinator_core/ops/gate_dimension_review.py",
        "coordinator_core/ops/generate_exec_summary.py",
        "coordinator_core/ops/generator_provenance.py",
        "coordinator_core/ops/merge_branch_into_workstream.py",
        "coordinator_core/ops/merge_quiet_activity_gate.py",
        "coordinator_core/ops/migrate_branch_canonical_case.py",
        "coordinator_core/ops/migrate_completion_log_legacy.py",
        "coordinator_core/ops/migrate_cross_repo_layout.py",
        "coordinator_core/ops/new_project_scaffold.py",
        "coordinator_core/ops/normalize_claimed_frontmatter.py",
        "coordinator_core/ops/orphan_branch_sweep.py",
        "coordinator_core/ops/parse_resolves_trailer.py",
        "coordinator_core/ops/percolate_check_inverse_drift.py",
        "coordinator_core/ops/plan_suggest_completion_steps.py",
        "coordinator_core/ops/platform_outcome_records.py",
        "coordinator_core/ops/promote_shipped_in_flight_stubs.py",
        "coordinator_core/ops/propagate_body.py",
        "coordinator_core/ops/reap_orphaned_agent_dirs.py",
        "coordinator_core/ops/record_history.py",
        "coordinator_core/ops/release_tagging.py",
        "coordinator_core/ops/renormalize_index.py",
        "coordinator_core/ops/resolve_swept_baton.py",
        "coordinator_core/ops/review_brightline_gate.py",
        "coordinator_core/ops/review_coverage_core.py",
        "coordinator_core/ops/review_trail_readjudication_report.py",
        "coordinator_core/ops/review_trail_write.py",
        "coordinator_core/ops/rollup_derive.py",
        "coordinator_core/ops/run_semgrep_scan.py",
        "coordinator_core/ops/run_shellcheck_sweep.py",
        "coordinator_core/ops/session/fix_concrete_path_citations.py",
        "coordinator_core/ops/session/guard_concrete_path_citations.py",
        "coordinator_core/ops/session/guard_settings_integrity.py",
        "coordinator_core/ops/session/resolve_chain_terminal_disposition.py",
        "coordinator_core/ops/session/safe_commit_offer.py",
        "coordinator_core/ops/staleness_git.py",
        "coordinator_core/ops/strategic/version_highlights.py",
        "coordinator_core/ops/sync_main.py",
        "coordinator_core/ops/tracker/push_suggestion.py",
        "coordinator_core/ops/verify_arch_audit_atlas_refresh.py",
        "coordinator_core/ops/verify_fix_files_changed.py",
        "coordinator_core/ops/verify_orientation_cache_sync.py",
        "coordinator_core/ops/workday_complete_backfill_scan.py",
        "coordinator_core/ops/workday_complete_step2_5_dirty_tree.py",
        "coordinator_core/ops/workday_start_step0_reconcile.py",
        "coordinator_core/ops/workday_surface_stale_stash_entries.py",
        "coordinator_core/ops/workweek_trail_scope.py",
        "coordinator_core/orient_assemble/readers_branch_reconcile.py",
        "coordinator_core/orientation/regenerate_cache.py",
        "coordinator_core/person_resolver.py",
        "coordinator_core/pickup_assemble/__init__.py",
        "coordinator_core/plan_assemble/predicates/composition_graph.py",
        "coordinator_core/plan_assemble/predicates/concurrent_preflight.py",
        "coordinator_core/plan_assemble/predicates/substrate_scans.py",
        "coordinator_core/plugin_health/drift.py",
        "coordinator_core/plugin_health/release_currency.py",
        "coordinator_core/quick_wrap_assemble/__init__.py",
        "coordinator_core/reconcile/ac27_differential_oracle.py",
        "coordinator_core/reconcile/commit_reality.py",
        "coordinator_core/review_assemble/residue.py",
        "coordinator_core/session/core.py",
        "coordinator_core/session/scope.py",
        "coordinator_core/session/shape.py",
        "coordinator_core/session_attribution.py",
        "coordinator_core/subagent_sandbox/engine.py",
        "coordinator_core/warm/skew.py",
        "coordinator_core/workday_complete/cockpit_contract_freshness.py",
        "coordinator_core/workstream_complete/__init__.py",
        "coordinator_core/workstream_complete/directives_commit_tail.py",
        "coordinator_core/workstream_complete/directives_memo_lifecycle.py",
        "coordinator_core/workstream_complete/session_identity.py",
        "coordinator_core/write_guards/validate_frontmatter_schema_deny.py",
    }
)

#: Frozen inventory of module-private numbers bounding a git spawn. Same
#: freeze, same rules, same shrink-only direction as the runner register
#: above. Read the values these names hold before assuming any of them was
#: chosen: the hitlist's headline finding is that 48% of every dial in the
#: tree is one of three copy-pasted numbers, and `ops/emit/doe_drift.py`'s
#: own comment justifies its 30 by recording that another site has the
#: same 30.
_GRANDFATHERED_DIALS: frozenset = frozenset(
    {
        ("coordinator/bin/break_glass.py", "check_claude_sync(timeout)"),
        ("coordinator/bin/check-install-divergence.py", "_GIT_TIMEOUT_SECS"),
        ("coordinator/bin/coordinator-prepare-commit-msg", "_resolve_staged_paths(timeout)"),
        ("coordinator/bin/coordinator-prepare-commit-msg.py", "_resolve_staged_paths(timeout)"),
        ("coordinator/bin/lib/workday_ceremony_lib.py", "git(timeout)"),
        ("coordinator/bin/percolate-round.py", "_GIT_PUSH_TIMEOUT_SECS"),
        ("coordinator/bin/reap-integrated-review-findings.py", "_GIT_TIMEOUT_SECS"),
        ("coordinator/bin/workday-start-day-branch-resolve.py", "_GIT_TIMEOUT"),
        ("coordinator/lib/release_currency.py", "_LOCAL_GIT_TIMEOUT_SECS"),
        ("coordinator/lib/release_currency.py", "_REMOTE_FETCH_TIMEOUT_SECS"),
        ("coordinator/lib/release_currency.py", "_REMOTE_GIT_TIMEOUT_SECS"),
        ("coordinator_core/archive_stamp.py", "_SUBPROCESS_TIMEOUT_SEC"),
        ("coordinator_core/backlog_grind_assemble/readers_mise.py", "_GIT_TIMEOUT"),
        ("coordinator_core/bash_guards/dispatch_checks.py", "_run_git(timeout)"),
        ("coordinator_core/dag.py", "_git_history_is_complete(timeout_s)"),
        ("coordinator_core/dag.py", "build_git_history_cache(timeout_s)"),
        ("coordinator_core/execute_plan_assemble/close_out_and_stamp.py", "_GIT_TIMEOUT_SECS"),
        ("coordinator_core/git/divergence.py", "_run_git(timeout)"),
        ("coordinator_core/git/repo_root.py", "_TIMEOUT_SECS"),
        ("coordinator_core/git_scope.py", "FOREIGN_REPO_GIT_TIMEOUT_SECONDS"),
        ("coordinator_core/hooks/auto_push.py", "GIT_PUSH_TIMEOUT_SECS"),
        ("coordinator_core/hooks/auto_push.py", "GIT_READ_TIMEOUT_SECS"),
        ("coordinator_core/hooks/subagent_fabrication_check.py", "_GIT_STATUS_TIMEOUT_SECONDS"),
        ("coordinator_core/install/first_run.py", "_PUBLISH_ROUND_ADVISORY_BUDGET_SECS"),
        ("coordinator_core/install/prereq_probe.py", "_NETWORK_PROBE_TIMEOUT_SECS"),
        ("coordinator_core/machine_resolver.py", "_GIT_TIMEOUT"),
        ("coordinator_core/ops/agent_worktree_sweep.py", "_CHERRY_PICK_TIMEOUT_SECS"),
        ("coordinator_core/ops/agent_worktree_sweep.py", "_PORCELAIN_TIMEOUT_SECS"),
        ("coordinator_core/ops/bootstrap_orchestrate.py", "_GIT_TIMEOUT_SECS"),
        ("coordinator_core/ops/bootstrap_repo.py", "_COMMIT_TIMEOUT_SECS"),
        ("coordinator_core/ops/bootstrap_repo.py", "_GIT_TIMEOUT_SECS"),
        ("coordinator_core/ops/cascade_retract.py", "_SUBPROCESS_TIMEOUT_SEC"),
        ("coordinator_core/ops/changelog_ops.py", "_SUBPROCESS_TIMEOUT"),
        ("coordinator_core/ops/create_github_remote.py", "_GIT_TIMEOUT"),
        ("coordinator_core/ops/create_github_remote.py", "_NETWORK_TIMEOUT"),
        ("coordinator_core/ops/detect_changed_dependency_manifests.py", "_GIT_TIMEOUT_SECONDS"),
        ("coordinator_core/ops/draft_plan_aging.py", "_GIT_LOG_TIMEOUT_SECS"),
        ("coordinator_core/ops/fan_out_integrator.py", "_SUBPROCESS_TIMEOUT_SECS"),
        ("coordinator_core/ops/gate_dimension_review.py", "_GIT_TIMEOUT_SECS"),
        ("coordinator_core/ops/generate_exec_summary.py", "_SUBPROCESS_TIMEOUT_SECS"),
        ("coordinator_core/ops/merge_branch_into_workstream.py", "_GIT_TIMEOUT"),
        ("coordinator_core/ops/merge_quiet_activity_gate.py", "_GIT_TIMEOUT_SECONDS"),
        ("coordinator_core/ops/migrate_branch_canonical_case.py", "_GIT_NETWORK_TIMEOUT_SECS"),
        ("coordinator_core/ops/migrate_branch_canonical_case.py", "_GIT_TIMEOUT_SECS"),
        ("coordinator_core/ops/new_project_scaffold.py", "_GIT_TIMEOUT"),
        ("coordinator_core/ops/orphan_branch_sweep.py", "_GIT_TIMEOUT"),
        ("coordinator_core/ops/percolate_check_inverse_drift.py", "_SUBPROCESS_TIMEOUT_SEC"),
        ("coordinator_core/ops/promote_shipped_in_flight_stubs.py", "_GIT_TIMEOUT_SECS"),
        ("coordinator_core/ops/release_tagging.py", "_GIT_TIMEOUT"),
        ("coordinator_core/ops/release_tagging.py", "_NETWORK_TIMEOUT"),
        ("coordinator_core/ops/resolve_swept_baton.py", "_GIT_TIMEOUT_SECONDS"),
        ("coordinator_core/ops/run_semgrep_scan.py", "_GIT_TIMEOUT_SECONDS"),
        ("coordinator_core/ops/run_shellcheck_sweep.py", "_GIT_TIMEOUT_SECONDS"),
        ("coordinator_core/ops/session/resolve_chain_terminal_disposition.py", "_GIT_TIMEOUT_SECONDS"),
        ("coordinator_core/ops/strategic/version_highlights.py", "_SUBPROCESS_TIMEOUT"),
        ("coordinator_core/ops/verify_arch_audit_atlas_refresh.py", "_GIT_TIMEOUT_SECS"),
        ("coordinator_core/ops/verify_orientation_cache_sync.py", "_SUBPROCESS_TIMEOUT_SECS"),
        ("coordinator_core/ops/workday_complete_backfill_scan.py", "_GIT_TIMEOUT"),
        ("coordinator_core/ops/workday_complete_step2_5_dirty_tree.py", "_GIT_TIMEOUT_SECS"),
        ("coordinator_core/ops/workweek_trail_scope.py", "_GIT_LOG_TIMEOUT_SECS"),
        ("coordinator_core/orient_assemble/readers_branch_reconcile.py", "_GIT_TIMEOUT"),
        ("coordinator_core/person_resolver.py", "_GIT_TIMEOUT"),
        ("coordinator_core/plan_assemble/predicates/composition_graph.py", "_GIT_TIMEOUT_SEC"),
        ("coordinator_core/plugin_health/drift.py", "_run_git(timeout)"),
        ("coordinator_core/plugin_health/release_currency.py", "_LOCAL_GIT_TIMEOUT_SECS"),
        ("coordinator_core/plugin_health/release_currency.py", "_REMOTE_FETCH_TIMEOUT_SECS"),
        ("coordinator_core/plugin_health/release_currency.py", "_REMOTE_GIT_TIMEOUT_SECS"),
        ("coordinator_core/workday_complete/cockpit_contract_freshness.py", "_LOCAL_GIT_TIMEOUT_SECONDS"),
        ("coordinator_core/workday_complete/cockpit_contract_freshness.py", "_LS_REMOTE_TIMEOUT_SECONDS"),
        ("coordinator_core/workstream_complete/__init__.py", "_REVIEW_SCALE_GIT_TIMEOUT"),
    }
)

#: Independent second copies of each register's size, and the whole reason
#: this is a ratchet. Deliberately literals -- computing them from `len()` of
#: the register they guard would make this file agree with any register at
#: all. Lowering either is free and is the point; raising either is the
#: deliberate, reviewable act of arguing that the tree needs one more private
#: git runner than it had yesterday.
_PINNED_RUNNER_CEILING = 205
_PINNED_DIAL_CEILING = 70


def _runner_message(sites: list) -> str:
    by_module: dict = {}
    for site in sites:
        by_module.setdefault(site.module, []).append(site.enclosing)
    listed = "\n".join(
        f"  {module}: {', '.join(sorted(set(names)))}" for module, names in sorted(by_module.items())
    )
    return (
        "these modules spawn git without going through coordinator_core.git.run:\n"
        f"{listed}\n"
        "Call run_git(args, cwd=...) instead; it carries the bound. On a budgeted "
        "path pass that path's own budget through (timeout=CEREMONY_BUDGET_SECS), "
        "which narrows and cannot widen."
    )


def _dial_message(dials: list) -> str:
    listed = "\n".join(f"  {module}: {name}" for module, name in sorted(dials))
    return (
        "these module-private numbers bound a git spawn:\n"
        f"{listed}\n"
        "coordinator_core.git.run holds the two bounds a git spawn may carry: "
        "LOCAL_PLUMBING_BUDGET_SECS and REMOTE_BUDGET_SECS. Delete the constant "
        "and call run_git."
    )


def test_no_new_private_git_runner_outside_the_frozen_inventory():
    """The gate. A module spawning git outside the inventory fails here."""
    sites, _dials = collect_private_git_runners()
    new = [site for site in sites if site.module not in _GRANDFATHERED_RUNNER_MODULES]
    assert not new, _runner_message(new)


def test_no_new_module_private_git_dial_outside_the_frozen_inventory():
    """The other half. A new number bounding a git spawn fails here, whether
    it is a module constant or a parameter default."""
    _sites, dials = collect_private_git_runners()
    new = [dial for dial in dials if dial not in _GRANDFATHERED_DIALS]
    assert not new, _dial_message(new)


def test_the_registers_are_shrink_only():
    """The ratchet. Adding a grandfather row costs what raising a budget
    costs: this literal must move too, in the same diff, as an argument that
    the tree needs one more private git runner than it had yesterday."""
    assert len(_GRANDFATHERED_RUNNER_MODULES) <= _PINNED_RUNNER_CEILING, (
        f"the runner register grew to {len(_GRANDFATHERED_RUNNER_MODULES)}, above the "
        f"pinned {_PINNED_RUNNER_CEILING}. It shrinks only. A new module needing a git "
        f"read calls coordinator_core.git.run.run_git; it does not join this list."
    )
    assert len(_GRANDFATHERED_DIALS) <= _PINNED_DIAL_CEILING, (
        f"the dial register grew to {len(_GRANDFATHERED_DIALS)}, above the pinned "
        f"{_PINNED_DIAL_CEILING}. It shrinks only. The two bounds a git spawn may "
        f"carry live in coordinator_core.git.run."
    )


def test_every_grandfathered_runner_still_spawns_git():
    """Self-invalidation. A row naming a module that has migrated, been
    renamed, or been deleted is not harmless: it is a standing, reviewed-
    looking pre-approval for whatever next takes that path. A failure here is
    a DELETE, not a re-key."""
    sites, _dials = collect_private_git_runners()
    live = {site.module for site in sites}
    dead = sorted(_GRANDFATHERED_RUNNER_MODULES - live)
    assert not dead, (
        "these _GRANDFATHERED_RUNNER_MODULES rows no longer name a module that "
        "spawns git -- delete them (and lower _PINNED_RUNNER_CEILING to match):\n"
        + "\n".join(f"  {module}" for module in dead)
    )


def test_every_grandfathered_dial_still_bounds_a_git_spawn():
    """Self-invalidation for the dial register, same rule and same remedy."""
    _sites, dials = collect_private_git_runners()
    live = set(dials)
    dead = sorted(_GRANDFATHERED_DIALS - live)
    assert not dead, (
        "these _GRANDFATHERED_DIALS rows no longer name a live dial -- delete them "
        "(and lower _PINNED_DIAL_CEILING to match):\n"
        + "\n".join(f"  {module}: {name}" for module, name in dead)
    )


def test_the_collector_is_not_vacuous():
    """Guards the guard. Every assertion above passes trivially against an
    empty sweep, so a scope, traversal, or exclusion change that silently
    stops finding anything would land green. This is the leg that notices."""
    sites, dials = collect_private_git_runners()
    assert sites, (
        "the collector found no git spawn anywhere in "
        f"{_GATE_SCOPE_ROOTS} -- the gate is asserting nothing. Check "
        "_GATE_SCOPE_ROOTS, the spawn_policy traversal, and _SPAWN_API_NAMES."
    )
    assert dials, "the collector found no git dial anywhere -- see above."


def test_the_collector_fires_on_a_synthetic_private_runner(tmp_path):
    """Fails-when-inverted leg. Proves the detector reports the shape it
    claims to, including the split argv/spawn pair that a naive one-function
    matcher misses, rather than passing because the tree happens to be clean.
    """
    direct = (
        "import subprocess\n"
        "_GIT_TIMEOUT = 30\n"
        "def _run_git(args, timeout=_GIT_TIMEOUT):\n"
        "    return subprocess.run(['git', *args], timeout=timeout)\n"
    )
    sites, dials = _collect_module("synthetic/direct.py", direct)
    assert [site.enclosing for site in sites] == ["_run_git"]
    assert dials == [("synthetic/direct.py", "_GIT_TIMEOUT")]

    split = (
        "import subprocess\n"
        "def _run(cmd, timeout):\n"
        "    return subprocess.run(cmd, timeout=timeout)\n"
        "def _git(args):\n"
        "    return _run(['git', *args], timeout=15)\n"
    )
    split_sites, _split_dials = _collect_module("synthetic/split.py", split)
    assert [site.enclosing for site in split_sites] == ["_git"]


def test_a_module_calling_the_shared_runner_is_not_a_site():
    """The negative control that matters most: migrating must actually clear
    the flag. A module that calls `run_git(["status"])` builds no `git` argv
    of its own and must be invisible to both legs -- otherwise the gate
    punishes the fix it exists to demand."""
    migrated = (
        "from coordinator_core.git.run import run_git\n"
        "_UNRELATED_TIMEOUT = 30\n"
        "def read_status(cwd=None):\n"
        "    return run_git(['status', '--porcelain'], cwd=cwd).stdout\n"
    )
    sites, dials = _collect_module("synthetic/migrated.py", migrated)
    assert sites == []
    assert dials == []


def test_the_primitives_three_numbers_ratchet_down_only():
    """The seam's own numbers, pinned the way `test_ceremony_budget_ratchet.py`
    pins the ceremony budget: second, independent copies that must move in the
    same diff. The local budget is the one the six already-correct modules had
    converged on; the remote budget is a runaway guard and is not a licence to
    put a network leg on a budgeted path (DR-349); the headroom term is the
    shared box's scheduling cost and grows only when the BOX changes."""
    assert git_run.LOCAL_PLUMBING_BUDGET_SECS <= 2.0, (
        f"the local git budget was raised to {git_run.LOCAL_PLUMBING_BUDGET_SECS}s. "
        f"`git -C <repo> rev-parse HEAD` is 26.9 ms of PROCESS time on this box "
        f"(DR-344 § 4); a local git call that does not fit in 2.0s is a defect "
        f"report about that call. This ratchets down only. If the symptom is "
        f"timeouts under concurrent load, the term to look at is "
        f"_SPAWN_SCHEDULING_HEADROOM_SECS, not this one."
    )
    assert git_run.REMOTE_BUDGET_SECS <= 30.0, (
        f"the remote git budget was raised to {git_run.REMOTE_BUDGET_SECS}s. It is a "
        f"runaway guard, not a budget, and DR-349 grants network legs no standing "
        f"carve-out. This ratchets down only."
    )
    assert git_run._SPAWN_SCHEDULING_HEADROOM_SECS <= 10.0, (
        f"the scheduling headroom was raised to "
        f"{git_run._SPAWN_SCHEDULING_HEADROOM_SECS}s. 10.0 is ~2.2x the worst wall "
        f"sample measured 2026-08-21 (4,588 ms for `git --version`). Raising it "
        f"claims the box got slower at SCHEDULING spawns, which is a measurement, "
        f"not an inference from a red test. This ratchets down only."
    )


def test_the_budget_and_the_wall_bound_are_not_the_same_number():
    """The split G1 measured this module into, and the one property that stops
    it being 'simplified' back.

    `subprocess.run(timeout=)` is WALL CLOCK; the budgets are PROCESS time. On
    a box running 50-70 concurrent sessions a bare `git --version` takes 33.6ms
    of process time and up to 4,588ms of wall purely waiting to be scheduled,
    so a 2.0s wall bound false-fires on more than 5% of spawns. The wall bound
    must therefore sit strictly above the budget, by the headroom term.

    This is the leg that fails if someone folds the headroom into the budget
    constants -- which would restore the brightline's number to 12.0 and let a
    leg get six times slower without anything noticing."""
    assert git_run._wall_bound(None, False) > git_run.LOCAL_PLUMBING_BUDGET_SECS
    assert git_run._wall_bound(None, True) > git_run.REMOTE_BUDGET_SECS
    assert (
        git_run._wall_bound(None, False) - git_run._resolve_budget(None, False)
        == git_run._SPAWN_SCHEDULING_HEADROOM_SECS
    )
    # Additive, never a multiplier: the scheduling delay is a fixed per-spawn
    # cost of sharing the box, so narrowing the budget must not shrink it.
    assert (
        git_run._wall_bound(0.25, False) - git_run._wall_bound(None, False)
        == 0.25 - git_run.LOCAL_PLUMBING_BUDGET_SECS
    )


def test_an_explicit_timeout_narrows_the_budget_and_never_widens_it():
    """The property that makes `run_git`'s `timeout=` not a dial. DR-349 § 3's
    `min()`-after-resolution, the shape `ipc._timeout_for` already proves for
    `ceremony.*`. Asserted on the BUDGET axis, which is the one a call site
    argues about; the wall bound is derived and is not a call-site decision."""
    assert git_run._resolve_budget(None, False) == git_run.LOCAL_PLUMBING_BUDGET_SECS
    assert git_run._resolve_budget(0.25, False) == 0.25
    assert git_run._resolve_budget(3600, False) == git_run.LOCAL_PLUMBING_BUDGET_SECS
    assert git_run._resolve_budget(None, True) == git_run.REMOTE_BUDGET_SECS
    assert git_run._resolve_budget(3600, True) == git_run.REMOTE_BUDGET_SECS
    assert git_run._resolve_budget(1.0, True) == 1.0


def test_stdin_input_runs_in_binary_mode_and_is_never_newline_translated(monkeypatch):
    """The `--stdin` contract, pinned structurally rather than by spawning.

    Python's text-mode stdin wrapper carries `newline=None` and rewrites every
    `\\n` to `os.linesep`, so a `-z` NUL-delimited pipe fed in text mode on
    Windows arrives with `\\r\\n` and git echoes back C-quoted paths that match
    nothing -- the scar `coordinator/bin/percolate-round.py` carries, which
    surfaced between a publish round's real run and its commit. The only
    defence is never being in text mode when stdin is fed, so that is what
    this asserts: bytes through untouched, no `encoding`/`errors` in the
    kwargs, and no `stdin=` alongside `input=` (`subprocess.run` raises
    ValueError on both).

    Non-spawning by construction -- it records the kwargs rather than running
    git, so it neither costs the shared box a process nor lands on
    `test_no_new_spawning_tests.py`'s ratchet."""
    import subprocess

    recorded = {}

    class _Completed:
        returncode = 0
        stdout = b"a/b\0c/d\0"
        stderr = b""

    def _record(argv, **kwargs):
        recorded["argv"] = argv
        recorded["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(subprocess, "run", _record)

    payload = b"a/b\0c/d\0"
    result = git_run.run_git(["check-ignore", "-z", "--stdin"], input=payload)

    assert recorded["kwargs"]["input"] == payload, "stdin bytes must reach git unaltered"
    assert "encoding" not in recorded["kwargs"], "feeding stdin must not select text mode"
    assert "errors" not in recorded["kwargs"], "feeding stdin must not select text mode"
    assert "stdin" not in recorded["kwargs"], "input= and stdin= are mutually exclusive"
    assert recorded["argv"][0] == "git"
    # Binary mode returns bytes; the seam decodes so callers still see str.
    assert result.stdout == "a/b\0c/d\0"


def test_a_call_without_stdin_keeps_text_mode_and_a_closed_stdin(monkeypatch):
    """The other half of the mode switch: every pre-existing caller keeps the
    behaviour it has today, and still gets `stdin=DEVNULL` so a git command
    that would prompt cannot block on an inherited terminal."""
    import subprocess

    recorded = {}

    class _Completed:
        returncode = 0
        stdout = "ok\n"
        stderr = ""

    def _record(argv, **kwargs):
        recorded["kwargs"] = kwargs
        return _Completed()

    monkeypatch.setattr(subprocess, "run", _record)
    git_run.run_git(["rev-parse", "HEAD"])

    assert recorded["kwargs"]["encoding"] == "utf-8"
    assert recorded["kwargs"]["errors"] == "replace"
    assert recorded["kwargs"]["stdin"] == subprocess.DEVNULL
    assert "input" not in recorded["kwargs"]


def test_str_input_raises_type_error_before_any_spawn(monkeypatch):
    """The bytes-only contract the module docstring's negative-spec claims
    ("Does NOT accept `str` for `input`") is now real, not just documented.

    Passing `str` used to reach `subprocess.run` in binary mode and raise an
    uncaught `TypeError` from inside `Popen.communicate` -- a failure path
    `run_git`'s own `except` clauses do not catch, contradicting "Does NOT
    raise on any failure path". The fix raises a clear `TypeError` at the
    seam itself, before a subprocess is even considered, and this test pins
    that: `subprocess.run` must never be called for a `str` input.

    Non-spawning by construction -- same discipline as the two tests above."""
    import subprocess

    def _fail_if_called(*args, **kwargs):
        raise AssertionError("subprocess.run must not be reached for str input")

    monkeypatch.setattr(subprocess, "run", _fail_if_called)

    with pytest.raises(TypeError):
        git_run.run_git(["check-ignore", "-z", "--stdin"], input="a/b\0c/d\0")


def test_the_pre_split_spellings_are_gone_and_stay_gone():
    """The transitional aliases are deleted, and this is the leg that keeps
    them from growing back.

    `LOCAL_PLUMBING_TIMEOUT_SECS` / `REMOTE_TIMEOUT_SECS` existed only because
    `ops/emit/envelope.py` migrated onto this seam under the pre-split
    spellings mid-flight. Their written deletion condition -- `envelope.py`
    and `emit/sections/commit_closures.py`'s comment naming the
    `*_BUDGET_SECS` spellings -- is discharged, so they are gone.

    Inverted from an equality assertion rather than dropped, because the thing
    worth guarding survives the deletion: two live names for one number is how
    a seam starts carrying two numbers. An alias cannot reintroduce the G7
    defect while it is genuinely an alias, and nothing stops a later session
    from re-adding one of these as an independent constant. This fails if it
    does.
    """
    for retired in ("LOCAL_PLUMBING_TIMEOUT_SECS", "REMOTE_TIMEOUT_SECS"):
        assert not hasattr(git_run, retired), (
            f"{retired} is a retired pre-split spelling — the budget lives at "
            "LOCAL_PLUMBING_BUDGET_SECS / REMOTE_BUDGET_SECS. Import the "
            "budget name; do not re-add a second name for the same number."
        )
