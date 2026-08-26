"""
coordinator_core.install.tests.test_write_reaching_modules_declare — the
mechanism half of the write-surface debt item.

Spec backlink: state/debt-backlog/2026-08-06-write-surface-declarations-must-live-wit-e49b9cfd8ad1.yaml
Evidence: state/audits/2026-08-06-install-substrate-write-surface-completeness.md

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

``_ALLOWLIST`` holds two structurally different kinds of entry, both
requiring an inline reason:

  - **Legitimately exempt, permanently.** The module reaches a flagged
    primitive but the primitive never lands a write on the real machine
    (e.g. it writes only inside a ``tempfile.mkdtemp()`` sandbox that is
    ``rmtree``'d before return), or the flagged call is a false positive
    this predicate cannot structurally rule out (e.g. ``str.replace``,
    which shares its attribute name with ``Path.replace``/``os.replace``).
  - **Known gap.** The module genuinely writes to the machine and
    genuinely does not declare. Per this dispatch's scope, authoring any
    writer's ``WRITE_SURFACE`` is out-of-scope here — these are reported
    to the dispatching EM, not fixed in this change. Each such entry names
    what it writes so a future author closing the gap does not have to
    re-derive it.

Review follow-up (code-reviewer, e9b7a2f9c chain review slice E, P1): the
original open/fdopen detector could not see ``os.open(...)``/``io.open(...)``
attribute-call shapes, including the ``O_CREAT | O_RDWR`` build-lock write
that commit's own docstring named as newly-declared. ``_os_open_call_is_write``,
``_module_imported_as``, and the receiver-resolution in ``_flagged_calls``
close that gap; aliased ``import os as _os`` is resolved, but
``from os import open as X`` and cross-module re-export/indirection are not
(see the negative spec immediately below).

Review follow-up (code-reviewer, this commit, P1, second pass): the
``O_*``-flag attribute leaf inside ``_os_open_call_is_write`` previously
trusted an attribute NAME (``O_RDONLY``, ``O_CREAT``, ...) regardless of
which receiver it hung off — ``Foo.O_RDONLY`` (a class attribute that is
actually a write bitmask at runtime) or a monkeypatched ``os.O_RDONLY``
both resolved as "not a write" purely because the tail name matched a
known read-only flag. The leaf now also requires the receiver to be one
of this module's actual ``os`` import aliases (the same set
``_module_imported_as`` already computes) before trusting the attribute
name at all; any other receiver is treated as unresolved and fails toward
flagging.

Negative spec — what this test structurally CANNOT catch:
  - A write reached through a delegate call whose callee lives in another
    package (e.g. a call into ``coordinator_core.machine_local`` or a
    sibling top-level package) — this walk covers only
    ``coordinator_core/install/*.py``, one level, no cross-package
    call-graph trace.
  - A write hidden inside a subprocess-invoked CLI (e.g. ``git clone``,
    ``brew install``) whose own on-disk effects are invisible to Python
    AST analysis of the *caller*. This predicate flags the subprocess
    *spawn* itself as a write-reaching signal (a real spawn call site is
    exactly where such a hidden write would be reached from), but cannot
    see what the spawned process actually touches.
  - Any write reached through dynamic dispatch (``getattr(obj, name)()``,
    a callback stored in a dict/registry) rather than a literal attribute
    or name call — the AST walk only recognizes ``obj.method(...)`` and
    ``bare_name(...)`` call shapes.
  - Drift between a declared ``WRITE_SURFACE`` and what the module's code
    actually does at runtime — this test asserts *presence* of a
    declaration for a write-reaching module, never that the declaration's
    clauses match the code (that is a lockstep/drift check, explicitly out
    of ``write_surface.py``'s own remit per its negative spec).
  - A flag/mode value whose receiver NAME is a real ``os`` import alias
    but whose runtime BINDING has been changed after import (e.g.
    ``os = SomeOtherModule`` reassigning the module-level name itself, or
    a monkeypatch of the *module object* rather than one of its
    attributes) — this walk resolves receiver identity by matching the
    syntactic name against the set of names a module-level/nested
    ``import os`` statement bound, per ``_module_imported_as``. It cannot
    see, and structurally cannot see without executing the module, a
    later rebinding of that same name to a different runtime object. This
    is the residual instance of "resolves by name, not by binding" that
    the receiver-identity fix (this commit, P1 second pass) narrows but
    cannot fully close — closing it requires runtime execution, not
    static AST analysis.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Optional

_INSTALL_DIR = Path(__file__).resolve().parent.parent

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
    args to a ``str.replace`` call are near-universally string literals
    (the substring being replaced and its replacement); both args to a
    real ``os.replace``/``Path.replace`` call are path-shaped values built
    at runtime, never string literals. This heuristic is not a type
    checker — see the module docstring's negative spec — but it is enough
    to keep ``.replace("\\\\", "/")``-style text transforms (confirmed by
    hand for every module this test currently scans) out of the flagged
    set without silently dropping real ``os.replace``/``Path.replace``
    call sites, which pass real (non-constant) path values as both args."""
    if len(call.args) != 2:
        return False
    return not any(isinstance(arg, ast.Constant) for arg in call.args)


