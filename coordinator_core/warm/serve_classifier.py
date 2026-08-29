"""coordinator_core.warm.serve_classifier — the committed instrument that
answers "does this `coordinator/bin/<name>` CLI warm-serve?", so no count in
the 2026-08-27 "every bin name warm-serves" workstream is ever hand-derived
again.

Purpose: three passes at this question already produced three wrong answers
(a name check that missed arity, a prefix-match exclusion, and a hand-rolled
`sys.path` count that exceeded its own population) — see the origin plan's
Problem section (`docs/plans/2026-08-27-every-bin-name-warm-serves-and-a-
classifier-says-so.md`). This module is the fix: a single AST-only predicate,
committed and tested, that both the C8 guard test and `coordinator/bin/warm-
serve-partition.py` consume, instead of a fresh scratch script per session.

LIFTED, not re-derived: the structural module-body-inertness predicate below
(`_is_main_def` through `find_module_body_violations`, and its 14-shape
fixture-test coverage in `coordinator_core/warm/tests/test_serve_classifier.py`)
is a direct port of `find_entrypoint_inertness_violations` from
`X:/DoE-claude/coordinator/tests/test_bin_entrypoint_inertness.py`  # abs-path-ok: cites the DoE checkout lifted from; the plugin-installed coordinator tree carries no tests/, so no repo-relative form resolves
(711 lines, checkout as of 2026-08-27) — the origin plan directs lifting this rather than
re-deriving a second inertness checker (Anti-scope: "Do not build a second
inertness predicate"). Claude-klabauter is DoE's HARD prereq, so this module owns the
canonical copy going forward; the dependency direction means DoE can import
FROM claude-klabauter once this lands, never the reverse. A cross-repo memo offering
DoE that consolidation is this plan's job to name, not to land (C1 body).

Three deltas over the lifted predicate:

1. **Arity.** DoE's `_is_main_def` proves a `main` def exists; it says
   nothing about whether it is callable as `main(argv) -> int`. `def
   main():` — zero-arity — passed DoE's predicate and ~160 claude-klabauter names
   were counted as warm-serving on that basis alone; every one of them
   raises `TypeError` the first time the door calls it with one argument.
   `classify_entrypoint` checks arity independently of module-body purity.

2. **Script existence.** A name absent from `coordinator/bin/<name>.py`
   entirely is a distinct failure mode from "resolves but is unservable" —
   `_resolve_entrypoint_script` (coordinator_core/ops/invoke_from_argv.py)
   already fails this closed at request time; this module names it as its
   own partition bucket so the count is visible ahead of any request.

3. **Module-scope import purity — the load-bearing delta.** DoE's own
   `_INERT_STATEMENT_BODY_TYPES` treats a module-scope `Import`/`ImportFrom`
   as inert by construction (see that module's docstring). So
   `from lib.cc_invoke import require_dispatch_engine_on_path` — verbatim
   the module-scope line that killed `coordinator-auto-push.py` on the
   settings-home forwarder route (2a66fc8e9) — PASSES the lifted predicate,
   and passes it again with arity bolted on. `find_module_body_violations`
   below adds a third conjunct DoE's checker does not have: a module-scope
   import (including one guarded inside the permitted
   `try: import X / except ImportError: X = None` shape) must resolve to
   `__future__` or a stdlib module, on pain of a "module-scope non-stdlib
   import" finding. `lib.*` and `coordinator_core.*` are never stdlib, so
   this is what actually catches the forwarder-route hazard; the door route
   survives non-stdlib imports today only because `_ensure_bin_dir_importable`
   bootstraps `sys.path` for it first (see the origin plan's "Two load
   routes" table) — a bootstrap the forwarder does not share.

`_PURE_CALL_TARGETS` below is DoE's whitelist SHAPE, reseeded from claude-klabauter's
own `coordinator/bin/*.py` corpus (419 files, not DoE's 17) — see the seeding
survey docstring on the constant itself for the reviewed count and the
targets deliberately left OUT (repo-local engine-bootstrap helpers, which
this classifier exists to keep catching, not launder past).

Negative-spec (RAG-bait):
    This module does NOT invoke, import, or exec any `coordinator/bin/*.py`
    module body to answer these questions — see the origin plan's Anti-scope
    ("Do not invoke the 382 to measure them"): importing every module body
    into this process (let alone the shared warm server) is the exact hazard
    under investigation. Every verdict here comes from `ast.parse` over the
    file's own source text, nothing more.

    This module does NOT regenerate `warm_entrypoint_allowlist.json` — that
    is C2's `forwarder_door_census.py`, a distinct writer this module only
    READS from via `load_allowlist_names`.

    This module does NOT fix a single flagged name. `classify_population`
    and `partition_report` are read-only over the corpus; C3-C6 are the
    chunks that make the flagged buckets shrink.

    This module's predicate answers module-body inertness -- whether
    executing the file's TOP-LEVEL statements mutates process-global state
    -- and NOT the stronger claim "this name causes no process-global
    mutation at request time." A `_bootstrap_*` helper deferred out of
    module scope (the C6k import-motion shape) that writes `sys.path` or
    `globals()` the first time a request calls it is invisible to this walk
    by construction, because the walk never descends into a function body.
    "servable N / every failure bucket 0" means every surveyed module body
    is inert at import time; a reader who hears "no shared-process mutation
    remains" is reading a claim this module does not make.

Spec backlink: docs/plans/2026-08-27-every-bin-name-warm-serves-and-a-classifier-says-so.md, chunk C1
"""

