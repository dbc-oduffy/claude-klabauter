"""Structural guard (C1): no production `git commit` call site can EXPRESS a
pathspec-less commit.

Spec backlink: `docs/plans/2026-08-27-no-commit-call-site-can-express-a-bare-c.md`,
`## Tasks` chunk C1.

THE DISCRIMINATOR, MEASURED NOT ASSUMED. `claude-klabauter-77`'s controlled negative at
`5824056bc` staged 12 foreign paths in the shared index, then committed one unrelated path
per arm: `commit_paths`, `stage_in_process + commit_scoped`, `git commit -- <p>`,
`git commit -F msg -- <p>`, and `git commit --amend -m msg -- <p>` each landed exactly 1
entry. Both pathspec-LESS arms (`-m` bare, `--amend` bare) landed 12. The control is the
finding: presence of a pathspec is what discriminates, not the route, the flag, or the
tree-building strategy.

NEGATIVE-SPEC BLOCK -- READ BEFORE TOUCHING THIS FILE. A test that supplies a pathspec and
asserts the resulting commit is clean CANNOT FAIL: it passes against the unfixed code shown
above just as it passes against the fixed code, because the control arm (`git commit --
<path>`) was already clean before this plan existed. This module therefore asserts nothing
about what a commit PRODUCES at runtime -- `test_planted_pathspec_commit_is_not_flagged`
below proves only that the STATIC read does not false-positive a correctly-scoped call, not
that a pathspec makes any given commit safe on a shared tree (it does, but that is git
semantics this module has no opinion on and cannot re-verify). The property this module
enforces is structural: can the call site's argv EXPRESS a commit with no pathspec at all,
read from source, never from a subprocess result.

REUSE FROM `coordinator_core.spawn_policy`, UNMODIFIED (pinned API,
`tasks/shell-spawn-regrowth-gate/PINNED-API.md`): `discover_source_files`,
`is_test_tree_site`, `DEFAULT_EXCLUDE` for file discovery, exactly as
`test_no_unbatched_per_item_git_spawn.py`'s `AmpSite` collector does. `sites_in_source` and
`SpawnParseError` are named in this plan's pinned-API list but are DELIBERATELY NOT CALLED
here -- measured, not assumed: `sites_in_source` resolves argv0 for DIRECT and one-hop
spawns, but two of this module's own calibration sites (`apply_base.scoped_commit`,
`commit_exec_bit._handler`) call `run_git`/`_git` as an imported cross-module helper, and
`sites_in_source` returns ZERO sites for both files -- it cannot see through that hop at
all, one-hop-by-construction the same way `AmpSite`'s own docstring documents for its own
scope. Calling it here would silently drop both calibration sites and the DR-151 inventory
entry along with them. So, exactly as `AmpSite` is a sibling walker over the shared discovery
primitives rather than a wrapper around `sites_in_source`'s resolution, `CommitSite` below is
its own direct AST walk keyed on the argv literal itself, never on `sites_in_source`'s argv0
classification.

THE PREDICATE. For each `Call` whose argv-shaped argument (first positional, or an `args=`
keyword) statically flattens to a list containing the literal `"commit"`, AND that list also
contains the literal `"git"` or is reached through a callee whose name contains `"git"`
(covers `run_git`/`_git`/`_run_git`-shaped wrappers whose own `argv[0]` is implicit): decide
whether the flattened list can ever omit a pathspec.

  - CLEAN: a literal `"--"` element, or an element with the `--pathspec-from-file=` prefix,
    is present anywhere in the flattened list.
  - DIRTY: no such element is present, and every part of the list was resolved fully static
    (no `Name`/attribute standing in for a whole sub-list this pass could not read).
  - UNKNOWN: no clean marker was found, but at least one part of the list is a splat/whole
    -sublist reference this pass could not resolve to a literal (an unbound parameter, an
    attribute access like `acc.commit_paths` used as a `Starred` element it cannot trace, or
    a name with no findable same-scope assignment). UNKNOWN is never folded into CLEAN -- an
    unresolved splat that turns out empty at runtime is exactly the case this predicate must
    not wave through.

CALIBRATION (must hold, and is asserted by name below):
  - `contract/apply_base.py :: scoped_commit` -- `pathspec = ["--", str(resolved)]` built
    unconditionally, then `run_git(["commit", "-m", message, *pathspec], repo_root)`. MUST
    read CLEAN.
  - `ops/workday_complete_step2_5_dirty_tree.py :: _act_gitignore` --
    `_run_git(["commit", "-m", _GITIGNORE_COMMIT_MSG], cwd=repo_root)`, no pathspec
    expressible at all. MUST read DIRTY. The same file's `_act_commit` --
    `_run_git(["commit", "-m", commit_msg, "--"] + acc.commit_paths, cwd=repo_root)` -- MUST
    read CLEAN, proving the collector separates two sites in one file rather than tarring the
    whole module with one verdict.

TWO LEGS, ONE OF THEM STANDING. `test_no_new_bare_commit_sites_outside_known_inventory` is
the STANDING gate (unmarked, fast tier): red only when a non-CLEAN site's `(path, enclosing,
callee)` key falls outside `_KNOWN_BARE_COMMIT_SITES`. There is deliberately NO
`designed_red` burn-down leg here -- `test_no_unbatched_per_item_git_spawn.py`'s own
docstring records two peers independently misreading its burn-down leg's diff as ~42
regressions when the real answer was four; a two-entry frozen inventory does not need that
machinery, and adding one would reintroduce the exact confusion that file's history warns
against.

THE INVENTORY IS A FREEZE, NOT AN EXEMPTION. Each entry below carries its reason and the
condition that retires it. Removing an entry is the win; adding one is a decision, not a
green-the-suite move -- see `test_no_new_bare_commit_sites_outside_known_inventory`'s own
docstring for what a NEW site outside the inventory means and why this run does not silently
add one.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

from coordinator_core.spawn_policy import is_test_tree_site
from coordinator_core.spawn_policy.detect import DEFAULT_EXCLUDE, discover_source_files

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
_THIS_FILE = pathlib.Path(__file__).resolve()

#: This plan's scope is `coordinator_core` only -- the six named sites (two frozen inventory
#: entries plus the four deliberately-unread grep hits) all live under it. Unlike
#: `test_no_unbatched_per_item_git_spawn.py`'s AC4 widening to `coordinator/bin`, nothing in
#: this plan's Problem/Tasks names that root, so it is not added speculatively.
_GATE_SCOPE_ROOTS: tuple[str, ...] = ("coordinator_core",)

_PATHSPEC_FROM_FILE_PREFIX = "--pathspec-from-file="


@dataclasses.dataclass(frozen=True)
class CommitSite:
    """Sibling to `spawn_policy.SpawnSite`, exactly as `AmpSite`
    (`test_no_unbatched_per_item_git_spawn.py`) is -- NOT an extension of the frozen
    `SpawnSite` dataclass. Carries the pathspec verdict that frozen shape has no field for."""

    path: str
    lineno: int
    enclosing: str
    callee: str
    verdict: str  # "clean" | "dirty" | "unknown"

    @property
    def key(self) -> tuple[str, str, str]:
        """Structural identity for the frozen-inventory subset check: `(path, enclosing,
        callee)`. Excludes `lineno` (a renumbering must not look like a new site) and
        `verdict` (a site fixed to CLEAN should retire its own inventory row, not silently
        stop matching one still present)."""
        return (self.path, self.enclosing, self.callee)


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted_name(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    return "<dynamic>"


def _string_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _joined_str_prefix(node: ast.AST) -> str | None:
    """The leading literal segment of an f-string (`ast.JoinedStr`), e.g.
    `f"--pathspec-from-file={pathspec_file}"` -> `"--pathspec-from-file="`. Needed because
    `git_native.py :: commit_with_message_file_pathspec_scoped` -- a MUST-READ-CLEAN site
    named in this plan's Problem section -- spells its `--pathspec-from-file=` marker as an
    f-string, not a plain string constant; a scan that only reads `ast.Constant` elements
    misses it and reports a false DIRTY. Returns `None` for anything else (an f-string with
    no leading literal segment, or a non-f-string), which is opaque and ignored rather than
    guessed at -- an f-string interpolating the WHOLE flag (`f"{flag}"`) still cannot be
    read as a pathspec marker, so declining is correct, not a gap."""
    if isinstance(node, ast.JoinedStr) and node.values:
        first = node.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _argv_arg(call: ast.Call) -> ast.expr | None:
    """The argv-shaped argument of `call`: first positional arg, or an `args=` keyword --
    the two shapes every site in this module's calibration set uses."""
    if call.args:
        return call.args[0]
    for kw in call.keywords:
        if kw.arg == "args":
            return kw.value
    return None


