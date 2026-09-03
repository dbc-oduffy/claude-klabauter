"""A stored timestamp must not reach a reader without the age beside it.

WHY THIS IS A GUARD AND NOT A CONVENTION. Every durable record in this tree
stamps UTC; almost every surface a reader checks one against is local. The
offset is present in the stamp and gets misread anyway, because reading a
timestamp is not the same act as subtracting one. Measured 2026-09-02: a
publish lock stamped `19:00:02.830245+00:00` was read as seventy minutes old
and a stale-lock diagnosis was built on it before the holder was checked and
found alive at ten minutes -- three times in one session, on three different
surfaces. `coordinator_core.timestamps` closes each site it is applied to; a
guard is what stops the next site from being written bare.

WHAT IS FLAGGED. An f-string or `.format()` interpolation whose expression is
a bare `_at`-suffixed name, attribute, subscript, or `.get("..._at")` call --
the shapes a stored stamp actually reaches a renderer as. Passing the value
through `timestamps.with_age` / `with_age_date` (or any other call) is not a
bare interpolation and is never flagged, so the fix is always to wrap rather
than to suppress.

NEGATIVE SPEC -- what this deliberately does not do:

- It does not scan test modules. A test asserting on a rendering is not a
  reader-facing surface, and flagging them would bury the signal.
- It does not judge machine-read sites. A stamp written into JSON, YAML
  frontmatter, a git argument, or a persisted artifact must stay bare, and an
  age baked into a durable file is false the moment it is written. Those are
  EXEMPT with a named reason, not silently skipped.
- It does not key exemptions by line number. Line numbers in this tree are
  stale within the hour; the key is (module path, field name).

KNOWN GAP -- what this guard does NOT catch. `_bare_at_field` inspects only
the AST node sitting directly inside the interpolation. It has no data-flow
tracing, so an `_at`-suffixed value assigned to a plain-named local and then
interpolated is invisible to the walk:

    stamp = record.get("checked_at")
    msg = f"checked at {stamp}"

`stamp` is a bare `Name` that does not end in `_at`; no violation is
recorded and no `EXEMPT` entry is ever needed. This guard stops a bare `_at`
render written directly, and does not stop one reached through an
intermediate variable of any other name -- ordinary variable extraction
defeats it. Treat a clean `test_no_bare_timestamp_reaches_a_reader` run as
evidence for the direct case only, not as proof that no aliased site exists.
"""

from __future__ import annotations

import ast
import os
from typing import Optional

_ENGINE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Renders exempt from carrying an age, each with the reason it is exempt.
#: Keyed `(<path relative to coordinator_core/>, <field name>)`. A new entry
#: needs a reason of the same kind as these -- "not now" is not one.
EXEMPT: dict[tuple[str, str], str] = {
    ("claims_emit.py", "ran_at"): (
        "validation error naming a stamp that does not parse -- an age is "
        "impossible by construction of the branch that prints it"
    ),
    ("ops/gate_liveness/emit_discharge.py", "landed_at"): (
        "validation error on a malformed date string"
    ),
    ("ops/handoff_backfill_claim_stamp.py", "claimed_at"): (
        "validation error on a missing/non-string field"
    ),
    ("ops/deliverable_equivalence.py", "closed_at"): (
        "validation error -- the row is rejected for carrying the field at all"
    ),
    ("tracker_entities.py", "created_at"): (
        "validation error on a stamp that fails the ISO-8601 prefix check"
    ),
    ("session/fleet_delegation.py", "granted_at"): (
        "grant-validation errors: unparseable, or out of the window against a "
        "`now` the same message already prints"
    ),
    ("session/fleet_delegation.py", "expires_at"): (
        "grant-validation errors: unparseable, or out of the window against a "
        "`now` the same message already prints"
    ),
    ("ops/completion_ops.py", "created_at"): (
        "machine-read -- composes a `session|status|created_at` ledger entry "
        "written into a plan file"
    ),
    ("subagent_sandbox/provision_report.py", "spawned_at"): (
        "machine-read -- emits a YAML frontmatter field value"
    ),
    ("ops/normalize_env.py", "saved_at"): (
        "machine-read -- emits `SAVED_AT=` into a backup env file"
    ),
    ("ops/orphan_branch_sweep.py", "pr_merged_at"): (
        "machine-read -- composes a `git log --after=` argument"
    ),
    ("goals/reassess_krs.py", "recorded_at"): (
        "PERSISTED prose: the same provenance string is written into the goal "
        "YAML as a comment line. A relative age is true only at write time, so "
        "baking one into a durable artifact makes it wrong by tomorrow"
    ),
    ("orientation/abandoned_claim_signal.py", "claimed_at"): (
        "PERSISTED prose: the signal text is written into the orientation "
        "cache by regenerate_cache, where a computed age would go stale in "
        "place while reading as measured"
    ),
    ("group_em/watch.py", "armed_struck_at"): (
        "not a stored stamp -- minted from `time.time()` on the line above the "
        "render, so its age is trivially zero"
    ),
    ("ops/plan_status_transition.py", "override_at"): (
        "not a stored stamp -- minted at override time and printed in the same "
        "call, so its age is trivially zero"
    ),
    ("ops/plan_status_transition.py", "reopened_at"): (
        "not a stored stamp -- minted at reopen time and printed in the same "
        "call, so its age is trivially zero"
    ),
}


