"""coordinator_core.spawn_policy.marker_check — shared pytest-marker AST checks.

THIRD CONSUMER (C8, docs/plans/2026-09-11-perf-ratchets-measure-process-
time-not-t.md § C8): `coordinator_core.tests.test_no_wall_clock_ratchets`
needs to know not just WHETHER a marker is present but what keyword
arguments it carries -- specifically whether `deliberate_wall_clock(...)`
was given a non-empty string-literal `reason=`. `decorator_names` and
`has_marker_decorator` unwrap every `ast.Call` to its dotted name and
throw the call's keywords away; they cannot answer this and, per this
module's own NEGATIVE SPEC below, never will -- they stay byte-identical.
`marker_call_nodes` is the additive sibling: same two input shapes the
existing functions take (a decorator list, or a statement body carrying a
`pytestmark` assignment), but it returns the matching `ast.Call` nodes
themselves, keywords intact, instead of a bool. C8 decides what counts as
"discharged" (a `reason=` keyword whose value is a non-empty `str`
Constant) -- this module still only answers "is marker X present here,"
now with its keywords visible to the caller that needs them.

Lifted (staff-eng F7, docs/plans/2026-08-20-the-spawn-ratchet-stops-
accumulating-arrears.md § C4) out of
`coordinator_core/tests/test_no_new_spawning_tests.py`, where this logic
originated as three private helpers (`_decorator_names`,
`_has_spawns_process_marker`, `_has_module_level_pytestmark`). Two
independent consumers now need the identical answer to "does this pytest
marker cover this decorator list / this module" — the ratchet test itself
(unchanged behaviour, now importing rather than defining) and the new
write-time nudge `coordinator_core.write_guards.
nudge_unmarked_spawning_test` (C4) — and the repo already hoisted this
exact shared-logic-out-of-the-ratchet pattern once before, for
`wrapper_resolution.py`; this module follows that precedent rather than
re-deriving it or importing a pytest-only module (with module-level
`import pytest`) as a production dependency of every matching write.

Pure AST, no execution, no disk access beyond what the caller already
parsed — mirrors `spawn_policy.detect.sites_in_source`'s "text/tree in,
answer out" shape so both consumers can call this from either a live
file on disk (the ratchet) or a not-yet-written buffer (the guard).

Negative-spec:
  - Does NOT itself decide whether a file/function "spawns" — that stays
    `spawn_policy.detect.sites_in_source` (guard) or the ratchet's own
    `_FunctionSpawnScanner`/`WrapperResolver` (ratchet). This module only
    answers "is marker X present here."
  - Does NOT change behaviour: `has_module_level_pytestmark` and
    `has_marker_decorator` are byte-identical in outcome to the pre-lift
    `_has_module_level_pytestmark`/`_has_spawns_process_marker` they
    replace (`test_no_new_spawning_tests.py`'s own suite continues to pass
    unchanged, proving the lift is behaviour-preserving).
  - `marker_call_nodes` does NOT decide what a "discharged" marker is (a
    reasoned `deliberate_wall_clock`, a bare `spawns_process`, or anything
    else) -- that judgment stays with the caller (C8's own guard). This
    module only surfaces the matching `ast.Call` nodes, keywords intact.

Spec backlink: docs/plans/2026-08-20-the-spawn-ratchet-stops-accumulating-arrears.md § C4
"""

from __future__ import annotations

import ast

__all__ = [
    "SPAWNS_PROCESS_MARKER",
    "decorator_names",
    "has_marker_decorator",
    "has_module_level_pytestmark",
    "marker_call_nodes",
]

#: The marker name every consumer of this module currently cares about, as
#: a named default rather than a repeated string literal — the ratchet's
#: Rule 4 (`pytest.mark.cadence`) still passes its own marker explicitly to
#: `has_module_level_pytestmark`, this is only the common-case default.
SPAWNS_PROCESS_MARKER = "pytest.mark.spawns_process"