def _find_top_level_assignment(name: str, scope_body: list[ast.stmt]) -> ast.AST | None:
    """The value of the LAST direct (unnested -- not inside an `if`/`for`/`try`) assignment
    to `name` in `scope_body`. Deliberately shallow: a conditional reassignment is exactly
    the shape this predicate cannot trust, and is left unresolved (UNKNOWN) rather than
    guessed at."""
    result: ast.AST | None = None
    for stmt in scope_body:
        if (
            isinstance(stmt, ast.Assign)
            and len(stmt.targets) == 1
            and isinstance(stmt.targets[0], ast.Name)
            and stmt.targets[0].id == name
        ):
            result = stmt.value
    return result


def _find_for_loop_literal_tuple(name: str, scope_body: list[ast.stmt]) -> list[ast.expr] | None:
    """`for name in (<list literal>, <list literal>, ...)` at this scope's top level --
    returns the loop's literal iterable elements (each itself expected to be a `List`/`Tuple`
    argv), or `None`. Covers the `percolate_preflight_scratch_publish._git_init_and_commit`
    shape: `for args in (["git", "init", ...], [...], [...]): subprocess.run(args, ...)`,
    where the argv-bearing literal sits in the loop's iterable, not at the call site."""
    for stmt in scope_body:
        if (
            isinstance(stmt, ast.For)
            and isinstance(stmt.target, ast.Name)
            and stmt.target.id == name
            and isinstance(stmt.iter, (ast.Tuple, ast.List))
        ):
            return list(stmt.iter.elts)
    return None


