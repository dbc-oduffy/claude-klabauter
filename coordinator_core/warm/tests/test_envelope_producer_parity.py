"""coordinator_core.warm.tests.test_envelope_producer_parity -- the parity
gate the warm request envelope never had: `coordinator_core/warm/client.py
:: _try_warm_dispatch_inner` (Python) and `coordinator_core/warm/door/
door.c` (C) each build the same JSON-RPC envelope by hand, independently,
and nothing asserted they stamp the same field set.

Why this suite exists at all. `_session_id` went missing from the C side
while the Python side had carried it since the seam was built -- a caller
through the native door resolved to whoever spawned the resident server,
not the caller, and passed a possession gate (`basis=author`) on a
stranger's identity
(`state/bug-backlog/2026-08-29-the-warm-door-s-exe-route-stamps-the-ser-47373b19c77e.yaml`).
That instance is fixed. The class is not: at the time this module was
written `client.py` stamps four envelope-level fields and `door.c` stamps
three, and nothing but a human diff would have noticed either divergence.
Spec: docs/plans/2026-08-30-the-warm-envelope-s-two-producers-cannot.md § C1
Raised by: cross-repo/inbox/2026-08-30-doe-claude-em-warm-envelope-has-two-producers-and-no-parity-check.md

FIVE LEGS, none of which may be dropped.

  LEG 1 (`test_envelope_field_sets_match`) -- the two field sets, read by
  DIFFERENT means from the two sources (`ast` on the Python side, an
  anchored text scan on the C side), compared per field. A key given as an
  attribute on the Python side (`publish_lane.PUBLISH_LANE_FIELD`,
  `settings_home_claim.SETTINGS_HOME_FIELD`) is resolved by importing that
  module and reading the constant, never by hardcoding the string here --
  hardcoding it would make this test agree with itself the moment the
  constant changed instead of the source.

  LEG 2 (`test_the_exception_list_is_closed_and_pinned`) -- `_publish_lane`
  is the one known, DELIBERATE one-sided field
  (`publish_lane.PUBLISH_LANE_OPS` is a closed list of one member,
  `ceremony.scoped_git_commit`, killed 2026-08-23 under DR-344 and absent
  from `coordinator_core/ops/` -- the lane has no reachable op, so the
  door omitting the field costs nothing today, and adding it to `door.c`
  would widen which route can enter the one lane the 2s brightline does
  not govern, on a dead roster, for no caller). The exception set is
  asserted to be EXACTLY `{"_publish_lane"}`; a second one-sided field
  must fail here rather than being silently absorbed into a list that
  grows without a reader.

  LEG 3 (`test_the_parity_check_discriminates`) -- the parity predicate
  itself, run against synthetic fixture strings (never the real sources)
  where one side gains a field the other lacks, and required to go red
  WITH THE FIELD NAMED in the failure message. A parity assertion written
  against an already-green pair proves the harness runs, never that it
  discriminates.

  LEG 4 (`test_a_second_one_sided_field_is_not_silently_absorbed`) --
  the other half of LEG 2, run against synthetic field sets rather than
  the real sources: an UNPINNED one-sided field must fail the parity
  predicate, and the one pinned exception must not be swept up alongside
  it.

  LEG 5 (`test_c_field_scan_is_anchored_not_whole_file`) -- the
  `entrypoint` negative-spec: a field that lives inside `params`, not at
  the envelope level, must not leak into the C-side scan, proving the
  region anchor excludes it rather than happening not to match it today.

Negative-spec (RAG-bait): this suite does NOT spawn a subprocess, does NOT
invoke `door.exe`, does NOT start the warm engine, and does NOT import the
op registry -- both field sets are static facts about two source files on
disk, read as text/AST, never observed by running either producer.

Bar matched: `coordinator_core/warm/tests/test_entrypoint_argv_route_
parity.py` -- its per-item-never-sampled discipline and its pinned failing
leg are the standard this module holds itself to, and its module docstring
is the shape this one follows.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ENGINE_ROOT = Path(__file__).resolve().parents[3]
_CLIENT_PY = _ENGINE_ROOT / "coordinator_core" / "warm" / "client.py"
_DOOR_C = _ENGINE_ROOT / "coordinator_core" / "warm" / "door" / "door.c"
#: The POSIX twin. It is a THIRD producer of the same envelope, and it was
#: outside this guard until 2026-08-30: C1b widened `client.py` and `door.c` to
#: `_caller` and retired the bare `_session_id` key, and `door_posix.c` kept
#: stamping the retired key -- a POSIX caller's identity dropped on the floor by
#: a server that no longer reads it, with every leg this guard DID compare
#: agreeing with itself. A parity guard that covers two of three producers
#: reports green for exactly the drift it exists to catch.
_DOOR_POSIX_C = _ENGINE_ROOT / "coordinator_core" / "warm" / "door" / "door_posix.c"

_INNER_FUNC_NAME = "_try_warm_dispatch_inner"

#: `_publish_lane` is a recorded, deliberate asymmetry, not a defect this
#: module exists to flag -- see module docstring LEG 2. Any other one-sided
#: field is unpinned and must fail `test_envelope_field_sets_match`.
_KNOWN_ONE_SIDED_FIELDS = frozenset({"_publish_lane"})


def _resolve_key(node: ast.expr, local_str_bindings: "dict[str, str] | None" = None) -> "str | None":
    """The literal field name a dict key or subscript index names, or None
    if it is not a field-stamping key at all (e.g. the `**msg` spread
    itself, which contributes no NAME this reader can enumerate -- `msg`'s
    own fields are the caller's JSON-RPC payload, not something either
    producer stamps).

    A string constant resolves directly. An attribute access
    (`publish_lane.PUBLISH_LANE_FIELD`, `settings_home_claim.
    SETTINGS_HOME_FIELD`) resolves by IMPORTING the named module and
    reading the constant off it -- never by hardcoding the literal string,
    which would make this test agree with itself when the constant
    changes rather than with the source it is reading. A bare name
    (`field = "_x"; request[field] = ...`) resolves against
    `local_str_bindings`, the function's own simple string-constant local
    assignments -- never a hardcoded guess at what the variable holds.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name) and local_str_bindings is not None:
        return local_str_bindings.get(node.id)
    if isinstance(node, ast.Attribute):
        if isinstance(node.value, ast.Name):
            import importlib

            module = None
            for candidate in (
                f"coordinator_core.warm.{node.value.id}",
                f"coordinator_core.{node.value.id}",
            ):
                try:
                    module = importlib.import_module(candidate)
                except ModuleNotFoundError:
                    continue
                if hasattr(module, node.attr):
                    break
            if module is None or not hasattr(module, node.attr):
                return None
            value = getattr(module, node.attr)
            if isinstance(value, str):
                return value
    return None


