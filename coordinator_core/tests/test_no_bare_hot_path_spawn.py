"""Standing regrowth gate (stage 1, hot-path-scoped): every subprocess spawn
reachable from a hook on an ordinary tool call must be console-suppressed.

Context: `docs/plans/2026-08-07-no-window-subprocess-primitive.md` C2 ships
`coordinator_core.win_portability.no_console_creationflags()` and C3/C4
migrate the known bare hot-path sites onto it. This gate is the regrowth
prevention (AC5) — a new bare spawn landing in the hot path after that
migration must fail the fast tier the moment it lands, the same "prevention,
not just a one-time sweep" shape as the shell-spawn regrowth gate
(`coordinator_core/tests/test_no_unsanctioned_shell_spawn.py`) and the
cwd-unguarded-spawn gate this module is modeled on.

PRECEDENT VERDICT (per dispatch instructions — judge, don't copy silently):
`test_no_cwd_unguarded_coordinator_core_subprocess_spawn.py`'s core shape —
ast-walk a fixed call-name set (`run`/`Popen`/`check_output`/`call`/
`check_call`, bare-imported or attribute form), assert a kwarg-shaped
property, prefer false-negative over false-positive, prove teeth with a
planted-fixture self-test — is SOUND and is reused here unchanged. One
adjustment: that gate deliberately does not verify the callee actually
resolves to `subprocess` (any `.run(...)`/`.Popen(...)` attribute call
qualifies), which is safe for its own narrow "does the argv import
coordinator_core" question but would be noisy here — an unrelated `.run()`
method (e.g. a guard's own `check`/`run`-style dispatcher) is common in this
codebase's hot-path modules. This collector therefore requires the call to
be traceable to the `subprocess` module (`subprocess.run(...)` on a `Name`
bound to the `subprocess` import, or a name bound via `from subprocess
import run`) before it counts a site — a narrower, not a looser, filter than
the precedent's, kept in service of the same false-negative-over-false-
positive preference stated in its own docstring.

DOES NOT extend `spawn_policy.detect.SpawnSite` (frozen dataclass, pinned
API, `tasks/shell-spawn-regrowth-gate/PINNED-API.md`) — this module defines
its own sibling site type (`BareSpawnSite`) carrying the suppression signal
`SpawnSite` deliberately does not record. Traversal (`discover_source_files`,
`DEFAULT_EXCLUDE`) and the test-tree partition (`is_test_tree_site`,
imported from `coordinator_core.spawn_policy`) are reused
rather than re-derived, exactly the reuse-the-enumeration/assert-a-
different-thing shape `write_guards/nudge_shell_shaped_spawn.py` uses against
`spawn_policy.sites_in_source` for its own, differently-shaped, advisory
purpose.

SCOPE — repo-wide, STAGE 2 (C10's closing step; stage 1 was hot-path-only:
bash_guards, write_guards, hooks, ops/session). `_GATE_SCOPE_DIRS` is the
single named constant this gate's scope lives in -- widening it was a
one-line edit to that tuple plus a re-run, no rewrite of the detection logic
below, per the plan's own C6 body.

STAGE 3 — TEST-TREE ARM. `test_no_bare_hot_path_spawn` (and the production
`find_bare_hot_path_spawns` collector it calls) skips every
`is_test_tree_site` path outright — by design, since a test fixture spawning
`git` to build a scratch repo is common and not itself "the hot path". That
skip means a bare, console-flashing spawn landing in a test file has never
been gated at all. `find_bare_test_tree_spawns`/`test_no_bare_test_tree_spawn`
are the mirror-image collector/gate for exactly that population: same
detection logic (imported, not re-derived — this module's own stage-1/2
precedent), same `discover_source_files` traversal, `is_test_tree_site`
inverted (KEEP the test-tree paths instead of skipping them). Scope defaults
to the same `_GATE_SCOPE_DIRS` the production arm uses (Review:
overengineering-reviewer -- a dedicated `_TEST_TREE_GATE_SCOPE_DIRS`
constant used to exist here whose one value was "no scoping", i.e. no
different from `_GATE_SCOPE_DIRS`'s own default); a caller widening the
test-tree arm's scope independently passes an explicit `scope_dirs=`.

Reasoned, named exemptions for the test-tree arm (none currently resolve to
an in-scope test-tree file, so `find_bare_test_tree_spawns`'s `exemptions`
defaults to an empty set rather than naming a dedicated,
permanently-empty module constant — named here per this module's own
negative-spec convention rather than left silent, same reasoning as
`_EXEMPT_CALL_SITES` above):

  - `coordinator_core/ops/verify_no_powershell_flash.py`'s
    `subprocess.run(...)` — NOT a test-tree file (`is_test_tree_site` is
    False: no `tests/` component, no `test_` basename), so this arm never
    walks it at all. Named here anyway because its reason — "console
    behaviour is the thing under test, suppressing it would corrupt the
    measurement" — is exactly the shape of reason a genuine test-tree
    exemption would carry, and it is already compliant-by-tag
    (`# popup-intentional-last-resort`) under the production arm.
  - The `# popup-intentional-last-resort`-tagged site in
    `coordinator_core/win_portability.py` — also not a test-tree file, also
    already compliant-by-tag under the production arm this module already
    runs. Named for the same reason as above.
  - Any `shell=True` site this arm's sweep skipped because it carries a
    `creationflags=`/`**no_console_creationflags()` kwarg: `CREATE_NO_WINDOW`
    suppresses the console of the process CreateProcess actually launches,
    which under `shell=True` on Windows is `cmd.exe`, not the ultimate
    command — the flag is real but does not, by itself, prove the whole
    chain is console-free. A repo-wide AST sweep at 2026-08-30 (see this
    commit) found ZERO test-tree call sites combining `shell=True` with a
    creationflags-shaped kwarg, so there is nothing to list by
    `(relpath, lineno)` yet. Left named rather than silent so the next
    `shell=True`-carrying test fixture that also reaches for
    `no_console_creationflags()` gets a human decision instead of a
    false-green pass.

Definition of "bare" (per plan C6 body): a subprocess spawn call with no
`creationflags=` keyword and no double-star kwargs splat that resolves to
the `no_console_creationflags()` primitive (directly, via a module-scope
`Name` bound from it, or via a `_no_console`-shaped helper attribute/call)
is bare/unsuppressed. An unresolvable splat is treated as bare too --
false-negative-over-false-positive applies to the splat shape exactly as it
does to everything else in this module. A call whose source span (open-paren
line through close-paren line) carries a `# popup-intentional-last-resort`
tag is compliant-by-exemption.

Spawn-call universe, by family: `subprocess.{run,Popen,check_output,call,
check_call}`, `os.{system,popen}`, `asyncio.{create_subprocess_exec,
create_subprocess_shell}` (bare-imported, `from`-imported, or
aliased-module attribute form). NOT covered, and not attempted here: a
local re-bind (`run = subprocess.run` then `run(cmd)`, not an
`Import`/`ImportFrom`) and any other dynamic-dispatch call shape that
doesn't resolve statically to one of the modules above -- named here per
this module's own negative-spec convention rather than left silent.

STANDING GATE vs. WIDENED COLLECTOR -- two different assertions share this
module's collector, deliberately, rather than one assertion over the union:
`find_bare_hot_path_spawns(..., families=...)` defaults to
`{"subprocess"}` only, and `test_no_bare_hot_path_spawn` (the fast-tier
standing gate, must stay GREEN) never passes anything wider. The `os`/
`asyncio` families are real, live in the collector, and reachable by an
explicit `families=` argument -- but they surface pre-existing sites this
workstream did not introduce (see `state/audits/
2026-08-07-spawn-gate-widening-surfaces-28-preexisting-sites.md`), so
`test_widened_spawn_families_surface_known_preexisting_sites` (marked
`designed_red`, deselected by the fast tier) is the ONLY assertion that
exercises them. Do not fold the widened families into the standing gate's
default without first burning down that audit's worklist -- that is a
follow-up workstream, not a narrowing of this one.

Spec backlink: pln-no-window-subprocess-primitive-750d2d § C6, AC5.
"""