from __future__ import annotations

import ast
import json
import sys
from dataclasses import dataclass
from pathlib import Path

#: This file lives at `<engine_root>/coordinator_core/warm/serve_classifier.py`
#: — `parents[2]` is `<engine_root>` itself, the same root `coordinator/bin/`
#: and `coordinator_core/ops/invoke_from_argv.py` hang off. No second
#: root-resolution mechanism is introduced here.
_ENGINE_ROOT = Path(__file__).resolve().parents[2]
_BIN_DIR = _ENGINE_ROOT / "coordinator" / "bin"
_ALLOWLIST_PATH = _ENGINE_ROOT / "coordinator_core" / "ops" / "warm_entrypoint_allowlist.json"


@dataclass(frozen=True)
class Finding:
    """One module-body-inertness violation. `key()` is the baseline-ratchet
    identity DoE's own shrinking-baseline mechanism uses (path + text, not
    line number, so a baseline entry survives an unrelated line shifting)."""

    path: str
    line: int
    text: str
    reason: str
    is_sys_path_mutation: bool = False

    def key(self) -> tuple[str, str]:
        return (self.path, self.text)


# ---------------------------------------------------------------------------
# Lifted structural predicate (DoE's find_entrypoint_inertness_violations),
# ported verbatim in shape. See module docstring for the three deltas.
# ---------------------------------------------------------------------------


def _is_docstring_expr(stmt: ast.stmt) -> bool:
    return (
        isinstance(stmt, ast.Expr)
        and isinstance(stmt.value, ast.Constant)
        and isinstance(stmt.value.value, str)
    )


def _is_main_def(stmt: ast.stmt) -> bool:
    return isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "main"


def _is_name_main_guard(stmt: ast.stmt) -> bool:
    """`if __name__ == "__main__":` -- the one permitted module-scope `If`."""
    if not isinstance(stmt, ast.If):
        return False
    test = stmt.test
    if not isinstance(test, ast.Compare) or len(test.ops) != 1 or len(test.comparators) != 1:
        return False
    if not isinstance(test.ops[0], ast.Eq):
        return False
    left, right = test.left, test.comparators[0]
    name_side = left if isinstance(left, ast.Name) else (right if isinstance(right, ast.Name) else None)
    const_side = right if isinstance(right, ast.Constant) else (left if isinstance(left, ast.Constant) else None)
    if name_side is None or const_side is None:
        return False
    return name_side.id == "__name__" and const_side.value == "__main__"


_INERT_STATEMENT_BODY_TYPES = (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign)


