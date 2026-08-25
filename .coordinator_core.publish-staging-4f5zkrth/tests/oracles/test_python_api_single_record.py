"""Oracles for exemptions whose callee is an in-repo PYTHON API rather than a CLI.

The strongest oracle shape available: the claim "this verb takes one handoff, so N handoffs need
N calls" is settled by the function's own signature, with nothing imported for side effect,
nothing spawned, and nothing executed. It cannot be flaky and it cannot be slow.

Where a claim can be pinned this way it should be, in preference to exercising a `main()` -- and
in preference to spawning the CLI wrapper around it, which is what the exempted call site does
and what the exemption is about.
"""

from __future__ import annotations

import inspect

import coordinator_core.archive_stamp as archive_stamp

#: Sequence annotations that would REFUTE a one-record claim if a verb ever grew one.
_PLURAL_HINTS = ("list[", "List[", "Sequence[", "Iterable[", "tuple[", "Tuple[", "set[")


def test_archive_stamp_verbs_take_exactly_one_handoff():
    """`reap-orphaned-in-flight-handoffs::main -> _run_archive_stamp_cli` spawns the CLI once per
    orphaned handoff. The claim is that no verb behind it accepts a batch.

    Asserted over the FIRST PARAMETER of each verb, which is the record-identity slot -- the
    same discipline the sibling-CLI oracles use, and for the same reason: a verb may take plural
    options (several notes, several flags) without being able to describe two handoffs."""
    verbs = ("stamp_shipped_in", "cs_ship_handoff", "cs_unclaim_handoff", "cs_claim_handoff")

    missing = [v for v in verbs if not callable(getattr(archive_stamp, v, None))]
    assert not missing, (
        f"archive_stamp no longer exposes {missing} -- the exemption at "
        "`reap-orphaned-in-flight-handoffs::main` is pinned to verbs that have moved or been "
        "renamed, so it is currently unverified. Re-read the module rather than dropping this."
    )

    plural: list[str] = []
    for verb in verbs:
        signature = inspect.signature(getattr(archive_stamp, verb))
        first = next(iter(signature.parameters.values()))
        annotation = str(first.annotation)
        if any(hint in annotation for hint in _PLURAL_HINTS):
            plural.append(f"{verb}({first.name}: {annotation})")

    assert not plural, (
        "these archive_stamp verbs now take a COLLECTION as their first parameter, so the "
        f"one-handoff-per-spawn exemption no longer holds: {plural}. Batch the caller and "
        "delete its `_ORACLE_CLAIMS` entry."
    )


def test_archive_stamp_verbs_name_a_single_path_parameter():
    """The complement of the above, and the reason it is not circular: the first parameter must
    actually BE the handoff, not merely be non-plural. A verb refactored to take a config object
    or a repo root first would pass a scalar-annotation check while quietly having gained a
    batch form elsewhere in its signature."""
    for verb in ("stamp_shipped_in", "cs_ship_handoff", "cs_unclaim_handoff", "cs_claim_handoff"):
        signature = inspect.signature(getattr(archive_stamp, verb))
        first = next(iter(signature.parameters.values()))
        assert first.name == "handoff_path", (
            f"archive_stamp.{verb}'s first parameter is now {first.name!r}, not 'handoff_path'. "
            "The exemption this pins claims one handoff per invocation; that claim is about a "
            "signature that has changed shape and must be re-read, not assumed to still hold."
        )
