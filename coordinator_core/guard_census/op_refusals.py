"""Census of op refusals: one fate per ``raise *Refus*`` site under ``coordinator_core/ops``.

Row names are the ``module:Exception`` keys of ``deny_registry.op_refusal_sites``. A ``block``
row is an op that cannot proceed on the request it was handed (missing, unreadable or
inconsistent input) or a write into a publish mirror; ``redirect`` names the corrected call.
``review-floor`` is not a row: it left the table as an advisory (a ``Review-Floor-Note`` trailer).
"""

from __future__ import annotations

from typing import List

from coordinator_core.guard_census import BLOCK, Row

_OPS = "coordinator_core.ops."

#: ``(site, redirect)`` for every site whose refusal is ``malformed-request`` (op-only).
_MALFORMED = {
    "dispatch_emit.ask_brief:EmBriefRefused": "pass one readable --brief or --brief-file inside the repo root",
    "dispatch_emit.co_declared_settle:OwnHunkRefused": "re-declare the hunks against the current HEAD diff",
    "dispatch_emit.own_hunk_stage:OwnHunkRefused": "re-declare the hunks against the current HEAD diff",
    "dispatch_emit.ask_gate:SizingFireRefused": "re-fire after the run's outcome record is written",
    "dispatch_emit.op:SizingFireRefused": "pass a repo-relative baton or sizing that exists on disk",
    "dispatch_emit.sizing_fire:SizingFireRefused": "pass a sizing-object under state/sizings/ that parses",
    "dispatch_emit.close_without_script:CloseRefusal": "pass the run's own task-output file and a tracked plan path",
    "dispatch_emit.emit:FidelityBarRefusalError": "prep the plan until the fidelity gate reports prepped; the gate runs only when its env marker is set",
    "dispatch_emit.pipeline_compose:_ScriptCapRefused": "resize the run: the script is over the Workflow byte cap",
    "_research_sizing:ResearchSizingRefused": "pass a sizing the research drop can edit",
    "dod_floor_ratchet:RatchetRefused": "write a floor at or above the stored fail_under",
    "grind_ops:VerifyRefusalError": "pass a readable extraction manifest",
    "review_stamp:MintRefusal": "supply the missing judge result, build/test record or plan id the message names",
}

ROWS: List[Row] = [
    *(Row(_OPS + site, BLOCK, redirect, "malformed-request") for site, redirect in _MALFORMED.items()),
    Row(
        _OPS + "queue_promote:_OssMirrorWriteRefused", BLOCK,
        "point repos.content_root at a working tree, never the OSS publish mirror",
        "irreversible-external",
    ),
]
