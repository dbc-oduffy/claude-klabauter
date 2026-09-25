"""DoE#99: the commit-phase brief called an unreported path "the peer case"
on absence alone -- no report named it, so STOP. An executor's actual
touched-files list lives in the on-disk report FILE the brief already tells
the agent to READ, not in the terse `<STATUS>: <report path>` return line;
a wave where every executor answered with a bare `DONE: <report path>` line
left the brief nothing to reconcile against but that line, and it refused
four dirty, legitimately-DONE paths as a peer with no peer on the box.

Pin: reconciliation is against each report FILE's own touched-files list,
and "the peer case" fires only on a LIVE holder (`session-claim-cli
who-claims-path`), never on a report simply not naming a path.
"""

from coordinator_core.ops.dispatch_emit.emit import _commit_agent_call


def _brief() -> str:
    return _commit_agent_call(
        ["a.py"], "Commit wave 1", 0, ["C1"], results_var="wave1Results"
    )


def test_reconciliation_targets_the_report_file_not_the_return_line():
    brief = _brief()
    assert "report FILE" in brief
    assert "not the bare" in brief
    assert "STATUS>: <report path>" in brief
    assert "return line" in brief


def test_peer_case_requires_a_live_holder_not_mere_absence():
    brief = _brief()
    assert "session-claim-cli who-claims-path" in brief
    assert "NOT evidence of a peer by itself" in brief
    assert "LIVE holder is the peer case" in brief


def test_unclaimed_unreported_path_is_committed_not_refused():
    brief = _brief()
    assert "No live holder -> commit it" in brief
    assert "naming the path above your token line as unreported" in brief
