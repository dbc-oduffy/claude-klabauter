"""Structural inventory over every advisory-emitting guard in `dispatch.py`'s
registered chain: each one must either call `operator_override_note` (in its
own function body or a same-module helper it calls) or sit on the explicit,
named allowlist below.

Why this file exists: `guard_grep_via_bash.check()` silently stopped calling
`operator_override_note` (H11(b), 2026-07-30) while its own env var
(`COORDINATOR_OVERRIDE_GREP_VIA_BASH_GUARD`) kept functioning -- an
override route an agent reading the advisory had no way to discover, unlike
every other guard in the suite. Nothing failed red when that happened
because nothing inventoried the *set of guards that should be naming a
route* against the set that actually do. This file is that inventory --
the artifact a code-review finding (M17, 2026-07-30) named as missing, not just a fix for
the one guard that regressed.

`GuardBand.ADVISORY_REWRITE` and `GuardBand.PLATFORM_CONDITIONED_DENY` are
the two bands this suite uses for guards that can produce advisory/deny
text an agent reads and might want to bypass -- `CONFINEMENT_DENY` is a
different kind of boundary by this package's own doctrine (see
`dispatch.py`'s own "GuardBand" registration comments: several
CONFINEMENT_DENY guards are unconditionally non-suppressible, by design,
so their absence from this inventory is not a gap of the same shape).

Resolution walks the SAME structural path `test_guard_band_membership.py`
already uses (`dispatch._build_guard_chain` with a harmless dummy
command/payload) -- never executes a guard's real logic, only reads back
each `GuardEntry`'s `name`/`band`/`fn`, then resolves `fn`'s lambda source
back to the target function object via `fn.__globals__`, and does a small
same-module call-graph walk (depth-limited) so a guard that names its
override route only inside a private helper (e.g. `guard_grep_via_bash.
_composed_advisory`) still passes.
"""

from __future__ import annotations

import ast
import inspect
import re
import textwrap
from typing import Any, Callable, Dict, Optional, Set

from coordinator_core.bash_guards import dispatch as _dispatch
from coordinator_core.bash_guards.dispatch import GuardBand

#: Bands whose guards can emit advisory/deny text worth naming a bypass
#: route for. CONFINEMENT_DENY is deliberately excluded -- see module
#: docstring.
_ADVISORY_BANDS = (GuardBand.ADVISORY_REWRITE, GuardBand.PLATFORM_CONDITIONED_DENY)