def _local_str_bindings(func: ast.FunctionDef) -> "dict[str, str]":
    """Simple `name = "literal"` local assignments inside `func`, so a
    variable-bound subscript key (`f = "_x"; request[f] = ...`) can be
    resolved against the SAME function's own source rather than left
    opaque. Deliberately narrow: only a bare `Name` target and a string
    `Constant` value qualify -- anything else (tuple unpack, an f-string,
    a call result) is not a binding this reader trusts itself to read."""
    bindings: dict[str, str] = {}
    for node in ast.walk(func):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            bindings[node.targets[0].id] = node.value.value
    return bindings


def _unresolved_key_error(where: str, node: ast.AST) -> AssertionError:
    """The load-bearing failure mode this module exists to have: a field
    stamp this reader could not name is a GAP in the comparison, not an
    absence from it -- raised loud, naming the source line and the
    unresolved expression, rather than silently dropped from
    `python_fields` the way an unrecognised shape would be if this reader
    only ever added what it understood."""
    try:
        expr = ast.unparse(node)
    except Exception:
        expr = repr(node)
    lineno = getattr(node, "lineno", "?")
    return AssertionError(
        f"client.py:{lineno}: {where} names a field this reader cannot "
        f"resolve ({expr!r}) -- a field stamp this test cannot see is a "
        "hole in the parity check, not a field to skip"
    )