from __future__ import annotations

import ast
import dataclasses
import pathlib

import pytest

from coordinator_core.spawn_policy import SpawnParseError, is_test_tree_site
from coordinator_core.spawn_policy.detect import DEFAULT_EXCLUDE, discover_source_files
from coordinator_core.spawn_policy.spawn_names import (
    SPAWN_NAMES_BY_MODULE as _SPAWN_NAMES_BY_MODULE,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: STAGE 2 scope (repo-wide, C10's closing step). The one place this gate's
#: scope lives — widened from the stage-1 hot-path-only tuple
#: (bash_guards, write_guards, hooks, ops/session) to cover all of
#: `coordinator_core`, per docs/plans/2026-08-07-no-window-subprocess-primitive.md
#: C6/C10. "" matches every discovered relpath under the scan root.
#: Paths are `coordinator_core`-relative, POSIX-separated.
_GATE_SCOPE_DIRS: tuple[str, ...] = ("",)

# Review: overengineering-reviewer -- `_TEST_TREE_GATE_SCOPE_DIRS` used to be
# a second, separately-defined scope tuple whose one value ("" -- no
# scoping) only mirrored `_GATE_SCOPE_DIRS`'s shape rather than expressing a
# real, distinct need. `find_bare_test_tree_spawns` now defaults its
# `scope_dirs` param to `_GATE_SCOPE_DIRS` directly; widen the test-tree
# arm's scope independently of the production arm's by passing an explicit
# `scope_dirs=` argument at the call site, the same way any other
# non-default scoping call already works.

#: Spawn function names, keyed by the module they must resolve to. `os.system`
#: and `os.popen` can never carry `creationflags=` (no such parameter exists
#: on either), so any reachable call is unconditionally bare -- flagged unless
#: exempted, same as an unsuppressed `subprocess` call. Sourced from
#: `coordinator_core.spawn_policy.spawn_names` (moved there 2026-08-21, DR-357
#: widening) so `nudge_windows_subprocess_popup.py`'s per-call-site AST path
#: can import the identical table rather than re-deriving it -- see that
#: module's own docstring. The `_SPAWN_NAMES_BY_MODULE` name is kept as the
#: import alias so nothing below this line changes.

#: Back-compat alias: some call sites/tests still refer to the subprocess-only
#: name set directly.
_SPAWN_CALL_NAMES = _SPAWN_NAMES_BY_MODULE["subprocess"]

_EXEMPTION_TAG = "# popup-intentional-last-resort"

#: A splat callee/name is recognised as carrying a suppression primitive's
#: signal if its identifier is shaped like one of the TWO primitives
#: `win_portability` ships, or one of their documented thin wrappers
#: (`_no_console_kw`, `_no_console_kwargs`, `_no_console_window`,
#: `cc_invoke._no_console_kw`, ...). Matched by identifier shape, not by
#: resolving the callee's actual implementation -- consistent with this
#: module's static-AST-only design.
#:
#: `leaf_spawn`-shaped identifiers resolve here because
#: `leaf_spawn_creationflags()` (DETACHED_PROCESS) is a suppression route in
#: exactly the sense this gate polices: the child gets no console, so it
#: cannot flash a window. It is NOT interchangeable with
#: `no_console_creationflags()` -- a detached child's console-subsystem
#: DESCENDANTS each allocate their own VISIBLE console, which is why the
#: primitive is for proven-leaf spawns only. That distinction is adjudicated
#: by `test_leaf_spawn_creationflags.py`, which owns the leaf primitive's
#: three axes; this gate stays value-blind and asks only whether SOME
#: suppression is wired at all. Both questions have to be asked, and neither
#: gate answers the other's.
_SUPPRESSION_IDENTIFIER_PREFIXES = ("no_console", "leaf_spawn")


def _is_no_console_shaped(identifier: str) -> bool:
    return identifier.lstrip("_").startswith(_SUPPRESSION_IDENTIFIER_PREFIXES)


def _call_func_name(node: ast.expr) -> str | None:
    """The trailing identifier of a `Name` or `Attribute` node, e.g.
    `no_console_creationflags` for both `no_console_creationflags` and
    `cc_invoke.no_console_creationflags`. `None` for anything else."""

    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _assign_targets(stmt: ast.stmt) -> list[ast.expr] | None:
    """Target list for a module-scope `Assign` or `AnnAssign`, else `None`.
    `_NO_CONSOLE: Dict[str, Any] = no_console_creationflags()` (an
    `AnnAssign`, seen in this codebase's hot-path modules) must resolve the
    same way as the unannotated `_NO_CONSOLE = no_console_creationflags()`
    shape."""

    if isinstance(stmt, ast.Assign):
        return stmt.targets
    if isinstance(stmt, ast.AnnAssign) and stmt.value is not None:
        return [stmt.target]
    return None


def _dict_has_creationflags_key(node: ast.expr) -> bool:
    """True for a dict literal carrying a `"creationflags"` key, e.g.
    `{"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}` -- the
    `_NO_WINDOW`-shaped constant some hot-path modules bind directly rather
    than via `no_console_creationflags()`. Unambiguous by construction (the
    key IS the suppression flag), so resolving it is strictly narrower than
    accepting an arbitrary splat, not a loosening of the false-negative
    preference."""

    return isinstance(node, ast.Dict) and any(
        isinstance(key, ast.Constant) and key.value == "creationflags" for key in node.keys
    )


def _splats_a_no_console_source(keywords: list[ast.keyword], known: set[str]) -> bool:
    """True when a `**` splat in `keywords` carries the suppression primitive.

    NOT optional sugar: the builder shape this recognises --
    ``kwargs = dict(cwd=..., check=True, **no_console_creationflags())``
    followed by ``subprocess.run([...], **kwargs)`` -- is the form ten
    already-correct call sites in `coordinator_core/git/tests/` use, and
    without this the gate reports every one of them as a bare spawn. A
    detector that cannot see through one level of the codebase's own
    prevailing idiom manufactures work on correct code, which is the same
    class of error as missing a real one."""

    for kw in keywords:
        if kw.arg is not None:
            continue
        target = kw.value.func if isinstance(kw.value, ast.Call) else kw.value
        func_name = _call_func_name(target)
        if func_name is not None and _is_no_console_shaped(func_name):
            return True
        if isinstance(kw.value, ast.Name) and kw.value.id in known:
            return True
    return False


def _dict_splats_a_no_console_source(node: ast.Dict, known: set[str]) -> bool:
    """The dict-literal twin of `_splats_a_no_console_source` --
    ``{**no_console_creationflags(), "cwd": ...}``. A `None` key is how
    `ast` spells `**value` inside a dict display."""

    for key, value in zip(node.keys, node.values):
        if key is not None:
            continue
        target = value.func if isinstance(value, ast.Call) else value
        func_name = _call_func_name(target)
        if func_name is not None and _is_no_console_shaped(func_name):
            return True
        if isinstance(value, ast.Name) and value.id in known:
            return True
    return False


def _collect_no_console_names(stmts: list[ast.stmt]) -> set[str]:
    """Resolution of `NAME = no_console_creationflags(...)`-shaped,
    `NAME = {"creationflags": ...}`-shaped, and `NAME["creationflags"] =
    ...`-shaped statements anywhere in `stmts`, so a later `**NAME` splat
    can be recognised even when the assignment and the splat are in
    different statements -- including, for the subscript-assignment shape,
    different branches of the SAME conditional (`if hasattr(os, "fork"):
    ... else: popen_kwargs["creationflags"] = ...`), which is exactly the
    detached-spawn pattern this codebase uses. Descends into `if`/`try`/
    `with`/`for`/`while` bodies (same function scope in Python) but NEVER
    into a nested `FunctionDef`/`AsyncFunctionDef`/`Lambda` -- a same-named
    local inside a nested function binding something unrelated must not
    leak into this resolution. Callers apply this once at module scope and
    once per function scope, then union the two sets."""

    names: set[str] = set()

    def _walk(body: list[ast.stmt]) -> None:
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue  # new scope -- do not resolve names into it or out of it

            targets = _assign_targets(stmt)
            if targets is not None:
                value = stmt.value  # type: ignore[union-attr]
                resolved = False
                if isinstance(value, ast.Call):
                    func_name = _call_func_name(value.func)
                    resolved = func_name is not None and _is_no_console_shaped(func_name)
                    if not resolved:
                        resolved = _splats_a_no_console_source(value.keywords, names)
                elif isinstance(value, ast.Dict):
                    resolved = _dict_has_creationflags_key(value) or _dict_splats_a_no_console_source(
                        value, names
                    )
                elif _dict_has_creationflags_key(value):
                    resolved = True
                if resolved:
                    for target in targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)

            if (
                isinstance(stmt, ast.Assign)
                and len(stmt.targets) == 1
                and isinstance(stmt.targets[0], ast.Subscript)
                and isinstance(stmt.targets[0].value, ast.Name)
            ):
                subscript = stmt.targets[0]
                slice_node = subscript.slice
                if isinstance(slice_node, ast.Constant) and slice_node.value == "creationflags":
                    names.add(subscript.value.id)  # type: ignore[union-attr]

            for field in ("body", "orelse", "finalbody"):
                child_body = getattr(stmt, field, None)
                if child_body:
                    _walk(child_body)
            handlers = getattr(stmt, "handlers", None)
            if handlers:
                for handler in handlers:
                    _walk(handler.body)

    _walk(stmts)
    return names

#: Known, LIVE, outstanding exemptions -- keyed on (relpath, lineno), matching
#: the precedent gate's own convention. Empty by construction: any real
#: site this gate would otherwise flag either already routes through the
#: primitive (C3/C4) or carries the inline `# popup-intentional-last-resort`
#: tag, which this gate honours directly rather than via a duplicate register.
_EXEMPT_CALL_SITES: set[tuple[str, int]] = set()

# Review: overengineering-reviewer -- `_TEST_TREE_EXEMPT_CALL_SITES` used to
# be a second, module-level, empty-by-construction registry mirroring
# `_EXEMPT_CALL_SITES`'s shape for an arm that has never needed one. The
# shared `_bare_spawns_in_population` engine already carries the exemption
# set as a caller-supplied argument, so `find_bare_test_tree_spawns` passes
# an inline `set()` default instead of naming a dedicated, permanently-empty
# module constant. A real test-tree exemption, when one arrives, becomes a
# literal `{(relpath, lineno), ...}` passed at that call site.


@dataclasses.dataclass(frozen=True)
class BareSpawnSite:
    """Sibling of `spawn_policy.detect.SpawnSite`, carrying the suppression
    signal that dataclass deliberately does not record. NOT a subtype or
    extension of `SpawnSite` -- see module docstring."""

    path: str
    lineno: int
    enclosing: str
    family: str  # "subprocess" / "os" / "asyncio"


class _SubprocessImportResolver(ast.NodeVisitor):
    """Tracks which local names resolve to a spawn-bearing module
    (`subprocess`, `os`, `asyncio`) or to one of its spawn functions, so
    `subprocess.run(...)`, `sp.run(...)` (aliased import), `os.system(...)`,
    and `run(...)` (via `from subprocess import run`) are all recognised
    while an unrelated same-named method (`guard.run(...)`) is not."""

    def __init__(self) -> None:
        #: local alias -> module name ("subprocess" / "os" / "asyncio")
        self.module_aliases: dict[str, str] = {}
        #: local alias -> (module name, original function name)
        self.function_aliases: dict[str, tuple[str, str]] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name in _SPAWN_NAMES_BY_MODULE:
                self.module_aliases[alias.asname or alias.name] = alias.name
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        spawn_names = _SPAWN_NAMES_BY_MODULE.get(node.module or "")
        if spawn_names:
            for alias in node.names:
                if alias.name in spawn_names:
                    self.function_aliases[alias.asname or alias.name] = (node.module, alias.name)
        self.generic_visit(node)


class _BareSpawnVisitor(ast.NodeVisitor):
    """Collects every bare (unsuppressed) subprocess spawn call in one parsed
    module, tracking enclosing-function names for reporting."""

    def __init__(
        self,
        source_lines: list[str],
        resolver: _SubprocessImportResolver,
        module_no_console_names: set[str],
        families: frozenset[str],
    ) -> None:
        self._source_lines = source_lines
        self._resolver = resolver
        self._module_no_console_names = module_no_console_names
        self._families = families
        self._enclosing_stack: list[str] = ["<module>"]
        #: stack of active no-console-name sets, one per enclosing scope --
        #: module scope at the bottom, each function scope's own resolution
        #: (module names unioned with that function's local names) pushed on
        #: entry and popped on exit, so a name resolved inside one function
        #: never leaks into a sibling function.
        self._no_console_stack: list[set[str]] = [module_no_console_names]
        self.sites: list[tuple[int, str, str]] = []

    def _current_enclosing(self) -> str:
        return self._enclosing_stack[-1]

    def _current_no_console_names(self) -> set[str]:
        return self._no_console_stack[-1]

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._enclosing_stack.append(node.name)
        local_names = _collect_no_console_names(node.body)
        self._no_console_stack.append(self._module_no_console_names | local_names)
        self.generic_visit(node)
        self._no_console_stack.pop()
        self._enclosing_stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef  # type: ignore[assignment]

    def _spawn_family(self, call: ast.Call) -> str | None:
        """The family (`"subprocess"`/`"os"`/`"asyncio"`) this call resolves
        to, filtered to `self._families`, or `None` if it isn't a
        recognised (and in-scope) spawn call at all."""

        func = call.func
        family: str | None = None
        if isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                module = self._resolver.module_aliases.get(func.value.id)
                if module is not None and func.attr in _SPAWN_NAMES_BY_MODULE[module]:
                    family = module
        elif isinstance(func, ast.Name):
            resolved = self._resolver.function_aliases.get(func.id)
            if resolved is not None:
                family = resolved[0]
        if family is not None and family in self._families:
            return family
        return None

    def _call_is_suppressed(self, call: ast.Call) -> bool:
        no_console_names = self._current_no_console_names()
        for kw in call.keywords:
            if kw.arg == "creationflags":
                return True
            if kw.arg is None:  # a **splat -- only counts if it plausibly
                # carries the suppression primitive's flag. An unresolvable
                # splat is NOT treated as suppressed: this module's stated
                # preference is false-negative over false-positive, and a
                # splat we cannot trace to `no_console_creationflags()` (or
                # a `_no_console`-shaped wrapper) is exactly the shape a
                # future bare spawn would hide behind.
                splat_target = kw.value.func if isinstance(kw.value, ast.Call) else kw.value
                func_name = _call_func_name(splat_target)
                if func_name is not None and _is_no_console_shaped(func_name):
                    return True
                if isinstance(kw.value, ast.Name) and kw.value.id in no_console_names:
                    return True
        return False

    def _call_has_exemption_tag(self, call: ast.Call) -> bool:
        end_lineno = getattr(call, "end_lineno", None) or call.lineno
        for lineno in range(call.lineno, end_lineno + 1):
            idx = lineno - 1
            if 0 <= idx < len(self._source_lines) and _EXEMPTION_TAG in self._source_lines[idx]:
                return True
        return False

    def visit_Call(self, node: ast.Call) -> None:
        family = self._spawn_family(node)
        if family is not None:
            if not self._call_is_suppressed(node) and not self._call_has_exemption_tag(node):
                self.sites.append((node.lineno, self._current_enclosing(), family))
        self.generic_visit(node)


def _in_scope(relpath: str, scope_dirs: tuple[str, ...]) -> bool:
    parts = pathlib.PurePosixPath(relpath).parts
    return any(
        parts[: len(pathlib.PurePosixPath(scope_dir).parts)] == pathlib.PurePosixPath(scope_dir).parts
        for scope_dir in scope_dirs
    )


#: Families the standing gate asserts on -- `subprocess` only. Widening this
#: default is exactly what § "STANDING GATE vs. WIDENED COLLECTOR" in the
#: module docstring says not to do without first burning down the audit's
#: worklist; pass `families=` explicitly at the call site instead (as
#: `test_widened_spawn_families_surface_known_preexisting_sites` does).
_STANDING_GATE_FAMILIES: frozenset[str] = frozenset({"subprocess"})

_ALL_FAMILIES: frozenset[str] = frozenset(_SPAWN_NAMES_BY_MODULE)


def _bare_spawns_in_file(
    relpath: str,
    file_path: pathlib.Path,
    families: frozenset[str] = _STANDING_GATE_FAMILIES,
) -> list[BareSpawnSite]:
    """Single-file half of the collector: parse `file_path` and return its
    bare (unsuppressed) spawn sites, `relpath`-keyed for `_EXEMPT_CALL_SITES`
    lookup.

    Factored out of `find_bare_hot_path_spawns` so a caller that already has
    a specific file in hand (a pinned population, not a directory to walk)
    can get real per-file coverage without paying that function's directory
    walk -- see `_REPO_ROOT_PY_FILES`'s docstring for why the walk is
    disproportionate for that caller.
    """
    source = file_path.read_text(encoding="utf-8")
    try:
        tree = ast.parse(source, filename=str(file_path))
    except SyntaxError as exc:
        raise SpawnParseError(relpath, str(exc)) from exc

    resolver = _SubprocessImportResolver()
    resolver.visit(tree)
    if not resolver.module_aliases and not resolver.function_aliases:
        return []

    module_no_console_names = _collect_no_console_names(tree.body)
    source_lines = source.splitlines()
    visitor = _BareSpawnVisitor(source_lines, resolver, module_no_console_names, families)
    visitor.visit(tree)

    violations: list[BareSpawnSite] = []
    for lineno, enclosing, family in visitor.sites:
        if (relpath, lineno) in _EXEMPT_CALL_SITES:
            continue
        violations.append(
            BareSpawnSite(path=relpath, lineno=lineno, enclosing=enclosing, family=family)
        )
    return violations


def _bare_spawns_in_population(
    root: pathlib.Path,
    scope_dirs: tuple[str, ...],
    families: frozenset[str],
    *,
    want_test_tree: bool,
    exemptions: set[tuple[str, int]],
) -> list[BareSpawnSite]:
    """Shared collector engine for both the production and test-tree arms
    (Review: overengineering-reviewer -- `find_bare_hot_path_spawns`/
    `find_bare_test_tree_spawns` were a line-for-line copy of this walk;
    the only real differences were the `is_test_tree_site` polarity and
    which exemption registry applied, so those are now parameters instead
    of a duplicated function body).

    `want_test_tree=False` reproduces `find_bare_hot_path_spawns`'s old
    behaviour exactly (skip test-tree paths, apply `_EXEMPT_CALL_SITES`
    inside `_bare_spawns_in_file`); `want_test_tree=True` reproduces
    `find_bare_test_tree_spawns`'s (keep only test-tree paths, apply
    the caller-supplied `exemptions` set after the fact, since
    `_bare_spawns_in_file` only knows the production `_EXEMPT_CALL_SITES`
    registry).
    """
    discovered, _excluded = discover_source_files(root, exclude=DEFAULT_EXCLUDE)

    violations: list[BareSpawnSite] = []
    for relpath, file_path in discovered:
        if not _in_scope(relpath, scope_dirs):
            continue

        if is_test_tree_site(relpath) != want_test_tree:
            continue

        if not want_test_tree:
            violations.extend(_bare_spawns_in_file(relpath, file_path, families))
            continue

        for site in _bare_spawns_in_file(relpath, file_path, families):
            if (site.path, site.lineno) in exemptions:
                continue
            violations.append(site)

    return violations


def find_bare_hot_path_spawns(
    root: pathlib.Path,
    scope_dirs: tuple[str, ...] = _GATE_SCOPE_DIRS,
    families: frozenset[str] = _STANDING_GATE_FAMILIES,
) -> list[BareSpawnSite]:
    """Walk `root` (a `coordinator_core`-shaped directory), scoped to
    `scope_dirs` and `families`, and return every bare (unsuppressed) spawn
    site outside the test tree.

    Used against the real `coordinator_core/` tree (both the `subprocess`-
    only standing gate and the widened-family `designed_red` worklist) and
    against an isolated `tmp_path` fixture (this gate's own self-tests,
    which all use the `subprocess`-only default). Call-through onto the
    shared `_bare_spawns_in_population` engine -- see its docstring.
    """
    return _bare_spawns_in_population(
        root, scope_dirs, families, want_test_tree=False, exemptions=_EXEMPT_CALL_SITES
    )


def find_bare_test_tree_spawns(
    root: pathlib.Path,
    scope_dirs: tuple[str, ...] = _GATE_SCOPE_DIRS,
    families: frozenset[str] = _STANDING_GATE_FAMILIES,
    exemptions: set[tuple[str, int]] | None = None,
) -> list[BareSpawnSite]:
    """STAGE 3 mirror of `find_bare_hot_path_spawns`: walk `root`, scoped to
    `scope_dirs` and `families`, and return every bare (unsuppressed) spawn
    site INSIDE the test tree (the population the production collector
    skips). Call-through onto the shared `_bare_spawns_in_population`
    engine, `is_test_tree_site` polarity inverted -- see that engine's
    docstring and module docstring "STAGE 3".

    `exemptions` defaults to an empty set: no test-tree exemption has ever
    resolved to an in-scope file (module docstring "STAGE 3 -- TEST-TREE
    ARM"), so there is no dedicated, permanently-empty module constant to
    default to any more (Review: overengineering-reviewer, `_TEST_TREE_
    EXEMPT_CALL_SITES`).
    """
    return _bare_spawns_in_population(
        root,
        scope_dirs,
        families,
        want_test_tree=True,
        exemptions=exemptions if exemptions is not None else set(),
    )


def _format_violation(site: BareSpawnSite) -> str:
    if site.family == "subprocess":
        return (
            f"{site.path}:{site.lineno} ({site.enclosing}) -- bare subprocess spawn in "
            f"the hot path (reachable from a hook on an ordinary tool call): no "
            f"creationflags= kwarg and no **kwargs splat, so it allocates a "
            f"conhost.exe window on Windows. Fix: splat "
            f"coordinator_core.win_portability.no_console_creationflags() into the "
            f"call, e.g. subprocess.run([...], **no_console_creationflags()). If "
            f"this is a genuine last resort (e.g. shell=True's cmd.exe "
            f"intermediary needs the STARTUPINFO route instead), tag the line "
            f"`# popup-intentional-last-resort`."
        )
    if site.family == "os":
        return (
            f"{site.path}:{site.lineno} ({site.enclosing}) -- bare os.system/os.popen "
            f"call in the hot path: neither function accepts a `creationflags` "
            f"parameter at all, so any reachable call unconditionally allocates a "
            f"conhost.exe window on Windows. Fix: migrate to "
            f"subprocess.run([...], shell=True, **no_console_creationflags()) or an "
            f"equivalent suppressible call, or tag the line "
            f"`# popup-intentional-last-resort` if this is a genuine last resort."
        )
    # asyncio
    return (
        f"{site.path}:{site.lineno} ({site.enclosing}) -- bare "
        f"asyncio.create_subprocess_exec/create_subprocess_shell call in the hot "
        f"path: no creationflags= kwarg and no **kwargs splat. Unlike os.system/"
        f"os.popen, asyncio's create_subprocess_* DOES accept creationflags (it is "
        f"forwarded to the underlying Popen), so this family has a direct "
        f"suppression route -- splat coordinator_core.win_portability."
        f"no_console_creationflags() into the call, e.g. await "
        f"asyncio.create_subprocess_exec(*argv, **no_console_creationflags()). If "
        f"this is a genuine last resort, tag the line "
        f"`# popup-intentional-last-resort`."
    )


def test_no_bare_hot_path_spawn():
    """Standing gate (AC5), stage 1: no `subprocess` spawn under
    `_GATE_SCOPE_DIRS` may be bare/unsuppressed, except an explicit
    `# popup-intentional-last-resort`-tagged site or a narrow, dated
    `_EXEMPT_CALL_SITES` entry -- empty by construction at land, since C3/C4
    migrate every known bare hot-path site onto the primitive before this
    gate lands.

    `subprocess`-family only, deliberately -- see the module docstring's
    "STANDING GATE vs. WIDENED COLLECTOR" section. The `os`/`asyncio`
    families are exercised only by
    `test_widened_spawn_families_surface_known_preexisting_sites`
    (`designed_red`, deselected by the fast tier `main` is protected by).
    """
    violations = find_bare_hot_path_spawns(REPO_ROOT / "coordinator_core")
    assert violations == [], "\n\n".join(_format_violation(site) for site in violations)


def find_double_console_suppressions(
    root: pathlib.Path,
) -> list[BareSpawnSite]:
    """Spawn calls carrying TWO suppression sources at once.

    `subprocess.run(argv, **_NOWIN, **no_console_creationflags())` is not
    belt-and-braces -- both mappings carry a `creationflags` key, so Python
    raises `TypeError: got multiple values for keyword argument
    'creationflags'` when the call runs. It compiles, it imports, it passes
    collection, and every "is this site suppressed?" check reads GREEN,
    because a doubly-suppressed site is suppressed twice over. Only
    executing it fails.

    Why this exists as its own gate: the 2026-08-30 repo-wide sweep splatted
    the primitive into 1,473 call sites and verified each one on six axes --
    all of them asking whether a site was suppressed ENOUGH. None asked
    whether it was suppressed twice, so 16 sites went in broken and took 64
    tests in `coordinator_core/git/tests/` down with them. A detector that
    can only see under-application will keep re-admitting the sweep's own
    over-application."""

    # Review: overengineering-reviewer -- was a THIRD, divergent file walk
    # (`root.rglob("*.py")`), disagreeing with the other two arms of this
    # gate about which files are in the repo. Routed through the same
    # `discover_source_files`/`DEFAULT_EXCLUDE` population they share.
    discovered, _excluded = discover_source_files(root, exclude=DEFAULT_EXCLUDE)

    sites: list[BareSpawnSite] = []
    for relpath, file_path in sorted(discovered):
        try:
            source = file_path.read_text(encoding="utf-8")
            tree = ast.parse(source)
        except (OSError, SyntaxError):
            continue
        if "no_console" not in source:
            continue
        module_names = _collect_no_console_names(tree.body)
        scopes: list[tuple[ast.AST, set[str]]] = [(tree, module_names)]
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scopes.append((node, module_names | _collect_no_console_names(node.body)))
        seen: set[int] = set()
        for scope_node, names in scopes:
            for call in ast.walk(scope_node):
                if not isinstance(call, ast.Call) or call.lineno in seen:
                    continue
                sources = 0
                for kw in call.keywords:
                    if kw.arg == "creationflags":
                        sources += 1
                        continue
                    if kw.arg is not None:
                        continue
                    target = kw.value.func if isinstance(kw.value, ast.Call) else kw.value
                    func_name = _call_func_name(target)
                    if func_name is not None and _is_no_console_shaped(func_name):
                        sources += 1
                    elif isinstance(kw.value, ast.Name) and kw.value.id in names:
                        sources += 1
                if sources >= 2:
                    seen.add(call.lineno)
                    sites.append(BareSpawnSite(relpath, call.lineno, "<double>", "subprocess"))
    return sites


def test_no_double_console_suppression():
    """Two suppression sources on one call is a runtime `TypeError`, not
    redundancy -- see `find_double_console_suppressions`. Remove the added
    splat; the pre-existing local mapping was already correct."""

    violations = find_double_console_suppressions(REPO_ROOT / "coordinator_core")
    assert violations == [], "\n".join(
        f"{site.path}:{site.lineno} -- two `creationflags` sources on one spawn "
        "call. Both mappings carry the key, so this raises `TypeError: got "
        "multiple values for keyword argument 'creationflags'` when it runs. "
        "Drop the redundant `**no_console_*()` splat and keep the local mapping."
        for site in violations
    )


def test_no_bare_test_tree_spawn():
    """STAGE 3 standing gate: no `subprocess` spawn inside the test tree
    (`is_test_tree_site`) may be bare/unsuppressed either, except an explicit
    `# popup-intentional-last-resort`-tagged site or an explicit
    `exemptions=` entry passed to `find_bare_test_tree_spawns`. Deliberately
    NOT `designed_red`:
    per the dispatch instructions extending this gate, an existing bare
    test-tree spawn is a real finding to report, not a population to wave
    through -- unlike `test_widened_spawn_families_surface_known_preexisting_sites`
    (a different, already-scoped-out family widening), this arm is not
    given a standing exemption from failing.
    """
    violations = find_bare_test_tree_spawns(REPO_ROOT / "coordinator_core")
    assert violations == [], "\n\n".join(_format_violation(site) for site in violations)


@pytest.mark.designed_red
def test_widened_spawn_families_surface_known_preexisting_sites():
    """Red by design, 2026-08-07 -- reported, deliberately not gated.

    Widening the collector past `subprocess` to `os.system`/`os.popen`/
    `asyncio.create_subprocess_*` (Finding 2 of this workstream's review)
    surfaces 28 pre-existing, unsuppressed sites: 25 `asyncio.
    create_subprocess_exec` call sites (July-dated, none introduced by this
    workstream) plus 3 `subprocess` sites whose suppression is genuinely
    conditional/dynamic in a way this AST-only collector cannot trace even
    after this workstream's function-scope resolution pass. Full inventory,
    file/line/enclosing/family, and the verified-suppressed status of each
    of the 3: `state/audits/
    2026-08-07-spawn-gate-widening-surfaces-28-preexisting-sites.md`.

    Why `designed_red`, not gated: burning these down is a follow-up
    workstream (`asyncio.create_subprocess_exec` DOES accept `creationflags`
    -- see that audit's "route to burn-down" note -- so it is a real,
    tractable backlog, not a design dead-end). Gating on them here would
    turn a widening the no-window workstream wants VISIBLE into a blocker
    for the 11 other sessions sharing `main` right now. This test's failure
    output is exactly that worklist, in the marker's own terms: run it
    explicitly to see the current burn-down surface.
    """
    violations = find_bare_hot_path_spawns(REPO_ROOT / "coordinator_core", families=_ALL_FAMILIES)
    assert violations == [], "\n\n".join(_format_violation(site) for site in violations)


def test_gate_detects_a_planted_bare_hot_path_spawn(tmp_path):
    """Proves the gate has teeth: without this test, a permanently-clean
    hot-path tree would make the gate's soundness pass-by-absence rather
    than by verification."""
    scope_dir = tmp_path / "write_guards"
    scope_dir.mkdir()
    fixture = scope_dir / "nudge_planted_fixture.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(payload):\n"
        "    subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True)\n",
        encoding="utf-8",
    )

    violations = find_bare_hot_path_spawns(tmp_path, scope_dirs=("write_guards",))

    assert len(violations) == 1
    site = violations[0]
    assert site.path.endswith("nudge_planted_fixture.py")
    assert site.lineno == 4
    assert site.enclosing == "check"


