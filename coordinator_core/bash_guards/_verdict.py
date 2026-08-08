"""coordinator_core.bash_guards._verdict -- SILENT, a third guard outcome
carried OUT-OF-BAND (C1 of `docs/plans/2026-08-07-guards-reach-a-verdict-on-
powershell-or-stay-silent.md`).

Problem this module exists to close: every command guard in this package
returns the SAME value -- bare ``None`` -- both when it has cleared a
command as safe and when it never understood the command at all (e.g. a
PowerShell-idiom command a bash-only guard's parser cannot handle). A caller
cannot tell "declined to rule" from "cleared" apart, because they are the
same Python object. AC1 (this plan) requires that distinction to exist for
the DYNAMIC residue: a guard that HAS declared it can read a dialect and
then meets input its own parser cannot handle (``root_node.has_error``, an
absent dialect enum, an ``ImportError`` fallback) -- STATIC per-guard
non-coverage is a separate, already-covered concern (the sibling plan's
``MATCHERS`` mechanism; see this module's own "Relationship to MATCHERS"
below).

MANDATED SHAPE (re-scoped 2026-08-07, eng-director findings 2/3 -- read the
plan's C1 body before touching this file): SILENT rides OUT-OF-BAND. Every
guard module keeps returning ``Optional[Dict]`` exactly as it does today --
the return CONTRACT of every guard's ``check()`` is UNCHANGED, and this
module makes ZERO edits to ``dispatch.py``'s guard-chain loop. A guard that
cannot rule records ``SILENT`` on a per-call collector channel (via
``record_silent``) ALONGSIDE its ordinary ``None`` return, rather than as
the return value itself. A caller that wants to observe the distinction
opens a collection with ``collecting()`` around the guard call and reads the
yielded list afterward (C7's structural test is that caller).

This is deliberately the SAME collector idiom as
``coordinator_core.session.declared_writes`` (``ContextVar``-backed,
context-manager-scoped, no-op when no collection is open) -- a second,
differently-shaped collector for the identical "some code deep in a call
stack wants to leave a breadcrumb for its caller without changing its own
return type" problem would be a second dialect of the same primitive this
repo already has one of. This module owns its OWN ``ContextVar`` (SILENT
declarations are guard-verdict signal, not a file-write declaration --
conflating the two collections under one list would let a test that opens
one accidentally observe the other), but the shape -- ``ContextVar[Optional[
List]]``, a ``collecting()`` context manager, a no-op record function when
no collection is open -- is copied deliberately, not reinvented.

Underscore-prefixed per this package's private-helper convention
(``_helpers.py``, ``_platform_verdict.py``) -- this is an internal seam
between a guard and a test/caller that opens a collection around it, not a
public dispatch-facing API.

NEGATIVE SPEC -- this is NOT a fourth ``permissionDecision`` value and must
NEVER be emitted into a PreToolUse hook envelope. ``SILENT`` never appears
inside a dict a guard's ``check()`` returns, never appears inside anything
``dispatch.py``'s ``evaluate_payload_json`` returns to its own caller, and
is not JSON-serializable by design (see ``SILENT``'s own docstring) so that
an accidental attempt to fold it into an envelope fails loudly instead of
shipping a malformed hook response.

Relationship to MATCHERS (the sibling plan's C1,
`docs/plans/2026-08-07-command-guards-fire-under-both-tool-names.md`): that
mechanism covers STATIC per-guard non-coverage -- a guard declares, ahead of
time, the ``tool_name``s its detection can read at all, and the sibling's
own dispatch-chain loop skips calling the guard's ``fn()`` entirely for a
non-matching ``tool_name``. That is a decision made BEFORE the guard runs,
about whether to run it. ``SILENT`` is the complementary, narrower case: a
guard that DID run (because ``MATCHERS`` widened it to a dialect it claims
to read) and then met a specific input its parser could not handle. The two
mechanisms are not alternatives to one another -- a guard can be excluded by
``MATCHERS`` for a whole dialect it never claims, AND independently record
``SILENT`` for a single malformed command within a dialect it does claim.

This is the same structural failure class named upstream, one layer up, by
a ratified tripwire family -- cite ``coordinator-tripwires.md
§ GUARD-WIRING-SILENT-SKIP`` (example-doctrine-repo repo): "absence of a guard must
not be indistinguishable from the guard passing... a loud warning that lets
the operation proceed anyway is not the fix either." That entry
cross-references its sibling ``§ BASH-POLICY-DENY-GUARD-FAIL-OPEN-
INVERSION``, which names ``block_reviewer_bash_outside_allowlist``
explicitly (see this plan's C6). ``SILENT`` slots into this existing
doctrine family rather than being a novel abstraction invented here.

Spec backlink: docs/plans/2026-08-07-guards-reach-a-verdict-on-powershell-or-stay-silent.md § C1
"""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from typing import Iterator, List, NamedTuple, Optional

__all__ = [
    "SILENT",
    "SilentDeclaration",
    "record_silent",
    "collecting",
    "active_silences",
    "was_silent",
]