#: EXPLICIT, NAMED opt-outs. A guard lands here only with a one-line reason
#: -- this is the allowlist Finding 1 (code-review) asked for, kept empty
#: unless a guard has a PM-ruled no-override-route reason (see entries
#: below), so the default state of this suite is "every advisory guard
#: names its route."
#: Review: coordinator:code-reviewer -- docstring previously claimed the
#: dict was "kept empty by default" while shipping two entries below.
_NO_OVERRIDE_NOTE_ALLOWLIST: Dict[str, str] = {
    # docs/plans/2026-08-06-apply-guard-class-census.md, C13/C14b
    # (commit 2ac049c5b) -- block_noncanonical_branch_creation.py flipped
    # CONFINEMENT_DENY -> ADVISORY_REWRITE per DR-277 ("guards are advisory
    # by default"), landing it in this file's scan for the first time. Its
    # own NEGATIVE SPEC 3 (PM ruling R4, same ruling as the C5/C7 entries
    # below) says it never appends `operator_override_note` and never names
    # a bypass env var -- no override route exists to name, same rationale
    # as its sibling guards in this trio.
    "block-noncanonical-branch-creation": "PM ruling R4 -- no override env var by design (see block_noncanonical_branch_creation.py's own NEGATIVE SPEC 3).",
    # docs/plans/2026-08-01-branch-creation-seam-guards.md, chunks C5/C7 --
    # PM ruling R4: neither guard names any `COORDINATOR_OVERRIDE_*`/
    # `COORDINATOR_ALLOW_*`/`COORDINATOR_DISABLE_*` bypass, deliberately
    # (each module's own "NEGATIVE SPEC" says so explicitly, precisely to
    # avoid `_alternative_liveness._OVERRIDE_RE` misreading a "there is no
    # bypass" sentence as an offered one). No override route exists to name.
    "branch-set-precedence": "PM ruling R4 -- no override env var by design (see guard_branch_set_precedence.py's own NEGATIVE SPEC 3).",
    "longlived-branch-naming": "PM ruling R4 -- no override env var by design (see guard_longlived_branch_naming.py's own NEGATIVE SPEC).",
    # docs/plans/2026-08-02-write-confinement-guards.md (example-doctrine-repo), chunk
    # C4/C6 -- deliberately no `COORDINATOR_OVERRIDE_*`/`COORDINATOR_ALLOW_*`
    # env-var bypass: the marker `touch` command IS this bump's clear
    # mechanism, and `_write_bump_message.py`'s own "TONE IS A REQUIREMENT"
    # section states no env-var/config bypass is ever named, because
    # `_blanket_disarm.py` (~lines 5-15) records that no such bypass is
    # reachable from inside a session -- naming one would be the same
    # historical mistake this suite's guards have repeatedly gotten wrong.
    "bump-foreign-repo-write": "docs/plans/2026-08-02-write-confinement-guards.md -- no override env var by design; the marker touch line is the clear mechanism, not an env var (see _write_bump_message.py's own NEGATIVE SPEC).",
    # Same plan, chunk C5 -- the outside-repo sibling of the entry above.
    # Identical rationale: same shared `_write_bump_message.py` copy, same
    # marker-touch clear mechanism, no separate override route of its own.
    "bump-outside-repo-write": "docs/plans/2026-08-02-write-confinement-guards.md -- no override env var by design; the marker touch line is the clear mechanism, not an env var (see _write_bump_message.py's own NEGATIVE SPEC).",
    # docs/plans/2026-08-07-git-index-lock-contention-campaign.md -- the
    # self-heal leg. `check_reap_stale_git_lock` always returns `None`
    # (allow, unchanged) whatever it finds -- see guard_reap_stale_git_lock
    # .py's own module docstring ("this guard never rewrites `cmd` at all;
    # its mechanism is a side effect ... and it always returns None"). It
    # never emits an advisory message of any kind, so there is no override
    # route for it to name -- unlike its `git-no-optional-locks` neighbor,
    # which rewrites `cmd` and therefore has advisory text to gate.
    "reap-stale-git-lock": "guard_reap_stale_git_lock.py's own module docstring -- side-effect-only guard that always returns None, never emits advisory text, so no override route exists to name.",
}

#: Matches the call expression inside `lambda: <call>(...)` as registered
#: in `dispatch._build_guard_chain`'s `GuardEntry` list -- e.g.
#: `_dc.check_no_verify` or `_check_worktree_creation`.
_LAMBDA_CALL_TARGET = re.compile(r"lambda:\s*([A-Za-z_][A-Za-z0-9_.]*)\s*\(")

#: A trailing `[<digit>]` subscript on the lambda's call expression -- the
#: memo-closure shape `_build_guard_chain` uses for a guard whose hard-deny
#: and advisory legs share one underlying oracle call (Review: staff-eng,
#: Finding 0, `destructive-git-revert`/`destructive-git-revert-advisory`):
#: `lambda: _git_revert_full()[0]` / `lambda: _git_revert_full()[1]`, where
#: `_git_revert_full` is a per-dispatch-call closure (not a module-level
#: name in `fn.__globals__`) memoizing `_dc._check_destructive_git_revert_
#: full`'s `(deny, advisory)` tuple.
_LAMBDA_SUBSCRIPT = re.compile(r"\)\s*\[(\d+)\]")