def _flatten_argv(
    node: ast.AST, scope_body: list[ast.stmt], seen: set[str]
) -> tuple[list[str], bool]:
    """Flattens `node` (a `List`/`Tuple`/`BinOp(Add)` argv expression) into its literal
    string elements, resolving one level of `Name`/`Starred(Name)` splat per hop against
    `scope_body`'s own top-level assignments (guards infinite/circular resolution via
    `seen`).

    Returns `(elements, resolved)`. `resolved` is False the moment ANY whole-sublist
    reference (a splat this pass could not trace to a literal, or a bare attribute/opaque
    expression standing in for a list) is met -- a single opaque VALUE occupying one argv
    slot (a message string, an f-string, a function call result) does not count against it,
    because it can never itself BE a pathspec marker; only a reference that could stand in
    for an entire, unseen run of elements can hide one."""
    elements: list[str] = []
    resolved = True

    def visit(n: ast.AST) -> None:
        nonlocal resolved
        if isinstance(n, (ast.List, ast.Tuple)):
            for elt in n.elts:
                if isinstance(elt, ast.Starred):
                    visit_whole_ref(elt.value)
                    continue
                value = _string_value(elt)
                if value is not None:
                    elements.append(value)
                elif isinstance(elt, (ast.List, ast.Tuple)):
                    visit(elt)
                else:
                    prefix = _joined_str_prefix(elt)
                    if prefix is not None:
                        elements.append(prefix)
                    # else: an opaque single-slot value -- not structural, ignored.
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            visit(n.left)
            visit(n.right)
        else:
            visit_whole_ref(n)

    def visit_whole_ref(n: ast.AST) -> None:
        nonlocal resolved
        if isinstance(n, ast.Name):
            if n.id in seen:
                resolved = False
                return
            seen.add(n.id)
            assigned = _find_top_level_assignment(n.id, scope_body)
            if assigned is None:
                resolved = False
                return
            visit(assigned)
        elif isinstance(n, (ast.List, ast.Tuple)):
            visit(n)
        else:
            # An attribute access (`acc.commit_paths`), a call result, or any other
            # expression standing in for a whole sub-list this pass cannot trace.
            resolved = False

    visit(node)
    return elements, resolved


def _classify(elements: list[str], resolved: bool) -> str:
    for element in elements:
        if element == "--" or element.startswith(_PATHSPEC_FROM_FILE_PREFIX):
            return "clean"
    return "dirty" if resolved else "unknown"