def test_gate_ignores_a_suppressed_spawn(tmp_path):
    """Negative control: a `creationflags=` kwarg must not be flagged."""
    scope_dir = tmp_path / "write_guards"
    scope_dir.mkdir()
    fixture = scope_dir / "suppressed_fixture.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def check(payload):\n"
        "    subprocess.run(['git', 'status'], creationflags=0x08000000)\n",
        encoding="utf-8",
    )

    violations = find_bare_hot_path_spawns(tmp_path, scope_dirs=("write_guards",))

    assert violations == []


def test_gate_ignores_a_primitive_splat_spawn(tmp_path):
    """Negative control: the `**no_console_creationflags()` splat shape (the
    primitive's own call convention) must not be flagged."""
    scope_dir = tmp_path / "hooks"
    scope_dir.mkdir()
    fixture = scope_dir / "primitive_fixture.py"
    fixture.write_text(
        "import subprocess\n"
        "from coordinator_core.win_portability import no_console_creationflags\n"
        "\n"
        "def _git():\n"
        "    subprocess.run(['git', 'status'], **no_console_creationflags())\n",
        encoding="utf-8",
    )

    violations = find_bare_hot_path_spawns(tmp_path, scope_dirs=("hooks",))

    assert violations == []


def test_gate_honours_the_last_resort_exemption_tag(tmp_path):
    """Negative control: a `# popup-intentional-last-resort`-tagged bare
    call is compliant-by-exemption, matching `nudge_windows_subprocess_popup`'s
    own accepted escape hatch."""
    scope_dir = tmp_path / "ops" / "session"
    scope_dir.mkdir(parents=True)
    fixture = scope_dir / "exempted_fixture.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def restore():\n"
        "    subprocess.run(\n"
        "        ['git', 'rev-parse', '--git-dir'],\n"
        "        # popup-intentional-last-resort\n"
        "    )\n",
        encoding="utf-8",
    )

    violations = find_bare_hot_path_spawns(tmp_path, scope_dirs=("ops/session",))

    assert violations == []