def _is_permitted_try(stmt: ast.stmt) -> bool:
    """A Try whose body AND except-handler bodies contain only imports and
    assignments -- the `try: import yaml / except ImportError: yaml = None`
    guarded-optional-dependency shape. Structurally permitted (no process
    mutation lives in this shape), but its imports are NOT exempted from the
    module-scope import-purity conjunct below -- see
    `_check_permitted_try_imports`."""
    if not isinstance(stmt, ast.Try):
        return False
    if not all(isinstance(s, _INERT_STATEMENT_BODY_TYPES) for s in stmt.body):
        return False
    for handler in stmt.handlers:
        if not all(isinstance(s, _INERT_STATEMENT_BODY_TYPES) for s in handler.body):
            return False
    if stmt.orelse and not all(isinstance(s, _INERT_STATEMENT_BODY_TYPES) for s in stmt.orelse):
        return False
    if stmt.finalbody and not all(isinstance(s, _INERT_STATEMENT_BODY_TYPES) for s in stmt.finalbody):
        return False
    return True


# A PURITY WHITELIST, not a blocklist -- deliberately the inverse of a
# call-name blocklist (a blocklist of impure calls never terminates, because
# the next process-mutating call nobody has thought of yet ships ungated). A
# module-scope Assign/AnnAssign RHS, and a decorator or default-argument
# expression applied to a module-scope def/class, is inert only if every
# Call it contains targets one of these entries.
#
# RESEEDED for claude-klabauter's `coordinator/bin/*.py` corpus (419 files) at
# gate-authoring time (2026-08-27) -- DoE's own whitelist was seeded from
# their 17-file corpus and does not transfer; running it unmodified against
# claude-klabauter's corpus reports a large false-violation set the origin plan's C1
# body warns against ("Budget the whitelist re-seed -- it is the expensive
# part"). Surveyed via a one-off AST walk over every module-scope
# Assign/AnnAssign RHS, decorator, function default, and class base/keyword
# expression in the corpus (497 call sites, 47 distinct target keys) and
# reviewed entry-by-entry per DoE's own rule: "a DELIBERATE act, not a way
# to make a new site pass" -- each entry below executes at import but
# mutates no state outside the binding it produces, confined to filesystem
# path arithmetic, string formatting, or def/class-time metadata attachment.
#
# 25 entries seeded, covering 391 of the 497 surveyed call sites. The
# DELIBERATELY EXCLUDED remainder (`require_dispatch_engine_on_path`,
# `require_engine_on_path`, `require_colocated_engine_on_path`, and a
# handful of single-site repo-local loader helpers such as
# `_load_native_op_module` / `_resolve_plugin_root`) are exactly the
# engine-bootstrap and module-loading calls this classifier exists to keep
# flagging -- whitelisting them would launder the C1 defect past the gate
# it was built to catch, not fix it.
_PURE_CALL_TARGETS = frozenset(
    {
        # pathlib / os.path arithmetic -- reads no process-global state,
        # produces a new value only.
        "Path",
        "pathlib.Path",
        "resolve",  # Path(...).resolve() chained-call form
        "with_name",
        "dirname",
        "abspath",
        "join",
        "isfile",
        # string / regex / collection construction.
        "re.compile",
        "frozenset",
        "str",
        "textwrap.dedent",
        # read-only lookups -- `os.environ.get(...)` / dict.get(...); no
        # site in the surveyed corpus targets a network-`.get`.
        "get",
        "getattr",
        # def/class-time metadata attachment -- each confines its effect to
        # the class or function being defined, same bar DoE's own decorator
        # entries document.
        "dataclass",
        "field",
        "object",
        # `@property`, `@x.setter`, `@staticmethod`, `@classmethod` and
        # `@contextlib.contextmanager` are stdlib def-time wrappers: each
        # returns a descriptor or a wrapped function and touches nothing
        # outside the class or function being defined. Their absence made the
        # classifier report `wsc-session-disposition` and `publish` as impure
        # for using `@property` -- a false positive in the instrument this
        # plan exists to make trustworthy, which is worse than the count it
        # was built to correct.
        "property",
        "staticmethod",
        "classmethod",
        # NOTE: `setter`/`getter`/`deleter` are handled structurally in
        # `_check_decorators`, not by name here -- `@x.setter` where `x` is
        # a bare `Name` yields `_expr_target_key` == "x.setter", which can
        # never match a bare "setter" entry (that shape only matches a
        # receiver that is itself an Attribute or Call, e.g. `a.b.setter`,
        # which this idiom never produces). See `_check_decorators`.
        "contextlib.contextmanager",
        "contextmanager",
        "functools.lru_cache",
        "pytest.fixture",
        "parametrize",  # pytest.mark.parametrize
        "unittest.skipUnless",
        # importlib object construction -- creates a spec/module OBJECT,
        # executes no caller-supplied code (that happens at a later,
        # separately-checked `exec_module` call this whitelist does not
        # cover).
        "spec_from_file_location",
        "module_from_spec",
        "_ilu.spec_from_file_location",  # observed local `importlib.util` alias
        "_ilu.module_from_spec",
    }
)


