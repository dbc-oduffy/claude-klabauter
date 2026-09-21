"""
grind_stages — the queue-grind engine's stage library: one composer per
stage kind (§ Design § Stage library,
docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md's C5 row).

Purpose: each ``compose_*`` function is a pure function of its arguments
that returns the JS text of ONE ``agent(...)`` call -- prompt, options
object, everything -- for one row of the stage-library table (``triage``,
``refute-close``, ``fix``, ``verify`` in both its agent and op forms,
``commit`` in both its per-row and ledger-only forms, and ``undo``). No
function here writes to disk, derives a graph, or schedules a batch --
``grind_compose.py`` (C7) is the sole caller that stitches these calls into
one script's edge interpreter.

This module owns the trap text every stage-kind prompt must carry:
- trap 2: only the committer stages -- every non-commit composer states the
  positive rule ("you do not stage; only the committer does") and never
  names the forbidden git verbs;
- trap 3: the commit composer's prompt names ``--declared-revert`` for every
  removed path;
- trap 4: the commit composer's prompt reconciles an indeterminate outcome
  against ``git log``/``git status`` before any retry, never retrying blind;
- trap 5: the fix composer's prompt pre-checks its locked files for peer
  dirt (``PEER_DIRTY``) before doing any work;
- the triage schema carries the t-shirt size and tradeoff-statement fields
  the engine's size/tradeoff routing gate (DR-404 § Run authority) reads.

The model literal is always ``'sonnet'``, written directly at every
composer's call site -- never looked up through
``emit._model_opt``/``emit._AGENT_MODELS``, whose default is Haiku
(eng-director F5/prior-art item 1). The git-commit-agent runs on Sonnet
here even though the plan path's ``_AGENT_MODELS`` pins it to Haiku (plan
EM note 4); re-homed to this module since the call sites are composed here,
not at C7.

Every prompt is a pure function of its arguments: no counters, no spend, no
wall-clock time read anywhere in this module. A run stamp, when a prompt
needs one, arrives as a caller-supplied argument -- this module never calls
``time.time()``/``datetime.now()`` itself.

Negative-spec: this module owns no graph traversal, no admission/budget
logic, no manifest handling, and no disk writes -- those are
``grind_compose.py`` (C7) and ``backlog_grind_assemble/grind_rows.py``'s
row verbs. It composes call text only.

Spec backlink: docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md
§ Design § Stage library, Tasks § C5.
"""
from __future__ import annotations

import json
from typing import Optional, Sequence

from coordinator_core.contract.grind_vocab import (
    OP_RUNNER_AGENT_TYPE,
    TRIAGE_RECORD_FIELDS,
    TSHIRT_SIZES,
)
from coordinator_core.ops.dispatch_emit.emit import _degrade_agent_type
from coordinator_core.ops.workflow_scaffold import _js_string_literal

#: The two non-op agent types this module's composers dispatch under.
#: ``OP_RUNNER_AGENT_TYPE`` (imported above) is the third.
GENERAL_PURPOSE_AGENT_TYPE = "general-purpose"
COMMIT_AGENT_TYPE = "coordinator:git-commit-agent"

#: Written directly at every composer's call site (module docstring above,
#: plan body's "the model literal 'sonnet' is written at every agent( call
#: site"). Never routed through ``emit._model_opt``.
_MODEL_LITERAL = "model: 'sonnet'"

#: The positive-rule sentence every non-commit composer's prompt carries
#: verbatim (trap 2). States the rule it wants followed, never the
#: forbidden git verbs (plan body, § Design § Stage library: "Prompts state
#: the positive rule ... and never name the forbidden git verbs").
_NO_STAGING_CLAUSE = (
    "You do not stage or commit anything. Only the committer stage does "
    "that."
)


def _schema_opt(schema: dict) -> str:
    """Render ``schema`` as the JS object-literal text for an ``agent(...)``
    call's ``schema:`` option. A JSON document is valid JS object-literal
    syntax, so ``json.dumps`` (sorted, for determinism across re-emits) is
    sufficient -- no separate JS-object serializer is needed."""
    return f"schema: {json.dumps(schema, sort_keys=True)}"