def test_gate_ignores_out_of_scope_directories(tmp_path):
    """Negative control: an explicit narrower `scope_dirs` argument still
    excludes a bare spawn outside it -- proves the scope list, not the
    detection logic, is what bounds this gate. (Stage 1 exercised this via
    the default `_GATE_SCOPE_DIRS`; stage 2's default is repo-wide
    (`("",)`), so this now passes an explicit narrower scope directly.)"""
    scope_dir = tmp_path / "install"
    scope_dir.mkdir()
    fixture = scope_dir / "unscoped_fixture.py"
    fixture.write_text(
        "import subprocess\n"
        "\n"
        "def setup():\n"
        "    subprocess.run(['git', 'status'], capture_output=True)\n",
        encoding="utf-8",
    )

    violations = find_bare_hot_path_spawns(tmp_path, scope_dirs=("hooks",))

    assert violations == []


def test_gate_ignores_an_unrelated_dot_run_call(tmp_path):
    """Negative control: an unrelated `.run(...)` method call (not resolvable
    to the `subprocess` module) must not be flagged -- the precedent adjustment
    named in the module docstring."""
    scope_dir = tmp_path / "hooks"
    scope_dir.mkdir()
    fixture = scope_dir / "unrelated_run_fixture.py"
    fixture.write_text(
        "class Guard:\n"
        "    def run(self, payload):\n"
        "        return payload\n"
        "\n"
        "def check(payload):\n"
        "    return Guard().run(payload)\n",
        encoding="utf-8",
    )

    violations = find_bare_hot_path_spawns(tmp_path, scope_dirs=("hooks",))

    assert violations == []