def _python_envelope_fields() -> set[str]:
    """The envelope-level fields `_try_warm_dispatch_inner` stamps onto
    `request`, read from `client.py`'s OWN AST -- never by importing and
    calling the function, which this module's negative-spec forbids
    (no engine start) and which would also require a live pipe target this
    suite does not have.

    Four shapes stamp a field, all walked: the `{**msg, "_engine_token":
    token}` dict-literal key, a `request[<key>] = ...` subscript
    assignment, `request.setdefault(<key>, ...)`, and
    `request.update({...})`. `msg`'s own spread contributes nothing
    nameable here (see `_resolve_key`) and is correctly excluded -- its
    fields are the caller's own JSON-RPC payload, not something this
    function stamps.

    Any of the four whose field name cannot be resolved -- a computed key,
    a call result, an `.update()` argument that isn't a literal dict --
    RAISES rather than silently dropping the field from the comparison;
    see `_unresolved_key_error`. `ast.walk(func)` sweeps the whole
    function body, not just assignments to `request`, so an unrelated
    dict literal elsewhere in the same function would also be swept in --
    latent today (the function has no such dict); see F5 in the review
    that added this fail-loud behaviour for why that's an accepted,
    documented tradeoff rather than a restructure.
    """
    tree = ast.parse(_CLIENT_PY.read_text(encoding="utf-8"), filename=str(_CLIENT_PY))
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == _INNER_FUNC_NAME
    )
    return _python_envelope_fields_from_func(func)


def _python_envelope_fields_from_func(func: ast.FunctionDef) -> set[str]:
    """The AST-walk body of `_python_envelope_fields`, factored out so a
    synthetic fixture function can be run through the SAME resolution and
    fail-loud logic the real source is checked with (see
    `test_a_setdefault_stamp_with_unresolvable_key_fails_loud`)."""
    local_bindings = _local_str_bindings(func)
    fields: set[str] = set()
    # ENVELOPE-LEVEL ONLY, the mirror of `_c_envelope_fields`'s depth rule.
    # `request["_caller"] = {...}` (C1b) stamps ONE envelope field whose value
    # is a nested object; an unqualified `ast.Dict` walk would additionally
    # report that object's own members -- `pid`, `session_id`, `cwd` -- as
    # envelope fields door.c never stamps at that level, failing the parity leg
    # on a difference that is not one. The member keys are still checked, by
    # `test_caller_object_mirrors_the_caller_context_dataclass` in
    # test_envelope_carries_caller_identity.py, against the dataclass they
    # serialise -- so skipping them here drops no coverage.
    # Allowlist, not a denylist: the ONLY dict literal whose keys are envelope
    # fields is the one bound to `request` itself. A denylist ("skip the dict
    # assigned to request[...]") missed `resolve_caller_context({"session_id":
    # ...})` -- a dict that is a call ARGUMENT, neither the envelope nor a
    # stamped value -- and silently readmitted its key.
    envelope_dicts = {
        id(node.value)
        for node in ast.walk(func)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Dict)
        and any(isinstance(t, ast.Name) and t.id == "request" for t in node.targets)
    }
    for node in ast.walk(func):
        if isinstance(node, ast.Dict):
            if id(node) not in envelope_dicts:
                continue
            for key in node.keys:
                if key is None:  # the `**msg` spread itself
                    continue
                resolved = _resolve_key(key, local_bindings)
                if resolved is None:
                    raise _unresolved_key_error("a dict-literal key", key)
                fields.add(resolved)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "request"
                ):
                    resolved = _resolve_key(target.slice, local_bindings)
                    if resolved is None:
                        raise _unresolved_key_error("a `request[...] =` assignment", target.slice)
                    fields.add(resolved)
        elif isinstance(node, ast.Call):
            if not (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
                    and node.func.value.id == "request"):
                continue
            if node.func.attr == "setdefault" and node.args:
                resolved = _resolve_key(node.args[0], local_bindings)
                if resolved is None:
                    raise _unresolved_key_error("a `request.setdefault(...)` call", node.args[0])
                fields.add(resolved)
            elif node.func.attr == "update" and node.args:
                arg = node.args[0]
                if not isinstance(arg, ast.Dict):
                    raise _unresolved_key_error(
                        "a `request.update(...)` call whose argument is not a literal dict", arg
                    )
                for key in arg.keys:
                    if key is None:
                        continue
                    resolved = _resolve_key(key, local_bindings)
                    if resolved is None:
                        raise _unresolved_key_error("a `request.update({...})` key", key)
                    fields.add(resolved)
    return fields