def _agent_call(
    prompt: str,
    *,
    label: str,
    phase_title: str,
    agent_type: str,
    agent_type_host: Optional[str],
    effort: str,
    schema: dict,
) -> str:
    """Compose one ``const ... = await agent(...)`` call's text. Shared by
    every ``compose_*`` function below -- the only thing that varies per
    stage kind is the prompt, the label, the agentType, the effort and the
    schema; the option-object shape and the literal model are identical
    everywhere (module docstring above)."""
    return (
        "  await agent("
        f"{_js_string_literal(prompt)}, "
        "{ "
        f"label: {_js_string_literal(label)}, "
        f"phase: {_js_string_literal(phase_title)}, "
        f"agentType: {_js_string_literal(_degrade_agent_type(agent_type, agent_type_host))}, "
        f"{_MODEL_LITERAL}, "
        f"effort: {_js_string_literal(effort)}, "
        f"{_schema_opt(schema)} "
        "});"
    )


def compose_triage_call(
    *,
    label: str,
    phase_title: str,
    run_dir: str,
    batch_id: str,
    triage_depth: str,
    agent_type_host: Optional[str] = None,
) -> str:
    """`triage` (general-purpose, sonnet, medium). Runs `grind-row check`
    first, then per row returns verdict, evidence, a t-shirt size plus
    sizing evidence, a tradeoff statement (empty when there is none),
    triage-declared files, and a fix plan. Appends one ledger line per row
    as it finishes, and writes the per-batch triage record to
    `<run_dir>/records/<batch-id>.json` for a verify op to read."""
    prompt = (
        "You are the triage stage. Run `grind-row check --manifest <script> "
        f"--batch {batch_id}` first, and skip any row it reports as `stale` "
        "or `vanished`. For every remaining row, decide a verdict, cite the "
        "evidence for it, size the row XS through XXL with the evidence for "
        "that size, and write a fix plan. Only fill in a tradeoff statement "
        "when the fix genuinely carries one -- leave it empty otherwise. "
        "Name every file your triage declares the row touches. As you "
        "finish each row, append one ledger line for it immediately (a "
        "retried agent skips rows that already have one) and write the "
        f"per-batch triage record to {run_dir}/records/{batch_id}.json. "
        f"Triage depth for this batch is '{triage_depth}'. " + _NO_STAGING_CLAUSE
    )
    row_schema = {
        "type": "object",
        "required": list(TRIAGE_RECORD_FIELDS),
        "properties": {
            "row": {"type": "string"},
            "verdict": {"type": "string"},
            "evidence": {"type": "string"},
            "tshirt_size": {"type": "string", "enum": sorted(TSHIRT_SIZES)},
            "sizing_evidence": {"type": "string"},
            "tradeoff": {"type": "string"},
            "declared_files": {"type": "array", "items": {"type": "string"}},
            "fix_plan": {"type": "string"},
        },
    }
    schema = {
        "type": "object",
        "required": ["rows"],
        "properties": {"rows": {"type": "array", "items": row_schema}},
    }
    return _agent_call(
        prompt,
        label=label,
        phase_title=phase_title,
        agent_type=GENERAL_PURPOSE_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="medium",
        schema=schema,
    )


def compose_refute_close_call(
    *,
    label: str,
    phase_title: str,
    agent_type_host: Optional[str] = None,
) -> str:
    """`refute-close` (general-purpose, sonnet, medium). Tries to refute
    each close proposal it is handed. Closes only the confirmed ones, via
    `grind-row close`."""
    prompt = (
        "You are the refute-close stage. For each close proposal you are "
        "handed, actively try to refute it -- look for evidence the row is "
        "not actually resolved. Close only the proposals that survive that "
        "attempt, via `grind-row close`, and report every proposal you "
        "refuted along with why. " + _NO_STAGING_CLAUSE
    )
    schema = {
        "type": "object",
        "required": ["confirmed", "refuted"],
        "properties": {
            "confirmed": {"type": "array", "items": {"type": "string"}},
            "refuted": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["row", "reason"],
                    "properties": {
                        "row": {"type": "string"},
                        "reason": {"type": "string"},
                    },
                },
            },
        },
    }
    return _agent_call(
        prompt,
        label=label,
        phase_title=phase_title,
        agent_type=GENERAL_PURPOSE_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="medium",
        schema=schema,
    )