def _is_git_commit_candidate(elements: list[str], callee: str) -> bool:
    """Whether this argv is a git `commit` worth classifying.

    NAMING DEPENDENCY, a coverage edge rather than a style note. The second disjunct reads the
    CALLEE's name because the two calibration sites reach git through a cross-module helper
    (`run_git`, `_git`) that supplies the binary itself, so `"git"` never appears in the argv
    the call site writes. That holds only while such helpers keep `git` in their names:
    renaming `run_git` to something git-free would drop its call sites out of candidacy
    silently, with no test going red. Widening this to treat any unresolvable callee as a
    candidate was rejected -- it makes every `run(...)` in the repo a candidate and buries the
    inventory in noise -- so the dependency is recorded here. If a wrapper is renamed, this
    predicate is the thing to fix.
    """
    return "commit" in elements and ("git" in elements or "git" in callee.lower())


class _CommitSiteVisitor(ast.NodeVisitor):
    """Direct AST walk for `git commit` argv sites -- see module docstring's "Reuse from
    spawn_policy" section for why this is a sibling walker, not a consumer of
    `sites_in_source`'s own (insufficiently deep) argv0 resolution."""

    def __init__(self, path: str, module_body: list[ast.stmt]) -> None:
        self.path = path
        self._name_stack: list[str] = []
        self._body_stack: list[list[ast.stmt]] = [module_body]
        self.sites: list[CommitSite] = []

    def _enclosing(self) -> str:
        return ".".join(self._name_stack) if self._name_stack else "<module>"

    def _visit_scope(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef) -> None:
        self._name_stack.append(node.name)
        self._body_stack.append(node.body)
        self.generic_visit(node)
        self._body_stack.pop()
        self._name_stack.pop()

    visit_FunctionDef = _visit_scope
    visit_AsyncFunctionDef = _visit_scope
    visit_ClassDef = _visit_scope

    def visit_Call(self, node: ast.Call) -> None:
        argv_node = _argv_arg(node)
        if argv_node is not None:
            scope_body = self._body_stack[-1]
            candidate_argvs: list[ast.expr] = [argv_node]
            if isinstance(argv_node, ast.Name):
                loop_elts = _find_for_loop_literal_tuple(argv_node.id, scope_body)
                if loop_elts is not None:
                    candidate_argvs = loop_elts

            callee = _dotted_name(node.func)
            for argv in candidate_argvs:
                elements, resolved = _flatten_argv(argv, scope_body, set())
                if _is_git_commit_candidate(elements, callee):
                    self.sites.append(
                        CommitSite(
                            path=self.path,
                            lineno=getattr(argv, "lineno", node.lineno),
                            enclosing=self._enclosing(),
                            callee=callee,
                            verdict=_classify(elements, resolved),
                        )
                    )
        self.generic_visit(node)


def find_commit_sites(text: str, path: str) -> list[CommitSite]:
    """All `CommitSite`s in `text` (module source, `path` for reporting only). Raises
    `SyntaxError` on unparseable source -- callers scanning many files should catch it
    per-file, matching `sites_in_source`'s own contract."""
    tree = ast.parse(text, filename=path)
    visitor = _CommitSiteVisitor(path, tree.body)
    visitor.visit(tree)
    return visitor.sites