#: Recovers the shared `_check_<name>_full` function a memo-closure calls,
#: so the subscript above can be resolved to the SPECIFIC `check_<name>` /
#: `check_<name>_advisory` top-level function it denotes, rather than the
#: closure itself (which is neither `_check_*` nor `check_*` and is not a
#: function the call-graph walk below can meaningfully inspect).
_FULL_FN_CALL = re.compile(r"([A-Za-z_][A-Za-z0-9_.]*)\._check_(\w+)_full\(")

_MAX_CALL_GRAPH_DEPTH = 3


def _module_attr_chain(module_globals: Dict[str, Any], dotted: str) -> Any:
    parts = dotted.split(".")
    obj: Any = module_globals.get(parts[0])
    for attr in parts[1:]:
        obj = getattr(obj, attr, None)
        if obj is None:
            return None
    return obj


def _closure_var(fn: Callable, name: str) -> Any:
    """Look `name` up among `fn`'s own closure cells (its free variables),
    for the case `fn.__globals__` (module-level names only) does not have
    it -- a lambda registered inside `_build_guard_chain` closing over a
    per-dispatch-call local (e.g. `_git_revert_full`) is exactly this case;
    `__globals__` never sees a function's own locals, closure or not."""
    code = getattr(fn, "__code__", None)
    closure = getattr(fn, "__closure__", None)
    if code is None or closure is None:
        return None
    for freevar, cell in zip(code.co_freevars, closure):
        if freevar == name:
            try:
                return cell.cell_contents
            except ValueError:
                return None
    return None


def _resolve_memo_closure_subscript(closure_fn: Callable, index: int) -> Optional[Callable]:
    """`closure_fn` is a per-dispatch-call closure (like `_git_revert_full`)
    memoizing a shared `<module>._check_<name>_full(...)` call and returning
    its `(deny, advisory)` tuple. Resolve `index` (0 or 1) to the actual
    registered top-level function that leg corresponds to --
    `check_<name>` for 0, `check_<name>_advisory` for 1 -- by reading the
    closure's own source for the `_check_<name>_full` call it wraps, never
    by guessing at `closure_fn`'s own name."""
    try:
        src = inspect.getsource(closure_fn)
    except (OSError, TypeError):
        return None
    match = _FULL_FN_CALL.search(src)
    if not match:
        return None
    module_ref, base = match.group(1), match.group(2)
    module = _module_attr_chain(closure_fn.__globals__, module_ref)
    if module is None:
        return None
    name = "check_%s" % base if index == 0 else "check_%s_advisory" % base
    target = getattr(module, name, None)
    return target if callable(target) else None


def _resolve_call_target(fn: Callable) -> Optional[Callable]:
    """Resolve a `GuardEntry.fn` lambda back to the actual guard function it
    calls, via the lambda's own global namespace (module-level `_dc` alias
    or a bare `_check_*` import) -- never by re-executing the lambda.

    Also handles the memo-closure indirection (see `_LAMBDA_SUBSCRIPT`'s
    docstring): a lambda calling a per-dispatch-call closure and subscripting
    its tuple result resolves through `_resolve_memo_closure_subscript` to
    the specific `check_*`/`check_*_advisory` function the subscript denotes,
    not to the closure itself."""
    src = inspect.getsource(fn)
    match = _LAMBDA_CALL_TARGET.search(src)
    if not match:
        return None
    parts = match.group(1).split(".")
    obj: Any = fn.__globals__.get(parts[0])
    if obj is None and len(parts) == 1:
        obj = _closure_var(fn, parts[0])
    for attr in parts[1:]:
        obj = getattr(obj, attr, None)
        if obj is None:
            return None
    if obj is None or not callable(obj):
        return None
    subscript = _LAMBDA_SUBSCRIPT.search(src)
    if subscript is not None:
        resolved = _resolve_memo_closure_subscript(obj, int(subscript.group(1)))
        if resolved is not None:
            return resolved
    return obj