def _expr_target_key(node: ast.expr) -> str | None:
    """Identify what a Call (or a bare decorator reference, which is an
    implicit call) targets, well enough to check it against
    `_PURE_CALL_TARGETS`. `Path(...)` -> "Path"; `re.compile(...)` ->
    "re.compile"; `Path(__file__).resolve()` -> "resolve" (the attribute
    name alone, since the receiver is itself a Call, not a bare module)."""
    if isinstance(node, ast.Call):
        return _expr_target_key(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name):
            return f"{node.value.id}.{node.attr}"
        return node.attr
    return None


def _is_literal_method_call(call: ast.Call) -> bool:
    """True for a method invoked directly on a LITERAL -- a triple-quoted
    constant followed by `.strip()`, or `"a,b".split(",")`.

    The receiver is a `Constant`, so the call cannot reach process state
    whatever the method is named, and no name-based whitelist entry is needed.
    Adding `strip` to `_PURE_CALL_TARGETS` would also bless `anything.strip()`
    on an arbitrary object; this does not.

    Why this exists: six module-scope prompt constants in
    `workday-start-inbox-blitz-assemble.py` are a triple-quoted literal
    followed by `.strip()`, and the classifier reported all six as
    "module-scope process mutation". A false positive here is not a cosmetic
    miscount -- it is the instrument this plan was written to make trustworthy
    reporting a clean file as dirty.
    """
    return isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Constant)


def _is_pure_expr(node: ast.expr) -> bool:
    """True iff every Call anywhere inside `node` targets a whitelisted pure
    callable. Fails CLOSED on a Call whose own `func` is not itself a Name
    or Attribute."""
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if not isinstance(sub.func, (ast.Name, ast.Attribute)):
                return False
            if _is_literal_method_call(sub):
                continue
            if _expr_target_key(sub) not in _PURE_CALL_TARGETS:
                return False
    return True


def _is_pure_assign(stmt: ast.Assign | ast.AnnAssign) -> bool:
    if stmt.value is None:
        return True
    return _is_pure_expr(stmt.value)


def _source_line_text(source: str, node: ast.AST) -> str:
    segment = ast.get_source_segment(source, node)
    if segment is None:
        return f"<line {node.lineno}>"
    return segment.strip().splitlines()[0]


_DESCRIPTOR_PROTOCOL_ATTRS = frozenset({"setter", "getter", "deleter"})


def _is_descriptor_protocol_decorator(dec: ast.expr) -> bool:
    """True for `@x.setter` / `@x.getter` / `@x.deleter` on ANY receiver
    shape -- the receiver is always the property object being redefined
    (`x` a bare `Name` in the common idiom, or an Attribute/Call chain),
    never process state, so the receiver shape is irrelevant to purity.
    Checked on `attr` directly rather than through `_expr_target_key` +
    `_PURE_CALL_TARGETS`, because a bare-name whitelist entry can only ever
    match a receiver that is itself an Attribute or Call (`a.b.setter`) --
    not the bare-Name receiver (`@x.setter`) this idiom actually produces."""
    return isinstance(dec, ast.Attribute) and dec.attr in _DESCRIPTOR_PROTOCOL_ATTRS


def _check_decorators(stmt: ast.stmt, relpath: str, source: str) -> list[Finding]:
    violations: list[Finding] = []
    for dec in getattr(stmt, "decorator_list", []):
        if _is_descriptor_protocol_decorator(dec):
            continue
        target = _expr_target_key(dec)
        pure = target in _PURE_CALL_TARGETS and (not isinstance(dec, ast.Call) or _is_pure_expr(dec))
        if not pure:
            violations.append(
                Finding(relpath, dec.lineno, _source_line_text(source, dec), "impure decorator")
            )
    return violations