class _SilentSentinel:
    """The SILENT sentinel type -- a single instance (``SILENT`` below) is
    the only value of this type that ever exists. Deliberately NOT a plain
    ``object()`` (which would satisfy uniqueness but give a useless
    ``repr()`` in a failing test's assertion diff) and deliberately NOT a
    dict/JSON-serializable shape (a bare ``dict``, ``None``, ``True``, or a
    string could all be mistaken for -- or accidentally coerced into -- a
    hook envelope fragment; this type compares equal to nothing but itself,
    has no ``keys``/``__iter__``, and is not JSON-serializable, so it cannot
    be silently accepted anywhere a dict or JSON value is expected --
    ``json.dumps`` raises ``TypeError`` on it rather than emitting a value).

    NEGATIVE SPEC: never place an instance of this type inside a dict a
    guard's ``check()`` returns, and never pass it to ``json.dumps`` --
    both are the exact fourth-``permissionDecision``-value shape this
    module's docstring forbids. This class exists only to be recorded on
    the out-of-band channel below (``record_silent``), never returned.
    """

    __slots__ = ()

    def __repr__(self) -> str:
        return "SILENT"

    def __bool__(self) -> bool:
        # A guard-verdict sentinel that evaluated falsy would be
        # indistinguishable, at an `if verdict:` call site, from an absent
        # value -- exactly the ambiguity this module exists to close.
        # Truthy-by-construction is a deliberate tripwire against that
        # collapse, even though the mandated shape never returns SILENT as
        # a verdict value in the first place.
        return True


#: The one SILENT instance. Recorded on the out-of-band channel via
#: ``record_silent`` -- never returned from a guard's ``check()``.
SILENT = _SilentSentinel()


class SilentDeclaration(NamedTuple):
    """One recorded SILENT declaration: which guard declined to rule, and
    why. ``reason`` is free text for a human/test reader (e.g. "backtick
    line-continuation: root_node.has_error") -- this module makes no claim
    about its shape or vocabulary; C4/C5's guards each write their own.
    """

    guard_name: str
    reason: str


_ACTIVE: ContextVar[Optional[List[SilentDeclaration]]] = ContextVar(
    "coordinator_core_bash_guards_silent_declarations", default=None
)


def record_silent(guard_name: str, reason: str) -> None:
    """Record that ``guard_name`` declined to rule on the current command,
    for the reason given. A NO-OP when no collection is open (``collecting``
    below was never entered) -- mirrors ``declared_writes.declare_write``'s
    contract exactly: a guard that calls this outside a test's/caller's
    collection behaves precisely as it did before this module existed,
    including on every real production dispatch, since ``dispatch.py``
    never opens a collection (this chunk's own Anti-scope forbids editing
    ``dispatch.py`` at all). The out-of-band channel is therefore inert in
    production and observable only to a caller that deliberately opens it.

    Never raises: called from inside a guard's own hot path, so a defect
    here must not be the reason a guard fails to return its ordinary
    verdict. An empty/falsy ``guard_name`` is still recorded as given --
    this function does not validate its caller's naming convention, only
    appends what it is given.
    """
    active = _ACTIVE.get()
    if active is None:
        return
    active.append(SilentDeclaration(guard_name, reason))


@contextlib.contextmanager
def collecting() -> Iterator[List[SilentDeclaration]]:
    """Open a SILENT-declaration collection for the duration of the block.

    Yields the list ``record_silent`` appends to. This is the "caller" the
    module docstring's AC1 discharge argument refers to: C7's structural
    test wraps ONE guard's ``check(payload)`` call in ``with collecting()
    as silences:`` and afterward inspects both the guard's ordinary return
    value AND ``silences`` -- the pair together is what makes
    "declined-to-rule" distinguishable from "cleared" (a ``None`` return
    with an EMPTY ``silences`` list is a genuine clean verdict; a ``None``
    return with a NON-EMPTY ``silences`` list is a declared SILENT).

    Nesting is supported and shadows the outer collection for the life of
    the block, exactly like ``declared_writes.collecting`` -- a guard that
    itself calls into another guard (none do today) cannot leak a
    declaration into an ambient outer collection it was never handed.
    """
    collected: List[SilentDeclaration] = []
    token = _ACTIVE.set(collected)
    try:
        yield collected
    finally:
        _ACTIVE.reset(token)


def active_silences() -> Optional[List[SilentDeclaration]]:
    """Return the open collection, or ``None`` when no collection is open.

    Exposed for tests and for a future caller that needs to check whether a
    collection is live without opening a second, nested one; guards
    themselves should call ``record_silent`` rather than reaching for this.
    """
    return _ACTIVE.get()


def was_silent(guard_name: str, declarations: List[SilentDeclaration]) -> bool:
    """``True`` iff ``guard_name`` appears at least once in ``declarations``
    (the list yielded by ``collecting()``). A small convenience so a test
    does not hand-roll the same ``any(d.guard_name == ... for d in ...)``
    scan at every call site -- the exact kind of drift-prone repetition
    this package's own docstrings elsewhere (see ``_helpers.py``'s
    ``csn_check``/``is_trivial_reason`` consolidation notes) warn against
    re-deriving per caller.
    """
    return any(d.guard_name == guard_name for d in declarations)
