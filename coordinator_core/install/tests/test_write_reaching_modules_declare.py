"""
coordinator_core.install.tests.test_write_reaching_modules_declare — the
mechanism half of the write-surface debt item.

Spec backlink: state/debt-backlog/2026-08-06-write-surface-declarations-must-live-wit-e49b9cfd8ad1.yaml
Evidence: state/audits/2026-08-06-install-substrate-write-surface-completeness.md
Inversion spec: docs/plans/2026-09-11-invert-the-write-detector-prove-read-only.md (C2)

Purpose: the census that produced the audit above was corrected twice by
hand and was still wrong both times, because the ``WRITE_SURFACE``
declaration lived in the caller while the writers it was supposed to cover
migrated out behind delegate calls (``write_path_entry_guard_blocks``,
``migrate_substrate_to_settings_home``, ``ensure_coordinator_venv``).
Patching the caller's clause count fixes today's gap, not the mechanism —
the next delegate extracted from any file in this package silently takes
its write surface with it, and nothing goes red.

This test is the mechanism. It derives, by walking the AST of every
top-level module in ``coordinator_core/install/`` (never hand-listing
writers — a hand list is exactly the census this debt item exists to
delete), which modules call a mutating filesystem/subprocess primitive,
then asserts every such module either exports a module-level
``WRITE_SURFACE`` or carries a named, reasoned entry in
``_ALLOWLIST`` below. An unreasoned allowlist entry is how this test would
quietly stop working, so every entry states why it is there.

History (five incremental resolution-shape fixes to the open/fdopen arm,
e9b7a2f9c and four commits after it, each closing one more argument shape
one at a time — the aliased-import gap, the O_* receiver-identity gap, the
mixed-flags-partial-resolution gap, and two more of the same kind):
every one of those fixes still decided write-vs-read by READING the
call's arguments, so the next unread shape was always a new gap. This
commit (the inversion, D1-D5 below) removes that property structurally
instead of patching it a sixth time: the open/fdopen arm no longer reads
arguments at all (D3) — everything flags, and only a site-keyed, proof-gated
exemption (D1/D2) can excuse a specific call.

``_ALLOWLIST`` holds two structurally different kinds of entry, both
requiring an inline reason:

  - **Legitimately exempt, permanently.** The module reaches a flagged
    primitive but the primitive never lands a write on the real machine
    (e.g. it writes only inside a ``tempfile.mkdtemp()`` sandbox that is
    ``rmtree``'d before return), or the flagged call is a false positive
    this predicate cannot structurally rule out (e.g. ``str.replace``,
    which shares its attribute name with ``Path.replace``/``os.replace``).
    An open/fdopen false positive is a DIFFERENT kind of false positive and
    never goes here — see ``_READ_ONLY_OPENS`` below (D1): a module-keyed
    ``_ALLOWLIST`` entry would hide every future write anywhere in that
    module, not just the one call that prompted it, so open-family false
    positives get a site-keyed, proof-gated entry there instead.
  - **Known gap.** The module genuinely writes to the machine and
    genuinely does not declare. Per this dispatch's scope, authoring any
    writer's ``WRITE_SURFACE`` is out-of-scope here — these are reported
    to the dispatching EM, not fixed in this change. Each such entry names
    what it writes so a future author closing the gap does not have to
    re-derive it.

D1 (``_READ_ONLY_OPENS``) — a separate, site-keyed exemption surface for
open/fdopen calls, gated by a total shape proof. An entry is keyed
``(module filename, enclosing function qualname, primitive)`` — the same
key shape as ``coordinator_core/tests/test_no_unbatched_per_item_git_spawn.py
:: _EXEMPT_SITES`` — and its value is a mandatory reason. A flagged call is
excused only when its site has an entry AND ``_proves_read_only`` holds for
the call itself; neither half excuses a call alone. It starts empty: no
module changes verdict at HEAD (2026-09-11 census). It is the place the
first false positive goes, not a backlog being moved.

D5 — slice A (a second write-detector to share a shared primitive with)
does not exist. The manifest scan that carried that defect
(``coordinator_core/ops/write_surface_manifest.py``) was fixed then later
deleted (kill ledger K-029/K-103) along with its cross-check test. What
survives, ``coordinator_core/install/write_surface_discovery.py``, detects
*declarations*, not *writes* — a different job. One divergence between it
and this file's own ``_declares_write_surface`` remains, recorded rather
than fixed: ``write_surface_discovery.py``'s version sees a
``WRITE_SURFACE`` bound inside a module-level ``if``/``try``, while this
test module's own ``_declares_write_surface`` scans ``tree.body`` only. So
a module that declares inside a platform branch reads as undeclared here
and this test goes red — the divergence fails LOUD, not silently.
Unifying the two implementations is a shared-primitive change and belongs
to its own baton, not this one.

Negative spec — what this test structurally CANNOT catch:
  - Resolution is by NAME, not by runtime BINDING. A module-level name
    reassigned after import (``os = SomeOtherModule``, or a monkeypatch of
    the module object itself rather than one of its attributes) defeats
    every alias-resolution check here, which key on the syntactic name a
    module-level/nested ``import`` statement bound
    (``_module_imported_as``). A bare ``open``/``fdopen`` re-exported
    through a cross-module indirection layer this single-module AST walk
    cannot trace is the same class of gap, one level up.
  - The mutating-primitive vocabulary (``_MUTATING_ATTRS``,
    ``_SUBPROCESS_NAMES``) is a CLOSED name list — a primitive outside it
    (``os.rename``, ``os.rmdir``, ``os.chmod``, ``os.write``,
    ``Path.touch``, ``Path.rename``: none of these flag today) is
    invisible. Since these never match the ``open``/``fdopen`` name in the
    first place, so are the file-creating constructors outside that name
    list: ``io.FileIO(p, "w")``, ``tempfile.mkstemp``/
    ``NamedTemporaryFile(delete=False)``, ``sqlite3.connect(p)``,
    ``zipfile.ZipFile(p, "w")``, ``logging.FileHandler(p)``.
  - Any write reached through dynamic dispatch (``getattr(obj, name)()``,
    a callback stored in a dict/registry) rather than a literal attribute
    or name call — the AST walk only recognizes ``obj.method(...)`` and
    ``bare_name(...)`` call shapes.
  - A write hidden inside a subprocess-invoked CLI (e.g. ``git clone``,
    ``brew install``) whose own on-disk effects are invisible to Python
    AST analysis of the *caller*. This predicate flags the subprocess
    *spawn* itself as a write-reaching signal, but cannot see what the
    spawned process actually touches.
  - A write reached through a delegate call whose callee lives in another
    package — this walk covers only ``coordinator_core/install/*.py``,
    one level, no cross-package call-graph trace.
  - Drift between a declared ``WRITE_SURFACE`` and what the module's code
    actually does at runtime — this test asserts *presence* of a
    declaration for a write-reaching module, never that the declaration's
    clauses match the code.
  - ``_is_str_replace_false_positive`` still decides
    ``os.replace``/``Path.replace`` by reading its two constant arguments
    — a real rename with two constant arguments (e.g.
    ``os.replace("tmp.json", "state.json")``) goes unflagged with no test
    failing. This keeps the arguments-decide property this file's own
    inversion otherwise rejects, scoped narrowly to ``.replace``; filed
    separately, not widened here.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import NamedTuple, Optional

_THIS_FILE = Path(__file__).resolve()
_INSTALL_DIR = _THIS_FILE.parent.parent

_MUTATING_ATTRS = {
    "write_text",
    "write_bytes",
    "copy",
    "copy2",
    "copyfile",
    "copytree",
    "move",
    "rmtree",
    "remove",
    "unlink",
    "makedirs",
    "mkdir",
    "symlink_to",
    "symlink",
}
"""Attribute names that, when called, land a real filesystem mutation on
some target path — regardless of receiver type (``os``, ``shutil``, or
``pathlib.Path``), since the AST alone cannot resolve which module an
attribute call's receiver belongs to."""