def _check_import_rebinding(stmt: ast.Import | ast.ImportFrom, relpath: str, source: str) -> list[Finding]:
    """An explicit `import ... as <name>` / `from ... import ... as <name>`
    that renames something to a name this gate already trusts is itself a
    violation -- the gate cannot tell whether the renamed thing is really
    the trusted callable."""
    violations: list[Finding] = []
    for alias in stmt.names:
        if alias.asname and alias.asname in _PURE_CALL_TARGETS:
            violations.append(
                Finding(relpath, stmt.lineno, _source_line_text(source, stmt), "whitelisted-name import alias")
            )
    return violations


#: __future__ is importable at module scope by definition (it is a
#: compile-time directive, not a runtime side effect) but is not a member of
#: `sys.stdlib_module_names` -- added explicitly so `from __future__ import
#: annotations` never trips the import-purity conjunct.
_STDLIB_MODULE_ROOTS = frozenset(sys.stdlib_module_names) | {"__future__"}


def _check_module_scope_import_purity(stmt: ast.Import | ast.ImportFrom, relpath: str, source: str) -> list[Finding]:
    """C1's third conjunct (the load-bearing delta over the lifted
    predicate): a module-scope import must resolve to `__future__` or a
    stdlib module. `lib.*`, `coordinator_core.*`, and any third-party root
    (`yaml`, ...) fail this -- these are exactly the imports that resolve on
    the bootstrapped warm-door route (`_ensure_bin_dir_importable` puts
    `coordinator/bin` and its `lib/` on `sys.path` first) and raise
    `ModuleNotFoundError` on the un-bootstrapped settings-home forwarder
    route (`_resolve_claude_klabauter.py :: _run_target_in_process` -> `runpy.run_path`,
    no bootstrap). A relative import (`from . import x`, `level > 0`) is
    never stdlib and always flagged -- a bin CLI has no package to be
    relative to."""
    violations: list[Finding] = []
    if isinstance(stmt, ast.Import):
        for alias in stmt.names:
            root = alias.name.split(".")[0]
            if root not in _STDLIB_MODULE_ROOTS:
                violations.append(
                    Finding(relpath, stmt.lineno, _source_line_text(source, stmt), "module-scope non-stdlib import")
                )
                break
        return violations
    # ast.ImportFrom
    if stmt.level and stmt.level > 0:
        violations.append(
            Finding(relpath, stmt.lineno, _source_line_text(source, stmt), "module-scope non-stdlib import")
        )
        return violations
    root = (stmt.module or "").split(".")[0]
    if root not in _STDLIB_MODULE_ROOTS:
        violations.append(
            Finding(relpath, stmt.lineno, _source_line_text(source, stmt), "module-scope non-stdlib import")
        )
    return violations


def _check_permitted_try_imports(stmt: ast.Try, relpath: str, source: str) -> list[Finding]:
    """The guarded-optional-dependency shape (`try: import yaml / except
    ImportError: yaml = None`) is structurally permitted by
    `_is_permitted_try` -- no process mutation lives in that shape -- but
    its imports are NOT exempted from the import-purity conjunct: a guarded
    `import yaml` still resolves to a non-stdlib module, and the door-vs-
    forwarder asymmetry it can hit is identical whether or not the import is
    wrapped in a `try`. Walks body, every handler's body, orelse, and
    finalbody -- the same statement lists `_is_permitted_try` already
    proved contain only imports and assignments."""
    violations: list[Finding] = []
    bodies = [stmt.body, stmt.orelse, stmt.finalbody] + [h.body for h in stmt.handlers]
    for body in bodies:
        for sub in body:
            if isinstance(sub, (ast.Import, ast.ImportFrom)):
                violations.extend(_check_module_scope_import_purity(sub, relpath, source))
    return violations


def _check_assign_rebinding(stmt: ast.Assign | ast.AnnAssign, relpath: str, source: str) -> list[Finding]:
    targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
    violations: list[Finding] = []
    for target in targets:
        if isinstance(target, ast.Name) and target.id in _PURE_CALL_TARGETS:
            violations.append(
                Finding(relpath, stmt.lineno, _source_line_text(source, stmt), "whitelisted-name rebinding")
            )
    return violations