#: Anchors the C-side scan to the envelope-level region of the request
#: builder -- everything AFTER `params` closes and BEFORE the envelope's
#: own closing brace. Deliberately excludes `params`'s own body: the
#: `entrypoint` field lives there, is a JSON-RPC PARAM (mirrored on the
#: Python side by the caller's own payload, not by `_try_warm_dispatch_
#: inner`), and its `,\"entrypoint\":\"` literal would otherwise match the
#: same scan pattern as a phantom envelope-level field. Anchoring here is
#: what `test_the_population_is_the_live_allowlist`'s sibling module calls
#: "an unrelated string elsewhere in the file cannot contribute a phantom
#: field" -- applied to a region within the SAME file, not just the file
#: boundary.
#: Anchored on the `_engine_token`-closing append CALL ITSELF rather than the
#: comment sentence above it: a comment survives a code move and keeps matching,
#: so a comment anchor can silently scan the wrong slice instead of failing.
#: Zero-width lookahead, so the append it anchors on stays INSIDE the region.
_C_REGION_START = r"(?=" + re.escape(
    'req_ok &= buf_append_cstr(&req, "},\\"_engine_token\\":\\"");'
) + r")"
_C_REGION_END = r'req_ok &= buf_append_cstr\(&req, "\}\\n"\);'

#: The POSIX twin's own start anchor. It opens the same envelope one character
#: differently (`"\"},\"_engine_token\":\""` -- it closes a string as well as
#: `params`), so it needs its own anchor rather than a loosened shared one: a
#: regex widened until it matched both would also match a region it should have
#: refused, which is how an anchor stops anchoring.
_C_POSIX_REGION_START = r"(?=" + re.escape(
    'req_ok &= buf_append_cstr(&req, "\\"},\\"_engine_token\\":\\"");'
) + r")"

#: A `,\"<field>\":` literal passed to `buf_append_cstr` -- the shape every
#: envelope-level field append in `door.c` takes, verified against all
#: three known appends (`_engine_token`, `_settings_home`, `_session_id`).
_C_FIELD_PATTERN = re.compile(r',\\"([A-Za-z_][A-Za-z0-9_]*)\\":')


def _c_envelope_region(
    source: str, *, start_anchor: str | None = None, label: str = "door.c"
) -> str:
    """The envelope-level slice of `door.c`'s request-building region,
    located by anchored text scan (never by line number, which drifts).
    Raises with a clear message if either anchor goes missing -- a
    disappeared anchor means the region moved or was rewritten, and this
    module must not silently scan the wrong slice or the whole file.
    """
    start_match = re.search(start_anchor or _C_REGION_START, source)
    if start_match is None:
        raise AssertionError(
            f"{label}: could not find the envelope-region start anchor "
            f"({start_anchor or _C_REGION_START!r}) -- {label}'s request builder "
            "moved or was rewritten; update this test's anchor, do not remove it"
        )
    end_match = re.search(_C_REGION_END, source[start_match.end():])
    if end_match is None:
        raise AssertionError(
            f"{label}: could not find the envelope-region end anchor "
            f"({_C_REGION_END!r}) after the start anchor -- {label}'s "
            "request builder moved or was rewritten"
        )
    region_start = start_match.end()
    region_end = region_start + end_match.end()
    return source[region_start:region_end]


