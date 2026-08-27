"""coordinator_core.bash_guards.roster -- public, payload-free enumeration
of every guard `dispatch.py::_build_guard_chain` registers.

Purpose: DoE-claude's `x-effective-delivery` hook-delivery-manifest emitter
(`docs/reference/hook-delivery-manifest.md`) cannot see the guards
`preuse-bash-dispatch.py` fans out to -- they exist only as claude-klabauter Python
modules, reachable solely through `dispatch.py`'s private, payload-
parameterised `_build_guard_chain`. This module is the public seam that
closes that gap: a stable, payload-free read of the live registration, safe
to call from outside this package (including across the plane boundary,
per that contract's "Natural emitter source" section -- claude-klabauter is DoE's
hard prereq, so this dependency direction is the allowed one).

Spec backlink: pln-guard-roster-export-minus-the-a4dec3, chunk C1.
Contract: docs/reference/hook-delivery-manifest.md (`tool_names` /
`script` / "Script tail key" sections -- this module is the claude-klabauter-side
root of truth those sections describe).

Structural-read discipline (shared with `tests/test_guard_band_membership.
py::_chain`, the proven-safe precedent this module's `guard_roster()`
copies the call shape from): `_build_guard_chain` is called with an inert
dummy command/payload so the registration list can be built at all, but
every `GuardEntry.fn` closure is inspected ONLY for its `__code__`/
`__globals__`/`__closure__` -- never called. Calling one would run a real
guard against a fabricated payload, which is exactly what this module
exists to avoid needing.

Negative spec: this module never normalises, sorts, or widens a guard's
declared `matchers` -- it reports the registration's own tuple verbatim,
including every guard still at the `("Bash",)` default. A roster that
"fixed" a narrow guard on the way past would falsify the one field this
module exists to make truthful.

`guard_roster()`'s dummy payload deliberately hand-copies the literal
shape `test_guard_band_membership.py`'s own dummy chain uses rather than
importing it from that test module: a source module importing from a test
module inverts the dependency direction, which is worse than the small
duplication. The payload's only contract is "be inert" -- it does not need
to track the test file.
"""

from __future__ import annotations

import dis
import types
from dataclasses import dataclass
from typing import Optional, Tuple

from coordinator_core.bash_guards import dispatch as _dispatch
from coordinator_core.ops.session.guard_settings_integrity import _tail_key as _tail_key


@dataclass(frozen=True)
class GuardRosterEntry:
    """One roster entry -- plain data, no closures, no live `GuardEntry`
    references. Field order and names match the plan's AC2/AC6 exactly.

      id           -- `GuardEntry.name`, verbatim.
      matchers     -- `GuardEntry.matchers`, verbatim (never widened).
      band         -- `GuardEntry.band`'s own string value (`GuardBand.value`,
                       e.g. `"confinement-deny"`), not the enum member --
                       kept JSON-serialisable without a caller-side coercion.
      fail_closed  -- `GuardEntry.fail_closed`, verbatim.
      script       -- see `_script_tail_for`.
    """

    id: str
    matchers: Tuple[str, ...]
    band: str
    fail_closed: bool
    script: str


_SELF_MODULE = _dispatch.__name__