def _bare_at_field(node: ast.AST) -> Optional[str]:
    """The `_at` field name this expression renders bare, or None.

    A `Call` other than `.get("..._at")` returns None on purpose: that is the
    shape a `timestamps.with_age(...)` wrap takes, and every other helper a
    site might legitimately route through.
    """
    if isinstance(node, ast.Name) and node.id.endswith("_at"):
        return node.id
    if isinstance(node, ast.Attribute) and node.attr.endswith("_at"):
        return node.attr
    if isinstance(node, ast.Subscript):
        key = node.slice
        if isinstance(key, ast.Constant) and isinstance(key.value, str):
            if key.value.endswith("_at"):
                return key.value
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr == "get" and node.args:
            first = node.args[0]
            if isinstance(first, ast.Constant) and isinstance(first.value, str):
                if first.value.endswith("_at"):
                    return first.value
    return None


def _scan_module(path: str) -> list[tuple[int, str]]:
    """Every `(lineno, field)` bare timestamp render in one module."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for value in node.values:
                if isinstance(value, ast.FormattedValue):
                    field = _bare_at_field(value.value)
                    if field:
                        found.append((value.lineno, field))
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "format":
                args = list(node.args) + [kw.value for kw in node.keywords]
                for arg in args:
                    field = _bare_at_field(arg)
                    if field:
                        found.append((node.lineno, field))
    return found


# Review: overengineering-reviewer -- this walk and `scan_engine`'s were
# byte-identical in traversal/exclusion rules and parsed every non-test file
# under coordinator_core/ twice per run. One shared walk, filtered two ways,
# halves the parse cost and removes the risk of the two rules drifting apart.
def _engine_renders() -> list[tuple[str, int, str]]:
    """Every `(rel path, lineno, field)` bare-render candidate under
    `coordinator_core/`, unfiltered by `EXEMPT` -- callers decide what to do
    with a candidate that is or isn't in the table."""
    renders: list[tuple[str, int, str]] = []
    for dirpath, dirnames, filenames in os.walk(_ENGINE_ROOT):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for filename in filenames:
            if not filename.endswith(".py") or filename.startswith("test_"):
                continue
            full = os.path.join(dirpath, filename)
            rel = os.path.relpath(full, _ENGINE_ROOT).replace(os.sep, "/")
            if "/tests/" in rel or rel.startswith("tests/"):
                continue
            for lineno, field in _scan_module(full):
                renders.append((rel, lineno, field))
    return renders


def scan_engine() -> list[tuple[str, int, str]]:
    """Every unexempted bare timestamp render under `coordinator_core/`."""
    return [
        (rel, lineno, field)
        for rel, lineno, field in _engine_renders()
        if (rel, field) not in EXEMPT
    ]


def test_no_bare_timestamp_reaches_a_reader():
    violations = scan_engine()
    assert not violations, (
        "A stored timestamp is rendered without an age. A reader subtracts a "
        "UTC stamp against a local clock and invents staleness that is not "
        "there.\n"
        + "\n".join(f"  {rel}:{line} renders {field!r} bare" for rel, line, field in violations)
        + "\n\nWrap it: `coordinator_core.timestamps.with_age(<stamp>)`, or "
        "`with_age_date(<stamp>)` for a YYYY-MM-DD field.\n"
        "A machine-read or persisted site instead needs an EXEMPT entry in "
        "this module, with the reason it must stay bare."
    )


def test_every_exemption_still_names_a_real_render():
    """A stale exemption is a hole with a reason attached."""
    live = {(rel, field) for rel, _lineno, field in _engine_renders()}
    orphaned = sorted(key for key in EXEMPT if key not in live)
    assert not orphaned, (
        "EXEMPT names renders that no longer exist -- delete them, or the next "
        "bare render at that path/field is admitted silently:\n"
        + "\n".join(f"  {rel} :: {field}" for rel, field in orphaned)
    )


def test_the_guard_catches_a_bare_render():
    """The guard's own falsifier -- an unwrapped stamp must trip the walk."""
    tree = ast.parse('msg = f"checked at {results[\'checked_at\']}"')
    joined = next(n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr))
    fields = [
        _bare_at_field(v.value)
        for v in joined.values
        if isinstance(v, ast.FormattedValue)
    ]
    assert fields == ["checked_at"]

    wrapped = ast.parse(
        'msg = f"checked at {timestamps.with_age(results[\'checked_at\'])}"'
    )
    joined = next(n for n in ast.walk(wrapped) if isinstance(n, ast.JoinedStr))
    assert [
        _bare_at_field(v.value)
        for v in joined.values
        if isinstance(v, ast.FormattedValue)
    ] == [None]