_SUBPROCESS_NAMES = {"run", "Popen", "call", "check_call", "check_output"}
"""A subprocess spawn is flagged too (per the debt item's proposed_action
and this module's own negative spec) — a write hidden behind a CLI
invocation is reached from exactly this call site, even though this
predicate cannot see what the spawned process does."""


def _is_str_replace_false_positive(call: ast.Call) -> bool:
    """``str.replace(old, new)`` shares its attribute name with
    ``Path.replace``/``os.replace`` (a real rename/move primitive). Both
    args to a ``str.replace`` call are near-universally string literals;
    both args to a real ``os.replace``/``Path.replace`` call are
    path-shaped values built at runtime, never string literals. See the
    module docstring's negative spec — this heuristic survives the
    inversion unwidened."""
    if len(call.args) != 2:
        return False
    return not any(isinstance(arg, ast.Constant) for arg in call.args)


_OS_OPEN_READ_FLAGS = {
    "O_RDONLY",
    "O_NONBLOCK",
    "O_NDELAY",
    "O_SYNC",
    "O_DSYNC",
    "O_RSYNC",
    "O_NOCTTY",
    "O_CLOEXEC",
    "O_NOFOLLOW",
    "O_BINARY",
    "O_TEXT",
}
"""Recognized ``os.O_*`` attributes P2 (below) trusts as read-only — the
CLOSED positive list; anything else is not a proof."""


def _subprocess_imported_names(tree: ast.Module) -> set[str]:
    """Bare names actually bound by a module-level ``from subprocess import
    ...`` (respecting any ``as`` alias) — the only way a bare-name
    subprocess call can be trusted to be a real spawn rather than a
    same-module function of that name."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _module_imported_as(tree: ast.Module, module_name: str) -> set[str]:
    """Bare local names bound to ``module_name`` by a module-level ``import
    module_name`` or ``import module_name as alias``, ANYWHERE in the tree
    (not ``tree.body`` only) — a module-level-only scan cannot see
    ``import os`` nested inside a function body or a platform branch, and a
    deferred import is exactly the shape the install package uses for
    platform-conditional work. Over-binding an alias can only cause a call
    to be flagged (fail-toward-flagging), never missed."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    names.add(alias.asname or alias.name)
    return names


def _from_import_bound_names(tree: ast.Module, target_names: frozenset[str]) -> set[str]:
    """Bare local names anywhere in the tree that a ``from <any module>
    import <name> [as alias]`` binds, for any name in ``target_names`` —
    the D3 alias-flagging rule keys on this."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in target_names:
                    names.add(alias.asname or alias.name)
    return names


def _bare_open_fdopen_names_rebound(tree: ast.Module) -> frozenset[str]:
    """Which of the bare names ``{"open", "fdopen"}`` get REBOUND to
    something other than the builtin by any ``from <module> import X [as
    Y]`` anywhere in the tree — ``from gzip import open`` binds the bare
    name ``open`` to ``gzip.open``, so a later bare ``open(p)`` call may not
    be the builtin at all. P1's bare-name shape never proves at a site
    whose module rebinds the name this way (D2)."""
    targets = frozenset({"open", "fdopen"})
    rebound: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound in targets:
                    rebound.add(bound)
    return frozenset(rebound)


# ---------------------------------------------------------------------------
# Qualname computation (D3's pin) — dotted `Class.method` for a method,
# `outer.inner` for a nested function (never `outer.<locals>.inner`),
# `"<module>"` for module level and for class bodies, and the innermost
# enclosing `def`'s dotted name for a lambda (a Lambda pushes no scope).
# ---------------------------------------------------------------------------


def _qualname_for_scope(scope: tuple[tuple[str, str], ...]) -> str:
    if not any(kind == "func" for kind, _name in scope):
        return "<module>"
    return ".".join(name for _kind, name in scope)


def _walk_calls(node: ast.AST, scope: tuple[tuple[str, str], ...], visit) -> None:
    """Recursive descent with the same coverage as ``ast.walk`` (every node
    reachable via ``ast.iter_child_nodes`` transitively, which is all of
    them), tracking the enclosing class/function scope stack so each
    ``ast.Call`` can be reported with its enclosing qualname."""
    if isinstance(node, ast.Call):
        visit(node, _qualname_for_scope(scope))
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        child_scope = scope + (("func", node.name),)
    elif isinstance(node, ast.ClassDef):
        child_scope = scope + (("class", node.name),)
    else:
        child_scope = scope
    for child in ast.iter_child_nodes(node):
        _walk_calls(child, child_scope, visit)


class _CallHit(NamedTuple):
    name: str
    lineno: int
    qualname: str
    node: ast.Call


def _flagged_calls(module_path: Path) -> list[_CallHit]:
    """Return every (primitive-name, lineno, enclosing qualname, call node)
    this module's AST reaches that looks like a mutating filesystem call, a
    subprocess spawn, or an open/fdopen call.

    D3: the open/fdopen arm below is blind to arguments and to receiver —
    it flags every call whose callee is named ``open``/``fdopen`` (bare, or
    an attribute on ANY receiver), plus every bare name a
    ``from <any module> import open|fdopen [as X]`` binds anywhere in the
    tree. Only ``_proves_read_only`` (the exemption pass) reads open-family
    arguments."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    subprocess_imported_names = _subprocess_imported_names(tree)
    open_alias_names = _from_import_bound_names(tree, frozenset({"open", "fdopen"}))
    hits: list[_CallHit] = []

    def visit(node: ast.Call, qualname: str) -> None:
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
            is_attribute_call = True
        elif isinstance(func, ast.Name):
            name = func.id
            is_attribute_call = False
        else:
            return
        if name == "replace":
            if _is_str_replace_false_positive(node):
                hits.append(_CallHit(name, node.lineno, qualname, node))
            return
        if name in ("open", "fdopen"):
            hits.append(_CallHit(name, node.lineno, qualname, node))
            return
        if not is_attribute_call and name in open_alias_names:
            hits.append(_CallHit(name, node.lineno, qualname, node))
            return
        if name in _SUBPROCESS_NAMES:
            if is_attribute_call or name in subprocess_imported_names:
                hits.append(_CallHit(name, node.lineno, qualname, node))
            return
        if name in _MUTATING_ATTRS:
            hits.append(_CallHit(name, node.lineno, qualname, node))

    _walk_calls(tree, (), visit)
    return hits