def compose_fix_call(
    *,
    label: str,
    phase_title: str,
    row_id: str,
    locked_files: Sequence[str],
    agent_type_host: Optional[str] = None,
) -> str:
    """`fix` (general-purpose, sonnet, high). Holds the lock on its files
    plus `ledger:<row-id>`. Pre-checks its locked files for peer dirt
    (trap 5, `PEER_DIRTY`) before doing any work. Returns
    `NEEDS_WIDER_SCOPE` with the extra files, or `NEEDS_PLAN` (engine-mapped
    to `baton`), or a non-empty tradeoff statement (engine-mapped to
    `needs-judgment`). Otherwise fixes, tests, and runs `grind-row close`."""
    locked_list = ", ".join(locked_files) if locked_files else "(none declared)"
    prompt = (
        f"You are the fix stage for row {row_id}. You hold the lock on "
        f"[{locked_list}] plus `ledger:{row_id}`. Before doing any work, "
        "pre-check every locked file for peer dirt -- if a locked file has "
        "changed under you since the lock was acquired, stop and report "
        "PEER_DIRTY rather than fixing over it. If the fix needs files "
        "beyond your locked set, stop and report NEEDS_WIDER_SCOPE with the "
        "extra files, and take no other action. If the fix needs a plan "
        "before it can proceed, report NEEDS_PLAN. If your fix genuinely "
        "carries a tradeoff triage did not catch, report that tradeoff "
        "instead of proceeding. Otherwise, fix the row, run its tests, and "
        "run `grind-row close` when they pass. " + _NO_STAGING_CLAUSE
    )
    schema = {
        "type": "object",
        "required": ["outcome"],
        "properties": {
            "outcome": {
                "type": "string",
                "enum": [
                    "done",
                    "NEEDS_WIDER_SCOPE",
                    "PEER_DIRTY",
                    "NOT_REPRODUCED",
                    "NEEDS_PLAN",
                ],
            },
            "extra_files": {"type": "array", "items": {"type": "string"}},
            "tradeoff": {"type": "string"},
        },
    }
    return _agent_call(
        prompt,
        label=label,
        phase_title=phase_title,
        agent_type=GENERAL_PURPOSE_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="high",
        schema=schema,
    )


def compose_verify_agent_call(
    *,
    label: str,
    phase_title: str,
    agent_type_host: Optional[str] = None,
) -> str:
    """`verify` in its agent form (general-purpose, sonnet, high). Tries to
    reject the fix it is handed. Read-only apart from the named tests."""
    prompt = (
        "You are the verify stage. Try to reject the fix you are handed -- "
        "look for a way it fails, not a reason to wave it through. You are "
        "read-only apart from running the named tests: do not edit any "
        "file. Report pass only if your attempt to reject it failed. "
        + _NO_STAGING_CLAUSE
    )
    schema = {
        "type": "object",
        "required": ["outcome"],
        "properties": {
            "outcome": {"type": "string", "enum": ["pass", "fail"]},
            "reason": {"type": "string"},
        },
    }
    return _agent_call(
        prompt,
        label=label,
        phase_title=phase_title,
        agent_type=GENERAL_PURPOSE_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="high",
        schema=schema,
    )


def compose_verify_op_call(
    *,
    label: str,
    phase_title: str,
    op: str,
    run_dir: str,
    batch_id: str,
    agent_type_host: Optional[str] = None,
) -> str:
    """`verify` in its op form (`coordinator:queue-grind-op-runner`, sonnet,
    low). Shell-only: runs
    `coordinator-invoke <op> --params-file <run_dir>/records/<batch-id>.json`
    and returns the JSON verbatim. Exit 0 passes; otherwise the JSON names
    the failing ids."""
    prompt = (
        f"Run `coordinator-invoke {op} --params-file "
        f"{run_dir}/records/{batch_id}.json` and return its JSON output "
        "verbatim. Exit 0 means the batch passes. A non-zero exit means the "
        "JSON output names the failing row ids -- return it unchanged "
        "either way; do not summarize or reinterpret it. " + _NO_STAGING_CLAUSE
    )
    schema = {
        "type": "object",
        "required": ["exit_code", "output"],
        "properties": {
            "exit_code": {"type": "integer"},
            "output": {"type": "object"},
        },
    }
    return _agent_call(
        prompt,
        label=label,
        phase_title=phase_title,
        agent_type=OP_RUNNER_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="low",
        schema=schema,
    )