def _calls_operator_override_note(func: Callable) -> bool:
    """True iff `func`'s own body contains an actual CALL to
    `operator_override_note` -- an AST walk over `ast.Call` nodes, never a
    text/substring search. A substring search is fooled by this very test
    file's own docstrings (which say the words "operator_override_note" in
    prose) and would be equally fooled by a guard module's docstring or a
    comment mentioning the helper without calling it -- exactly the
    distinction Finding 1 turned on (the env var stayed live while the
    CALL disappeared)."""
    try:
        src = textwrap.dedent(inspect.getsource(func))
        tree = ast.parse(src)
    except (OSError, TypeError, SyntaxError):
        return False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else getattr(callee, "attr", None)
        if name == "operator_override_note":
            return True
    return False


def _same_module_call_graph_calls_override_note(
    func: Callable, seen: Optional[Set[Callable]] = None, depth: int = 0
) -> bool:
    """True iff `func` or a same-module helper it calls (by identifier
    match against the module's globals, depth-limited) calls
    `operator_override_note` -- so a guard's override-route mention is
    found even when it lives in a private helper the top-level `check`/
    `check_*` function delegates to (e.g. `guard_grep_via_bash.check` ->
    `_composed_advisory`)."""
    if seen is None:
        seen = set()
    if func in seen or depth > _MAX_CALL_GRAPH_DEPTH:
        return False
    seen.add(func)
    if _calls_operator_override_note(func):
        return True
    try:
        src = inspect.getsource(func)
    except (OSError, TypeError):
        return False
    module = inspect.getmodule(func)
    if module is None:
        return False
    for name in set(re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(", src)):
        candidate = getattr(module, name, None)
        if inspect.isfunction(candidate) and inspect.getmodule(candidate) is module:
            if _same_module_call_graph_calls_override_note(candidate, seen, depth + 1):
                return True
    return False


def _build_chain():
    return _dispatch._build_guard_chain(
        cmd="echo hi",
        session_id="test-session-override-inventory",
        cwd="/tmp",
        payload={"tool_name": "Bash", "tool_input": {"command": "echo hi"}},
        policy_file=None,
        host_is_windows=False,
    )


def test_every_advisory_guard_names_its_override_route_or_is_allowlisted():
    chain = _build_chain()
    advisory_entries = [e for e in chain if e.band in _ADVISORY_BANDS]
    assert advisory_entries, "no ADVISORY_REWRITE/PLATFORM_CONDITIONED_DENY guards found -- the chain build changed shape, fix this test's premise before trusting its result"

    missing = []
    unresolved = []
    for entry in advisory_entries:
        if entry.name in _NO_OVERRIDE_NOTE_ALLOWLIST:
            continue
        target = _resolve_call_target(entry.fn)
        if target is None:
            unresolved.append(entry.name)
            continue
        if not _same_module_call_graph_calls_override_note(target):
            missing.append(entry.name)

    assert not unresolved, (
        "could not resolve the guard function for: %s -- this test's lambda-source "
        "resolution needs updating to match dispatch.py's current registration shape, "
        "not a real pass for these guards" % unresolved
    )
    assert not missing, (
        "the following advisory-emitting guards never call operator_override_note "
        "(directly or via a same-module helper) and are not on the explicit "
        "allowlist -- either restore an override-route mention or add the guard to "
        "_NO_OVERRIDE_NOTE_ALLOWLIST with a named reason: %s" % missing
    )


def test_allowlist_entries_are_actually_registered_guards():
    """An allowlist entry naming a guard that no longer exists in the chain
    is dead config -- it would silently stop covering anything without this
    check ever telling anyone."""
    chain_names = {e.name for e in _build_chain()}
    stale = set(_NO_OVERRIDE_NOTE_ALLOWLIST) - chain_names
    assert not stale, (
        "these allowlist entries do not match any guard currently registered "
        "in dispatch._build_guard_chain -- remove or fix them: %s" % stale
    )