def _open_call_is_write(call: ast.Call, *, mode_index: int = 1) -> bool:
    """``open(path, mode)`` / ``os.fdopen(fd, mode)`` / ``io.open(path, mode)``
    (``mode_index=1``, the default: a *module-level* function/attribute call
    where the receiver is not the value being opened) — or a bound instance
    method call like ``Path(...).open(mode)`` / ``some_file_obj.open(mode)``
    (``mode_index=0``: the receiver IS the thing being opened, so ``mode`` is
    the first positional arg, not the second). Determine whether the mode
    argument (at ``mode_index``, or a ``mode=`` keyword) requests a write. No
    mode given at all defaults to text-read (``"r"``), not a write. A mode
    that IS given but is not a string literal (a variable or an expression)
    is unknowable statically and must fail TOWARD flagging, not away from
    it — silently treating an unresolvable mode as read-only would recreate
    exactly the invisibility this predicate exists to close."""
    mode_arg: Optional[ast.expr] = None
    if len(call.args) > mode_index:
        mode_arg = call.args[mode_index]
    else:
        for kw in call.keywords:
            if kw.arg == "mode":
                mode_arg = kw.value
                break
    if mode_arg is None:
        return False
    if isinstance(mode_arg, ast.Constant) and isinstance(mode_arg.value, str):
        return any(ch in mode_arg.value for ch in "wax+")
    return True


_OS_OPEN_WRITE_FLAGS = {"O_WRONLY", "O_RDWR", "O_APPEND", "O_CREAT", "O_TRUNC", "O_EXCL"}

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
"""Recognized ``os.O_*`` attributes this predicate knows are NOT write
flags — an attribute name outside both this set and
``_OS_OPEN_WRITE_FLAGS`` is treated as unresolved (fails toward flagging),
not silently assumed read-only."""