#: THE FROZEN INVENTORY -- a freeze of every non-CLEAN site this collector finds under
#: `coordinator_core` TODAY, not an exemption list. Each entry carries its classification
#: (SELF-CREATED REPO: the enclosing function itself creates the repo it commits into, so a
#: bare commit cannot reach a peer's staged work because no peer has anything staged in a
#: repo that did not exist a moment ago; or AMBIENT REPO: it commits into a repo it was
#: handed or discovered, so a bare commit absorbs whatever is staged there -- a real
#: defect), the evidence line, and the condition that retires it. Removing an entry (fixing
#: the site to CLEAN, or -- for AMBIENT entries -- dispositioning the defect) is the win;
#: adding a new one outside this freeze is a decision each future run must re-justify, never
#: a silent widen. See `test_no_new_bare_commit_sites_outside_known_inventory`'s docstring.
_KNOWN_BARE_COMMIT_SITES: frozenset[tuple[str, str, str]] = frozenset(
    {
        # DR-151: a path-restricted commit silently resets the staged `update-index
        # --chmod=+x` mode under `core.fileMode=false`, so the exec-bit fix this op exists
        # to land would evaporate from the commit it produces. RETIRES when the v2 plan's C4
        # (`docs/plans/2026-08-27-something-must-commit-ceremony-commit-v2.md`) lands and is
        # VERIFIED (not merely coded) to read the mode from the index entry rather than a
        # stat -- see this plan's C3, gated on exactly that confirmation.
        ("coordinator_core/ops/ceremony/commit_exec_bit.py", "_handler", "_git"),
        # Pathspec'd deletions re-stage their worktree copies on Windows git and undo the
        # preceding `git rm --cached`, so the `.gitignore` + deletions commit cannot carry a
        # pathspec without reverting its own prior step. No fix in flight; RETIRES only when
        # a private-index or per-hunk staging design lands for this leg (out of scope here,
        # named in the plan's Out-of-scope section as its own cost against the 500ms
        # brightline).
        (
            "coordinator_core/ops/workday_complete_step2_5_dirty_tree.py",
            "_act_gitignore",
            "_run_git",
        ),
        # SELF-CREATED REPO. `_scratch_git_repo()` builds `tmp` via
        # `tempfile.mkdtemp(prefix="altlive-repo-")` and runs `git init -q` on it before this
        # commit ever fires; no peer's work can be staged in a repo that did not exist a
        # moment earlier. RETIRES if the probe fixture is reworked to stage a real pathspec
        # (no functional need to, since the isolation argument holds regardless).
        (
            "coordinator_core/bash_guards/_alternative_liveness.py",
            "_scratch_git_repo",
            "_run",
        ),
        # SELF-CREATED REPO. Same `_scratch_git_repo()` fixture as above, consumed by this
        # probe to seed one committed state file before triggering `git reset --hard`.
        # RETIRES with the same fixture rework, if ever done.
        (
            "coordinator_core/bash_guards/_alternative_liveness.py",
            "_trigger_destructive_git_revert",
            "_run",
        ),
        # SELF-CREATED REPO. `tmp = tempfile.mkdtemp(prefix="altlive-branchset-")` then
        # `git init -q -b main` on it in the same function, before either bare commit call
        # (the initial seed commit and the branch-candidate commit) fires. RETIRES with a
        # fixture rework, if ever done.
        (
            "coordinator_core/bash_guards/_alternative_liveness.py",
            "_trigger_guard_branch_set_precedence",
            "_run",
        ),
        # SELF-CREATED REPO. `sample()` builds `root` under `tempfile.mkdtemp(...)`, and
        # `build_fixture(root)` runs `_git(["init", "-q", "-b", "probe/baseline"], root)` as
        # the first call in the same function before this bare commit. A one-shot benchmark
        # fixture; RETIRES if the benchmark is reworked to stage a real pathspec.
        (
            "coordinator_core/benchmarks/handoff_supersede_baseline.py",
            "build_fixture",
            "_git",
        ),
        # SELF-CREATED REPO. Guarded by `if not (dest / ".git").is_dir():` -- the `git init
        # str(dest)` call and this `--allow-empty` commit are inside the same conditional
        # branch, so the commit only ever fires immediately after this function itself just
        # created the repo at `dest`. RETIRES if the provisioning step is reworked to avoid a
        # bare commit (no functional need to, since the guard makes it self-contained).
        (
            "coordinator_core/install/first_run.py",
            "provision_stamped_engine",
            "_run",
        ),
        # AMBIENT REPO -- REAL DEFECT. `actual_path` is an operator-selected, pre-existing
        # repo path (from `--root`/interactive selection in the caller's loop over
        # `selected_repos`), not created by this function. Only
        # `docs/coordinator-currency.yaml` is staged via a scoped `git add --`, but the
        # commit itself is bare (`git -C <actual_path> commit -m "..."` with no `--`), so it
        # absorbs anything else already staged in that repo. No fix in flight; surfaced
        # separately as its own disposition item, not fixed under this chunk's write scope.
        (
            "coordinator_core/ops/bootstrap_orchestrate.py",
            "main",
            "subprocess.run",
        ),
        # AMBIENT REPO -- REAL DEFECT. `root_path` comes from `--root`, which
        # `main`'s own arg handling (see the "--root not provided and not inside a git repo"
        # error path) allows to be an EXISTING repo the operator points at; `git init` at
        # this repo is offered interactively and is declinable, never forced. The commit at
        # `["git", "-C", root_path, "commit", "-m", "chore(coordinator): bootstrap"]` carries
        # no pathspec, so it absorbs whatever else is staged in that ambient repo. No fix in
        # flight; surfaced separately as its own disposition item.
        (
            "coordinator_core/ops/bootstrap_repo.py",
            "main",
            "subprocess.run",
        ),
        # SELF-CREATED REPO. `_git_init_and_commit(root)`'s own docstring: "Makes `root` a
        # one-commit git work tree" -- `root` is a `self_test()` scratch fixture dir (`src1`/
        # `src2`, built under a tempdir by the caller), and `git init -q` is the first argv
        # in this same function's loop, before the bare `commit -q -m` that follows it.
        # RETIRES if the fixture is reworked to stage a real pathspec (no functional need
        # to).
        (
            "coordinator_core/ops/percolate_preflight_scratch_publish.py",
            "_git_init_and_commit",
            "subprocess.run",
        ),
        # AMBIENT REPO -- and read the next paragraph before "fixing" it. `dest =
        # context.dest_repo_root` is a real publish mirror handed to this function, not
        # created here, so the classification is correct: `_run_git(["add", "-A"], cwd=dest)`
        # stages everything dirty in that mirror and the commit is bare (no `--`), and the
        # docstring says "no pathspec or manifest is built here."
        #
        # AMBIENT DOES NOT MEAN SAME-SEVERITY-AS-THE-SHARED-TREE, and this entry is the
        # reason that distinction is written down. This collector reports EXPRESSIBILITY and
        # is deliberately blind to whether anyone else stages in the target repo. The publish
        # mirrors are single-owner and percolate REPLACES them wholesale (see
        # `default_manifest_path`'s docstring: the smack path "replaces the destination
        # wholesale and lets `git add -A` derive the change set"), so the swallow-a-peer's-
        # stage failure needs peers staging there, which is not this repo's shape. Residual
        # risk is two concurrent rounds against one mirror, or a human editing in it.
        #
        # THE OBVIOUS FIX IS A TRAP. Full account, the 1,028-file incident and the
        # severity rationale: state/bug-backlog/2026-08-27-percolate-round-bare-commit-
        # absorbs-a-concurrent-round-or-a-hand-edit-in-the-mirror.yaml
        # `kept` is already the exact staged path list, so `commit -m msg -- *kept` looks
        # free. It is not: `kept` deliberately includes staged DELETIONS, and passing deleted
        # paths as pathspecs on Windows re-stages the worktree copies and undoes the removal
        # -- the same hazard that sends `workday_complete_step2_5_dirty_tree.py`'s gitignore
        # leg bare. This function's own comment (its `DIRECTION MATTERS` block) records the
        # outcome when deletions were declined here: 1,028 of 4,045 staging files stuck at
        # mirror HEAD, permanently unremovable, because each round re-staged the deletion and
        # the filter put it back. `add -A` is load-bearing, not laziness -- nothing upstream
        # builds a pathspec to substitute.
        #
        # RETIRES if percolate grows a real manifest-derived pathspec that handles deletions,
        # which is a publish-design change and not a commit-argument change.
        (
            "coordinator_core/percolate/round.py",
            "step_commit",
            "_run_git",
        ),
    }
)


