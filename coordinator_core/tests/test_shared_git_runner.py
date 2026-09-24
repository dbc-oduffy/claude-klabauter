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
  - Cross-MODULE indirection is invisible. A module importing another
    module's private `_run_git` is not counted here; the DEFINING module
    already is, and counting the importer too would double-charge one defect.
  - A dial reaching its spawn through more than one hop of local binding
    (`bound = _GIT_TIMEOUT; _run_git(args, timeout=bound)`) is invisible.
    One-hop resolution only, matching `spawn_policy`'s own scope.

A THIRD REGISTER, ADDED LATER (`_FROZEN_DESTRUCTIVE_VERB_SITES`). The two
registers above ask WHO SPAWNS: migrate a module onto `git.run` and both
clear at once. Neither asks WHAT VERB -- a module already inside
`_GRANDFATHERED_RUNNER_MODULES`, or one that has already migrated onto
`run_git`, can add a `reset --hard` today and nothing above objects, because
migration is exactly the axis those two registers watch and this one is
orthogonal to it. `docs/reference/git-action-seam-carve-outs.md`'s "Not in
scope" paragraph named that gap and handed it here. The verb register is a
frozen, shrink-only, subset-asserted inventory in the same shape as the two
above, keyed on (module, enclosing function, verb) rather than (module,
enclosing) -- the verb is part of identity, since fixing one destructive
call while leaving a sibling one in the same function must not look like a
null diff. It shares this file's traversal helpers (`_leaf_name`,
`_argv_exprs`, `_resolved_git_names`, `_enclosing_names`,
`_generic_runner_names`, `_SPAWN_API_NAMES`) but stays a SEPARATE collector
function from `_collect_module` -- one parameterised walk would blur the
WHO-SPAWNS/WHAT-VERB distinction into a flag. Its scope is narrower than the
two registers above: `coordinator_core/**` only, matching this plan's file
scope (`docs/plans/2026-09-11-destructive-git-guards-are-action-shaped.md`
§ File scope) -- `coordinator/bin` and `coordinator/lib` are not swept for
this axis. Verb detection covers two spawn shapes: element 0 of a
`run_git`- or `_run_git`-shaped argv, and element 1 of a raw `["git", ...]`
(or `which`-resolved-head) argv literal. Migrating a call from a private
runner onto `run_git` does NOT clear a row here, and must not: `run_git(["reset",
"--hard"])` is exactly as destructive as the private-runner spelling it
replaced. KNOWN BLIND SPOTS for this register are the same as the two
above -- `shell=True` string form and cross-module indirection are
invisible -- plus one more of its own: a destructive verb reached only
through more than one hop of local binding (a name holding `"reset"` passed
through a second function before reaching the argv) is invisible, matching
the runner register's one-hop-only dial resolution. This register does NOT
assert refusal or safety -- it enumerates and ratchets visibility only (no
deny semantics; see the plan's Anti-scope).

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

#: Modules that must NEVER migrate onto the seam, and are therefore exempt
#: rather than grandfathered. The distinction is the whole point: a
#: grandfathered row says "has not migrated yet" and is expected to shrink
#: away; a row here says "migrating would BREAK this module", so it is not
#: debt and must not be counted as any.
#:
#: `remove-claude-klabauter-precommit-hook.py` removes an installed `.git/hooks/
#: pre-commit` gate chain, and its ratified negative spec is that it never
#: imports `coordinator_core` at all -- it has to keep running after the
#: installer op module and its CLI trampoline are deleted, which is the
#: precise failure it exists to route around
#: (`docs/plans/2026-08-25-the-staged-rollback-gate-dies-without-blocking-a-
#: commit.md` C1, DR-359). Importing `git.run` to satisfy this gate would
#: hand the remover the dependency whose absence is its reason to exist.
#:
#: An entry here is NOT free: `test_contract_exempt_modules_still_declare_
#: their_standalone_contract` fails the moment the docstring stops saying so,
#: which is what stops this from becoming a second, softer register.
_CONTRACT_EXEMPT_MODULES: frozenset[str] = frozenset(
    {"coordinator/bin/remove-claude-klabauter-precommit-hook.py"}
)

#: Same roots the amplification gate scans, plus `coordinator/lib`: the G7
#: census found the identical defect on both sides of the core/CLI seam
#: (`percolate-round.py`'s inherited 600 and `workday_ceremony_lib.py :: git()`'s
#: inherited 300 are the CLI half), so scoping this to `coordinator_core`
#: would gate the smaller side of one problem.
_GATE_SCOPE_ROOTS: tuple[str, ...] = ("coordinator_core", "coordinator/bin", "coordinator/lib")

#: Keyword names that carry an argv at a spawn call site.
_ARGV_KEYWORDS: frozenset[str] = frozenset({"args", "argv", "cmd", "program_args"})

#: Verbs a git subprocess argument makes destructive (plan AC2). A
#: module-level literal, no wildcard. `commit` is forced in even though the
#: plan's own census row 9 seed omitted it -- it is the verb in the
#: 2026-08-06 incident this baton's own bug entry records.
_DESTRUCTIVE_VERBS: frozenset[str] = frozenset(
    {
        "add",
        "am",
        "apply",
        "branch",
        "checkout",
        "cherry-pick",
        "clean",
        "commit",
        "filter-branch",
        "gc",
        "mv",
        "prune",
        "push",
        "rebase",
        "reset",
        "restore",
        "revert",
        "rm",
        "stash",
        "switch",
        "tag",
        "update-ref",
        "worktree",
    }
)

#: Leaf names this collector treats as a `run_git`-shaped call -- the
#: canonical seam and the private-runner spelling most modules in this tree
#: still carry. WHO spawns is G7's question; this collector only asks WHAT
#: VERB, so both spellings count identically.
_RUN_GIT_LEAF_NAMES: frozenset[str] = frozenset({"run_git", "_run_git"})

#: Narrower than `_GATE_SCOPE_ROOTS`: `coordinator_core/**` only, matching
#: this plan's file scope. `coordinator/bin` and `coordinator/lib` are the
#: CLI half of G7's population and are not this baton's file scope.
_VERB_GATE_SCOPE_ROOTS: "tuple[str, ...]" = ("coordinator_core",)


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


def _is_which_git(node: ast.AST) -> bool:
    """True for a `shutil.which("git")` call (however `which` is imported)."""
    return (
        isinstance(node, ast.Call)
        and _leaf_name(node.func) == "which"
        and bool(node.args)
        and isinstance(node.args[0], ast.Constant)
        and node.args[0].value == "git"
    )


def _resolved_git_names(tree: ast.Module) -> "frozenset[str]":
    """Names in this module bound to a RESOLVED git binary path.

    The blind spot this closes, found 2026-08-25: a module that writes
    `git_bin = shutil.which("git")` and then spawns `[git_bin, "-C", ...]`
    carries no literal `"git"` head, so the head-matching below saw nothing
    and the module spawned git entirely outside this gate's inventory. Five
    modules were in that state when it was found (`git/ls_files.py`,
    `git/ls_files_bytes.py`,
    `ops/normalize_env.py`), all since migrated onto `run_git` -- which is
    why no register row was needed for any of them, and why this detector
    must stay: nothing else would have said so.

    Two binding shapes, both seen in that set: assignment straight from
    `which("git")`, and assignment from a module-local zero-argument function
    that wraps one (`auto_push.git_exe()`'s shape). Deliberately NOT a
    name-pattern match on `git_bin`/`git_exe` -- a register this gate freezes
    must key on what a module DOES, not on what it named a variable."""
    resolver_funcs = {
        fn.name
        for fn in ast.walk(tree)
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(_is_which_git(c) for c in ast.walk(fn))
    }
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        resolved_here = any(_is_which_git(c) for c in ast.walk(value)) or (
            isinstance(value, ast.Call) and _leaf_name(value.func) in resolver_funcs
        )
        if not resolved_here:
            continue
        names.update(t.id for t in targets if isinstance(t, ast.Name))
    return frozenset(names)


def _git_argv_names(tree: ast.Module) -> "frozenset[str]":
    """Names in this module bound directly to a git-headed `["git", ...]`
    (or tuple) LITERAL -- the one-hop binding shape `_carries_git_argv`
    cannot see on its own, since it walks the argv EXPRESSION at the spawn
    call site and a bare name there carries no `"git"` literal to find:

        cmd = ["git", "ls-files", ...]
        subprocess.run(cmd)

    Mirrors `_resolved_git_names`'s shape (walk every module/function-local
    `Assign`/`AnnAssign`, collect `Name` targets) rather than a second
    bespoke traversal -- one binding pattern, resolved the same way
    regardless of what the bound value turns out to be. The accumulate
    pair `argv = ["git"]; argv += args` is covered without a separate
    `AugAssign` case: the initial literal `Assign` is what binds the name
    here, and the later re-bind stays git-headed."""
    names: set = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        if not (isinstance(value, (ast.List, ast.Tuple)) and value.elts):
            continue
        head = value.elts[0]
        if isinstance(head, ast.Constant) and head.value == "git":
            names.update(t.id for t in targets if isinstance(t, ast.Name))
    return frozenset(names)


def _carries_git_argv(
    expr: ast.expr,
    resolved_names: "frozenset[str]" = frozenset(),
    argv_names: "frozenset[str]" = frozenset(),
) -> bool:
    """True if `expr` contains an argv whose HEAD is git -- the
    `["git", ...]` (or tuple) literal, a name `resolved_names` says holds a
    resolved git binary path, or a bare name `argv_names` says is itself
    already bound to a git-headed argv literal.

    Walks rather than matching the node directly, because the argv is
    routinely a composition: `["git", "-C", root] + args`, `["git", *args]`,
    `["git", "log"] + list(paths)`. All three carry the same literal head.
    The bare-name case is checked separately, one hop only, matching this
    gate's other one-hop resolutions: `expr` itself is the whole call-site
    argv, so a Name found anywhere else inside a larger composition is not
    treated as carrying it."""
    if isinstance(expr, ast.Name) and expr.id in argv_names:
        return True
    for node in ast.walk(expr):
        if isinstance(node, (ast.List, ast.Tuple)) and node.elts:
            head = node.elts[0]
            if isinstance(head, ast.Constant) and head.value == "git":
                return True
            if isinstance(head, ast.Name) and head.id in resolved_names:
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
    resolved_names = _resolved_git_names(tree)
    argv_names = _git_argv_names(tree)

    def is_git_spawn(call: ast.Call) -> bool:
        name = _leaf_name(call.func)
        if name is None or (name not in _SPAWN_API_NAMES and name not in generics):
            return False
        return any(
            _carries_git_argv(argv, resolved_names, argv_names) for argv in _argv_exprs(call)
        )

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


def _scope_files(roots: "tuple[str, ...]" = None) -> list:
    """Every non-test source file under `roots` (default `_GATE_SCOPE_ROOTS`),
    as `(repo-relative posix path, absolute path)`. Reuses `spawn_policy`'s
    traversal and test-tree partition rather than mirroring them -- the
    amplification gate's own docstring records the census and the gate
    disagreeing twice on scope when each walked the tree its own way.

    Parameterised over `roots` so the verb register below can sweep the
    narrower `_VERB_GATE_SCOPE_ROOTS` without a second copy of this
    traversal."""
    if roots is None:
        roots = _GATE_SCOPE_ROOTS
    out: list = []
    for root_name in roots:
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
            if (
                is_test_tree_site(relpath)
                or relpath == _PRIMITIVE_MODULE
                or relpath in _CONTRACT_EXEMPT_MODULES
            ):
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


@dataclasses.dataclass(frozen=True)
class DestructiveVerbSite:
    """One call site reaching a git subprocess with a destructive verb.
    Keyed on (module, enclosing, verb) -- unlike `GitSpawnSite`, the verb IS
    part of identity here: a module fixing one destructive call while
    leaving a sibling destructive call in the same function must not look
    like a null diff against this register."""

    module: str
    enclosing: str
    verb: str


def _verb_from_constant(node: ast.expr) -> "str | None":
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _raw_git_argv_verb(expr: ast.expr, resolved_names: "frozenset[str]") -> "str | None":
    """Element 1 of a `["git", ...]` (or `which`-resolved-head) literal
    found anywhere inside `expr`, if that element is a string constant.
    Walks `expr` rather than matching it directly for the same reason
    `_carries_git_argv` does -- the argv is routinely a composition."""
    for node in ast.walk(expr):
        if isinstance(node, (ast.List, ast.Tuple)) and len(node.elts) >= 2:
            head = node.elts[0]
            is_git_head = (isinstance(head, ast.Constant) and head.value == "git") or (
                isinstance(head, ast.Name) and head.id in resolved_names
            )
            if not is_git_head:
                continue
            verb = _verb_from_constant(node.elts[1])
            if verb is not None:
                return verb
    return None


def _collect_verb_sites(relpath: str, source: str) -> list:
    """One module's destructive-verb sites. Shares `_leaf_name`,
    `_argv_exprs`, `_resolved_git_names`, `_enclosing_names`,
    `_generic_runner_names` and `_SPAWN_API_NAMES` with `_collect_module`
    above -- the shared traversal the module docstring's third-register
    section requires -- but stays a SEPARATE function: `_collect_module`
    asks WHO SPAWNS (migrating onto `git.run` clears it) and this one asks
    WHAT VERB (migrating does not clear it, and must not). One
    parameterised walk would blur that distinction into a flag."""
    tree = ast.parse(source)
    enclosing = _enclosing_names(tree)
    resolved_names = _resolved_git_names(tree)
    generics = _generic_runner_names(tree)
    sites: set = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _leaf_name(node.func)
        if name is None:
            continue
        is_run_git_shaped = name in _RUN_GIT_LEAF_NAMES
        if not (is_run_git_shaped or name in _SPAWN_API_NAMES or name in generics):
            continue
        for argv in _argv_exprs(node):
            verb = None
            if is_run_git_shaped and isinstance(argv, (ast.List, ast.Tuple)) and argv.elts:
                verb = _verb_from_constant(argv.elts[0])
            if verb is None:
                verb = _raw_git_argv_verb(argv, resolved_names)
            if verb is not None and verb in _DESTRUCTIVE_VERBS:
                sites.add(
                    DestructiveVerbSite(
                        module=relpath,
                        enclosing=enclosing.get(id(node), "<module>"),
                        verb=verb,
                    )
                )
    return sorted(sites, key=lambda s: (s.module, s.enclosing, s.verb))


def collect_destructive_verb_sites() -> list:
    """Sweep `_VERB_GATE_SCOPE_ROOTS`. Same skip-on-parse-error rule as
    `collect_private_git_runners`, for the identical reason -- a shared tree
    carries a peer session's mid-write file, and this gate has no opinion
    about that."""
    sites: list = []
    for relpath, path in _scope_files(_VERB_GATE_SCOPE_ROOTS):
        try:
            source = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        try:
            sites.extend(_collect_verb_sites(relpath, source))
        except (SyntaxError, ValueError):
            continue
    return sites


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
        "coordinator/bin/coordinator-prepare-commit-msg.py",
        "coordinator/bin/coordinator-safe-commit.py",
        "coordinator/bin/cross-repo-memo.py",
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
        "coordinator/bin/parallel-review-gate-decision.py",
        "coordinator/bin/percolate-full-payload-proof.py",
        "coordinator/bin/percolate-push.py",
        "coordinator/bin/percolate-round.py",
        "coordinator/bin/probe-prereq.py",
        "coordinator/bin/publish-allowlist-generate.py",
        "coordinator/bin/publish.py",
        "coordinator/bin/reap-integrated-review-findings.py",
        "coordinator/bin/reap-stale-subagent-sidecars.py",
        "coordinator/bin/record-platform-outcome.py",
        "coordinator/bin/red-set-report.py",
        "coordinator/bin/refresh-plugin-live-install.py",
        "coordinator/bin/regen-cockpit-schema.py",
        "coordinator/bin/repo-setup-args-and-register.py",
        "coordinator/bin/repomap/generate-repomap.py",
        "coordinator/bin/spinoff-deliverable-and-commit.py",
        "coordinator/bin/standup.py",
        "coordinator/bin/test-fixtures/check-workstream-complete-deletion-blocks/run-smoke.py",
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
        "coordinator_core/cartography/file_index.py",
        "coordinator_core/cartography/tree.py",
        "coordinator_core/chain_attribution.py",
        "coordinator_core/consolidate_assemble/__init__.py",
        "coordinator_core/consolidate_assemble/apply.py",
        "coordinator_core/coverage.py",
        "coordinator_core/dag.py",
        "coordinator_core/diff_scoped_tests.py",
        "coordinator_core/distill/delete_guard.py",
        "coordinator_core/frontmatter/schema_validate.py",
        "coordinator_core/git/commit_signing.py",
        "coordinator_core/git/divergence.py",
        "coordinator_core/git/repo_root.py",
        "coordinator_core/git_scope.py",
        "coordinator_core/hooks/auto_push.py",
        "coordinator_core/hooks/context_pressure_precompact.py",
        "coordinator_core/hooks/day_branch_assert.py",
        "coordinator_core/hooks/example_retrieval_repo_detect.py",
        "coordinator_core/hooks/subagent_fabrication_check.py",
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
        "coordinator_core/ops/configure_git.py",
        "coordinator_core/ops/create_github_remote.py",
        "coordinator_core/ops/cruft_sweep.py",
        "coordinator_core/ops/cutover_gate.py",
        "coordinator_core/ops/detect_changed_dependency_manifests.py",
        "coordinator_core/ops/detect_project_runtime.py",
        "coordinator_core/ops/dirty_tree_gate.py",
        "coordinator_core/ops/doc_staleness.py",
        "coordinator_core/ops/dod_floor_ratchet.py",
        "coordinator_core/ops/draft_plan_aging.py",
        "coordinator_core/ops/emit/context.py",
        "coordinator_core/ops/emit/doe_drift.py",
        "coordinator_core/ops/emit/enrich.py",
        "coordinator_core/ops/emit/resolvers.py",
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
        "coordinator_core/ops/reap_in_flight_claims.py",
        "coordinator_core/ops/reap_orphaned_agent_dirs.py",
        "coordinator_core/ops/record_history.py",
        "coordinator_core/ops/release_tagging.py",
        "coordinator_core/ops/renormalize_index.py",
        "coordinator_core/ops/resolve_swept_baton.py",
        "coordinator_core/ops/review_brightline_gate.py",
        "coordinator_core/ops/review_coverage_core.py",
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
        "coordinator_core/percolate/store.py",
        "coordinator_core/pickup_assemble/__init__.py",
        "coordinator_core/plan_assemble/predicates/composition_graph.py",
        "coordinator_core/plan_assemble/predicates/concurrent_preflight.py",
        "coordinator_core/plan_assemble/predicates/substrate_scans.py",
        "coordinator_core/plugin_health/drift.py",
        "coordinator_core/plugin_health/release_currency.py",
        "coordinator_core/quick_wrap_assemble/__init__.py",
        "coordinator_core/reconcile/ac27_differential_oracle.py",
        "coordinator_core/review_assemble/residue.py",
        "coordinator_core/session/shape.py",
        "coordinator_core/session_attribution.py",
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
        ("coordinator/bin/coordinator-prepare-commit-msg.py", "_resolve_staged_paths(timeout)"),
        ("coordinator/bin/lib/workday_ceremony_lib.py", "git(timeout)"),
        ("coordinator/bin/parallel-review-gate-decision.py", "_GIT_TIMEOUT_SECS"),
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
        ("coordinator_core/ops/ceremony/git_native.py", "_DEFAULT_TIMEOUT_SECS"),
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
_PINNED_RUNNER_CEILING = 196
_PINNED_DIAL_CEILING = 70

#: Frozen inventory of destructive-verb call sites (plan AC2/AC3). FROZEN
#: 2026-09-19 over a full run of `collect_destructive_verb_sites()` across
#: `coordinator_core` -- 41 sites. Several of these are READS whose
#: subcommand happens to share a destructive verb's spelling
#: (`branch --show-current`, `worktree list`, `stash list`) -- the plan's
#: own census row 5 named these same files, and the register enumerates the
#: VERB, not read-vs-write: "was this call safe" is exactly the judgment
#: this baton declines to hand an AST collector (plan § "The fork is
#: closed; what is left is a notch"; no deny semantics, see Anti-scope).
#:
#: SHRINK-ONLY, same rule and same remedy as the two registers above: do
#: not add a row to silence a new site -- route the call through
#: `coordinator_core.git.run` if it is not already, or accept in writing
#: that the site is a known, intended destructive action.
_FROZEN_DESTRUCTIVE_VERB_SITES: frozenset = frozenset(
    {
        ("coordinator_core/backlog_grind_assemble/apply.py", "_commit_one", "commit"),
        ("coordinator_core/backlog_grind_assemble/apply.py", "_dispatch_checkout_and_backlog_note", "checkout"),
        ("coordinator_core/backlog_grind_assemble/apply.py", "_stage_paths", "add"),
        ("coordinator_core/backlog_grind_assemble/apply.py", "_unstage_paths", "reset"),
        ("coordinator_core/bash_guards/_alternative_liveness.py", "_scratch_git_repo", "add"),
        ("coordinator_core/bash_guards/_alternative_liveness.py", "_scratch_git_repo", "commit"),
        ("coordinator_core/bash_guards/_alternative_liveness.py", "_trigger_destructive_git_revert", "add"),
        ("coordinator_core/bash_guards/_alternative_liveness.py", "_trigger_destructive_git_revert", "commit"),
        ("coordinator_core/bash_guards/dispatch_checks.py", "_bt_add_subtree_foreign_paths", "add"),
        ("coordinator_core/baton_assemble/apply.py", "_cleanup_orphan_on_commit_pipeline_error", "reset"),
        ("coordinator_core/benchmarks/handoff_supersede_baseline.py", "build_fixture", "add"),
        ("coordinator_core/benchmarks/handoff_supersede_baseline.py", "build_fixture", "commit"),
        ("coordinator_core/benchmarks/maintenance_tier_budget.py", "_make_throwaway_clone", "add"),
        ("coordinator_core/consolidate_assemble/__init__.py", "branches_merged_into", "branch"),
        ("coordinator_core/consolidate_assemble/__init__.py", "list_worktrees", "worktree"),
        ("coordinator_core/consolidate_assemble/apply.py", "_clean_cherry_pick_conflict", "checkout"),
        ("coordinator_core/consolidate_assemble/apply.py", "_clean_cherry_pick_conflict", "cherry-pick"),
        ("coordinator_core/consolidate_assemble/apply.py", "_delete_branch", "branch"),
        ("coordinator_core/consolidate_assemble/apply.py", "_delete_branch", "push"),
        ("coordinator_core/consolidate_assemble/apply.py", "_dispatch_cherry_pick_and_delete", "cherry-pick"),
        ("coordinator_core/consolidate_assemble/apply.py", "_dispatch_worktree_prune", "worktree"),
        ("coordinator_core/consolidate_assemble/apply.py", "_dispatch_worktree_remove", "worktree"),
        ("coordinator_core/contract/apply_base.py", "scoped_commit", "add"),
        ("coordinator_core/contract/apply_base.py", "scoped_commit", "commit"),
        ("coordinator_core/hooks/day_branch_assert.py", "_current_branch", "branch"),
        ("coordinator_core/merge_assemble/__init__.py", "compute_version_bump_proposal", "tag"),
        ("coordinator_core/ops/ceremony/detached_render_commit.py", "commit_own_artifact", "add"),
        ("coordinator_core/ops/fan_out_integrator.py", "_git_current_branch", "branch"),
        ("coordinator_core/ops/fleet_machinery_sweep.py", "_git_rm_cached", "rm"),
        ("coordinator_core/ops/git_maintenance.py", "run_tier", "prune"),
        ("coordinator_core/ops/propagate_body.py", "_commit_delivery", "add"),
        ("coordinator_core/ops/propagate_body.py", "_commit_delivery", "reset"),
        ("coordinator_core/ops/propagate_body.py", "_commit_delivery", "update-ref"),
        ("coordinator_core/ops/renormalize_index.py", "_git_add_pathspec_from_stdin", "add"),
        ("coordinator_core/ops/workday_complete_step2_5_dirty_tree.py", "_act_gitignore", "add"),
        ("coordinator_core/ops/workday_complete_step2_5_dirty_tree.py", "_act_gitignore", "commit"),
        ("coordinator_core/ops/workday_complete_step2_5_dirty_tree.py", "_act_gitignore", "rm"),
        ("coordinator_core/ops/workday_surface_stale_stash_entries.py", "_run_stash_list", "stash"),
        ("coordinator_core/orient_assemble/readers_branch_reconcile.py", "_current_branch", "branch"),
        ("coordinator_core/percolate/round.py", "step_commit", "add"),
        ("coordinator_core/percolate/round.py", "step_commit", "commit"),
    }
)

#: Independent second copy of the register's size, literal for the same
#: reason `_PINNED_RUNNER_CEILING` is (see that constant's comment) --
#: importing the value under test would make this file agree with any
#: register whatsoever and assert nothing.
_PINNED_VERB_CEILING = 41


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


# These two gates were
# already red before the 00814cf56f ceiling drop, against modules unrelated to
# review-trail retirement (e.g. scope_orphan_census.py, session/scope.py). The
# ceiling drop shrinks the registers correctly; it does not touch, cause, or
# claim to fix these two pre-existing failures.
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
    the tree needs one more private git runner than it had yesterday.

    Equality, not `<=`: the two self-invalidation legs
    (`test_every_grandfathered_runner_still_spawns_git`,
    `test_every_grandfathered_dial_still_bounds_a_git_spawn`) already force
    a live register down to exactly its ceiling on any drift, so slack
    between `len(...)` and the pinned ceiling can only ever be a forgotten
    manual edit, never an observed state. A `<=` here would let that slack
    sit unnoticed instead of failing loudly."""
    assert len(_GRANDFATHERED_RUNNER_MODULES) == _PINNED_RUNNER_CEILING, (
        f"the runner register holds {len(_GRANDFATHERED_RUNNER_MODULES)} rows, not "
        f"the pinned {_PINNED_RUNNER_CEILING}. It shrinks only. A new module needing "
        f"a git read calls coordinator_core.git.run.run_git; it does not join this "
        f"list. A register smaller than its ceiling means a row was removed without "
        f"lowering the ceiling to match -- do that in the same diff."
    )
    assert len(_GRANDFATHERED_DIALS) == _PINNED_DIAL_CEILING, (
        f"the dial register holds {len(_GRANDFATHERED_DIALS)} rows, not the pinned "
        f"{_PINNED_DIAL_CEILING}. It shrinks only. The two bounds a git spawn may "
        f"carry live in coordinator_core.git.run. A register smaller than its "
        f"ceiling means a row was removed without lowering the ceiling to match -- "
        f"do that in the same diff."
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


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            'import shutil, subprocess\n'
            '_PROBE_TIMEOUT_SECS = 30\n'
            'def probe(root):\n'
            '    git_bin = shutil.which("git")\n'
            '    return subprocess.run([git_bin, "-C", root, "status"], timeout=_PROBE_TIMEOUT_SECS)\n',
            id="assigned-straight-from-which",
        ),
        pytest.param(
            'import shutil, subprocess\n'
            '_PROBE_TIMEOUT_SECS = 30\n'
            'def git_exe():\n'
            '    return shutil.which("git")\n'
            'def probe(root):\n'
            '    resolved = git_exe()\n'
            '    return subprocess.run([resolved, "-C", root, "status"], timeout=_PROBE_TIMEOUT_SECS)\n',
            id="assigned-from-a-local-resolver",
        ),
    ],
)
def test_collector_sees_a_which_resolved_argv_head(source):
    """The blind spot, pinned. A module resolving git through
    `shutil.which("git")` and spawning `[git_bin, ...]` carries no literal
    `"git"` head; before 2026-08-25 the collector saw nothing and the module
    spawned git wholly outside this register. Five modules were in that state
    when it was found, and the only reason none of them needed a row is that
    all five were migrated onto `run_git` in the same change -- which this
    gate could not have demanded, because it could not see them.

    Both binding shapes are pinned because both were live in that set: the
    direct `which` assignment, and the local zero-arg resolver
    (`hooks/auto_push.py :: git_exe`). The dial half rides along and is
    asserted here too: a dial only counts as a GIT dial when its module
    spawns git, so a collector blind to the spawn was equally blind to the
    module constant bounding it.
    """
    sites, dials = _collect_module("fake/probe.py", source)

    assert [s.enclosing for s in sites] == ["probe"]
    assert dials == [("fake/probe.py", "_PROBE_TIMEOUT_SECS")]


@pytest.mark.parametrize(
    "source",
    [
        pytest.param(
            'import subprocess\n'
            '_PROBE_TIMEOUT_SECS = 30\n'
            'def probe(root):\n'
            '    cmd = ["git", "-C", root, "ls-files"]\n'
            '    return subprocess.run(cmd, timeout=_PROBE_TIMEOUT_SECS)\n',
            id="one-hop-literal-binding",
        ),
        pytest.param(
            'import subprocess\n'
            '_PROBE_TIMEOUT_SECS = 30\n'
            'def probe(args, root):\n'
            '    argv = ["git"]\n'
            '    argv += ["-C", root]\n'
            '    argv += args\n'
            '    return subprocess.run(argv, timeout=_PROBE_TIMEOUT_SECS)\n',
            id="accumulate-augassign-binding",
        ),
    ],
)
def test_collector_sees_a_one_hop_bound_argv_name(source):
    """The blind spot this gate's own docstring did not yet name: the
    argv-expression walk at the call site sees only the EXPRESSION passed
    to the spawn, so a name bound to a `["git", ...]` literal one line
    earlier -- `cmd = ["git", ...]; subprocess.run(cmd)` -- carried no
    literal `"git"` head at the call itself and was invisible, the same way
    a `which`-resolved head was invisible before `_resolved_git_names`
    closed that gap. `_git_argv_names` closes this one the same way, and
    the accumulate shape (`argv = ["git"]; argv += args`) needs no separate
    case: the initial literal `Assign` is what binds the name, and the
    later re-bind stays git-headed."""
    sites, dials = _collect_module("fake/one_hop.py", source)

    assert [s.enclosing for s in sites] == ["probe"]
    assert dials == [("fake/one_hop.py", "_PROBE_TIMEOUT_SECS")]


def test_contract_exempt_modules_still_declare_their_standalone_contract():
    """Self-invalidation for the exemption list, the same rule the two
    registers get. An exemption whose reason has quietly gone away is worse
    than no exemption: it is a standing, reviewed-looking permission for a
    module that could now migrate like any other. The reason here is a
    RATIFIED negative spec in the module's own docstring, so that is what is
    checked -- if someone deletes the "never imports coordinator_core" line,
    the exemption dies with it and the module owes a migration.
    """
    for relpath in sorted(_CONTRACT_EXEMPT_MODULES):
        path = _REPO_ROOT / relpath
        assert path.is_file(), (
            f"_CONTRACT_EXEMPT_MODULES names {relpath}, which no longer exists "
            "-- delete the row"
        )
        source = path.read_text(encoding="utf-8", errors="replace")
        docstring = ast.get_docstring(ast.parse(source)) or ""
        assert "coordinator_core" in docstring and "Never imports" in docstring, (
            f"{relpath} is exempt from the shared-git-runner seam BECAUSE its "
            "docstring ratifies a standalone contract (\"Never imports "
            "coordinator_core...\"). That declaration is gone, so the exemption "
            "has no basis: either restore it, or drop the row and migrate the "
            "module onto coordinator_core.git.run like any other."
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

    # Double repointed to `Popen` (see the sibling test below for why). The
    # asserted properties are unchanged -- stdin bytes reach git unaltered and
    # feeding stdin selects binary mode -- but the kwarg they travel in moved:
    # `Popen` takes `stdin=PIPE` at construction and the bytes at
    # `communicate`, where `run()` took a single `input=`.
    class _Proc:
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, input=None, timeout=None):
            recorded["input"] = input
            return (b"a/b\0c/d\0", b"")

    def _record(argv, **kwargs):
        recorded["argv"] = argv
        recorded["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", _record)

    payload = b"a/b\0c/d\0"
    result = git_run.run_git(["check-ignore", "-z", "--stdin"], input=payload)

    assert recorded["input"] == payload, "stdin bytes must reach git unaltered"
    assert "encoding" not in recorded["kwargs"], "feeding stdin must not select text mode"
    assert "errors" not in recorded["kwargs"], "feeding stdin must not select text mode"
    assert recorded["kwargs"]["stdin"] == subprocess.PIPE, "fed stdin is a pipe"
    assert recorded["argv"][0] == "git"
    # Binary mode returns bytes; the seam decodes so callers still see str.
    assert result.stdout == "a/b\0c/d\0"


def test_a_call_without_stdin_keeps_text_mode_and_a_closed_stdin(monkeypatch):
    """The other half of the mode switch: every pre-existing caller keeps the
    behaviour it has today, and still gets `stdin=DEVNULL` so a git command
    that would prompt cannot block on an inherited terminal."""
    import subprocess

    recorded = {}

    # DOUBLE REPOINTED 2026-08-31 from `subprocess.run` to `subprocess.Popen`.
    # The seam stopped delegating its timeout to `run()`, because on Windows
    # `run()`'s timeout path re-drains the killed child with an UNBOUNDED
    # second `communicate()` and can hang forever (measured; see `run_git`'s
    # own comment). The properties asserted here are the originals -- text
    # mode, and a CLOSED stdin so a prompting git cannot block on an inherited
    # terminal -- only the call they are observed on moved.
    class _Proc:
        returncode = 0

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def communicate(self, input=None, timeout=None):
            recorded["input"] = input
            return ("ok\n", "")

    def _record(argv, **kwargs):
        recorded["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(subprocess, "Popen", _record)
    git_run.run_git(["rev-parse", "HEAD"])

    assert recorded["kwargs"]["encoding"] == "utf-8"
    assert recorded["kwargs"]["errors"] == "replace"
    assert recorded["kwargs"]["stdin"] == subprocess.DEVNULL
    assert recorded["input"] is None


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
    `ops/emit/resolvers.py` migrated onto this seam under the pre-split
    spellings mid-flight. Their written deletion condition -- `resolvers.py`
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


def _verb_message(entries: list) -> str:
    listed = "\n".join(
        f"  {module}: {enclosing} -> git {verb}" for module, enclosing, verb in sorted(entries)
    )
    return (
        "these call sites reach a git subprocess with a destructive verb, "
        "unregistered:\n"
        f"{listed}\n"
        "Either this is a known, intended destructive action -- add a row "
        "to _FROZEN_DESTRUCTIVE_VERB_SITES and raise _PINNED_VERB_CEILING "
        "to match, in the same diff -- or the call should not be reaching "
        "git with that verb."
    )


def test_no_new_destructive_verb_site_outside_the_frozen_inventory():
    """The gate (plan AC2/AC3). A call site reaching git with an
    unregistered destructive verb fails here, in either spawn shape --
    `run_git`/`_run_git`-shaped argv element 0, or a raw `["git", verb]`
    (or `which`-resolved-head) argv element 1."""
    sites = collect_destructive_verb_sites()
    observed = {(s.module, s.enclosing, s.verb) for s in sites}
    new = observed - _FROZEN_DESTRUCTIVE_VERB_SITES
    assert not new, _verb_message(sorted(new))


def test_the_verb_register_is_shrink_only():
    """The ratchet (plan AC3). Adding a row costs what raising
    `_PINNED_RUNNER_CEILING` costs above: a literal that must move in the
    same diff, arguing in writing that the tree needs one more unenumerated
    destructive call than it had yesterday."""
    assert len(_FROZEN_DESTRUCTIVE_VERB_SITES) <= _PINNED_VERB_CEILING, (
        f"the destructive-verb register grew to "
        f"{len(_FROZEN_DESTRUCTIVE_VERB_SITES)}, above the pinned "
        f"{_PINNED_VERB_CEILING}. It shrinks only."
    )


def test_every_frozen_destructive_verb_site_is_still_live():
    """Self-invalidation (plan AC5), same rule as
    `test_every_grandfathered_runner_still_spawns_git`. A row naming a site
    that has been deleted, migrated off that verb, or renamed is a standing,
    reviewed-looking pre-approval for whatever next reaches git with that
    verb at that name. A failure here is a DELETE plus a ceiling drop, not
    a re-key."""
    sites = collect_destructive_verb_sites()
    live = {(s.module, s.enclosing, s.verb) for s in sites}
    dead = sorted(_FROZEN_DESTRUCTIVE_VERB_SITES - live)
    assert not dead, (
        "these _FROZEN_DESTRUCTIVE_VERB_SITES rows no longer name a live "
        "destructive-verb site -- delete them (and lower "
        "_PINNED_VERB_CEILING to match):\n"
        + "\n".join(f"  {module}: {enclosing} -> git {verb}" for module, enclosing, verb in dead)
    )


def test_the_verb_collector_is_not_vacuous():
    """Guards the guard (plan AC5), same rule as
    `test_the_collector_is_not_vacuous`. A scope, traversal, or exclusion
    change that silently stops finding anything would otherwise land
    green."""
    sites = collect_destructive_verb_sites()
    assert sites, (
        "the destructive-verb collector found no site anywhere in "
        f"{_VERB_GATE_SCOPE_ROOTS} -- the gate is asserting nothing. Check "
        "_VERB_GATE_SCOPE_ROOTS, _DESTRUCTIVE_VERBS and _RUN_GIT_LEAF_NAMES."
    )


def test_the_verb_collector_fires_on_a_synthetic_run_git_shaped_call():
    """Fails-when-inverted leg, `run_git`-shaped half (plan AC4). Matches
    `test_the_collector_fires_on_a_synthetic_private_runner`'s direct case:
    proves the detector reports the shape it claims to rather than passing
    because the tree happens to be clean."""
    source = (
        "from coordinator_core.git.run import run_git\n"
        "def _wipe(cwd):\n"
        "    return run_git(['reset', '--hard'], cwd=cwd)\n"
    )
    sites = _collect_verb_sites("synthetic/verb_direct.py", source)
    assert [(s.enclosing, s.verb) for s in sites] == [("_wipe", "reset")]


def test_the_verb_collector_fires_on_a_synthetic_raw_argv_call():
    """Fails-when-inverted leg, raw-argv half (plan AC4). Matches
    `test_the_collector_fires_on_a_synthetic_private_runner`'s split case:
    the raw-argv shape is the exact bypass population the baton exists
    for -- a gate that never reds here proves the harness runs, not that it
    fires."""
    source = (
        "import subprocess\n"
        "def _wipe(cwd):\n"
        "    return subprocess.run(['git', 'reset', '--hard'], cwd=cwd)\n"
    )
    sites = _collect_verb_sites("synthetic/verb_split.py", source)
    assert [(s.enclosing, s.verb) for s in sites] == [("_wipe", "reset")]


def test_a_which_resolved_raw_argv_verb_is_also_detected():
    """The `_resolved_git_names` blind spot G7 closed on 2026-08-25 applies
    identically to the verb axis: a module resolving git through
    `shutil.which("git")` and spawning `[git_bin, "reset", "--hard"]` carries
    no literal `"git"` head, and this collector must not be blind to it
    either."""
    source = (
        "import shutil, subprocess\n"
        "def _wipe(cwd):\n"
        "    git_bin = shutil.which('git')\n"
        "    return subprocess.run([git_bin, 'reset', '--hard'], cwd=cwd)\n"
    )
    sites = _collect_verb_sites("synthetic/verb_which.py", source)
    assert [(s.enclosing, s.verb) for s in sites] == [("_wipe", "reset")]


def test_a_module_calling_the_shared_runner_with_a_non_destructive_verb_is_not_a_site():
    """Negative control: a `run_git` call whose verb is not in
    `_DESTRUCTIVE_VERBS` (a plain read) must be invisible, or every module
    ever migrated onto the seam would immediately start failing this gate."""
    source = (
        "from coordinator_core.git.run import run_git\n"
        "def read_status(cwd=None):\n"
        "    return run_git(['status', '--porcelain'], cwd=cwd).stdout\n"
    )
    sites = _collect_verb_sites("synthetic/verb_read.py", source)
    assert sites == []