def _check_function_defaults(
    stmt: ast.FunctionDef | ast.AsyncFunctionDef, relpath: str, source: str
) -> list[Finding]:
    violations: list[Finding] = []
    for default in list(stmt.args.defaults) + [d for d in stmt.args.kw_defaults if d is not None]:
        if not _is_pure_expr(default):
            violations.append(
                Finding(
                    relpath,
                    default.lineno,
                    _source_line_text(source, default),
                    "impure default-argument expression",
                )
            )
    return violations


def _check_class_bases(stmt: ast.ClassDef, relpath: str, source: str) -> list[Finding]:
    violations: list[Finding] = []
    for base in stmt.bases:
        if not _is_pure_expr(base):
            violations.append(
                Finding(relpath, base.lineno, _source_line_text(source, base), "impure class base expression")
            )
    for kw in stmt.keywords:
        if kw.value is not None and not _is_pure_expr(kw.value):
            violations.append(
                Finding(
                    relpath, kw.value.lineno, _source_line_text(source, kw.value), "impure class keyword expression"
                )
            )
    return violations


def _sys_aliases(tree: ast.Module) -> frozenset[str]:
    """Every module-scope name bound to the `sys` module -- `import sys` ->
    `{"sys"}`, `import sys as s` -> `{"s"}`. Used to detect `sys.path`
    mutation STRUCTURALLY (an Attribute chain rooted at one of these names)
    rather than by the literal substring `"sys.path"` in the finding's
    rendered source text, which an alias (`s.path.insert(...)`) evades."""
    aliases: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                if alias.name == "sys":
                    aliases.add(alias.asname or alias.name)
    return frozenset(aliases)


def _contains_sys_path_mutation(node: ast.AST, sys_aliases: frozenset[str]) -> bool:
    """True iff any Attribute node inside `node` is `<alias>.path` for a
    name bound to the `sys` module -- catches `sys.path.insert(...)`,
    `sys.path += [...]`, and any aliased-import equivalent, wherever it
    appears in the statement (not just at the statement's own top level)."""
    for sub in ast.walk(node):
        if (
            isinstance(sub, ast.Attribute)
            and sub.attr == "path"
            and isinstance(sub.value, ast.Name)
            and sub.value.id in sys_aliases
        ):
            return True
    return False


def _check_body(
    body: list[ast.stmt], relpath: str, source: str, sys_aliases: frozenset[str] = frozenset()
) -> list[Finding]:
    """Check one statement-body (module top level, or a ClassDef body
    recursed into with the same rules) for module-body-inertness
    violations, including C1's third (import-purity) conjunct."""
    violations: list[Finding] = []
    for stmt in body:
        if isinstance(stmt, (ast.Import, ast.ImportFrom)):
            violations.extend(_check_import_rebinding(stmt, relpath, source))
            violations.extend(_check_module_scope_import_purity(stmt, relpath, source))
            continue
        if isinstance(stmt, ast.Pass):
            continue
        if _is_docstring_expr(stmt):
            continue
        if _is_name_main_guard(stmt):
            continue
        if _is_permitted_try(stmt):
            violations.extend(_check_permitted_try_imports(stmt, relpath, source))
            continue
        if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
            violations.extend(_check_assign_rebinding(stmt, relpath, source))
            if _is_pure_assign(stmt):
                continue
            violations.append(
                Finding(
                    relpath,
                    stmt.lineno,
                    _source_line_text(source, stmt),
                    "module-scope process mutation",
                    is_sys_path_mutation=_contains_sys_path_mutation(stmt, sys_aliases),
                )
            )
            continue
        if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
            violations.extend(_check_decorators(stmt, relpath, source))
            violations.extend(_check_function_defaults(stmt, relpath, source))
            continue
        if isinstance(stmt, ast.ClassDef):
            violations.extend(_check_decorators(stmt, relpath, source))
            violations.extend(_check_class_bases(stmt, relpath, source))
            violations.extend(_check_body(stmt.body, relpath, source, sys_aliases))
            continue
        violations.append(
            Finding(
                relpath,
                stmt.lineno,
                _source_line_text(source, stmt),
                "module-scope process mutation",
                is_sys_path_mutation=_contains_sys_path_mutation(stmt, sys_aliases),
            )
        )
    return violations