def _relpath(path: pathlib.Path, root: pathlib.Path) -> str:
    try:
        return path.resolve().relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        return path.resolve().relative_to(root.resolve()).as_posix()


def _assert_not_self_scanned(files: list[tuple[str, pathlib.Path]]) -> None:
    """Same re-entrancy sentinel `test_no_unbatched_per_item_git_spawn.py` carries: a silent
    recursion guard would make this gate pass vacuously if `is_test_tree_site`'s filtering
    were ever bypassed or misconfigured."""
    for _relpath_str, file_path in files:
        if file_path.resolve() == _THIS_FILE:
            raise RuntimeError(
                "re-entrancy: the pathspec-less-commit gate scanned its own file "
                f"({_relpath_str}) -- this would make the gate pass vacuously. "
                "is_test_tree_site's test-tree filtering was bypassed or misconfigured."
            )


def _discover_scope_files(roots: tuple[str, ...]) -> list[tuple[str, pathlib.Path]]:
    out: list[tuple[str, pathlib.Path]] = []
    for root_name in roots:
        root = _REPO_ROOT / root_name
        if not root.exists():
            continue
        discovered, _excluded = discover_source_files(root, exclude=DEFAULT_EXCLUDE)
        for _rel_posix, file_path in discovered:
            relpath = _relpath(file_path, root)
            if is_test_tree_site(relpath):
                continue
            out.append((relpath, file_path))
    _assert_not_self_scanned(out)
    return out


