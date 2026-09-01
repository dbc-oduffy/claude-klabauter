"""Pins that a judgment-point block propagates along directive-to-directive
`depends_on` edges in `contract.apply_base.execute_directives`.

Before this pin, `depends_on` carried two unrelated meanings that were never
reconciled: `directive_gate_open` read only judgment-point ids off it, and
`order_by_depends_on` read only directive ids off it (for ordering, filtering
out everything else as "already resolved"). A directive naming a DIRECTIVE
dependency therefore had no gate at all — when that dependency was blocked at
its own judgment point and never ran, the dependent still dispatched, against a
repo state its dependency was supposed to have established.

Live case (`/merging-to-main`, 2026-09-01): `merge_assemble`'s `d7` names
`depends_on: ["d2"]`. `d2` (release-tag cut) was blocked on an unresolved
`version_bump_final`, `d7` fired regardless, raised, and the run returned
`APPLY_EXIT_PARTIAL_MUTATION` — abandoning `d8` and, more seriously, the
`d_grant_handback` that revokes the ceremony's Tier-U write grant.

Negative spec — what this does NOT change:
    - `unresolved_judgment_points` still reports only ORIGINATING judgment
      points, never an intermediate directive id. An operator resolves the
      chain with `--decisions`, and a directive id is not something
      `--decisions` accepts.
    - A directive whose `depends_on` names a directive that LANDED (including
      one that landed `already_satisfied`) is unaffected and still fires.
    - A run with no blocked directives dispatches byte-identically.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from coordinator_core.contract import apply_base


def _judgment_point(point_id: str, resolves: list[str]) -> dict[str, Any]:
    return {
        "id": point_id,
        "question": f"resolve {point_id}?",
        "dispositions": [{"value": "go", "resolves": resolves}],
        "round_trip": "terminal",
    }


def _recorder() -> tuple[list[str], dict[str, Any]]:
    """A dispatch table whose every handler appends its own cli name to a
    shared list — the ONLY evidence of what actually dispatched, independent
    of what the report claims."""
    dispatched: list[str] = []

    def make(name: str):
        def handler(args: list[str], repo_root: Path) -> dict[str, Any]:
            dispatched.append(name)
            return {"cli": name, "returncode": 0}

        return handler

    table = {name: make(name) for name in ("upstream", "dependent", "unrelated")}
    return dispatched, table


def test_dependent_of_a_judgment_blocked_directive_does_not_dispatch(tmp_path: Path) -> None:
    dispatched, table = _recorder()
    directives = [
        {
            "id": "d_up",
            "cli": "upstream",
            "args": [],
            "depends_on": ["j_gate"],
            "already_satisfied": False,
        },
        {
            "id": "d_dep",
            "cli": "dependent",
            "args": [],
            "depends_on": ["d_up"],
            "already_satisfied": False,
        },
        {
            "id": "d_free",
            "cli": "unrelated",
            "args": [],
            "depends_on": None,
            "already_satisfied": False,
        },
    ]
    exit_code, report = apply_base.execute_directives(
        directives,
        [_judgment_point("j_gate", ["d_up"])],
        tmp_path,
        table,
        decisions={},
    )

    assert exit_code == apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT
    # The upstream is blocked at its judgment point; the dependent inherits
    # that block; the independent directive is untouched by either.
    assert dispatched == ["unrelated"]
    assert report["landed"] == ["d_free"]
    assert {r["id"] for r in report["results"]} == {"d_free"}
    # Only the originating judgment point is reported — `d_up` is not
    # something `--decisions` can resolve.
    assert report["unresolved_judgment_points"] == ["j_gate"]


def test_block_propagates_transitively(tmp_path: Path) -> None:
    dispatched, table = _recorder()
    directives = [
        {
            "id": "d_up",
            "cli": "upstream",
            "args": [],
            "depends_on": ["j_gate"],
            "already_satisfied": False,
        },
        {
            "id": "d_mid",
            "cli": "dependent",
            "args": [],
            "depends_on": ["d_up"],
            "already_satisfied": False,
        },
        {
            "id": "d_tail",
            "cli": "unrelated",
            "args": [],
            "depends_on": ["d_mid"],
            "already_satisfied": False,
        },
    ]
    exit_code, report = apply_base.execute_directives(
        directives,
        [_judgment_point("j_gate", ["d_up"])],
        tmp_path,
        table,
        decisions={},
    )

    assert exit_code == apply_base.APPLY_EXIT_HALTED_AT_JUDGMENT
    assert dispatched == []
    assert report["landed"] == []


def test_dependent_fires_once_its_judgment_point_is_resolved(tmp_path: Path) -> None:
    dispatched, table = _recorder()
    directives = [
        {
            "id": "d_up",
            "cli": "upstream",
            "args": [],
            "depends_on": ["j_gate"],
            "already_satisfied": False,
        },
        {
            "id": "d_dep",
            "cli": "dependent",
            "args": [],
            "depends_on": ["d_up"],
            "already_satisfied": False,
        },
    ]
    exit_code, report = apply_base.execute_directives(
        directives,
        [_judgment_point("j_gate", ["d_up"])],
        tmp_path,
        table,
        decisions={"j_gate": "go"},
    )

    assert exit_code == apply_base.APPLY_EXIT_OK
    assert dispatched == ["upstream", "dependent"]
    assert report["landed"] == ["d_up", "d_dep"]


def test_dependent_of_an_already_satisfied_directive_still_fires(tmp_path: Path) -> None:
    """`already_satisfied` is a LANDING, not a block — a narrated no-op
    (an absent producer, a `--force` bypass, a deferred post-merge step)
    must not take its dependents down with it."""
    dispatched, table = _recorder()
    directives = [
        {
            "id": "d_up",
            "cli": "upstream",
            "args": [],
            "depends_on": None,
            "already_satisfied": True,
            "skipped_reason": "producer absent",
        },
        {
            "id": "d_dep",
            "cli": "dependent",
            "args": [],
            "depends_on": ["d_up"],
            "already_satisfied": False,
        },
    ]
    exit_code, report = apply_base.execute_directives(directives, [], tmp_path, table)

    assert exit_code == apply_base.APPLY_EXIT_OK
    assert dispatched == ["dependent"]
    assert report["landed"] == ["d_up", "d_dep"]