#: Bare `subprocess`-family spawn sites in the live source roots this module's
#: standing gate does NOT walk, measured 2026-08-21. A RATCHET: each number may
#: only ever go DOWN, and the test below fails in BOTH directions so the figure
#: cannot rot in either.
#:
#: Why this exists. This module's docstring says "SCOPE -- repo-wide, STAGE 2",
#: but `test_no_bare_hot_path_spawn` calls
#: `find_bare_hot_path_spawns(REPO_ROOT / "coordinator_core")` -- so
#: `coordinator/bin` and `coordinator/lib` were never walked at all, and the
#: gate reported green because of what it declined to look at. That is the F1/
#: F13 recurrence shape (a check reporting clean on the strength of its own
#: blind spot) that `write_guards/nudge_windows_subprocess_popup.py`'s
#: negative-spec names, here in the gate meant to prevent it. Found 2026-08-21
#: while sizing the burn-down for
#: `state/audits/2026-08-21-detached-process-console-window-storm.md`;
#: `coordinator/bin/coordinator-prepare-commit-msg.py` is a LIVE git hook and
#: was among the unwalked files.
#:
#: These sites are NOT gated to zero here, deliberately: `no_console_creation
#: flags()` splatted into a spawn that wires no `stdout`/`stderr`/
#: `capture_output` silently loses the child's output (measured -- see
#: `win_portability`'s "THE CATCH"), so each site needs a per-site
#: capture-vs-passthrough call, not a sweep. The ratchet makes the population
#: visible and stops it growing while that burn-down proceeds. Sizing and
#: rationale: `docs/decisions/DR-357-console-popup-guard-fires-per-call-site-and-keeps-whole-file-scope.md`.
#: COVERAGE IS THE POINT: every source root holding `.py` files that the
#: standing `coordinator_core` leg does not walk appears here, including the
#: roots currently at ZERO. A zero entry is not filler -- it is what makes a
#: first bare spawn landing in `dist/` or `scripts/` fail immediately instead
#: of starting a new invisible population. Listing only the roots that happened
#: to be dirty would rebuild, one level up, the exact blind spot this ratchet
#: was added to close.
#:
#: Derived by enumerating every directory containing `.py` files and
#: subtracting the roots a gate leg already walks -- not by listing the roots
#: that looked interesting. `archive/`, `state/`, `tasks/`, `docs/` and
#: `cross-repo/` are prose/ephemera corpora, not source roots, and are out of
#: scope by the same reasoning `DEFAULT_EXCLUDE` uses.
_UNWALKED_ROOT_BASELINE: dict[str, int] = {
    "bin": 0,
    # 112 -> 110, 2026-08-21: coordinator-prepare-commit-msg.py, a LIVE
    # prepare-commit-msg hook spawning git from the console-less Bash-tool
    # parent, is the highest-value site in this population and is now fully
    # suppressed at all three of its spawns.
    "coordinator/bin": 93,
    "coordinator/lib": 10,
    "coordinator/scripts": 2,
    # `coordinator/tests` is deliberately ABSENT (was present, removed on
    # review 2026-08-21). Every path under a `tests/` directory is filtered by
    # `is_test_tree_site` AFTER the walk, so the entry was structurally
    # incapable of ever being non-zero — it read as coverage while providing
    # none, which is the same dishonesty as the unwalked roots this baseline
    # exists to fix. Test-tree exemption is the gate's design, applied
    # uniformly (`coordinator_core/**/tests/**` is exempt too), so this is not
    # a gap left open.
    "dist": 0,
    "scripts": 0,
}