def _declares_write_surface(module_path: Path) -> bool:
    """True iff this module's AST contains a module-level assignment whose
    target is the bare name ``WRITE_SURFACE``."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "WRITE_SURFACE":
                    return True
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if node.target.id == "WRITE_SURFACE":
                return True
    return False


def _install_modules() -> list[Path]:
    """Every top-level ``.py`` module directly in ``coordinator_core/install/``
    — never its nested ``tests/`` package, and never a ``test_*.py`` sibling
    — and never ``__init__.py``."""
    return sorted(
        p
        for p in _INSTALL_DIR.glob("*.py")
        if p.name != "__init__.py" and not p.name.startswith("test_")
    )


# ---------------------------------------------------------------------------
# D2 — the proof shape. A closed positive list; anything else, including an
# unresolvable expression, is not a proof.
# ---------------------------------------------------------------------------


def _p1_mode_is_read_only(call: ast.Call, *, mode_index: int) -> bool:
    """P1's mode check: absent mode proves (default text-read); a mode that
    IS given but is not a non-empty string literal drawn only from
    ``{r, b, t}`` does not prove."""
    mode_arg: Optional[ast.expr] = None
    if len(call.args) > mode_index:
        mode_arg = call.args[mode_index]
    else:
        for kw in call.keywords:
            if kw.arg == "mode":
                mode_arg = kw.value
                break
    if mode_arg is None:
        return True
    if not (isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str)):
        return False
    if not mode_arg.value:
        return False
    return all(ch in "rbt" for ch in mode_arg.value)


def _p2_flags_are_read_only(call: ast.Call, os_names: frozenset[str]) -> bool:
    """P2's flags check: flags must be PRESENT and form a ``|``-chain in
    which every leaf is ``<os alias>.<name in _OS_OPEN_READ_FLAGS>``."""
    flags_arg: Optional[ast.expr] = None
    if len(call.args) >= 2:
        flags_arg = call.args[1]
    else:
        for kw in call.keywords:
            if kw.arg == "flags":
                flags_arg = kw.value
                break
    if flags_arg is None:
        return False

    def _all_read(node: ast.expr) -> bool:
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return _all_read(node.left) and _all_read(node.right)
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id not in os_names:
                return False
            return node.attr in _OS_OPEN_READ_FLAGS
        return False

    return _all_read(flags_arg)


def _proves_read_only(call: ast.Call, os_names: frozenset[str], io_names: frozenset[str]) -> bool:
    """A closed positive list (D2). A call proves read-only only if it has
    no ``*``-unpacked positional argument, no ``**``-unpacked keyword, no
    ``opener`` keyword and no eighth positional argument (the ``opener``
    slot), AND it matches P1 (a bare-name ``open``, ``<io alias>.open``, or
    ``<os alias>.fdopen`` whose mode is absent or a non-empty ``str``
    literal drawn only from ``{r, b, t}``) or P2 (``<os alias>.open`` whose
    flags are present and form a ``|``-chain of recognized ``os.O_*``
    read-only leaves off a real ``os`` alias). Everything else — a
    ``<receiver>.open`` on a receiver that is not a real ``os``/``io``
    import alias (``Path.open``, ``gzip.open``, ``codecs.open``, a user
    class's ``.open``), a from-import alias, or any unresolvable expression
    — is NOT a proof and ends here in ``return False``, the fail-toward-
    flagging default."""
    if any(isinstance(arg, ast.Starred) for arg in call.args):
        return False
    if any(kw.arg is None for kw in call.keywords):
        return False
    if any(kw.arg == "opener" for kw in call.keywords):
        return False
    if len(call.args) > 7:
        return False

    func = call.func
    if isinstance(func, ast.Name):
        if func.id in ("open", "fdopen"):
            return _p1_mode_is_read_only(call, mode_index=1)
        return False
    if isinstance(func, ast.Attribute):
        receiver_name = func.value.id if isinstance(func.value, ast.Name) else None
        if receiver_name is None:
            return False
        if func.attr == "open" and receiver_name in io_names:
            return _p1_mode_is_read_only(call, mode_index=1)
        if func.attr == "fdopen" and receiver_name in os_names:
            return _p1_mode_is_read_only(call, mode_index=1)
        if func.attr == "open" and receiver_name in os_names:
            return _p2_flags_are_read_only(call, os_names)
        return False
    return False


# ---------------------------------------------------------------------------
# D1 — the exemption surface and the module verdict pass, factored so the
# live tests call it with the real constants and the synthetic tests with
# injected ones.
# ---------------------------------------------------------------------------

_READ_ONLY_OPENS: dict[tuple[str, str, str], str] = {}
"""Site-keyed (module filename, enclosing function qualname, primitive
name) → reason. Empty at HEAD (census row 3) — the place the first
reasoned open/fdopen false positive goes, not a backlog being moved. See
the module docstring's D1 paragraph."""


def _open_family_hits_excused(
    module_path: Path,
    read_only_opens: dict[tuple[str, str, str], str],
) -> bool:
    """True iff every open/fdopen-named call ``_flagged_calls`` reaches in
    this module is excused: BOTH a ``_READ_ONLY_OPENS`` entry exists at its
    site AND ``_proves_read_only`` holds for the call itself (D1 — neither
    half excuses a call alone). One of the two non-test callers of
    ``_proves_read_only`` (the other is its stale-entry self-check,
    ``_read_only_opens_staleness`` — AC2 treats both as the exemption
    pass): a bare ``open``/``fdopen`` call whose bare
    name is rebound by some from-import anywhere in the module is rejected
    here, before ``_proves_read_only`` is even called — that fixed
    three-argument function cannot see a module-wide rebinding fact for
    itself (D2's P1 bare-name-rebinding rule)."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    os_names = frozenset(_module_imported_as(tree, "os"))
    io_names = frozenset(_module_imported_as(tree, "io"))
    bare_rebound = _bare_open_fdopen_names_rebound(tree)
    for hit in _flagged_calls(module_path):
        if hit.name not in ("open", "fdopen"):
            continue
        key = (module_path.name, hit.qualname, hit.name)
        if key not in read_only_opens:
            return False
        func = hit.node.func
        if isinstance(func, ast.Name) and func.id in bare_rebound:
            return False
        if not _proves_read_only(hit.node, os_names, io_names):
            return False
    return True


def _module_verdicts(
    modules: list[Path],
    read_only_opens: dict[tuple[str, str, str], str],
    allowlist: dict[str, str],
) -> list[str]:
    """D1's module-verdict pass. A module is unexplained iff it has a
    flagged call, does not declare ``WRITE_SURFACE``, is not in
    ``allowlist``, and its flagged calls are not ALL open/fdopen calls
    excused via ``read_only_opens`` — any non-open-family hit (subprocess,
    a mutating attribute, ``.replace``) can never be excused this way."""
    unexplained: list[str] = []
    for module_path in modules:
        hits = _flagged_calls(module_path)
        if not hits:
            continue
        if _declares_write_surface(module_path):
            continue
        if module_path.name in allowlist:
            continue
        if any(hit.name not in ("open", "fdopen") for hit in hits):
            unexplained.append(module_path.name)
            continue
        if not _open_family_hits_excused(module_path, read_only_opens):
            unexplained.append(module_path.name)
    return unexplained


def _read_only_opens_staleness(
    entries: dict[tuple[str, str, str], str],
    install_dir: Path,
    modules: list[Path],
    allowlist: Optional[dict[str, str]] = None,
) -> tuple[list[str], list[str]]:
    """Factored so the live test (real ``_READ_ONLY_OPENS``, ``_INSTALL_DIR``)
    and the synthetic tests (injected dict, ``tmp_path`` modules) share one
    implementation — mirrors ``_module_verdicts``'s factoring. Mirrors
    ``test_allowlist_has_no_stale_entries``'s shape, with D1's addition: an
    entry whose site now holds a call that FAILS the proof is reported as a
    claim of read-only over a call that is not provably a read, not merely
    as stale."""
    allowlist = allowlist or {}
    current_names = {p.name for p in modules}
    stale: list[str] = []
    disproven: list[str] = []
    for (module_name, qualname, primitive), _reason in entries.items():
        if module_name not in current_names:
            stale.append(f"{module_name} (module no longer exists)")
            continue
        module_path = install_dir / module_name
        hits = [
            hit
            for hit in _flagged_calls(module_path)
            if hit.qualname == qualname and hit.name == primitive
        ]
        if not hits:
            stale.append(f"{module_name}:{qualname}:{primitive} (site no longer flagged)")
            continue
        if _declares_write_surface(module_path):
            stale.append(f"{module_name}:{qualname}:{primitive} (module now declares WRITE_SURFACE)")
            continue
        if module_name in allowlist:
            stale.append(f"{module_name}:{qualname}:{primitive} (module now allowlisted)")
            continue
        tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
        os_names = frozenset(_module_imported_as(tree, "os"))
        io_names = frozenset(_module_imported_as(tree, "io"))
        bare_rebound = _bare_open_fdopen_names_rebound(tree)
        for hit in hits:
            func = hit.node.func
            if isinstance(func, ast.Name) and func.id in bare_rebound:
                disproven.append(f"{module_name}:{qualname}:{primitive} (bare name now rebound)")
                continue
            if not _proves_read_only(hit.node, os_names, io_names):
                disproven.append(f"{module_name}:{qualname}:{primitive} (call no longer proves read-only)")
    return stale, disproven


# ---------------------------------------------------------------------------
# Allowlist — every entry below is a module this walk flags as write-
# reaching that does NOT (yet, or ever) carry a WRITE_SURFACE. See the
# module docstring for the two kinds of entry this holds.
# ---------------------------------------------------------------------------

_ALLOWLIST: dict[str, str] = {
    # --- Legitimately exempt, permanently ---
    "_shared.py": (
        "EXEMPT: atomic_write() is a generic write MECHANIC parameterized "
        "entirely by a caller-supplied `target` — it carries no "
        "destination-provenance knowledge of its own (see its own "
        "docstring: 'this function is pure write MECHANICS only'). The "
        "writer is whichever caller invokes it and points it somewhere; "
        "that caller is where a WRITE_SURFACE clause belongs, mirroring "
        "how this module's own protocol module (write_surface.py) declares "
        "no clauses of its own either."
    ),
    "junction.py": (
        "EXEMPT: same class as `_shared.py` above. `create_junction` / "
        "`remove_junction` are link MECHANICS parameterized entirely by a "
        "caller-supplied `link` and `target` — the module selects no path "
        "of its own and knows no destination provenance. The writer is "
        "whichever caller points it somewhere, and that caller declares it: "
        "`fleet_env.WRITE_SURFACE` clauses[0] names the env_root junction "
        "retarget explicitly, including that it is performed via this "
        "module. Note `remove_junction`'s `os.rmdir` removes the reparse "
        "point ONLY and never the target tree, so it reaches no surface the "
        "caller has not already declared."
    ),
    "sandbox_check.py": (
        "EXEMPT: every flagged write (`os.makedirs`, `shutil.copy2`, "
        "`Path.write_text`, plus the sandboxed `subprocess.run` calls it "
        "drives) lands inside a `tempfile.mkdtemp(prefix='install-sandbox-"
        "check.')` sandbox directory that is `shutil.rmtree`'d before "
        "`main()` returns — this module validates install SHAPE against a "
        "throwaway tree, and never touches a real machine surface."
    ),
    "step_zero_emit.py": (
        "EXEMPT: the module docstring states plainly 'This module NEVER "
        "mutates the machine — pure string transforms only.' The one "
        "flagged call (`result.replace(target, replacement)` in "
        "json_escape()) is str.replace on runtime-derived (non-constant) "
        "strings, not Path.replace/os.replace — the exact false-positive "
        "class this predicate's own docstring says it cannot structurally "
        "rule out by attribute name alone."
    ),
    "manifest_reader.py": (
        "EXEMPT: every flagged `subprocess.run` call (`_probe_candidate`, "
        "`_resolve_py_launcher_candidate`) invokes a candidate interpreter "
        "with `-c '<version-check>'`/`-c 'import sys;print(sys.executable)'` "
        "purely to read its version/path back via stdout — a functional "
        "probe, never a write."
    ),
    "prereq_probe.py": (
        "EXEMPT: this module IS the Step Zero functional-prerequisite "
        "probe suite (per its own docstring) — every flagged "
        "`subprocess.run` call site (`git credential fill`, "
        "`git ls-remote --exit-code`, and siblings this test's walk does "
        "not need to re-enumerate) reads a probe result, never writes."
    ),
    "run_platform_localize.py": (
        "EXEMPT: `_run_schema_validation`'s one flagged `subprocess.run` "
        "call runs `.github/scripts/validate-json-schemas.py` and reads "
        "its captured stdout/stderr for a pass/fail signal — read-only "
        "validation, not a write."
    ),
    # "policy_gate.py" gravestone -- module deleted 2026-08-29
    # (docs/plans/2026-08-26-every-forwarder-that-can-reach-the-door-does.md
    # C12, DR-365: the `.ps1` leg it gated is deleted with it). Its
    # exemption entry is removed, not left stale, per
    # `test_allowlist_has_no_stale_entries` below.
    "coordinator_install_entry.py": (
        "EXEMPT: pure delegation. Its one flagged call spawns the installer "
        "declared in `docs/install/agent-install-manifest.json` "
        "(`standalone_setup_script`) and does nothing else — it resolves the "
        "script, appends the contract's own flags plus operator passthrough, "
        "and returns the child's exit code. Every write belongs to the "
        "declared installer, which is where the WRITE_SURFACE clause belongs; "
        "declaring the spawner's writes here would duplicate the child's "
        "surface at a site that cannot know what the child writes, and would "
        "go stale the moment the manifest repoints at a different installer."
    ),
    "path_resolution_report.py": (
        "EXEMPT: read-only probes. Both flagged spawns are PATH-resolution "
        "and exec-proof checks — `command -v <name>` under a login shell, "
        "then the entrypoint's own `_EXEC_PROOF_ARGS`, which are "
        "`--dump-op-timeouts` and `--help`: argument-parsing/report paths, "
        "chosen because they prove the entrypoint executes without mutating "
        "anything. Both capture their output and discard the child's stdout "
        "(`>/dev/null` in the POSIX payload, `capture_output=True` on the "
        "Windows arm). A report module that mutated the machine it is "
        "diagnosing would be the defect, not the declaration gap."
    ),
    "door_serving_census.py": (
        "EXEMPT: read-only. `build_census`/`render_census` never spawn a "
        "process (module docstring: 'never probes unless --probe is "
        "given') and derive every bucket from `Path.is_file()`/"
        "`Path.iterdir()` reads over settings-home, this repo's "
        "`coordinator/bin/`, and the resolved engine root's `coordinator/"
        "bin/`. The one flagged `subprocess.run` is `_probe`, an opt-in, "
        "single-name spawn of the ALREADY-INSTALLED door image with "
        "`--help`, capturing both streams and re-emitting them verbatim -- "
        "the same read-only exec-proof shape as `path_resolution_report.py`'s "
        "own `--help`/`--dump-op-timeouts` probes, never called by "
        "`build_census`/`main()`'s default path."
    ),
    # --- Known gaps: genuinely write-reaching, genuinely undeclared today.
    # Out of this dispatch's scope to fix (writer WRITE_SURFACE authorship
    # is explicitly out-of-scope) — reported to the dispatching EM instead.
    "host_sampler_scheduler.py": (
        "KNOWN GAP, taxonomy-blocked: the only Python-level write is a "
        "`tempfile.mkstemp()` XML file removed in the same `finally` block, "
        "which is exempt on its own. The real machine mutation is the "
        "Windows Task Scheduler entry `schtasks /Create` registers (and "
        "`schtasks /Delete` removes) — durable state outside the repo that "
        "SHOULD be declared, but no member of the frozen eight-kind "
        "`WRITE_SURFACE_KINDS` vocabulary describes an OS scheduler task, "
        "and that tuple is externally agreed with DoE (2026-08-06 "
        "acceptance memo). Closing this needs a ninth kind ratified "
        "cross-repo first, not a kind invented here; see "
        "`coordinator_core/install/write_surface.py`'s own note that a new "
        "kind is never invented locally without updating that tuple."
    ),
    "door_install.py": (
        "KNOWN GAP: genuinely write-reaching, genuinely undeclared. "
        "`install_door()` copies the prebuilt door exe (and its provenance "
        "sidecar) to a caller-supplied `bin_dst`, or falls back to "
        "`door_build.build()` writing there directly, then "
        "`_remove_shadowing_forwarder_siblings` unlinks any `.ps1`/`.cmd` "
        "siblings at that same destination. Same `file-path`-shaped, "
        "caller-supplied-destination clause as "
        "`clone_sibling_repo.WRITE_SURFACE`'s own template. Authoring "
        "writer `WRITE_SURFACE` declarations is out of this dispatch's "
        "scope — reported to the dispatching EM instead."
    ),
    "door_uninstall.py": (
        "KNOWN GAP: genuinely write-reaching, genuinely undeclared. "
        "`uninstall_door()` unlinks the door exe, its provenance sidecar, "
        "and its build sidecar at a caller-supplied `bin_dst`, then "
        "re-emits a fallback forwarder there via "
        "`_reemit_fallback_forwarder` -- `door_install.py`'s exact "
        "counterpart, same clause shape. Authoring writer `WRITE_SURFACE` "
        "declarations is out of this dispatch's scope — reported to the "
        "dispatching EM instead."
    ),
    "door_route_signal.py": (
        "KNOWN GAP: genuinely write-reaching, genuinely undeclared. The "
        "flagged `subprocess.run` spawns the installed door binary itself "
        "with a real op to observe which route (`WARM_SERVER`/`IN_PROCESS`) "
        "answered it -- unlike this file's read-only-probe siblings above, "
        "the spawned door can itself dispatch a real op with real side "
        "effects, so this is a genuine indirect write surface, not a false "
        "positive. Authoring writer `WRITE_SURFACE` declarations is out of "
        "this dispatch's scope — reported to the dispatching EM instead."
    ),
}


def test_every_write_reaching_module_declares_or_is_allowlisted() -> None:
    unexplained = _module_verdicts(_install_modules(), _READ_ONLY_OPENS, _ALLOWLIST)
    assert not unexplained, (
        "write-reaching module(s) with no WRITE_SURFACE and no allowlist "
        f"entry: {unexplained} — either author a WRITE_SURFACE on the "
        "module that performs the write, or add a reasoned _ALLOWLIST "
        "entry explaining why it does not need one (or, for an open/fdopen "
        "false positive, a reasoned _READ_ONLY_OPENS entry — see D1)."
    )


def test_allowlist_has_no_stale_entries() -> None:
    """Every _ALLOWLIST key must name a module that (a) still exists and
    (b) is still flagged as write-reaching and (c) still does not declare —
    otherwise the entry is dead weight silently masking nothing, the same
    failure mode as an unexplained entry."""
    current_names = {p.name for p in _install_modules()}
    stale: list[str] = []
    for name in _ALLOWLIST:
        module_path = _INSTALL_DIR / name
        if name not in current_names:
            stale.append(f"{name} (module no longer exists)")
            continue
        if not _flagged_calls(module_path):
            stale.append(f"{name} (no longer flagged as write-reaching)")
            continue
        if _declares_write_surface(module_path):
            stale.append(f"{name} (now declares WRITE_SURFACE — drop the allowlist entry)")

    assert not stale, f"stale _ALLOWLIST entries: {stale}"


def test_read_only_opens_entries_have_non_blank_reason() -> None:
    """AC4: every `_READ_ONLY_OPENS` entry carries a non-blank reason."""
    for key, reason in _READ_ONLY_OPENS.items():
        assert reason.strip(), f"_READ_ONLY_OPENS entry {key} has a blank reason"


def test_read_only_opens_has_no_stale_or_disproven_entries() -> None:
    stale, disproven = _read_only_opens_staleness(
        _READ_ONLY_OPENS, _INSTALL_DIR, _install_modules(), _ALLOWLIST
    )
    assert not stale, f"stale _READ_ONLY_OPENS entries: {stale}"
    assert not disproven, (
        "_READ_ONLY_OPENS entries claiming read-only over a call that is "
        f"NOT provably a read: {disproven}"
    )


def test_allowlist_has_not_grown_past_fourteen_entries() -> None:
    """AC4's ceiling (census row 2): `_ALLOWLIST` did not absorb open/fdopen
    false positives the inversion should instead route to
    `_READ_ONLY_OPENS`."""
    assert len(_ALLOWLIST) <= 14, (
        f"_ALLOWLIST grew to {len(_ALLOWLIST)} entries — an open/fdopen "
        "false positive belongs in _READ_ONLY_OPENS (site-keyed, proof-"
        "gated), not _ALLOWLIST. Report any growth as a PM-facing result."
    )


def test_at_least_five_modules_declare_write_surface() -> None:
    """Sanity floor, not a hardcoded expectation. This walk is scoped to
    `coordinator_core/install/` only, where five currently declare
    (`ensure_venv`, `scaffold_structure`, `shell_rc_guard`, `substrate`,
    `substrate_migrate`). This assertion only guards against the AST walk
    itself silently seeing nothing (e.g. a glob typo), never against the
    count changing as writers are added."""
    declaring = [p.name for p in _install_modules() if _declares_write_surface(p)]
    assert len(declaring) >= 5, (
        f"expected at least a handful of declaring modules, found only "
        f"{declaring!r} — the AST walk may be broken (e.g. wrong glob)"
    )


# ---------------------------------------------------------------------------
# AC2 — the structural guarantees: the deleted helpers stay deleted,
# `_proves_read_only` stays singular and narrowly referenced, and the
# `_flagged_calls` open/fdopen arm stays argument-blind.
# ---------------------------------------------------------------------------


def test_open_call_is_write_helpers_are_deleted() -> None:
    source = _THIS_FILE.read_text(encoding="utf-8")
    assert len(re.findall(r"^def _(?:os_)?open_call_is_write\(", source, re.MULTILINE)) == 0
    assert len(re.findall(r"^def _proves_read_only\(", source, re.MULTILINE)) == 1


def test_proves_read_only_referenced_only_by_exemption_pass_and_tests() -> None:
    source = _THIS_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_THIS_FILE))
    bad_sites: list[str] = []

    def visit(node: ast.Call, qualname: str) -> None:
        if isinstance(node.func, ast.Name) and node.func.id == "_proves_read_only":
            last = qualname.rsplit(".", 1)[-1]
            allowed = {"_open_family_hits_excused", "_read_only_opens_staleness"}
            if qualname not in allowed and not last.startswith("test_"):
                bad_sites.append(qualname)

    _walk_calls(tree, (), visit)
    assert not bad_sites, f"_proves_read_only called outside the exemption pass/tests: {bad_sites}"


def _find_open_fdopen_arm(func_def: ast.FunctionDef) -> ast.If:
    for node in ast.walk(func_def):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Compare):
            comparators = node.test.comparators
            if comparators and isinstance(comparators[0], (ast.Tuple, ast.Set)):
                values = {
                    elt.value
                    for elt in comparators[0].elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                }
                if {"open", "fdopen"} <= values:
                    return node
    raise AssertionError("could not locate the open/fdopen arm in _flagged_calls")


def test_flagged_calls_open_arm_is_argument_blind() -> None:
    """AC2's closer: a differently-named helper reading `.args`/`.keywords`
    from the open/fdopen arm of `_flagged_calls` would pass the two greps
    above while quietly recreating the arguments-decide property D3
    removes. Only `hits.append(...)` (a method call, not a bare-name
    function call) and the `_CallHit(...)` tuple constructor (data
    bookkeeping, not a read/write decision) may appear in this arm."""
    source = _THIS_FILE.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(_THIS_FILE))
    func_def = next(
        n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_flagged_calls"
    )
    arm = _find_open_fdopen_arm(func_def)
    reads_args = any(
        isinstance(n, ast.Attribute) and n.attr in ("args", "keywords") for n in ast.walk(arm)
    )
    bare_name_calls = [
        n
        for n in ast.walk(arm)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id != "_CallHit"
    ]
    assert not reads_args, "open/fdopen arm reads call .args/.keywords"
    assert not bare_name_calls, (
        f"open/fdopen arm calls a bare-name helper: {bare_name_calls} — D3 requires "
        "the only function reachable from that arm to be none"
    )


def test_write_surface_discovery_divergence_is_recorded() -> None:
    """AC6: D5 is written into the module docstring."""
    assert "write_surface_discovery" in __doc__


# ---------------------------------------------------------------------------
# AC1 — no open-family shape passes undeclared.
# ---------------------------------------------------------------------------

_AC1_CORPUS: dict[str, str] = {
    "open_star_args": "def f(*args):\n    return open(*args)\n",
    "open_double_star_kw": "def f(p, **kw):\n    return open(p, **kw)\n",
    "open_double_star_literal_dict": "def f(p):\n    return open(p, **{'mode': 'w'})\n",
    "builtins_open": "import builtins\ndef f():\n    return builtins.open('r.log', 'w')\n",
    "gzip_open": "import gzip\ndef f():\n    return gzip.open('out.gz', 'wb')\n",
    "io_open_renamed": "from io import open as o\ndef f(p):\n    return o(p, 'w')\n",
    "unresolved_receiver_kwargs": "def f(p, **kw):\n    return p.open(**kw)\n",
    "os_fdopen_renamed": "from os import fdopen\ndef f(fd):\n    return fdopen(fd, 'w')\n",
    "open_with_opener": "def f(p, make_writer):\n    return open(p, 'r', opener=make_writer)\n",
    "unresolved_receiver_no_args": "def f(p):\n    return p.open()\n",
    "codecs_open_write": "import codecs\ndef f():\n    return codecs.open('out.txt', 'w')\n",
}


def test_ac1_open_family_shapes_all_flag_as_undeclared(tmp_path: Path) -> None:
    """AC1: each of census row 4's nine shapes, plus a no-argument
    unresolved-receiver `.open()` and `codecs.open(..., 'w')`, must be
    reported undeclared by `_module_verdicts` — the same verdict function
    the live test uses. Fails against a revert of D3 (the pre-D3 detector
    read arguments and let every one of these through unflagged)."""
    modules: list[Path] = []
    for name, source in _AC1_CORPUS.items():
        module_path = tmp_path / f"{name}.py"
        module_path.write_text(source, encoding="utf-8")
        modules.append(module_path)
    unexplained = _module_verdicts(modules, read_only_opens={}, allowlist={})
    assert set(unexplained) == {p.name for p in modules}, (
        f"expected every AC1 corpus module undeclared, got: {unexplained}"
    )


# ---------------------------------------------------------------------------
# P1/P2 accept/reject pairs for `_proves_read_only`.
# ---------------------------------------------------------------------------


def _make_call(source: str) -> tuple[ast.Call, frozenset[str], frozenset[str]]:
    """Parse `source` (which may include leading `import os`/`import io`
    lines) and return its single Call node plus the os/io alias sets
    `_module_imported_as` derives from it — the production alias-resolution
    path, not a hand-constructed set."""
    tree = ast.parse(source)
    os_names = frozenset(_module_imported_as(tree, "os"))
    io_names = frozenset(_module_imported_as(tree, "io"))
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    return call, os_names, io_names


def test_proves_read_only_p1_bare_open_no_mode_accepts() -> None:
    call, os_names, io_names = _make_call("open(p)\n")
    assert _proves_read_only(call, os_names, io_names)


def test_proves_read_only_p1_bare_open_write_mode_rejects() -> None:
    call, os_names, io_names = _make_call("open(p, 'w')\n")
    assert not _proves_read_only(call, os_names, io_names)


def test_proves_read_only_p1_io_open_read_mode_accepts() -> None:
    call, os_names, io_names = _make_call("import io\nio.open(p, 'rb')\n")
    assert _proves_read_only(call, os_names, io_names)


def test_proves_read_only_p1_os_fdopen_read_mode_accepts() -> None:
    call, os_names, io_names = _make_call("import os\nos.fdopen(fd, 'r')\n")
    assert _proves_read_only(call, os_names, io_names)


def test_proves_read_only_p2_os_open_read_flags_accepts() -> None:
    call, os_names, io_names = _make_call("import os\nos.open(p, os.O_RDONLY)\n")
    assert _proves_read_only(call, os_names, io_names)


def test_proves_read_only_p2_os_open_mixed_flags_rejects() -> None:
    call, os_names, io_names = _make_call("import os\nos.open(p, os.O_RDONLY | os.O_CREAT)\n")
    assert not _proves_read_only(call, os_names, io_names)


def test_proves_read_only_p2_os_open_no_flags_rejects() -> None:
    """P2 requires flags to be PRESENT — a no-flags-argument `os.open` call
    never matches P2, and P1 does not cover `<os alias>.open` at all."""
    call, os_names, io_names = _make_call("import os\nos.open(p)\n")
    assert not _proves_read_only(call, os_names, io_names)


def test_proves_read_only_rejects_star_args() -> None:
    call, os_names, io_names = _make_call("open(*args)\n")
    assert not _proves_read_only(call, os_names, io_names)


def test_proves_read_only_rejects_double_star_kwargs() -> None:
    call, os_names, io_names = _make_call("open(p, **kw)\n")
    assert not _proves_read_only(call, os_names, io_names)


def test_proves_read_only_rejects_opener_keyword() -> None:
    call, os_names, io_names = _make_call("open(p, 'r', opener=make_writer)\n")
    assert not _proves_read_only(call, os_names, io_names)


def test_proves_read_only_rejects_eighth_positional_opener_slot() -> None:
    call, os_names, io_names = _make_call(
        "open(p, 'r', -1, True, None, None, None, make_writer)\n"
    )
    assert not _proves_read_only(call, os_names, io_names)


def test_proves_read_only_unresolved_receiver_never_proves() -> None:
    """`Path.open`, `gzip.open`, a user class's `.open` — all look the same
    to the AST as an `os`/`io` alias, and none of them prove (D2)."""
    call, os_names, io_names = _make_call("p.open('r')\n")
    assert not _proves_read_only(call, os_names, io_names)
    call, os_names, io_names = _make_call("import gzip\ngzip.open(p, 'rb')\n")
    assert not _proves_read_only(call, os_names, io_names)


def test_bare_open_rebound_by_from_import_is_detected() -> None:
    """D2's from-gzip-import reject case (`from gzip import open` + a bare
    `open(p)`): `_proves_read_only`'s fixed three-argument signature cannot
    see a module-wide rebinding fact for itself, so the rejection lives in
    `_open_family_hits_excused` (its own test below). This test exercises
    the detector `_open_family_hits_excused` relies on."""
    tree = ast.parse("from gzip import open\nopen(p)\n")
    assert "open" in _bare_open_fdopen_names_rebound(tree)


def test_open_family_hits_excused_requires_both_entry_and_proof(tmp_path: Path) -> None:
    write_module = tmp_path / "write_synthetic_module.py"
    write_module.write_text("def f(p):\n    return open(p, 'w')\n", encoding="utf-8")
    # Proof without a passing proof, and no entry: never excused.
    assert not _open_family_hits_excused(write_module, {})
    # Entry present, but the call does not pass `_proves_read_only`.
    write_key = (write_module.name, "f", "open")
    assert not _open_family_hits_excused(write_module, {write_key: "reasoned, but wrong"})

    read_module = tmp_path / "read_synthetic_module.py"
    read_module.write_text("def f(p):\n    return open(p)\n", encoding="utf-8")
    # Proof holds, but no entry: still flagged.
    assert not _open_family_hits_excused(read_module, {})
    # Both entry and proof: excused.
    read_key = (read_module.name, "f", "open")
    assert _open_family_hits_excused(read_module, {read_key: "reasoned and proven"})


def test_open_family_hits_excused_rejects_rebound_bare_name(tmp_path: Path) -> None:
    module_path = tmp_path / "rebound_synthetic_module.py"
    module_path.write_text(
        "from gzip import open\ndef f(p):\n    return open(p)\n", encoding="utf-8"
    )
    key = (module_path.name, "f", "open")
    assert not _open_family_hits_excused(module_path, {key: "reasoned, but the name is rebound"})


# ---------------------------------------------------------------------------
# D1 stale-check, synthetic coverage: missing module, missing site, a site
# that now fails the proof, a module that now declares, a module that is
# now allowlisted.
# ---------------------------------------------------------------------------


def test_read_only_opens_staleness_detects_missing_module(tmp_path: Path) -> None:
    entries = {("gone.py", "f", "open"): "reason"}
    stale, disproven = _read_only_opens_staleness(entries, tmp_path, [])
    assert any("no longer exists" in s for s in stale)
    assert not disproven


def test_read_only_opens_staleness_detects_missing_site(tmp_path: Path) -> None:
    module_path = tmp_path / "m.py"
    module_path.write_text("def f(p):\n    return open(p)\n", encoding="utf-8")
    entries = {(module_path.name, "g", "open"): "reason"}
    stale, disproven = _read_only_opens_staleness(entries, tmp_path, [module_path])
    assert any("site no longer flagged" in s for s in stale)
    assert not disproven


def test_read_only_opens_staleness_detects_disproven_site(tmp_path: Path) -> None:
    module_path = tmp_path / "m.py"
    module_path.write_text("def f(p):\n    return open(p, 'w')\n", encoding="utf-8")
    entries = {(module_path.name, "f", "open"): "used to be a read, isn't anymore"}
    stale, disproven = _read_only_opens_staleness(entries, tmp_path, [module_path])
    assert not stale
    assert any("no longer proves" in d for d in disproven)


def test_read_only_opens_staleness_detects_module_now_declares(tmp_path: Path) -> None:
    module_path = tmp_path / "m.py"
    module_path.write_text("WRITE_SURFACE = []\ndef f(p):\n    return open(p)\n", encoding="utf-8")
    entries = {(module_path.name, "f", "open"): "reason"}
    stale, disproven = _read_only_opens_staleness(entries, tmp_path, [module_path])
    assert any("now declares" in s for s in stale)
    assert not disproven


def test_read_only_opens_staleness_detects_module_now_allowlisted(tmp_path: Path) -> None:
    module_path = tmp_path / "m.py"
    module_path.write_text("def f(p):\n    return open(p)\n", encoding="utf-8")
    entries = {(module_path.name, "f", "open"): "reason"}
    stale, disproven = _read_only_opens_staleness(
        entries, tmp_path, [module_path], allowlist={module_path.name: "reason"}
    )
    assert any("allowlisted" in s for s in stale)
    assert not disproven


# ---------------------------------------------------------------------------
# Regression coverage for the P1 fix (review of e9b7a2f9c): the detector
# must see os.open/io.open attribute-call shapes, not just the builtin
# open() and os.fdopen() shapes it already handled. Each test writes a
# synthetic module to disk and runs the real `_flagged_calls` walk against
# it — a test that only exercised the proof helpers in isolation would not
# catch a regression in the receiver-resolution wiring inside
# `_flagged_calls` itself.
# ---------------------------------------------------------------------------


def _flagged_names(source: str, tmp_path: Path) -> set[str]:
    module_path = tmp_path / "synthetic_module.py"
    module_path.write_text(source, encoding="utf-8")
    return {hit.name for hit in _flagged_calls(module_path)}


def test_os_open_with_create_and_rdwr_flags_is_flagged(tmp_path: Path) -> None:
    """The exact shape e9b7a2f9c's own docstring names: a build-lock file
    opened with `os.O_CREAT | os.O_RDWR`."""
    source = (
        "import os\n"
        "fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)\n"
    )
    assert "open" in _flagged_names(source, tmp_path)


def test_os_open_aliased_import_is_still_flagged(tmp_path: Path) -> None:
    """`import os as _os` must not hide `_os.open(..., O_CREAT)` from the
    walk — this is the aliased-import gap the fix deliberately closes."""
    source = "import os as _os\nfd = _os.open(path, _os.O_CREAT | _os.O_WRONLY)\n"
    assert "open" in _flagged_names(source, tmp_path)


def test_os_open_nested_import_resolves_alias(tmp_path: Path) -> None:
    """D4 re-target: under D3 the deferred-import shape is now ALWAYS
    flagged (blind to arguments), so the old "is it flagged" question is
    moot. What survives is the alias-resolution point this test always
    made: the nested-import alias must resolve when checking the
    exemption's PROOF, or a read-only call gets stuck flagged with no way
    to excuse it. Same nested-scope-blindness class as chain review slice
    A's "the AST scan could not see a declaration inside a platform
    branch"."""
    deferred_source = (
        "def install():\n"
        "    import os as _os\n"
        "    fd = _os.open(path, _os.O_RDONLY)\n"
    )
    module_path = tmp_path / "synthetic_module.py"
    module_path.write_text(deferred_source, encoding="utf-8")

    hits = _flagged_calls(module_path)
    site_hit = next(hit for hit in hits if hit.name == "open")

    # Derived through the PRODUCTION path -- walks the WHOLE tree, so it
    # sees the deferred `import os as _os`.
    tree = ast.parse(deferred_source)
    os_names = frozenset(_module_imported_as(tree, "os"))
    io_names = frozenset(_module_imported_as(tree, "io"))
    assert _proves_read_only(site_hit.node, os_names, io_names)

    read_only_opens = {
        (module_path.name, site_hit.qualname, "open"): (
            "deferred os.open(O_RDONLY), resolves via the whole-tree walk"
        )
    }
    assert _module_verdicts([module_path], read_only_opens, {}) == []

    # tree.body-only twin: the regression this test exists to catch. A
    # `tree.body`-only scan cannot see the deferred `import os as _os` at
    # all, so `os_names` comes back empty and the alias never resolves --
    # the proof fails and the module STAYS flagged even with the same
    # entry present.
    body_only_os_names: frozenset[str] = frozenset(
        alias.asname or alias.name
        for node in ast.parse(deferred_source).body
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "os"
    )
    assert not _proves_read_only(site_hit.node, body_only_os_names, io_names)