def find_module_body_violations(source: str, relpath: str) -> list[Finding]:
    """Walk `source`'s module scope and return every module-body-purity
    violation -- the lifted predicate's two conjuncts (structural process-
    mutation, `main` presence) plus C1's import-purity conjunct. Does NOT
    check arity -- see `_main_arity_ok` / `classify_entrypoint`, a
    deliberately separate axis (a file can be module-body-inert and still
    unservable for having `def main():` with no `argv` parameter).

    NEGATIVE SPEC -- this predicate answers module-body inertness at IMPORT
    time, NOT absence of process-global mutation at REQUEST time. A
    `_bootstrap_*` helper deferred out of module scope (C6k's own shape)
    that mutates `sys.path` or `globals()` the first time a request calls
    it is invisible here by construction -- this walk never descends into a
    function body. 'every failure bucket 0' over this predicate means every
    surveyed module body is inert; it does not mean the process this module
    serves in never mutates shared state."""
    tree = ast.parse(source, filename=relpath)
    sys_aliases = _sys_aliases(tree)
    violations: list[Finding] = []

    if not any(_is_main_def(stmt) for stmt in tree.body):
        violations.append(
            Finding(relpath, 1, "<no module-level def main>", "missing module-level main(argv)")
        )

    violations.extend(_check_body(tree.body, relpath, source, sys_aliases))

    return violations


# ---------------------------------------------------------------------------
# C1's own addition: arity + script existence, folded into one per-name
# verdict, and a partition report over a NAMED population.
# ---------------------------------------------------------------------------


def _main_def(tree: ast.Module) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    for stmt in tree.body:
        if _is_main_def(stmt):
            return stmt  # type: ignore[return-value]
    return None