def _os_open_call_is_write(call: ast.Call, os_names: frozenset[str]) -> bool:
    """``os.open(path, flags, mode=0o777)`` — unlike builtin/``io.open``,
    ``flags`` is an int bitmask built from ``os.O_*`` constants (e.g.
    ``os.O_CREAT | os.O_RDWR``), never a mode string, so
    ``_open_call_is_write``'s string-mode logic does not apply here. Any
    ``O_WRONLY``/``O_RDWR``/``O_APPEND``/``O_CREAT``/``O_TRUNC``/``O_EXCL``
    attribute reached anywhere inside the flags expression (a single
    attribute, or a ``|``-chained ``BinOp`` tree of several) signals write
    intent. Flags built entirely from RECOGNIZED read-only bits
    (``O_RDONLY``, ``O_NONBLOCK``, ... — see ``_OS_OPEN_READ_FLAGS``) are
    the only shape that resolves to "not a write". Anything else — a flags
    value this walk cannot resolve at all (a variable, a call result, no
    flags argument found), an ``os.O_*``-shaped attribute name outside both
    known sets, an ``O_*``-shaped attribute whose RECEIVER is not one of
    this module's actual ``os`` import aliases (``os_names``), OR a
    known-read-flag ORed together with any such unresolved term — must fail
    TOWARD flagging, same philosophy as ``_open_call_is_write``.
    Review follow-up (code-reviewer, this commit, P1): a *mixed* expression
    like ``os.O_RDONLY | extra_flags_from_elsewhere`` previously resolved
    to "not a write" because the walk only tracked "found any recognized
    attribute" rather than "the whole expression resolved" — it now
    requires every leaf of the ``|``-chain to be a recognized read flag
    before treating the expression as read-only.
    Review follow-up (code-reviewer, this commit, P1, second pass): the
    attribute leaf previously trusted an ``O_RDONLY``/``O_CREAT``-shaped
    attribute NAME regardless of receiver — ``Foo.O_RDONLY`` (a class
    attribute actually holding a write bitmask, or a monkeypatched
    ``os.O_RDONLY``) resolved as "not a write" purely because the tail name
    matched. The leaf now also requires the receiver name to be one of
    ``os_names`` — the set of names this module's ``import os`` statements
    actually bind (see ``_module_imported_as``) — before trusting the
    attribute name at all; any other receiver is unresolved, per the
    fail-toward-flagging contract."""
    flags_arg: Optional[ast.expr] = None
    if len(call.args) >= 2:
        flags_arg = call.args[1]
    else:
        for kw in call.keywords:
            if kw.arg == "flags":
                flags_arg = kw.value
                break
    if flags_arg is None:
        return True
    found_write_flag = False
    fully_resolved = True

    def _visit(node: ast.expr) -> None:
        nonlocal found_write_flag, fully_resolved
        # `|`-chaining is the only scaffolding this walk descends
        # through; every other node shape is a leaf, resolved or not.
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            _visit(node.left)
            _visit(node.right)
            return
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if node.value.id not in os_names:
                # An O_*-shaped attribute name reached through a receiver
                # that is not one of this module's actual `os` import
                # aliases (e.g. `Foo.O_RDONLY`, or `os` reassigned to
                # something else at runtime) — the tail name alone proves
                # nothing about the value it actually binds to. Review
                # follow-up (code-reviewer, this commit, P1, second pass).
                fully_resolved = False
                return
            if node.attr in _OS_OPEN_WRITE_FLAGS:
                found_write_flag = True
            elif node.attr not in _OS_OPEN_READ_FLAGS:
                # A recognized os.O_* attribute *shape* whose specific
                # name this walk does not know at all — treat exactly
                # like an unresolved leaf, not like a resolved one.
                fully_resolved = False
            return
        # Anything else (a bare Name, a Call result, a subscript, an
        # Attribute on a non-`os` receiver, ...) is unresolvable at
        # analysis time, regardless of what else in the same
        # `|`-chain WAS resolved. Review follow-up (code-reviewer,
        # this commit, P1): a *partially* recognized flags expression
        # must not short-circuit to "not a write" just because some
        # sibling term happened to resolve to a read-only flag.
        fully_resolved = False

    _visit(flags_arg)
    if found_write_flag:
        return True
    return not fully_resolved