def test_os_open_platform_branch_import_resolves_alias(tmp_path: Path) -> None:
    """Same point as the deferred-import twin above, for the platform-branch
    shape (`if sys.platform == 'win32': import os as _os`) — the other half
    of "a function or platform branch" this test's docstring always named."""
    platform_source = (
        "import sys\n"
        "if sys.platform == 'win32':\n"
        "    import os as _os\n"
        "    fd = _os.open(path, _os.O_RDONLY)\n"
    )
    module_path = tmp_path / "synthetic_module.py"
    module_path.write_text(platform_source, encoding="utf-8")

    hits = _flagged_calls(module_path)
    site_hit = next(hit for hit in hits if hit.name == "open")

    tree = ast.parse(platform_source)
    os_names = frozenset(_module_imported_as(tree, "os"))
    io_names = frozenset(_module_imported_as(tree, "io"))
    assert _proves_read_only(site_hit.node, os_names, io_names)

    read_only_opens = {
        (module_path.name, site_hit.qualname, "open"): (
            "platform-branch os.open(O_RDONLY), resolves via the whole-tree walk"
        )
    }
    assert _module_verdicts([module_path], read_only_opens, {}) == []

    body_only_os_names: frozenset[str] = frozenset(
        alias.asname or alias.name
        for node in ast.parse(platform_source).body
        if isinstance(node, ast.Import)
        for alias in node.names
        if alias.name == "os"
    )
    assert not _proves_read_only(site_hit.node, body_only_os_names, io_names)