def _main_arity_ok(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True iff `fn` can be called as `main(argv)` -- at least one
    positional parameter, or a `*args` catch-all. `def main():` (the
    ~160-name defect this chunk exists to catch) has neither."""
    args = fn.args
    positional = len(args.posonlyargs) + len(args.args)
    return positional >= 1 or args.vararg is not None


@dataclass(frozen=True)
class ServeVerdict:
    """One name's full warm-serve verdict: resolution, arity, and
    module-body purity, each an independent axis (see `find_module_body_
    violations`'s docstring for why arity is checked separately)."""

    name: str
    script_relpath: str
    script_exists: bool
    has_main: bool
    main_arity_ok: bool
    findings: tuple[Finding, ...]
    parse_error: str | None = None

    @property
    def servable(self) -> bool:
        """Resolves, exposes `main`, and that `main` is callable as
        `main(argv)` -- independent of module-body purity (a name can be
        servable on the bootstrapped door route today while still being an
        AC20 module-body-purity defect on the forwarder route)."""
        return self.script_exists and self.has_main and self.main_arity_ok

    @property
    def inert(self) -> bool:
        """Module body carries zero purity findings (sys.path mutation,
        non-stdlib import, or any other structural violation). A file that
        could not be read/parsed (`parse_error` set) is NOT inert -- it was
        never examined, which is a distinct state from "examined and
        clean"."""
        return self.script_exists and self.parse_error is None and len(self.findings) == 0

    @property
    def sys_path_mutation(self) -> bool:
        return any(f.is_sys_path_mutation for f in self.findings)

    @property
    def non_stdlib_import(self) -> bool:
        return any(f.reason == "module-scope non-stdlib import" for f in self.findings)


def _relpath(path: Path) -> str:
    return str(path.relative_to(_ENGINE_ROOT)).replace("\\", "/")


def classify_entrypoint(name: str, bin_dir: Path = _BIN_DIR) -> ServeVerdict:
    """Classify one allowlist NAME (not a path) against the same resolution
    rule the door itself uses (`coordinator_core.ops.invoke_from_argv ::
    _resolve_entrypoint_script`): `coordinator/bin/<name>.py`. Reads the
    file's source text and parses it -- never imports or execs it (see this
    module's negative-spec)."""
    script = bin_dir / f"{name}.py"
    script_relpath = _relpath(script) if script.is_relative_to(_ENGINE_ROOT) else str(script)
    if not script.is_file():
        return ServeVerdict(
            name=name,
            script_relpath=script_relpath,
            script_exists=False,
            has_main=False,
            main_arity_ok=False,
            findings=(),
        )
    try:
        source = script.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=script_relpath)
    except (UnicodeDecodeError, SyntaxError, ValueError) as exc:
        # Counted, not crashed -- an unparseable/non-UTF-8 file must show up
        # as its own bucket (`partition_report`'s `unparseable`) so "every
        # failure bucket 0" cannot be misread as "this file was examined and
        # found clean" when it was never examined at all.
        return ServeVerdict(
            name=name,
            script_relpath=script_relpath,
            script_exists=True,
            has_main=False,
            main_arity_ok=False,
            findings=(),
            parse_error=f"{type(exc).__name__}: {exc}",
        )
    main_fn = _main_def(tree)
    has_main = main_fn is not None
    arity_ok = has_main and _main_arity_ok(main_fn)  # type: ignore[arg-type]
    findings = tuple(find_module_body_violations(source, script_relpath))
    return ServeVerdict(
        name=name,
        script_relpath=script_relpath,
        script_exists=True,
        has_main=has_main,
        main_arity_ok=arity_ok,
        findings=findings,
    )


def classify_population(names: list[str], bin_dir: Path = _BIN_DIR) -> list[ServeVerdict]:
    """Classify every NAME in a named population (the allowlist's
    `entrypoints`, or any other explicit name list a caller supplies) --
    never a directory glob. See Anti-scope: "Do not re-derive a denominator
    from the six structural names" / "Do not quote a ratio without its
    population" -- this function's whole contract is that its input list IS
    the stated population."""
    return [classify_entrypoint(name, bin_dir) for name in names]


def load_allowlist_names(path: Path = _ALLOWLIST_PATH) -> list[str]:
    """The committed warm-load allowlist's `entrypoints` array -- the same
    file `coordinator_core.ops.invoke_from_argv` reads at import time."""
    data = json.loads(path.read_text(encoding="utf-8"))
    return list(data["entrypoints"])


def partition_report(verdicts: list[ServeVerdict]) -> dict:
    """The partition table the origin plan's Problem section hand-derived
    (and got wrong three times) -- now a function of `verdicts`, not a
    scratch script. Every bucket is mutually exclusive across
    {no_script, no_main, zero_arity_main, main_argv} and sums to `total`;
    `sys_path_mutation` / `non_stdlib_import` / `both_sys_path_and_non_
    stdlib` are independent overlays (a name can be `main_argv` AND carry a
    module-scope non-stdlib import -- servability and module-body purity
    are different properties, see `ServeVerdict.servable`'s docstring)."""
    total = len(verdicts)
    unparseable = sum(1 for v in verdicts if v.parse_error is not None)
    no_script = sum(1 for v in verdicts if not v.script_exists)
    no_main = sum(1 for v in verdicts if v.script_exists and v.parse_error is None and not v.has_main)
    zero_arity_main = sum(1 for v in verdicts if v.has_main and not v.main_arity_ok)
    main_argv = sum(1 for v in verdicts if v.has_main and v.main_arity_ok)
    cannot_serve = no_script + no_main + zero_arity_main + unparseable
    servable = sum(1 for v in verdicts if v.servable)
    servable_and_inert = sum(1 for v in verdicts if v.servable and v.inert)
    sys_path_mutation = sum(1 for v in verdicts if v.sys_path_mutation)
    non_stdlib_import = sum(1 for v in verdicts if v.non_stdlib_import)
    both = sum(1 for v in verdicts if v.sys_path_mutation and v.non_stdlib_import)
    already_inert = sum(1 for v in verdicts if v.inert)
    return {
        "total": total,
        "main_argv": main_argv,
        "zero_arity_main": zero_arity_main,
        "no_main": no_main,
        "no_script": no_script,
        "unparseable": unparseable,
        "cannot_serve": cannot_serve,
        "servable": servable,
        "servable_and_inert": servable_and_inert,
        "sys_path_mutation": sys_path_mutation,
        "non_stdlib_import": non_stdlib_import,
        "both_sys_path_and_non_stdlib": both,
        "already_inert": already_inert,
    }


def findings_for(verdicts: list[ServeVerdict]) -> list[Finding]:
    """Flatten every verdict's findings -- the detail behind
    `partition_report`'s counts, for a caller (e.g. the C8 guard, or a human
    reading `warm-serve-partition.py`'s output) that needs the actual lines,
    not just the tally."""
    out: list[Finding] = []
    for v in verdicts:
        out.extend(v.findings)
    return out
