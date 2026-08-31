"""Baseline falsifier -- prime exit criterion, CONTENT clause only.

The prime exit criterion is unchanged and is not restated here. It is SPLIT by
falsifiability class, and this probe is the instrument for exactly one half:

  CONTENT CLAUSE (this probe): "the record that reaches [the Group EM] names
  what that peer owes -- or names that no ledger exists for it, which is not
  the same claim -- so a tick that sends nothing must record which obligation
  it declined and why, and cannot close on an empty result."

  INITIATION CLAUSE (NOT this probe): "A claude-klabauter peer that has stopped reaches
  the Group EM without the Group EM having taken any action to look."

Why the initiation clause is not here, so nobody adds a fourth leg for it:
it is not falsifiable by any in-repo oracle, static OR dynamic. Statically the
initiator is the harness `Monitor` tool, armed out of process, with no part of
it in this tree. Dynamically -- the part that settles it -- any in-repo test
that exercises the delivery path must itself invoke it. The test IS the look.
A probe can prove `transitions()` emits the right line; it cannot prove nothing
had to ask for that line, because "nobody asked" is a negative existential over
an out-of-process actor. One layer further down (claude-klabauter-b7's point):
the watcher is a runnable whose STDOUT the harness turns into notifications, so
even a test that spawned the runnable would be observing stdout, not delivery.
The initiation clause is discharged by recorded observation instead -- see
`prime_exit_criterion.acceptance_by_observation` in the plan.

Three restatements of the criterion failed by trying to encode both clauses in
one instrument, each drifting toward what the code could pass. Do not write a
fourth. If a leg here cannot flip, the design or the leg is wrong -- say which.

Two legs, both FALSE at baseline, both closed by this spine:

  LEG NAMES -- `obligations.for_peer(repo_root, session_id)` returns the named
  rows behind the count, not just the count. `undischarged_obligations` already
  gives a count and already distinguishes None from 0; that is shipped behaviour
  and asserting it is a regression test, not a bar. What the wake needs, and
  what nothing exposes today, is WHICH obligations. Absence of the module or the
  function reads FALSE, which is the honest baseline state.

  LEG DECLINE -- an empty-result tick cannot close silently. Runs the digest
  builder against a roster with zero candidate peers and checks the returned
  structure records which obligation it declined and why, rather than being a
  bare empty result indistinguishable from "nothing to report".

Run: python coordinator_core/ops/tests/_falsifier_stopped_peer_reaches_gem.py
In-process only: no subprocess spawn, direct function calls, no source scan.
"""
from __future__ import annotations

import pathlib
import sys
import tempfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))


def leg_names_obligations_are_named_not_counted() -> dict:
    """`for_peer` returns the rows behind the count, and preserves the
    None-vs-empty distinction `undischarged_obligations` established: a peer
    with NO ledger is not the same claim as a peer whose ledger is empty.
    """
    try:
        from coordinator_core.group_em import obligations
    except ImportError as exc:
        return {"available": False, "why": f"module absent: {exc}", "named": False}

    for_peer = getattr(obligations, "for_peer", None)
    if for_peer is None:
        return {
            "available": False,
            "why": "coordinator_core.group_em.obligations exists but exposes no for_peer",
            "named": False,
        }

    from coordinator_core.group_em import send_pass

    with tempfile.TemporaryDirectory() as tmp:
        no_ledger = for_peer(tmp, "sess-no-ledger-0000000000000000")

        sid = "sess-has-ledger-000000000000000"
        share_dir = pathlib.Path(send_pass._session_share_dir(tmp, sid))
        share_dir.mkdir(parents=True, exist_ok=True)
        (share_dir / send_pass._LEDGER_FILENAME).write_text(
            '{"obligation_id": "ob-1", "seam": "review", '
            '"next_action": "ask what it needs", "discharged_at": null, "fired": false}\n'
            '{"obligation_id": "ob-2", "seam": "merge", '
            '"next_action": "reconcile the branch", "discharged_at": null, "fired": false}\n',
            encoding="utf-8",
        )
        with_ledger = for_peer(tmp, sid)

    rows_are_named = (
        isinstance(with_ledger, list)
        and len(with_ledger) == 2
        and all(isinstance(r, dict) and r.get("next_action") for r in with_ledger)
    )
    absence_preserved = no_ledger is None

    return {
        "available": True,
        "no_ledger_result": no_ledger,
        "with_ledger_result": with_ledger,
        "named": bool(rows_are_named and absence_preserved),
    }


def leg_decline_empty_tick_records_why() -> dict:
    from coordinator_core.group_em import send_pass

    with tempfile.TemporaryDirectory() as tmp:
        digest = send_pass.build_send_digest(
            tmp, roster=[], caller_session_id="sess-caller-0000000000000000"
        )

    declined = digest.get("declined")
    records_why = bool(declined) and all(
        isinstance(d, dict) and d.get("obligation") and d.get("reason") for d in declined
    )
    return {"digest": digest, "records_decline_on_empty_roster": records_why}


def main() -> None:
    print("=== LEG NAMES: obligations are named, not merely counted ===")
    names = leg_names_obligations_are_named_not_counted()
    print(names)
    leg_names_true = names["named"]

    print("\n=== LEG DECLINE: an empty-roster tick records what it declined and why ===")
    decline = leg_decline_empty_tick_records_why()
    print(decline)
    leg_decline_true = decline["records_decline_on_empty_roster"]

    print("\n=== VERDICT (content clause only; initiation clause is not testable here) ===")
    print(f"leg_names_obligations_are_named: {leg_names_true}")
    print(f"leg_decline_empty_tick_records_why: {leg_decline_true}")
    print(f"content_clause_true: {leg_names_true and leg_decline_true}")


if __name__ == "__main__":
    main()