def test_os_open_distinguishes_read_from_write_flags(tmp_path: Path) -> None:
    """D4 re-target: under D3 both shapes are now flagged (blind), so the
    old read-not-flagged / write-flagged asymmetry is gone — that asymmetry
    was the exact defect D3 removes. What replaces it: the PROOF layer
    still discriminates read from write, and this test moves the same
    discrimination to `_proves_read_only`."""
    read_source = "import os\nfd = os.open(path, os.O_RDONLY)\n"
    write_source = "import os\nfd = os.open(path, os.O_WRONLY)\n"
    assert "open" in _flagged_names(read_source, tmp_path)
    assert "open" in _flagged_names(write_source, tmp_path)

    read_call, os_names, io_names = _make_call(read_source)
    write_call, _os_names2, _io_names2 = _make_call(write_source)
    assert _proves_read_only(read_call, os_names, io_names)
    assert not _proves_read_only(write_call, os_names, io_names)


def test_os_open_unresolvable_flags_fails_toward_flagging(tmp_path: Path) -> None:
    """Flags built from a variable this walk cannot resolve statically must
    fail TOWARD flagging."""
    source = "import os\nfd = os.open(path, flags_from_elsewhere)\n"
    assert "open" in _flagged_names(source, tmp_path)


def test_os_open_mixed_known_read_flag_and_unresolved_term_is_flagged(
    tmp_path: Path,
) -> None:
    """Review follow-up (code-reviewer, P1): a flags expression combining
    ONE recognized read-only attribute (`os.O_RDONLY`) with ONE
    unresolvable term must still be flagged."""
    source = (
        "import os\n"
        "fd = os.open(path, os.O_RDONLY | extra_flags_from_elsewhere)\n"
    )
    assert "open" in _flagged_names(source, tmp_path)