#: Top-level `.py` files, which no root key above reaches — a baseline key is a
#: DIRECTORY and `find_bare_hot_path_spawns` walks recursively, so there is no
#: key that means "the repo root, non-recursively".
#:
#: Covered by POPULATION *and* by content, at two different costs. Scoping
#: `find_bare_hot_path_spawns` at `REPO_ROOT` to reach these two files
#: measured **6969ms CPU** — it enumerates every `.py` in the repo before the
#: scope filter applies, versus 1281ms for all six directory roots combined.
#: That is disproportionate for a fast-tier gate under this repo's load norm,
#: and "make the work cheaper" beats widening the budget -- but the fix is a
#: cheaper walk, not giving up on content coverage: `_bare_spawns_in_file`
#: parses exactly these two named files directly (no directory walk at all),
#: measured sub-millisecond. `test_top_level_py_files_have_no_bare_spawn`
#: below uses it, so an *existing* pinned file gaining a bare spawn later is
#: caught same as a new file arriving.
#:
#: The population pin still earns its place alongside that: a new top-level
#: `.py` file is invisible to every baseline root (none of them reach
#: `REPO_ROOT` non-recursively) and invisible to the content check above
#: (which only scans names already in this set), so nothing catches its
#: *arrival* without this assertion forcing a decision about it. Found in
#: review 2026-08-21; `conftest.py` is loaded by every pytest run, so this
#: surface is genuinely hot even though both files are currently clean.
_REPO_ROOT_PY_FILES: frozenset[str] = frozenset(
    {"conftest.py", "scratch_check_forwarders.py"}
)