def _subprocess_imported_names(tree: ast.Module) -> set[str]:
    """Bare names actually bound by a module-level ``from subprocess import
    ...`` (respecting any ``as`` alias) — cheap to derive from the same tree
    this walk already parses, and the only way a bare-name subprocess call
    can be trusted to be a real spawn rather than a same-module function of
    that name (see the false-positive rationale below)."""
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == "subprocess":
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _module_imported_as(tree: ast.Module, module_name: str) -> set[str]:
    """Bare local names bound to ``module_name`` by a module-level ``import
    module_name`` or ``import module_name as alias`` — used to resolve an
    ``<name>.open(...)`` attribute call back to ``os``/``io`` regardless of
    aliasing (e.g. ``import os as _os`` → ``_os.open(...)`` must still be
    recognized as ``os.open``). Deliberately does NOT chase
    ``from os import open as X`` (``os.open`` has no such realistic alias
    form in practice) or re-export/indirection through a third module —
    both are structurally out of reach of a single-module AST walk, per
    this file's own negative spec.

    Walks the WHOLE tree, not ``tree.body``: a module-level-only scan cannot
    see ``import os`` nested inside a function body or a platform branch
    (``if sys.platform == "win32": import os as _os``), and a deferred import
    is exactly the shape the install package uses for platform-conditional
    work. That nested-scope blindness is this workstream's recurring defect —
    the same shape as chain review slice A's "the AST scan could not see a
    declaration inside a platform branch". Binding an alias anywhere in the
    module is treated as binding it for the module: over-resolving an alias
    can only cause a call to be FLAGGED (fail-toward-flagging, the safe
    direction), never to be missed."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == module_name:
                    names.add(alias.asname or alias.name)
    return names


def _flagged_calls(module_path: Path) -> list[tuple[str, int]]:
    """Return every (primitive-name, lineno) this module's AST reaches that
    looks like a mutating filesystem call or a subprocess spawn."""
    tree = ast.parse(module_path.read_text(encoding="utf-8"), filename=str(module_path))
    subprocess_imported_names = _subprocess_imported_names(tree)
    os_imported_names = _module_imported_as(tree, "os")
    io_imported_names = _module_imported_as(tree, "io")
    hits: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            name = func.attr
            is_attribute_call = True
            receiver_name = func.value.id if isinstance(func.value, ast.Name) else None
        elif isinstance(func, ast.Name):
            name = func.id
            is_attribute_call = False
            receiver_name = None
        else:
            continue
        if name == "replace":
            if _is_str_replace_false_positive(node):
                hits.append((name, node.lineno))
            continue
        if name == "open":
            # Three shapes, three call conventions:
            #   - `os.open(path, flags, mode=...)` — flags is an int
            #     bitmask, not a mode string; needs its own predicate.
            #     Resolved via the module's actual `os` import alias(es).
            #   - Bare `open(path, mode)` / `io.open(path, mode)` — a
            #     module-level function call; `mode` is the SECOND
            #     positional arg (index 1).
            #   - An unresolved `<receiver>.open(mode)` (e.g.
            #     `Path(...).open("w")`, `some_file_obj.open("w")`) — a
            #     bound instance method; the receiver IS the thing being
            #     opened, so `mode` is the FIRST positional arg (index 0).
            #     This walk cannot structurally distinguish `Path.open`
            #     from any other instance's `.open` by attribute name
            #     alone, so any such call is checked generically.
            if is_attribute_call and receiver_name in os_imported_names:
                if _os_open_call_is_write(node, frozenset(os_imported_names)):
                    hits.append((name, node.lineno))
            elif not is_attribute_call or receiver_name in io_imported_names:
                if _open_call_is_write(node, mode_index=1):
                    hits.append((name, node.lineno))
            elif _open_call_is_write(node, mode_index=0):
                hits.append((name, node.lineno))
            continue
        if name == "fdopen" and is_attribute_call:
            if _open_call_is_write(node):
                hits.append((name, node.lineno))
            continue
        if name in _SUBPROCESS_NAMES:
            # Only `something.run(...)` (e.g. `subprocess.run`, a local
            # `_run` *wrapper* is still an attribute call at its own
            # `subprocess.run(...)` call site, which this walk also visits
            # separately) OR a bare name actually bound by a module-level
            # `from subprocess import ...` (tracked above). A bare `run(...)`
            # name call that is NOT one of those imported names is far more
            # often a same-module function of that name (confirmed by hand:
            # see check_install_singularity.py's own probe entry point) than
            # a subprocess spawn.
            if is_attribute_call or name in subprocess_imported_names:
                hits.append((name, node.lineno))
            continue
        if name in _MUTATING_ATTRS:
            hits.append((name, node.lineno))
    return hits


def _declares_write_surface(module_path: Path) -> bool:
    """True iff this module's AST contains a module-level assignment whose
    target is the bare name ``WRITE_SURFACE`` (mirrors how every existing
    declaring module — ``ensure_venv``, ``scaffold_structure``,
    ``shell_rc_guard``, ``substrate``, ``substrate_migrate`` — spells it)."""
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
    (both are test code, structurally exempt, not part of the writer set) —
    and never ``__init__.py`` (package marker, not a writer)."""
    return sorted(
        p
        for p in _INSTALL_DIR.glob("*.py")
        if p.name != "__init__.py" and not p.name.startswith("test_")
    )


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
    "policy_gate.py": (
        "EXEMPT: the one flagged call (`subprocess.run` in `_default_probe`) "
        "runs `_PROBE_COMMAND`, the literal string `Get-ExecutionPolicy`, "
        "with `-NoProfile -NonInteractive` and the inherited "
        "`PSExecutionPolicyPreference` popped from the child env — a "
        "read-only effective-policy query, never a mutation. The module "
        "docstring states plainly it 'is pure verdict computation — it "
        "probes and reports. It does not emit launchers, roll back "
        "anything, or mutate the machine in any way: no "
        "`Set-ExecutionPolicy`, no `Unblock-File`,' per the C7 ruling "
        "(`wont_do`, pm_approved) putting remediation out of scope for "
        "this module."
    ),
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
    unexplained: list[str] = []
    for module_path in _install_modules():
        if not _flagged_calls(module_path):
            continue
        if _declares_write_surface(module_path):
            continue
        if module_path.name in _ALLOWLIST:
            continue
        unexplained.append(module_path.name)

    assert not unexplained, (
        "write-reaching module(s) with no WRITE_SURFACE and no allowlist "
        f"entry: {unexplained} — either author a WRITE_SURFACE on the "
        "module that performs the write, or add a reasoned _ALLOWLIST "
        "entry explaining why it does not need one."
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


def test_at_least_five_modules_declare_write_surface() -> None:
    """Sanity floor, not a hardcoded expectation. The dispatch brief cites
    twelve WRITE_SURFACE-declaring modules repo-wide (`coordinator_core/`
    at large — includes `ops/configure_git.py` and five siblings outside
    this package); this walk is scoped to `coordinator_core/install/`
    only (per the debt item's own `surface:` field), where five currently
    declare (`ensure_venv`, `scaffold_structure`, `shell_rc_guard`,
    `substrate`, `substrate_migrate`). This assertion only guards against
    the AST walk itself silently seeing nothing (e.g. a glob typo), never
    against the count changing as writers are added."""
    declaring = [p.name for p in _install_modules() if _declares_write_surface(p)]
    assert len(declaring) >= 5, (
        f"expected at least a handful of declaring modules, found only "
        f"{declaring!r} — the AST walk may be broken (e.g. wrong glob)"
    )


# ---------------------------------------------------------------------------
# Regression coverage for the P1 fix (review of e9b7a2f9c): the detector must
# see os.open/io.open attribute-call shapes, not just the builtin open() and
# os.fdopen() shapes it already handled. Each test writes a synthetic module
# to disk and runs the real `_flagged_calls` walk against it — a test that
# only exercised `_os_open_call_is_write`/`_open_call_is_write` in isolation
# would not catch a regression in the receiver-resolution wiring inside
# `_flagged_calls` itself.
# ---------------------------------------------------------------------------


def _flagged_names(source: str, tmp_path: Path) -> set[str]:
    module_path = tmp_path / "synthetic_module.py"
    module_path.write_text(source, encoding="utf-8")
    return {name for name, _lineno in _flagged_calls(module_path)}


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
    """An `import os` that is not at module level — deferred into a function
    body, or sitting inside a platform branch — must still resolve the alias
    for `<name>.open(...)`.

    A `tree.body`-only scan sees neither shape, and a platform-conditional
    deferred import is exactly how this package does platform-specific work.
    This is the same nested-scope blindness as chain review slice A's "the AST
    scan could not see a declaration inside a platform branch", recurring one
    layer down in the detector that exists to catch it.

    The READ-ONLY shape is what makes this test non-vacuous, and the write
    shape deliberately is not asserted. With the alias unresolved, an
    `_os.open(...)` call does not fall out of the walk — it falls through to
    the generic unresolved-receiver `<recv>.open(mode)` branch, which reads
    arg 0 as a mode string, fails to resolve it, and flags. So the WRITE case
    is flagged either way, for the wrong reason, and asserting it would pass
    against a `tree.body` revert — vacuous.

    Read-only discriminates: resolved, `os.O_RDONLY` is correctly not a
    write; unresolved, the same generic fallback flags it as a false
    positive. A false positive is the safe direction but still wrong — it
    forces a spurious WRITE_SURFACE declaration or an allowlist entry on a
    module that writes nothing, and allowlist pressure is how this test
    quietly stops meaning anything."""
    deferred = (
        "def install():\n"
        "    import os as _os\n"
        "    fd = _os.open(path, _os.O_RDONLY)\n"
    )
    platform_branch = (
        "import sys\n"
        "if sys.platform == 'win32':\n"
        "    import os as _os\n"
        "    fd = _os.open(path, _os.O_RDONLY)\n"
    )
    assert "open" not in _flagged_names(deferred, tmp_path)
    assert "open" not in _flagged_names(platform_branch, tmp_path)


def test_os_open_distinguishes_read_from_write_flags(tmp_path: Path) -> None:
    """A pure-read `os.open(path, os.O_RDONLY)` must not be flagged, in
    contrast to the write-flags shape covered above — the detector
    distinguishes write-intent flags, it does not blanket-flag every
    `os.open` call.

    A read-only assertion alone (`"open" not in _flagged_names(...)`) is
    vacuous under a full revert of this commit: pre-fix, `os.open(...)`
    attribute-call detection did not exist at all, so `os.open` would also
    be absent from the flagged set on revert, for the wrong reason (no
    detection, not correct read/write discrimination). Asserting the write
    counterpart is flagged IN THE SAME TEST means the test fails on revert
    (the write case goes missing too), which a read-only-shaped test alone
    cannot do."""
    read_only_source = "import os\nfd = os.open(path, os.O_RDONLY)\n"
    write_source = "import os\nfd = os.open(path, os.O_WRONLY)\n"
    assert "open" not in _flagged_names(read_only_source, tmp_path)
    assert "open" in _flagged_names(write_source, tmp_path)


def test_os_open_unresolvable_flags_fails_toward_flagging(tmp_path: Path) -> None:
    """Flags built from a variable this walk cannot resolve statically must
    fail TOWARD flagging, mirroring `_open_call_is_write`'s philosophy for
    an unresolvable mode string."""
    source = "import os\nfd = os.open(path, flags_from_elsewhere)\n"
    assert "open" in _flagged_names(source, tmp_path)


def test_os_open_mixed_known_read_flag_and_unresolved_term_is_flagged(
    tmp_path: Path,
) -> None:
    """Review follow-up (code-reviewer, this commit, P1): a flags
    expression combining ONE recognized read-only attribute
    (`os.O_RDONLY`) with ONE unresolvable term (`extra_flags_from_elsewhere`,
    a bare Name invisible to the attribute walk) previously resolved to
    "not a write" — the walk set `found_any_flag_attr = True` on the
    recognized attribute and never checked whether the rest of the
    expression was resolved. The unresolved term could carry `O_CREAT`/
    `O_WRONLY` at runtime, so this must fail TOWARD flagging regardless of
    what else in the same `|`-chain resolved cleanly."""
    source = (
        "import os\n"
        "fd = os.open(path, os.O_RDONLY | extra_flags_from_elsewhere)\n"
    )
    assert "open" in _flagged_names(source, tmp_path)


def test_io_open_write_mode_is_flagged(tmp_path: Path) -> None:
    source = 'import io\nf = io.open(path, "w")\n'
    assert "open" in _flagged_names(source, tmp_path)


def test_io_open_distinguishes_read_from_write_mode(tmp_path: Path) -> None:
    """A read-only assertion alone (`"open" not in _flagged_names(...)`) is
    vacuous under a full revert of this commit: pre-fix, `io.open(...)`
    attribute-call detection did not exist at all, so `io.open` would also
    be absent from the flagged set on revert, for the wrong reason.
    Asserting the write counterpart is flagged IN THE SAME TEST (see
    `test_io_open_write_mode_is_flagged` above, kept standalone too) means
    this test fails on revert, which a read-only-shaped test alone
    cannot do."""
    read_source = 'import io\nf = io.open(path, "r")\n'
    write_source = 'import io\nf = io.open(path, "w")\n'
    assert "open" not in _flagged_names(read_source, tmp_path)
    assert "open" in _flagged_names(write_source, tmp_path)


def test_unresolved_receiver_open_write_mode_is_flagged(tmp_path: Path) -> None:
    """`Path(...).open("w")` (and any other unresolved `<receiver>.open`)
    falls through to the generic mode-string check rather than the
    os.open-specific flags check — this covers pathlib's `.open()` shape
    without needing to special-case `pathlib.Path` imports."""
    source = 'p = Path(x)\nf = p.open("w")\n'
    assert "open" in _flagged_names(source, tmp_path)


def test_bare_builtin_open_still_flagged_unaffected_by_the_fix(tmp_path: Path) -> None:
    """Pre-existing bare `open(path, "w")` coverage must survive the
    receiver-resolution change unchanged."""
    source = 'f = open(path, "w")\n'
    assert "open" in _flagged_names(source, tmp_path)


def test_os_open_non_os_receiver_read_flag_shaped_attr_is_flagged(tmp_path: Path) -> None:
    """Review follow-up (code-reviewer, this commit, P1, second pass): an
    ``O_RDONLY``-shaped attribute reached through a receiver that is NOT
    a real ``os`` import alias must not be trusted as read-only just
    because the tail name matches a known read flag — the receiver could
    be anything, including a class attribute that is actually a write
    bitmask at runtime (``Foo.O_RDONLY = os.O_CREAT | os.O_WRONLY``).
    Before the receiver-identity check, this resolved to "not a write"
    purely by attribute name, silently missing a genuine write. Uses a
    bare `Foo.O_RDONLY` receiver (not bound by `import os`) to force the
    unresolved-receiver path without needing an actual monkeypatch."""
    source = (
        "import os\n"
        "fd = os.open(path, Foo.O_RDONLY, 0o644)\n"
    )
    assert "open" in _flagged_names(source, tmp_path)


def test_os_fdopen_still_flagged_unaffected_by_the_fix(tmp_path: Path) -> None:
    """Pre-existing `os.fdopen(fd, "w")` coverage must survive unchanged —
    it never went through the receiver-resolution branch, but a regression
    here would signal the new `if name == "open":` branch above it started
    swallowing `fdopen` calls too."""
    source = 'import os\nf = os.fdopen(fd, "w")\n'
    assert "fdopen" in _flagged_names(source, tmp_path)