def _resolve_referenced_module(
    fn: types.FunctionType, _seen: Optional[set] = None
) -> Optional[str]:
    """Best-effort structural derivation of the module that actually backs
    a `GuardEntry.fn` zero-arg closure -- NEVER by calling `fn` (see module
    docstring); only its bytecode's `LOAD_GLOBAL` names and its
    `__closure__` (captured free variables) are inspected.

    Deliberately does NOT walk `fn.__code__.co_names` directly: CPython
    bytecode conflates global-lookup names (`LOAD_GLOBAL`) with attribute-
    access names (`LOAD_ATTR`/`LOAD_METHOD`) in that tuple, so for
    `lambda: _dc.check_x(cmd, ...)`, `co_names` holds both `'_dc'` (a real
    global) AND `'check_x'` (an attribute, never a global). Resolving every
    `co_names` entry against `fn.__globals__` is only accidentally correct
    today, because no `_dc.<method>` attribute name happens to collide with
    an unrelated global already present in `dispatch.py`'s namespace -- a
    future collision would silently misattribute a guard's `script` to the
    wrong module. `dis.get_instructions` disassembles the actual
    instruction stream instead, so only names genuinely loaded via
    `LOAD_GLOBAL` are considered -- a structural guarantee, not a naming
    coincidence. (CPython 3.11+ note: `LOAD_GLOBAL`'s raw `arg` low bit
    flags a pushed NULL for the call-shape optimization; `argval`, which
    `dis` already decodes to the plain name string, is used here rather
    than the raw `arg`, so that flag bit never leaks into the name.)

    Every registered entry's `fn` is one of two shapes:

      1. `lambda: _check_x(payload)` / `lambda: _dc.check_x(cmd, ...)` --
         the guard's own check function (or the module carrying it, for
         the `_dc.` attribute-access shape) is a DIRECT global reference in
         `fn.__globals__` (dispatch.py's own module namespace, since every
         lambda here is written inline at `dispatch.py` module scope). This
         is the common case and resolves in one pass.
      2. `lambda: _git_revert_full()[0]` -- a lambda closing over a helper
         `def`'d INSIDE `_build_guard_chain` itself (a genuine closure
         cell, not a global). That helper's own `__module__` is `dispatch`
         itself, which is not a usable answer (this module reports the
         guard's OWN module, not the dispatcher's), so this function
         recurses one level into the helper's own referenced globals/
         closure instead of stopping there.

    Returns `None` when nothing resolvable is found (fn references no
    global/closure object outside plain data), letting `_script_tail_for`
    fall back to `dispatch.py`'s own module -- never guessed at.
    """
    if not isinstance(fn, types.FunctionType):
        return None
    if _seen is None:
        _seen = set()
    if id(fn) in _seen:
        return None
    _seen.add(id(fn))

    candidates = []
    code = fn.__code__
    globs = fn.__globals__
    global_names = {
        instr.argval
        for instr in dis.get_instructions(code)
        if instr.opname == "LOAD_GLOBAL"
    }
    for name in global_names:
        obj = globs.get(name)
        if obj is not None:
            candidates.append(obj)
    if fn.__closure__:
        for cell in fn.__closure__:
            try:
                candidates.append(cell.cell_contents)
            except ValueError:
                # An unset cell (should not happen for a fully-built
                # closure at registration time) -- skip rather than guess.
                continue

    # First pass: a directly-referenced module or function whose OWN module
    # is not dispatch.py itself is the answer -- the shape-1 case above.
    for obj in candidates:
        if isinstance(obj, types.ModuleType):
            if obj.__name__ != _SELF_MODULE:
                return obj.__name__
        elif isinstance(obj, types.FunctionType):
            if obj.__module__ and obj.__module__ != _SELF_MODULE:
                return obj.__module__

    # Second pass: a referenced function that IS itself defined in
    # dispatch.py (shape-2 above) -- recurse into ITS own references
    # instead of reporting dispatch.py for a guard whose real logic lives
    # elsewhere.
    for obj in candidates:
        if isinstance(obj, types.FunctionType) and obj.__module__ == _SELF_MODULE:
            deeper = _resolve_referenced_module(obj, _seen)
            if deeper is not None:
                return deeper

    return None


def _script_tail_for(entry: "_dispatch.GuardEntry") -> str:
    """The module tail `bash_guards/<module>.py`, in `_tail_key`'s normal
    form (lowercased last two path segments) -- reusing that function
    rather than re-implementing the lowercasing/two-segment rule, per the
    plan's "ONE definition of that normal form in the tree" requirement.

    Isolated to this one function on purpose: DoE has been asked which
    value this field should hold for a fanned-out guard (module tail vs
    carrier tail vs a historical script name), and their answer must be a
    one-function change here, not a rewrite of `guard_roster()`.
    """
    module_dotted = _resolve_referenced_module(entry.fn) or _SELF_MODULE
    token = module_dotted.replace(".", "/") + ".py"
    tail = _tail_key(token)
    return tail if tail is not None else token


def guard_roster() -> Tuple[GuardRosterEntry, ...]:
    """Return every guard `dispatch.py`'s `_build_guard_chain` registers,
    as plain data -- no payload argument, no side effects at import beyond
    what importing `bash_guards` already costs (this module imports
    `dispatch` eagerly, same as any other `bash_guards` consumer already
    does).

    Reads the live registration exactly as `tests/test_guard_band_
    membership.py::_dummy_chain` does -- `_build_guard_chain(...)` with an
    inert dummy command/payload -- and NEVER invokes a returned entry's
    `fn` closure (see module docstring).
    """
    chain = _dispatch._build_guard_chain(
        cmd="echo coordinator-guard-roster-probe",
        session_id="guard-roster-probe",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo x"}},
        policy_file=None,
        host_is_windows=None,
    )
    return tuple(
        GuardRosterEntry(
            id=entry.name,
            matchers=tuple(entry.matchers),
            band=entry.band.value,
            fail_closed=entry.fail_closed,
            script=_script_tail_for(entry),
        )
        for entry in chain
    )