#: Every `&req`-targeted append call in the region, as (function, argument).
#: The structural pass below classifies all of them; matching only the field
#: shape would let an unrecognised call be skipped in silence, which is the
#: failure this module exists to prevent.
_C_APPEND_CALL = re.compile(r"(\w+)\(&req,\s*([^;]+?)\)\s*;")

#: The append functions `door.c` legitimately uses inside the envelope region.
#: `buf_append_json_escaped` only ever writes a VALUE, never a field name.
_C_APPEND_FUNCS = frozenset({"buf_append_cstr", "buf_append_json_escaped"})

#: String literals in the region that carry no field name: the closing quote of
#: a value, the close brace of a NESTED envelope-level object (`_caller`, added
#: by C1b of docs/plans/2026-08-30-every-op-runs-in-the-callers-environment.md),
#: and the envelope's own terminator. Anything else quoted must parse as a field
#: or stop the run.
_C_KNOWN_NON_FIELD_LITERALS = frozenset({'\\"', "}", "}\\n"})

#: Brace depth at which a field name is ENVELOPE-level. The region is anchored
#: to OPEN at envelope level (its first append closes `params` and writes
#: `_engine_token` beside it), so envelope members sit at 0 and a member of the
#: nested `_caller` object sits at 1. Depth is floored at 0 for the same reason:
#: a `}` closing an object opened BEFORE the region -- `params`, in that very
#: first append -- is outside this scan's accounting and must not push its
#: envelope-level siblings below the floor.
_C_ENVELOPE_DEPTH = 0


def _brace_delta(text: str) -> int:
    """Net object nesting the C string literal `text` opens (`+`) or closes.

    Only unescaped structural braces count. `door.c` writes its JSON by hand,
    so every brace in these literals IS structural -- there is no `{` inside a
    quoted VALUE here (values go through `buf_append_json_escaped`, which this
    reader skips outright)."""
    return text.count("{") - text.count("}")


def _c_envelope_fields(region: str) -> set[str]:
    """Every envelope-level field `door.c` appends within `region` -- an
    anchored text scan, deliberately a different reading technique than
    `_python_envelope_fields_from_func`'s AST walk, so the two field sets
    are derived by different means from the same class of fact and cannot
    agree with themselves by construction.

    Structural, not pattern-only. Every `&req`-targeted call in the region
    is classified; one this reader does not recognise -- an unknown append
    function, or a string literal that is neither a field nor a pinned
    non-field -- RAISES rather than being passed over. A field append
    refactored out of the inline `buf_append_cstr(&req, ",\\"_x\\":\\"")`
    shape into a shared helper would otherwise vanish from the comparison
    with no failure, loud or otherwise, which is exactly the silence this
    module exists to break.
    """
    fields: set[str] = set()
    depth = 0
    for func_name, raw_arg in _C_APPEND_CALL.findall(region):
        if func_name not in _C_APPEND_FUNCS:
            raise AssertionError(
                f"door.c: unclassified &req-targeted call {func_name}(&req, "
                f"{raw_arg.strip()}) in the envelope region -- not "
                f"{sorted(_C_APPEND_FUNCS)!r}. If this is a field-append helper, "
                "this reader cannot see the field it writes; teach it the shape "
                "rather than widening the pin"
            )
        if func_name == "buf_append_json_escaped":
            continue
        arg = raw_arg.strip()
        if not arg.startswith('"'):
            continue
        literal = arg[1:-1] if arg.endswith('"') else arg[1:]
        matched = list(_C_FIELD_PATTERN.finditer(literal))
        if matched:
            # ENVELOPE-LEVEL ONLY. `_caller` (C1b) is a NESTED object, so a
            # flat scan would report its member keys -- `pid`, `session_id` --
            # as envelope fields the Python producer never stamps, failing the
            # parity leg on a difference that is not one. A field belongs to
            # whichever object is open where its name appears, so depth is
            # read at the field's own OFFSET, never as a whole-literal count:
            # the envelope's own opening `{` sits in the same literal as its
            # first fields, and a per-literal count would push those fields to
            # the wrong side of it.
            for m in matched:
                at = max(0, depth + _brace_delta(literal[: m.start()]))
                if at == _C_ENVELOPE_DEPTH:
                    fields.add(m.group(1))
            depth = max(0, depth + _brace_delta(literal))
            continue
        if literal in _C_KNOWN_NON_FIELD_LITERALS:
            depth = max(0, depth + _brace_delta(literal))
            continue
        raise AssertionError(
            f"door.c: string literal {literal!r} appended to the envelope "
            "region is neither a field nor one of the pinned non-field "
            f"literals ({sorted(_C_KNOWN_NON_FIELD_LITERALS)!r}); update the "
            "pin or the field pattern deliberately, never by widening until "
            "this passes"
        )
    return fields