def test_io_open_write_mode_is_flagged(tmp_path: Path) -> None:
    source = 'import io\nf = io.open(path, "w")\n'
    assert "open" in _flagged_names(source, tmp_path)


def test_io_open_distinguishes_read_from_write_mode(tmp_path: Path) -> None:
    """D4 re-target, same shape as the os.open twin above: D3 flags both;
    `_proves_read_only` still discriminates."""
    read_source = 'import io\nf = io.open(path, "r")\n'
    write_source = 'import io\nf = io.open(path, "w")\n'
    assert "open" in _flagged_names(read_source, tmp_path)
    assert "open" in _flagged_names(write_source, tmp_path)

    read_call, os_names, io_names = _make_call(read_source)
    write_call, _os_names2, _io_names2 = _make_call(write_source)
    assert _proves_read_only(read_call, os_names, io_names)
    assert not _proves_read_only(write_call, os_names, io_names)


def test_unresolved_receiver_open_write_mode_is_flagged(tmp_path: Path) -> None:
    """`Path(...).open("w")` (and any other unresolved `<receiver>.open`)
    still flags — D3 flags every attribute call named `open`/`fdopen`
    regardless of receiver."""
    source = 'p = Path(x)\nf = p.open("w")\n'
    assert "open" in _flagged_names(source, tmp_path)


def test_bare_builtin_open_still_flagged_unaffected_by_the_fix(tmp_path: Path) -> None:
    """Pre-existing bare `open(path, "w")` coverage must survive unchanged."""
    source = 'f = open(path, "w")\n'
    assert "open" in _flagged_names(source, tmp_path)


def test_os_open_non_os_receiver_read_flag_shaped_attr_is_flagged(tmp_path: Path) -> None:
    """An `O_RDONLY`-shaped attribute reached through a receiver that is NOT
    a real `os` import alias still flags — D3 flags the call blind to its
    arguments regardless."""
    source = (
        "import os\n"
        "fd = os.open(path, Foo.O_RDONLY, 0o644)\n"
    )
    assert "open" in _flagged_names(source, tmp_path)


def test_os_fdopen_still_flagged_unaffected_by_the_fix(tmp_path: Path) -> None:
    """Pre-existing `os.fdopen(fd, "w")` coverage must survive unchanged."""
    source = 'import os\nf = os.fdopen(fd, "w")\n'
    assert "fdopen" in _flagged_names(source, tmp_path)