#: Files whose source contains no quoted `commit` literal cannot hold a commit argv this
#: collector could resolve, so they are skipped before `ast.parse` rather than parsed and
#: discarded. EMPIRICAL PER-REPO, NOT A STRUCTURAL GUARANTEE -- an earlier
#: revision of this comment called it "not a heuristic narrowing", which overclaimed. Measured
#: both ways over this repo (1234 files -> 43, identical site set, 19 sites either way), and
#: that equality holds today because of the COLLECTOR's blind spot rather than the prefilter's
#: inclusiveness: a `Name` filling a single argv slot (`sub = "commit"; run_git([sub, ...])`)
#: is never resolved to its value, so `find_commit_sites` misses it whether or not the
#: prefilter admitted the file -- and it WOULD admit it, the literal being present in the text.
#: Re-measure both branches after any change to argv resolution: the equality is a fact about
#: this repo and this collector, not a property the prefilter supplies.
#:
#: THE RESIDUAL, named rather than left for a reader to discover: an argv whose subcommand is
#: built dynamically (a variable, a concatenation) carries no quoted literal and is skipped.
#: That costs the gate nothing, because such an argv is outside this collector's STATIC reach
#: with or without the prefilter -- it resolves to no subcommand at all, prefiltered or not.
#: A future collector that resolves dynamic subcommands -- a whole-list
#: splat or a single-slot `Name`, one class of gap and not two -- must drop this prefilter in the same
#: change, or it will silently keep the old blind spot while appearing to have closed it.
#:
#: WHY IT EXISTS: the unfiltered walk cost 3723ms, over this repo's own ">2s for any process
#: is FORBIDDEN" load norm, in the fast tier, on a box running ~50 concurrent sessions. A
#: standing gate is paid by every peer on every fast run; 3.7s of AST parsing to answer a
#: question about 43 files is a cost this guard has no right to impose.
_COMMIT_LITERALS = ('"commit"', "'commit'")


def _may_hold_commit_argv(text: str) -> bool:
    return any(lit in text for lit in _COMMIT_LITERALS)


def _collect_all_sites() -> list[CommitSite]:
    sites: list[CommitSite] = []
    for relpath, file_path in _discover_scope_files(_GATE_SCOPE_ROOTS):
        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not _may_hold_commit_argv(text):
            continue
        try:
            sites.extend(find_commit_sites(text, relpath))
        except SyntaxError:
            continue
    return sites


def test_no_new_bare_commit_sites_outside_known_inventory():
    """THE STANDING GATE. Red only when a non-CLEAN (`dirty` or `unknown`) commit site's
    `(path, enclosing, callee)` key is not already in `_KNOWN_BARE_COMMIT_SITES`.

    A red result here on a repo with sites this plan's baton deliberately did NOT read
    (`bootstrap_repo.py`, `bootstrap_orchestrate.py`,
    `percolate_preflight_scratch_publish.py`, `tracker/push_suggestion.py`) is the guard
    doing its job on its first run, not a bug in the guard -- see the dispatch report for
    which of those four actually landed outside the inventory. Widening
    `_KNOWN_BARE_COMMIT_SITES` to make this pass, without a named reason for the new entry,
    is the exact failure this gate exists to prevent."""
    sites = _collect_all_sites()
    non_clean = [site for site in sites if site.verdict != "clean"]
    unexpected = sorted(
        {site.key for site in non_clean if site.key not in _KNOWN_BARE_COMMIT_SITES}
    )
    assert not unexpected, (
        "commit call site(s) outside the frozen inventory can express a pathspec-less "
        f"commit: {unexpected}. Each needs either a pathspec (fixing it) or a new, reasoned "
        "inventory entry (freezing it) -- never a silent widen."
    )