def _parity_mismatches(python_fields: set[str], c_fields: set[str], exceptions: frozenset[str]) -> list[str]:
    """The parity predicate under test in all three legs: per-field,
    never a count comparison, and never one that silently drops a field
    named in `exceptions` from ONE side only -- a field claimed as an
    exception is still checked to be one-sided, not merely uncompared."""
    mismatches = []
    for field in sorted(python_fields | c_fields):
        in_python = field in python_fields
        in_c = field in c_fields
        if in_python and in_c:
            continue
        if field in exceptions:
            continue
        side = "client.py" if in_python else "door.c"
        missing_from = "door.c" if in_python else "client.py"
        mismatches.append(f"{field!r}: stamped by {side}, missing from {missing_from}")
    return mismatches


def test_envelope_field_sets_match():
    """THE gate: every envelope-level field either side stamps is stamped
    by both, or is named in the closed, justified exception list -- never
    sampled, never a count."""
    python_fields = _python_envelope_fields()
    c_source = _DOOR_C.read_text(encoding="utf-8")
    c_fields = _c_envelope_fields(_c_envelope_region(c_source))

    mismatches = _parity_mismatches(python_fields, c_fields, _KNOWN_ONE_SIDED_FIELDS)
    assert not mismatches, (
        "warm envelope producers disagree on field set:\n" + "\n".join(mismatches)
    )


def test_posix_door_envelope_field_set_matches_too():
    """THE THIRD PRODUCER. `door_posix.c` builds the same envelope for the POSIX
    named-pipe leg, and until 2026-08-30 no parity leg read it: C1b widened
    `client.py` and `door.c` to `_caller` and retired the bare `_session_id`
    key, `door_posix.c` kept stamping the retired key, and every comparison
    this module DID make still agreed. A POSIX caller's identity would have
    been dropped on the floor by a server that no longer reads that key, with
    this file green throughout.

    Same predicate, same exception list -- a producer is either compared or it
    is unguarded, and there is no third state."""
    python_fields = _python_envelope_fields()
    posix_source = _DOOR_POSIX_C.read_text(encoding="utf-8")
    posix_fields = _c_envelope_fields(
        _c_envelope_region(
            posix_source, start_anchor=_C_POSIX_REGION_START, label="door_posix.c"
        )
    )

    mismatches = _parity_mismatches(python_fields, posix_fields, _KNOWN_ONE_SIDED_FIELDS)
    assert not mismatches, (
        "warm envelope producers disagree on field set (POSIX door):\n"
        + "\n".join(mismatches)
    )


