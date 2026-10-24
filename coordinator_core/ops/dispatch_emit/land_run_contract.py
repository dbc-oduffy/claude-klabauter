"""Caller-facing contract of ``dispatch.land_run``: op name, mode discriminator, reply keys.

Imports nothing beyond the stdlib so a caller can read the contract without loading a mode module.
"""

from __future__ import annotations

from typing import Final

#: The literal ``land_run`` registers under; ``@register_op`` spells it out and a test pins the two equal.
OP_NAME: Final = "dispatch.land_run"

# Switched by C12 after DoE phase 1 widens the digest op enum.
WAKE_DIGEST_COMMIT_OP: Final = "dispatch.terminal_commit"

#: ``refused`` value of a reply to a call whose params select more than one record mode.
REFUSED_MODE_AMBIGUOUS: Final = "mode-ambiguous"

#: Mode -> module exposing ``land(worktree_root, repo_root, params)``; ``land_run`` imports it lazily.
#: ``product`` has no entry: it is ``land_run`` itself.
MODE_MODULES: Final = {
    "halted": "coordinator_core.ops.dispatch_emit.land_run_halt",
    "grind": "coordinator_core.ops.dispatch_emit.land_run_grind",
    "fanout": "coordinator_core.ops.dispatch_emit.land_run_fanout",
}

#: Keys a caller reads from a landed reply. Refusal replies add their own detail keys (``ledger_failures``,
#: ``blob_drift``, ``integrity``, ``nothing_to_commit`` ...) that are not listed; no test bounds a reply to this set.
REPLY_KEYS: Final = frozenset({
    "committed",
    "sha",
    "already_landed",
    "error",
    "refused",
    "run_artifacts",
    "run_artifact_residue",
    "engine_residue",
    "close",
    "fanout",
    "siblings",
    "sizing_shipped",
    "stranded",
    "peer_held_left",
    "incomplete_reasons",
    "next",
    "receipts",
    "review_stamp",
    "review_stamp_refusal",
    "criterion_status",
    "plan_status",
    "plan_status_reason",
    "index_stale",
    "credited_chunks",
    "ledger_withheld_product",
    "exit_criterion_not_stamped",
})

#: Reply key: sorted chunk ids credited by the run and landed by this commit.
REPLY_CREDITED_CHUNKS: Final = "credited_chunks"

#: Commit-message trailer key; one line per credited row and path the landing leaves out:
#: ``Row-Unlanded: <row-id> <path> <reason>``.
UNLANDED_ROW_TRAILER: Final = "Row-Unlanded"

RECONCILE_CLAUSE: Final = (
    f"If the {OP_NAME} command replies with error code -32004 and its error.data "
    "carries reconcile_command, run reconcile_command once: a status whose state is finished with "
    "outcome result is the commit's reply, and finished without a retained result means the commit "
    f"landed. In every other -32004 case, run the same {OP_NAME} command exactly once "
    "more and return its reply verbatim; the engine reports a commit this run already landed as "
    "already_landed and never lands twice. A -32004 is never itself the commit's failure. A re-run "
    "refused run-token-in-flight is reported as that refusal, because the first call is still running."
)


class ModeAmbiguous(ValueError):
    """``params`` select more than one record mode; ``refused`` is the reply's ``refused`` value."""

    refused = REFUSED_MODE_AMBIGUOUS


def _is_halted(params: dict) -> bool:
    halted = params.get("halted_by")
    return isinstance(halted, dict) and isinstance(halted.get("halted"), str) and bool(halted["halted"])


def _is_fanout(params: dict) -> bool:
    spec = params.get("fanout")
    plans = spec.get("plans") if isinstance(spec, dict) else None
    return isinstance(plans, list) and bool(plans) and all(isinstance(p, str) and p for p in plans)


def _is_grind(params: dict) -> bool:
    grind = params.get("grind")
    return isinstance(grind, dict) and isinstance(grind.get("rows"), list)


def mode_of(params: object) -> str:
    """The landing mode of a call: ``fanout``, ``halted``, ``grind``, else ``product``.

    Raises ``ModeAmbiguous`` when params select more than one of ``halted_by``, ``fanout`` and ``grind``."""
    if not isinstance(params, dict):
        return "product"
    halted, fanout, grind = _is_halted(params), _is_fanout(params), _is_grind(params)
    if halted + fanout + grind > 1:
        raise ModeAmbiguous("params carry more than one of halted_by, fanout and grind")
    if fanout:
        return "fanout"
    if halted:
        return "halted"
    if grind:
        return "grind"
    return "product"