def test_top_level_py_population_is_pinned():
    """A new top-level `.py` file must be a decision, never a silent arrival.

    Not a spawn check — a POPULATION check, for the one surface no baseline
    key can reach. See `_REPO_ROOT_PY_FILES` for why this is not a walk.
    """
    found = {p.name for p in REPO_ROOT.glob("*.py")}
    assert found == set(_REPO_ROOT_PY_FILES), (
        "top-level .py population changed — added: "
        f"{sorted(found - set(_REPO_ROOT_PY_FILES))}, removed: "
        f"{sorted(set(_REPO_ROOT_PY_FILES) - found)}. No baseline root reaches "
        "the repo root non-recursively, so a new file here is uncovered by "
        "every gate leg. Either console-suppress its spawns and add it to "
        "_REPO_ROOT_PY_FILES, or move it under a covered root."
    )


def test_top_level_py_files_have_no_bare_spawn():
    """Content check for the pinned population itself -- catches a bare spawn
    landing in an EXISTING root file (`conftest.py` gaining one later), which
    the population pin above cannot: it only checks the filename set, not
    what's inside. Direct per-file parse, not a walk -- see
    `_REPO_ROOT_PY_FILES`'s docstring for the cost this avoids.
    """
    violations: list[BareSpawnSite] = []
    for name in sorted(_REPO_ROOT_PY_FILES):
        file_path = REPO_ROOT / name
        if not file_path.is_file():
            continue
        violations.extend(_bare_spawns_in_file(name, file_path))
    assert violations == [], "\n\n".join(_format_violation(site) for site in violations)