def test_both_doors_agree_with_each_other():
    """The two door twins ship as one wire format. Comparing each against the
    Python producer separately would let a field they BOTH omit pass unnoticed
    only if the exception list hid it; comparing them to each other names the
    Windows/POSIX split directly, which is the `multi-os-first-class` claim
    this repo makes about every transport."""
    win_fields = _c_envelope_fields(_c_envelope_region(_DOOR_C.read_text(encoding="utf-8")))
    posix_fields = _c_envelope_fields(
        _c_envelope_region(
            _DOOR_POSIX_C.read_text(encoding="utf-8"),
            start_anchor=_C_POSIX_REGION_START,
            label="door_posix.c",
        )
    )
    assert win_fields == posix_fields, (
        "door.c and door_posix.c stamp different envelope field sets: "
        f"windows-only={sorted(win_fields - posix_fields)}, "
        f"posix-only={sorted(posix_fields - win_fields)}"
    )


def test_the_exception_list_is_closed_and_pinned():
    """`_KNOWN_ONE_SIDED_FIELDS` is exactly `{"_publish_lane"}` -- pinned so
    a second one-sided field cannot be added to the exception set without
    this failing and forcing a reviewer to look at it, and so a field
    silently REMOVED from the exception set (making it start being
    enforced) is equally visible."""
    assert _KNOWN_ONE_SIDED_FIELDS == frozenset({"_publish_lane"}), (
        "the exception list changed without this pin being updated -- "
        f"got {sorted(_KNOWN_ONE_SIDED_FIELDS)}. `_publish_lane` is the "
        "one field this module accepts as one-sided (publish_lane."
        "PUBLISH_LANE_OPS is a closed list of one, ceremony.scoped_git_"
        "commit, killed under DR-344); any other entry is undocumented"
    )

    # `_publish_lane` is genuinely one-sided against the REAL sources today
    # -- if a future change adds it to door.c, the exception silently stops
    # doing anything (the field would already agree) and this leg should
    # keep passing; but it must never be exempting an ALREADY-two-sided
    # field from a check that would otherwise catch a real regression on
    # the *other* side. Assert what the exception is actually excusing.
    python_fields = _python_envelope_fields()
    c_source = _DOOR_C.read_text(encoding="utf-8")
    c_fields = _c_envelope_fields(_c_envelope_region(c_source))
    for field in _KNOWN_ONE_SIDED_FIELDS:
        in_python = field in python_fields
        in_c = field in c_fields
        assert in_python != in_c, (
            f"{field!r} is exempted as a known one-sided field, but is "
            f"present on both sides (client.py={in_python}, door.c={in_c}) "
            "-- the exception is no longer excusing anything real; "
            "consider removing it from the pinned set"
        )


def test_a_second_one_sided_field_is_not_silently_absorbed():
    """Half of LEG 2: a one-sided field NOT in the exception list must fail
    the parity predicate rather than being quietly waved through the way an
    open-ended or unpinned exception mechanism would."""
    python_fields = {"_engine_token", "_session_id", "_publish_lane", "_new_field_nobody_pinned"}
    c_fields = {"_engine_token", "_session_id"}

    mismatches = _parity_mismatches(python_fields, c_fields, _KNOWN_ONE_SIDED_FIELDS)
    assert any("_new_field_nobody_pinned" in m for m in mismatches), (
        "a second, unpinned one-sided field was absorbed rather than "
        f"failing the predicate: {mismatches!r}"
    )
    assert not any("_publish_lane" in m for m in mismatches), (
        "the pinned exception field was NOT excused by the predicate"
    )