def decorator_names(decorators: list[ast.expr]) -> list[str]:
    """Best-effort dotted-name rendering of a decorator list, e.g.
    `pytest.mark.spawns_process` -> "pytest.mark.spawns_process".

    Lifted verbatim from `test_no_new_spawning_tests.py::_decorator_names`.
    """
    names: list[str] = []
    for dec in decorators:
        node = dec
        # `@pytest.mark.spawns_process` parses as an Attribute chain (no
        # call); `@pytest.mark.spawns_process()` would parse as a Call
        # wrapping the same Attribute chain -- unwrap it either way.
        if isinstance(node, ast.Call):
            node = node.func
        parts: list[str] = []
        while isinstance(node, ast.Attribute):
            parts.append(node.attr)
            node = node.value
        if isinstance(node, ast.Name):
            parts.append(node.id)
        if parts:
            names.append(".".join(reversed(parts)))
    return names


def has_marker_decorator(
    decorators: list[ast.expr], marker: str = SPAWNS_PROCESS_MARKER
) -> bool:
    """True if `marker` (dotted form, e.g. "pytest.mark.spawns_process")
    appears anywhere in `decorators`.

    Lifted verbatim (generalized to any `marker`, default unchanged) from
    `test_no_new_spawning_tests.py::_has_spawns_process_marker`.
    """
    return marker in decorator_names(decorators)


def has_module_level_pytestmark(
    tree: ast.Module, marker: str = SPAWNS_PROCESS_MARKER
) -> bool:
    """True if the module declares `pytestmark = <marker>` or
    `pytestmark = [<marker>, ...]` at module level -- the file-wide
    equivalent of decorating every test individually.

    Lifted verbatim from
    `test_no_new_spawning_tests.py::_has_module_level_pytestmark`.
    """
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            continue
        value = node.value
        candidates = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        for candidate in candidates:
            target = candidate.func if isinstance(candidate, ast.Call) else candidate
            parts: list[str] = []
            while isinstance(target, ast.Attribute):
                parts.append(target.attr)
                target = target.value
            if isinstance(target, ast.Name):
                parts.append(target.id)
            if ".".join(reversed(parts)) == marker:
                return True
    return False


def _dotted_name(node: ast.expr) -> str:
    """Best-effort dotted-name rendering, unwrapping one `ast.Call`
    wrapper -- the same unwrap `decorator_names` does, shared here so the
    two functions agree on what "matches `marker`" means."""
    target = node.func if isinstance(node, ast.Call) else node
    parts: list[str] = []
    while isinstance(target, ast.Attribute):
        parts.append(target.attr)
        target = target.value
    if isinstance(target, ast.Name):
        parts.append(target.id)
    return ".".join(reversed(parts))


def marker_call_nodes(
    decorators: list[ast.expr] | None = None,
    body: list[ast.stmt] | None = None,
    marker: str = SPAWNS_PROCESS_MARKER,
) -> list[ast.Call]:
    """Keyword-aware sibling of `has_marker_decorator` /
    `has_module_level_pytestmark`: returns the matching `ast.Call` nodes
    (keywords intact) instead of a bool.

    Takes the same two input shapes those two functions take, so function,
    class and module levels all go through this one function:

      - `decorators`: a decorator list (`FunctionDef.decorator_list` or
        `ClassDef.decorator_list`) -- matches over Rule 2 already covers
        (bare `@pytest.mark.x` and `@pytest.mark.x(...)` alike). A bare
        `Attribute` match (no call, e.g. `@pytest.mark.spawns_process`
        with no parens) has no keywords to inspect and is NOT returned --
        callers that need to know a bare marker is present at all still
        use `has_marker_decorator`; this function only surfaces the ones
        whose keywords a caller can actually read.
      - `body`: a statement body that may carry a `pytestmark = <m>` /
        `pytestmark = [<m>, ...]` assignment -- `ast.Module.body` for the
        module level, `ast.ClassDef.body` for the class level (the same
        body shape `has_module_level_pytestmark` reads off `tree.body`).

    Exactly one of `decorators`/`body` is expected per call; passing both
    concatenates their matches. Only `ast.Call` candidates whose dotted
    name equals `marker` are returned -- non-call candidates are dropped
    for the reason above.
    """
    calls: list[ast.Call] = []

    for dec in decorators or []:
        if isinstance(dec, ast.Call) and _dotted_name(dec) == marker:
            calls.append(dec)

    for node in body or []:
        if not isinstance(node, ast.Assign):
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            continue
        value = node.value
        candidates = value.elts if isinstance(value, (ast.List, ast.Tuple)) else [value]
        for candidate in candidates:
            if isinstance(candidate, ast.Call) and _dotted_name(candidate) == marker:
                calls.append(candidate)

    return calls