def test_planted_bare_commit_is_caught():
    """Proves the gate can actually go red: a synthetic call shaped exactly like the DIRTY
    calibration site must classify as `dirty`."""
    src = (
        "def _act():\n"
        "    return _run_git(['commit', '-m', 'msg'])\n"
    )
    sites = find_commit_sites(src, "planted.py")
    assert sites and all(site.verdict == "dirty" for site in sites)


def test_planted_pathspec_commit_is_not_flagged():
    """The paired negative control: an otherwise-identical call carrying an explicit `--
    <path>` pathspec must classify as `clean`. See module docstring's negative-spec block --
    this proves only that the static read does not false-positive a scoped call, never that
    the resulting commit is safe at runtime."""
    src = (
        "def _act():\n"
        "    return _run_git(['commit', '-m', 'msg', '--', 'path/to/file'])\n"
    )
    sites = find_commit_sites(src, "planted.py")
    assert sites and all(site.verdict == "clean" for site in sites)


def test_planted_unresolved_splat_reads_unknown_never_clean():
    """An argv built from a splat this pass cannot trace to a literal must report `unknown`
    -- never fold into `clean` merely because no pathspec marker happened to be visible, and
    never fold into `dirty` either, since the untraced tail could itself carry one."""
    src = (
        "def _act(extra):\n"
        "    return _run_git(['commit', '-m', 'msg', *extra])\n"
    )
    sites = find_commit_sites(src, "planted.py")
    assert sites and all(site.verdict == "unknown" for site in sites)


def test_planted_pathspec_from_file_fstring_is_clean():
    """`--pathspec-from-file=` spelled as an f-string (as `git_native.py ::
    commit_with_message_file_pathspec_scoped` does) must classify CLEAN -- proves
    `_joined_str_prefix` and not just plain string constants."""
    src = (
        "def _act(pathspec_file):\n"
        "    return _run_git(['commit', '-F', 'msg', f'--pathspec-from-file={pathspec_file}'])\n"
    )
    sites = find_commit_sites(src, "planted.py")
    assert sites and all(site.verdict == "clean" for site in sites)


def test_calibration_git_native_pathspec_from_file_reads_clean():
    """`ops/ceremony/git_native.py :: commit_with_message_file_pathspec_scoped` -- the
    `--pathspec-from-file` + `--pathspec-file-nul` arm named in the plan's Problem section as
    "clean on inspection". Must read CLEAN."""
    path = "coordinator_core/ops/ceremony/git_native.py"
    text = (_REPO_ROOT / path).read_text(encoding="utf-8")
    sites = [
        site
        for site in find_commit_sites(text, path)
        if site.enclosing == "commit_with_message_file_pathspec_scoped"
    ]
    assert sites and all(site.verdict == "clean" for site in sites)


def test_calibration_apply_base_scoped_commit_reads_clean():
    """`contract/apply_base.py :: scoped_commit` -- `pathspec = ["--", str(resolved)]` built
    unconditionally, then spliced into the commit argv. Must read CLEAN; this is the plan's
    named MUST-READ-CLEAN calibration shape."""
    path = "coordinator_core/contract/apply_base.py"
    text = (_REPO_ROOT / path).read_text(encoding="utf-8")
    sites = [site for site in find_commit_sites(text, path) if site.enclosing == "scoped_commit"]
    assert sites and all(site.verdict == "clean" for site in sites)


def test_calibration_workday_complete_separates_two_sites_in_one_file():
    """`ops/workday_complete_step2_5_dirty_tree.py` carries both calibration shapes:
    `_act_gitignore`'s bare commit (MUST read DIRTY, and is the plan's second frozen
    inventory entry) and `_act_commit`'s pathspec'd commit (MUST read CLEAN) -- proving the
    collector separates them rather than tarring the whole module with one verdict."""
    path = "coordinator_core/ops/workday_complete_step2_5_dirty_tree.py"
    text = (_REPO_ROOT / path).read_text(encoding="utf-8")
    sites = find_commit_sites(text, path)
    by_enclosing = {site.enclosing: site for site in sites}
    assert by_enclosing["_act_gitignore"].verdict == "dirty"
    assert by_enclosing["_act_commit"].verdict == "clean"