def test_the_parity_check_discriminates():
    """LEG 3, pinned in the test rather than performed once at authoring
    time: the SAME predicate (`_parity_mismatches`), run against synthetic
    fixture strings -- never the real sources -- where one side gains a
    field the other lacks, must go red WITH THE FIELD NAMED.

    Without this, a green `test_envelope_field_sets_match` is consistent
    with a predicate that compares nothing -- exactly the failure mode
    `test_entrypoint_argv_route_parity.py`'s own pinned leg exists to rule
    out for its own gate."""
    baseline = {"_engine_token", "_session_id", "_settings_home"}

    # One side gains a field the other entirely lacks.
    python_fields = baseline | {"_extra_client_only_field"}
    c_fields = set(baseline)
    mismatches = _parity_mismatches(python_fields, c_fields, _KNOWN_ONE_SIDED_FIELDS)
    assert mismatches, "the predicate did not go red for a client-only field it should catch"
    assert any("_extra_client_only_field" in m for m in mismatches), (
        f"the predicate went red but did not name the offending field: {mismatches!r}"
    )
    assert any("client.py" in m and "door.c" in m for m in mismatches), (
        f"the predicate did not name which side stamps and which lacks the field: {mismatches!r}"
    )

    # And the mirror direction: a door-only field the client lacks.
    python_fields = set(baseline)
    c_fields = baseline | {"_extra_door_only_field"}
    mismatches = _parity_mismatches(python_fields, c_fields, _KNOWN_ONE_SIDED_FIELDS)
    assert mismatches, "the predicate did not go red for a door-only field it should catch"
    assert any("_extra_door_only_field" in m for m in mismatches), (
        f"the predicate went red but did not name the offending field: {mismatches!r}"
    )

    # A pair with no divergence at all must NOT go red -- otherwise this
    # leg would prove only that the predicate always fails, never that it
    # discriminates true divergence from none.
    assert not _parity_mismatches(set(baseline), set(baseline), _KNOWN_ONE_SIDED_FIELDS)


def test_a_setdefault_stamp_with_an_unresolvable_key_fails_loud():
    """The fail-loud path `_unresolved_key_error` exists for, actually
    fired: a synthetic function (never the real `client.py`) whose
    `request.setdefault(field, ...)` call names its field via a
    computed local (`field = _compute_field_name()`) rather than a
    string constant. `_local_str_bindings` only trusts a bare
    `name = "literal"` assignment, so `field` resolves to nothing and
    `_python_envelope_fields_from_func` must raise rather than silently
    dropping the stamp from the comparison -- see Finding 1."""
    fixture_src = (
        "def _try_warm_dispatch_inner(request):\n"
        "    field = _compute_field_name()\n"
        "    request.setdefault(field, 'x')\n"
    )
    tree = ast.parse(fixture_src, filename="<fixture>")
    func = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef))

    with pytest.raises(AssertionError, match="setdefault"):
        _python_envelope_fields_from_func(func)


def test_a_helper_shaped_c_append_fails_loud():
    """The structural-pass fail-loud path fired for real: a synthetic
    region (never real `door.c`) whose envelope-region shape is intact
    (`_engine_token`'s append is present) but one field is stamped
    through a shared helper, `append_envelope_field(&req, "_new_field",
    value)`, rather than the inline `buf_append_cstr` shape every known
    append currently takes. `_c_envelope_fields` must raise on the
    unrecognised append function rather than let the field vanish from
    the comparison with no failure -- see Finding 2."""
    region = (
        'req_ok &= buf_append_cstr(&req, "},\\"_engine_token\\":\\"");'
        'req_ok &= buf_append_json_escaped(&req, token);'
        'req_ok &= append_envelope_field(&req, "_new_field", value);'
    )

    with pytest.raises(AssertionError, match="append_envelope_field"):
        _c_envelope_fields(region)


def test_c_field_scan_is_anchored_not_whole_file():
    """Negative-spec, asserted: the `entrypoint` field, which lives INSIDE
    `params` (a JSON-RPC param, not an envelope-level field either
    producer stamps via `_try_warm_dispatch_inner`), must NOT appear in
    the anchored C-side scan -- proving the anchor excludes it rather than
    happening not to match it today."""
    c_source = _DOOR_C.read_text(encoding="utf-8")
    region = _c_envelope_region(c_source)
    assert "entrypoint" not in _C_FIELD_PATTERN.findall(region), (
        "the C-side scan picked up `entrypoint`, a params-level field -- "
        "the envelope-region anchor is not excluding it as intended"
    )
    # Sanity: `entrypoint` really is in the file (so the assertion above is
    # not vacuously true because the field was removed from door.c).
    assert '"entrypoint"' in c_source