def compose_commit_call(
    *,
    label: str,
    phase_title: str,
    row_id: str,
    touched_files: Sequence[str],
    removed_files: Sequence[str] = (),
    regenerate_op: Optional[str] = None,
    agent_type_host: Optional[str] = None,
) -> str:
    """`commit` (`coordinator:git-commit-agent`, sonnet, low). Stages the
    worker's touched list plus the row's ledger deletion (`grind-row
    settle`), runs the profile's index-regenerate op when one is named,
    then commits. Passes `--declared-revert` for every removed path
    (trap 3). An indeterminate outcome is reconciled against `git log` and
    `git status` before any retry, and never retried blind (trap 4)."""
    touched_list = ", ".join(touched_files) if touched_files else "(none)"
    regenerate_clause = (
        f" Before staging, run the index-regenerate op `{regenerate_op}`."
        if regenerate_op
        else ""
    )
    declared_revert_clause = (
        " Pass --declared-revert for every one of these removed paths: "
        f"[{', '.join(removed_files)}]."
        if removed_files
        else ""
    )
    prompt = (
        f"You are the committer for row {row_id}. You are the only stage "
        "that stages or commits anything. Stage exactly this touched list: "
        f"[{touched_list}], plus this row's ledger deletion via "
        f"`grind-row settle`.{regenerate_clause}{declared_revert_clause} "
        "Then commit. If the outcome is indeterminate, reconcile it against "
        "`git log` and `git status` before doing anything else -- never "
        "retry blind."
    )
    schema = {
        "type": "object",
        "required": ["outcome"],
        "properties": {
            "outcome": {"type": "string", "enum": ["committed", "commit-failed"]},
            "sha": {"type": "string"},
        },
    }
    return _agent_call(
        prompt,
        label=label,
        phase_title=phase_title,
        agent_type=COMMIT_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="low",
        schema=schema,
    )


def compose_commit_ledger_only_call(
    *,
    label: str,
    phase_title: str,
    profile: str,
    unsettled_row_ids: Sequence[str],
    run_id: Optional[str] = None,
    is_drain: bool = False,
    agent_type_host: Optional[str] = None,
) -> str:
    """`commit` (ledger-only) (`coordinator:git-commit-agent`, sonnet, low).
    At batch end and on drain, commits exactly the unsettled rows' ledger
    files. On the drain commit only, additionally writes and stages
    `state/queue-grind/<profile>/runs/<run-id>.json` in the same commit."""
    rows_list = ", ".join(unsettled_row_ids) if unsettled_row_ids else "(none)"
    drain_clause = (
        f" This is the drain commit: also write and stage "
        f"state/queue-grind/{profile}/runs/{run_id}.json in this same "
        "commit."
        if is_drain
        else ""
    )
    prompt = (
        "You are the committer for a ledger-only commit. You are the only "
        "stage that stages or commits anything. Stage exactly these "
        f"unsettled rows' ledger files: [{rows_list}], and nothing "
        f"else.{drain_clause} Then commit. If the outcome is indeterminate, "
        "reconcile it against `git log` and `git status` before doing "
        "anything else -- never retry blind."
    )
    schema = {
        "type": "object",
        "required": ["outcome"],
        "properties": {
            "outcome": {"type": "string", "enum": ["committed", "commit-failed"]},
            "sha": {"type": "string"},
        },
    }
    return _agent_call(
        prompt,
        label=label,
        phase_title=phase_title,
        agent_type=COMMIT_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="low",
        schema=schema,
    )


def compose_undo_call(
    *,
    label: str,
    phase_title: str,
    touched_files: Sequence[str],
    created_files: Sequence[str] = (),
    agent_type_host: Optional[str] = None,
) -> str:
    """`undo` (general-purpose, sonnet, low). Restores the fixer's own
    touched files from HEAD and removes the files it created."""
    touched_list = ", ".join(touched_files) if touched_files else "(none)"
    created_list = ", ".join(created_files) if created_files else "(none)"
    prompt = (
        f"Restore these files from HEAD: [{touched_list}], and remove "
        f"these files the fix created: [{created_list}]. " + _NO_STAGING_CLAUSE
    )
    schema = {
        "type": "object",
        "required": ["outcome"],
        "properties": {"outcome": {"type": "string", "enum": ["undone"]}},
    }
    return _agent_call(
        prompt,
        label=label,
        phase_title=phase_title,
        agent_type=GENERAL_PURPOSE_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="low",
        schema=schema,
    )