def test_bare_spawn_outside_coordinator_core_only_ever_shrinks():
    """Ratchet over the live source roots the standing gate does not walk.

    Fails in both directions on purpose. A rise is a new bare spawn landing in
    an unguarded root -- the regrowth this gate exists to stop. A fall is the
    burn-down making progress, and must be banked by lowering the baseline in
    the same commit; a ratchet that silently accepts a lower number stops being
    evidence of anything.
    """
    missing = [r for r in _UNWALKED_ROOT_BASELINE if not (REPO_ROOT / r).is_dir()]
    assert not missing, (
        "baseline names source root(s) that no longer exist: "
        + ", ".join(missing)
        + ". A renamed or deleted root must be re-derived here, not dropped -- "
        "a stale key silently stops covering wherever its files went."
    )
    measured = {
        root: len(find_bare_hot_path_spawns(REPO_ROOT / root))
        for root in _UNWALKED_ROOT_BASELINE
    }
    grew = {r: (n, _UNWALKED_ROOT_BASELINE[r]) for r, n in measured.items() if n > _UNWALKED_ROOT_BASELINE[r]}
    shrank = {r: (n, _UNWALKED_ROOT_BASELINE[r]) for r, n in measured.items() if n < _UNWALKED_ROOT_BASELINE[r]}

    assert not grew, (
        "new bare subprocess spawn(s) landed in a root the standing gate does "
        "not walk: "
        + ", ".join(f"{r} {base} -> {now}" for r, (now, base) in grew.items())
        + ". Splat coordinator_core.win_portability.no_console_creationflags() "
        "into the call -- or no_console_passthrough_kwargs() if the child's "
        "output must reach the operator -- or tag the line "
        "`# popup-intentional-last-resort`."
    )
    assert not shrank, (
        "burn-down progress is unbanked -- lower _UNWALKED_ROOT_BASELINE in "
        "this commit: "
        + ", ".join(f"{r} {base} -> {now}" for r, (now, base) in shrank.items())
    )


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
