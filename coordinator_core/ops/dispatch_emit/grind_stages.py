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
- trap 3: the commit composer's prompt names every removed path as a
  declared deletion (``deleted_paths``), never as a ``settle`` flag;
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
    is_expr: bool = False,
) -> str:
    """Compose one ``const ... = await agent(...)`` call's text. Shared by
    every ``compose_*`` function below -- the only thing that varies per
    stage kind is the prompt, the label, the agentType, the effort and the
    schema; the option-object shape and the literal model are identical
    everywhere (module docstring above).

    ``prompt`` is plain prompt TEXT by default, escaped here via
    ``_js_string_literal``. When a caller has already built a JS
    EXPRESSION that evaluates to the prompt at run time (``is_expr=True`` --
    ``_join_prompt_parts``'s output: static, escaped literal pieces
    concatenated with a live runtime expression via ``+``), it is emitted
    verbatim instead."""
    prompt_text = prompt if is_expr else _js_string_literal(prompt)
    return (
        "  await agent("
        f"{prompt_text}, "
        "{ "
        f"label: {_js_string_literal(label)}, "
        f"phase: {_js_string_literal(phase_title)}, "
        f"agentType: {_js_string_literal(_degrade_agent_type(agent_type, agent_type_host))}, "
        f"{_MODEL_LITERAL}, "
        f"effort: {_js_string_literal(effort)}, "
        f"{_schema_opt(schema)} "
        "});"
    )


def _join_prompt_parts(parts: Sequence[tuple[str, str]]) -> str:
    """Build a JS EXPRESSION (string literal(s) concatenated with ``+``)
    that evaluates, at RUN time, to a full prompt -- letting a caller splice
    a live JS expression (e.g. ``row.declaredFiles.join(', ')``) between
    static text segments. Each part is ``("lit", text)`` (escaped through
    ``_js_string_literal``, the existing escaper) or ``("expr", js_expr)``
    (emitted verbatim, parenthesised). Runtime interpolation support for
    ``compose_fix_call``/``compose_commit_call``/``compose_undo_call``/
    ``compose_commit_ledger_only_call`` -- fixes the break-class defect
    where a static per-row manifest path stood in for the live triage-
    declared/fixer-touched file list a real committer/undoer needs."""
    pieces: list[str] = []
    for kind, text in parts:
        if kind == "lit":
            if text:
                pieces.append(_js_string_literal(text))
        elif kind == "expr":
            pieces.append(f"({text})")
        else:
            raise ValueError(f"grind_stages._join_prompt_parts: unknown part kind {kind!r}")
    return " + ".join(pieces) if pieces else "''"


def _list_parts(files: Sequence[str], files_js: Optional[str]) -> list[tuple[str, str]]:
    """One or more ``_join_prompt_parts`` parts rendering a file/row-id list
    clause: a live ``files_js`` JS expression (joined with ``', '`` at run
    time) when given, else the static ``files`` list (or ``(none)``)."""
    if files_js:
        return [("expr", f"({files_js}).join(', ')")]
    return [("lit", ", ".join(files) if files else "(none)")]


def compose_triage_call(
    *,
    label: str,
    phase_title: str,
    run_dir: str,
    profile: str = "",
    batch_id: str = "",
    triage_depth: str = "",
    verdicts: Sequence[str] = (),
    batch_id_js: Optional[str] = None,
    triage_depth_js: Optional[str] = None,
    rows_js: Optional[str] = None,
    script_path_js: Optional[str] = None,
    run_id_js: Optional[str] = None,
    agent_type_host: Optional[str] = None,
    repo_root: str = ".",
) -> str:
    """`triage` (general-purpose, sonnet, medium). Runs `backlog-grind-assemble grind-row check`
    first, then per row returns verdict, evidence, a t-shirt size plus
    sizing evidence, a tradeoff statement (empty when there is none),
    triage-declared files, and a fix plan. Appends one ledger line per row
    (`backlog-grind-assemble grind-row append --profile P --row-id R --digest D --stage triage
    --verdict V --outcome O --evidence-file F --run-stamp T --repo-root
    <repo_root>`) as it finishes, and writes the per-batch triage record to
    `<run_dir>/records/<batch-id>.json` for a verify op to read.

    ``batch_id_js``/``triage_depth_js``/``rows_js``/``script_path_js``/
    ``run_id_js`` name JS runtime expressions to interpolate instead of the
    static values -- letting ONE composed call site serve every batch,
    telling the agent its own rows (row_id/path/digest) and the exact
    `backlog-grind-assemble grind-row check`/`append` invocations rather than a literal
    `<script>` placeholder."""
    batch_id_part: tuple[str, str] = ("expr", batch_id_js) if batch_id_js else ("lit", batch_id)
    depth_part: tuple[str, str] = ("expr", triage_depth_js) if triage_depth_js else ("lit", triage_depth)
    rows_part: tuple[str, str] = ("expr", rows_js) if rows_js else ("lit", "[]")
    script_part: tuple[str, str] = ("expr", script_path_js) if script_path_js else ("lit", "<script>")
    run_id_part: tuple[str, str] = ("expr", run_id_js) if run_id_js else ("lit", "<run-id>")
    parts: list[tuple[str, str]] = [
        ("lit", "You are the triage stage. Your rows (row_id/path/digest) are: "),
        rows_part,
        ("lit", ". Run `backlog-grind-assemble grind-row check --manifest "),
        script_part,
        ("lit", " --batch "),
        batch_id_part,
        ("lit", " --repo-root ."),
        (
            "lit",
            "` first, and list any row it reports as `stale` "
            "or `vanished` in `stale` rather than skipping it silently. "
            "Return `row` as each row's row_id exactly as given above -- "
            "never its path -- e.g. {\"row\": \"row3\", \"verdict\": ...}. "
            "For every remaining row, decide a verdict from exactly this "
            f"list, verbatim, and no other value: {sorted(verdicts)}. Cite the "
            "evidence for it, size the row XS through XXL with the evidence for "
            "that size, and write a fix plan. Set `has_tradeoff` to true only "
            "when the fix genuinely carries a tradeoff, and describe it in "
            "`tradeoff`; otherwise set `has_tradeoff` to false and leave "
            "`tradeoff` empty. "
            "Name every file your triage declares the row touches. As you finish "
            "each row, run `backlog-grind-assemble grind-row append --profile "
            f"{profile} --row-id <its row_id> --digest <its digest> --stage "
            "triage --verdict <its verdict> --outcome <its verdict> "
            "--evidence-file <a file with your evidence> --run-stamp ",
        ),
        run_id_part,
        ("lit", f" --repo-root {repo_root}"),
        (
            "lit",
            "` immediately (idempotent under a retried agent -- an identical "
            "line already appended is not re-appended) and write the "
            f"per-batch triage record to {run_dir}/records/",
        ),
        batch_id_part,
        ("lit", ".json. Triage depth for this batch is '"),
        depth_part,
        ("lit", "'. " + _NO_STAGING_CLAUSE),
    ]
    row_schema = {
        "type": "object",
        "required": list(TRIAGE_RECORD_FIELDS) + ["has_tradeoff"],
        "properties": {
            "row": {
                "type": "string",
                "description": "the row_id exactly as given in this batch's rows, never the row's path",
            },
            "verdict": {"type": "string", "enum": list(verdicts)},
            "evidence": {"type": "string"},
            "tshirt_size": {"type": "string", "enum": sorted(TSHIRT_SIZES)},
            "sizing_evidence": {"type": "string"},
            "has_tradeoff": {"type": "boolean"},
            "tradeoff": {"type": "string"},
            "declared_files": {"type": "array", "items": {"type": "string"}},
            "fix_plan": {"type": "string"},
        },
    }
    schema = {
        "type": "object",
        "required": ["rows"],
        "properties": {
            "rows": {"type": "array", "items": row_schema},
            "stale": {"type": "array", "items": {"type": "string"}},
        },
    }
    return _agent_call(
        _join_prompt_parts(parts),
        label=label,
        phase_title=phase_title,
        agent_type=GENERAL_PURPOSE_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="medium",
        schema=schema,
        is_expr=True,
    )


def compose_refute_close_call(
    *,
    label: str,
    phase_title: str,
    profile: str = "",
    profile_dir: str = "",
    profile_dir_js: Optional[str] = None,
    proposals_js: Optional[str] = None,
    run_id_js: Optional[str] = None,
    agent_type_host: Optional[str] = None,
    repo_root: str = ".",
) -> str:
    """`refute-close` (general-purpose, sonnet, medium). Tries to refute
    each close proposal it is handed. Closes only the confirmed ones, via
    `backlog-grind-assemble grind-row close --profile-dir D --profile P --row <path> --digest DIG
    --verdict refute-close --evidence-file F --closed-by refute-close
    --run-stamp T --repo-root <repo_root>`, reporting the `{old,new}` path
    pair the command prints so the committer can stage the archive add and
    declare the queue-path removal.

    ``proposals_js``/``run_id_js`` name JS runtime expressions -- the
    proposal rows (row_id/path/digest/triage evidence) this batch's
    refute-close node actually reached, and the run stamp -- interpolated
    instead of a static/omitted value."""
    proposals_part: tuple[str, str] = ("expr", proposals_js) if proposals_js else ("lit", "[]")
    profile_dir_part: tuple[str, str] = ("expr", profile_dir_js) if profile_dir_js else ("lit", profile_dir)
    run_id_part: tuple[str, str] = ("expr", run_id_js) if run_id_js else ("lit", "<run-id>")
    parts: list[tuple[str, str]] = [
        ("lit", "You are the refute-close stage. Your close proposals (row_id/path/digest/evidence) are: "),
        proposals_part,
        (
            "lit",
            ". For each one, actively try to refute it -- look for evidence the "
            "row is not actually resolved. For every proposal that survives "
            "that attempt, run `backlog-grind-assemble grind-row close --profile-dir ",
        ),
        profile_dir_part,
        (
            "lit",
            f" --profile {profile} --row <its path> --digest "
            "<its digest> --verdict refute-close --evidence-file <a file "
            "with your evidence> --closed-by refute-close --run-stamp ",
        ),
        run_id_part,
        ("lit", f" --repo-root {repo_root}"),
        (
            "lit",
            "`, and report the `{old,new}` path pair it prints as that "
            "row's `new_path`. If `backlog-grind-assemble grind-row close` exits 3 (digest mismatch "
            "-- the row changed since the manifest was emitted), put that "
            "row's id in `stale` instead. Report every proposal you refuted "
            "along with why, and never run `backlog-grind-assemble grind-row close` for one of "
            "those. In every one of `confirmed`/`refuted`/`stale`, return `row` "
            "as the row_id exactly as given in the proposals above, never the "
            "row's path -- e.g. {\"row\": \"row3\", \"new_path\": \"archive/2026-09/row3.yaml\"}. "
            + _NO_STAGING_CLAUSE,
        ),
    ]
    schema = {
        "type": "object",
        "required": ["confirmed", "refuted"],
        "properties": {
            "confirmed": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["row", "new_path"],
                    "properties": {
                        "row": {
                            "type": "string",
                            "description": "the row_id exactly as given in the proposals above, never the row's path",
                        },
                        "new_path": {"type": "string"},
                    },
                },
            },
            "refuted": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["row", "reason"],
                    "properties": {
                        "row": {
                            "type": "string",
                            "description": "the row_id exactly as given in the proposals above, never the row's path",
                        },
                        "reason": {"type": "string"},
                    },
                },
            },
            "stale": {"type": "array", "items": {"type": "string"}},
        },
    }
    return _agent_call(
        _join_prompt_parts(parts),
        label=label,
        phase_title=phase_title,
        agent_type=GENERAL_PURPOSE_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="medium",
        schema=schema,
        is_expr=True,
    )


def compose_fix_call(
    *,
    label: str,
    phase_title: str,
    row_id: str = "",
    row_id_js: Optional[str] = None,
    locked_files: Sequence[str] = (),
    locked_files_js: Optional[str] = None,
    feedback_js: Optional[str] = None,
    close_note_js: Optional[str] = None,
    profile: str = "",
    profile_dir_js: Optional[str] = None,
    row_path_js: Optional[str] = None,
    digest_js: Optional[str] = None,
    run_id_js: Optional[str] = None,
    repo_root: str = ".",
    agent_type_host: Optional[str] = None,
) -> str:
    """`fix` (general-purpose, sonnet, high). Holds the lock on its files
    plus `ledger:<row-id>`. Pre-checks its locked files for peer dirt
    (trap 5, `PEER_DIRTY`) before doing any work. Returns
    `NEEDS_WIDER_SCOPE` with the extra files, or `NEEDS_PLAN` (engine-mapped
    to `baton`), or a non-empty tradeoff statement (engine-mapped to
    `needs-judgment`). Otherwise fixes, tests, reports every file it
    touched AND created, and runs `backlog-grind-assemble grind-row close` when they pass,
    reporting the `{old,new}` path pair it prints as `close_result`.

    ``locked_files_js``/``row_id_js``/``feedback_js`` name JS expressions
    (a row's runtime triage-declared-files array, its own id, and -- on a
    verify-failure retry -- the verifier's own reason) to interpolate at
    RUN time instead of the static ``locked_files``/``row_id`` -- letting
    ONE composed call site serve every row, with real, live-declared lock
    keys rather than a static approximation (§ Design § Stage library,
    "Fix locks cover the TRIAGE-DECLARED files"), and the verifier's
    feedback fed back on a retry (DR-404: fail routes back to FIX with
    feedback, never a blind re-verify)."""
    row_id_part: tuple[str, str] = ("expr", row_id_js) if row_id_js else ("lit", row_id)
    tail = (
        "] plus `ledger:"
    )
    parts: list[tuple[str, str]] = [
        ("lit", "You are the fix stage for row "),
        row_id_part,
        ("lit", ". You hold the lock on ["),
    ]
    parts.extend(_list_parts(locked_files, locked_files_js))
    parts.append(("lit", tail))
    parts.append(row_id_part)
    parts.append(
        (
            "lit",
            "`. Before doing any work, "
            "pre-check every locked file for peer dirt -- if a locked file has "
            "changed under you since the lock was acquired, stop and report "
            "PEER_DIRTY rather than fixing over it. If the fix needs files "
            "beyond your locked set, stop and report NEEDS_WIDER_SCOPE with the "
            "extra files, and take no other action. If the fix needs a plan "
            "before it can proceed, report NEEDS_PLAN. Set `has_tradeoff` to "
            "true only when your fix genuinely carries a tradeoff triage did "
            "not catch, and describe it in `tradeoff`; otherwise set "
            "`has_tradeoff` to false and leave `tradeoff` empty. "
            "Otherwise, fix the row, run its tests, "
            "report every file you touched and every file you created, and "
            "when they pass run `backlog-grind-assemble grind-row close --profile-dir ",
        )
    )
    _manifest_stale_note = (
        " If `backlog-grind-assemble grind-row close` exits 3 (digest mismatch -- the row changed "
        "since the manifest was emitted), report MANIFEST_STALE and stop."
    )
    parts.append(("expr", profile_dir_js) if profile_dir_js else ("lit", "<profile dir>"))
    parts.append(("lit", f" --profile {profile} --row "))
    parts.append(("expr", row_path_js) if row_path_js else ("lit", "<row path>"))
    parts.append(("lit", " --digest "))
    parts.append(("expr", digest_js) if digest_js else ("lit", "<row digest>"))
    parts.append((
        "lit",
        " --verdict fix --evidence-file <a file with your evidence> --closed-by fix --run-stamp ",
    ))
    parts.append(("expr", run_id_js) if run_id_js else ("lit", "<run-id>"))
    parts.append((
        "lit",
        f" --repo-root {repo_root}`, reporting the `{{old,new}}` path pair it prints as "
        f"`close_result`.{_manifest_stale_note}",
    ))
    if feedback_js:
        parts.append(("expr", feedback_js))
    if close_note_js:
        parts.append(("expr", close_note_js))
    parts.append(("lit", " " + _NO_STAGING_CLAUSE))
    schema = {
        "type": "object",
        "required": ["outcome", "has_tradeoff"],
        "properties": {
            "outcome": {
                "type": "string",
                "enum": [
                    "done",
                    "NEEDS_WIDER_SCOPE",
                    "PEER_DIRTY",
                    "NOT_REPRODUCED",
                    "NEEDS_PLAN",
                    "MANIFEST_STALE",
                ],
            },
            "extra_files": {"type": "array", "items": {"type": "string"}},
            "touched_files": {"type": "array", "items": {"type": "string"}},
            "created_files": {"type": "array", "items": {"type": "string"}},
            "close_result": {
                "type": "object",
                "properties": {"old": {"type": "string"}, "new": {"type": "string"}},
            },
            "has_tradeoff": {"type": "boolean"},
            "tradeoff": {"type": "string"},
        },
    }
    return _agent_call(
        _join_prompt_parts(parts),
        label=label,
        phase_title=phase_title,
        agent_type=GENERAL_PURPOSE_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="high",
        schema=schema,
        is_expr=True,
    )


def compose_verify_agent_call(
    *,
    label: str,
    phase_title: str,
    row_id_js: Optional[str] = None,
    row_path_js: Optional[str] = None,
    touched_files_js: Optional[str] = None,
    evidence_js: Optional[str] = None,
    fix_plan_js: Optional[str] = None,
    agent_type_host: Optional[str] = None,
) -> str:
    """`verify` in its agent form (general-purpose, sonnet, high). Tries to
    reject the fix it is handed. Read-only apart from the named tests.

    ``row_id_js``/``row_path_js``/``touched_files_js``/``evidence_js``/
    ``fix_plan_js`` name JS runtime expressions -- the row's own id/path,
    the fixer's touched files, the triage evidence and the fix plan -- so
    the verifier is handed something to verify rather than a bare
    "reject the fix" instruction with no row context."""
    parts: list[tuple[str, str]] = [
        ("lit", "You are the verify stage for row "),
        ("expr", row_id_js) if row_id_js else ("lit", "<row id>"),
        ("lit", " at "),
        ("expr", row_path_js) if row_path_js else ("lit", "<row path>"),
        ("lit", ". The fixer touched: ["),
    ]
    parts.extend(_list_parts((), touched_files_js))
    parts.append(("lit", "]. Triage evidence: "))
    parts.append(("expr", evidence_js) if evidence_js else ("lit", "(none)"))
    parts.append(("lit", ". The fix plan was: "))
    parts.append(("expr", fix_plan_js) if fix_plan_js else ("lit", "(none)"))
    parts.append(
        (
            "lit",
            ". Try to reject the fix you are handed -- "
            "look for a way it fails, not a reason to wave it through. You are "
            "read-only apart from running the named tests: do not edit any "
            "file. Report pass only if your attempt to reject it failed. "
            + _NO_STAGING_CLAUSE,
        )
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
        _join_prompt_parts(parts),
        label=label,
        phase_title=phase_title,
        agent_type=GENERAL_PURPOSE_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="high",
        schema=schema,
        is_expr=True,
    )


def compose_verify_op_call(
    *,
    label: str,
    phase_title: str,
    run_dir: str,
    op: str = "",
    op_js: Optional[str] = None,
    batch_id: str = "",
    batch_id_js: Optional[str] = None,
    agent_type_host: Optional[str] = None,
) -> str:
    """`verify` in its op form (`coordinator:queue-grind-op-runner`, sonnet,
    low). Shell-only: runs
    `coordinator-invoke <op> --params-file <run_dir>/records/<batch-id>.json`
    and returns the JSON verbatim. Exit 0 passes; otherwise the JSON names
    the failing ids.

    ``op_js``/``batch_id_js`` name JS expressions (a per-batch-key verify-op
    const, and the row's owning batch id) to interpolate at RUN time --
    letting ONE composed call site serve every batch-key's op variant."""
    op_part: tuple[str, str] = ("expr", op_js) if op_js else ("lit", op)
    batch_id_part: tuple[str, str] = ("expr", batch_id_js) if batch_id_js else ("lit", batch_id)
    parts: list[tuple[str, str]] = [
        ("lit", "Run `coordinator-invoke "),
        op_part,
        ("lit", f" --params-file {run_dir}/records/"),
        batch_id_part,
        (
            "lit",
            ".json` and return its JSON output "
            "verbatim. Exit 0 means the batch passes. A non-zero exit means the "
            "JSON output names the failing row ids -- return it unchanged "
            "either way; do not summarize or reinterpret it. " + _NO_STAGING_CLAUSE,
        ),
    ]
    schema = {
        "type": "object",
        "required": ["exit_code", "output"],
        "properties": {
            "exit_code": {"type": "integer"},
            "output": {"type": "object"},
        },
    }
    return _agent_call(
        _join_prompt_parts(parts),
        label=label,
        phase_title=phase_title,
        agent_type=OP_RUNNER_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="low",
        is_expr=True,
        schema=schema,
    )


#: `commit`'s own wire schema -- identical for both the per-row form and the
#: ledger-only form (batch-end/drain): both are the SAME `coordinator:
#: git-commit-agent` stage kind, only the staged content differs.
_COMMIT_SCHEMA = {
    "type": "object",
    "required": ["outcome"],
    "properties": {
        "outcome": {"type": "string", "enum": ["committed", "commit-failed"]},
        "sha": {"type": "string"},
        "reason": {"type": "string"},
    },
}

#: The trailing "then commit" clause every `commit` prompt ends on
#: (trap 4: an indeterminate outcome is reconciled against `git log`/`git
#: status` before any retry, never retried blind) -- shared verbatim by
#: `compose_commit_call` and `compose_commit_ledger_only_call`.
_COMMIT_TAIL_LIT = (
    " Then commit. If the outcome is indeterminate, reconcile it against "
    "`git log` and `git status` before doing anything else -- never retry blind."
    " On commit-failed, put the verbatim refusal or the divergence you found in `reason`."
)


def _compose_commit_agent_call(
    parts: list[tuple[str, str]], *, label: str, phase_title: str, agent_type_host: Optional[str]
) -> str:
    """Shared tail: both `commit` forms hand their own prompt ``parts`` in
    here to finish the same way (schema, agent type, effort, ``is_expr``)."""
    return _agent_call(
        _join_prompt_parts(parts),
        label=label,
        phase_title=phase_title,
        agent_type=COMMIT_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="low",
        schema=_COMMIT_SCHEMA,
        is_expr=True,
    )


def compose_commit_call(
    *,
    label: str,
    phase_title: str,
    profile: str = "",
    row_id: str = "",
    row_id_js: Optional[str] = None,
    outcome_js: Optional[str] = None,
    touched_files: Sequence[str] = (),
    touched_files_js: Optional[str] = None,
    removed_files: Sequence[str] = (),
    removed_files_js: Optional[str] = None,
    regenerate_op: Optional[str] = None,
    repo_root: str = ".",
    agent_type_host: Optional[str] = None,
) -> str:
    """`commit` (`coordinator:git-commit-agent`, sonnet, low). Stages the
    worker's touched list plus the row's ledger deletion, via the full
    `backlog-grind-assemble grind-row settle --profile P --row-id R
    --repo-root <repo_root>` invocation (every flag `grind_rows.cmd_settle`
    requires -- matching how every other composer in this module renders
    its CLI invocation, never a bare/underspecified subcommand mention),
    runs the profile's index-regenerate op when one is named, then commits.
    Names every removed path as a declared deletion in the committer's own
    shapes (`deleted_paths` on `ceremony.commit_v2`, or the scoped-commit
    pathspec) -- never as a `settle` flag, which takes only `--profile`,
    `--row-id` and `--repo-root` (trap 3). On `commit-failed` the committer
    returns the verbatim refusal or divergence in `reason`, so a hand-back
    carries its cause. An
    indeterminate outcome is reconciled against `git log` and `git status`
    before any retry, and never retried blind (trap 4).

    ``touched_files_js``/``removed_files_js``/``row_id_js`` name JS
    expressions (e.g. the fixer's own returned touched-files list, the row
    paths a `close` moved to archive, and the row's own id) to interpolate
    at RUN time instead of the static values -- letting ONE composed call
    site serve every row, with the committer staging what the worker
    ACTUALLY touched, never a static per-row approximation."""
    row_id_part: tuple[str, str] = ("expr", row_id_js) if row_id_js else ("lit", row_id)
    outcome_part: tuple[str, str] = ("expr", outcome_js) if outcome_js else ("lit", "settled")
    parts: list[tuple[str, str]] = [
        ("lit", "You are the committer for row "),
        row_id_part,
        ("lit", ". You are the only stage that stages or commits anything. Stage exactly this touched list: ["),
    ]
    parts.extend(_list_parts(touched_files, touched_files_js))
    parts.append(
        ("lit", f"], plus this row's ledger deletion via `backlog-grind-assemble grind-row settle --profile {profile} --row-id ")
    )
    parts.append(row_id_part)
    parts.append(("lit", f" --repo-root {repo_root}` (it takes only those three flags; running it is part of staging this dispatch)."))
    if regenerate_op:
        parts.append(("lit", f" Before staging, run the index-regenerate op `{regenerate_op}`."))
    if removed_files or removed_files_js:
        parts.append(("lit", " Record every one of these removed paths as a declared deletion (`deleted_paths` on `ceremony.commit_v2`; in a scoped `git commit --`, name them in the pathspec): ["))
        parts.extend(_list_parts(removed_files, removed_files_js))
        parts.append(("lit", "]."))
    parts.append(("lit", f" Use commit subject `grind({profile}): "))
    parts.append(row_id_part)
    parts.append(("lit", " "))
    parts.append(outcome_part)
    parts.append(("lit", "` and a commit body naming this row."))
    parts.append(("lit", _COMMIT_TAIL_LIT))
    return _compose_commit_agent_call(
        parts, label=label, phase_title=phase_title, agent_type_host=agent_type_host
    )


def compose_commit_ledger_only_call(
    *,
    label: str,
    phase_title: str,
    profile: str,
    unsettled_row_ids: Sequence[str] = (),
    unsettled_row_ids_js: Optional[str] = None,
    run_id: Optional[str] = None,
    run_id_js: Optional[str] = None,
    is_drain: bool = False,
    record_js: Optional[str] = None,
    repo_root: str = ".",
    agent_type_host: Optional[str] = None,
) -> str:
    """`commit` (ledger-only) (`coordinator:git-commit-agent`, sonnet, low).
    At batch end and on drain, commits exactly the unsettled rows' ledger
    files. On the drain commit only, additionally runs `backlog-grind-
    assemble grind-row run-record` to write and stage
    `state/queue-grind/<profile>/runs/<run-id>.json` in the same commit --
    the committer has no Write tool, so the run record is written through
    this verb rather than hand-written (§ Design § Row verbs, `run-record`).

    ``unsettled_row_ids_js``/``run_id_js`` name JS expressions to
    interpolate at RUN time instead of the static values -- the real
    unsettled-row set at commit time, and the real run stamp, neither of
    which is known at emit time."""
    run_id_part: tuple[str, str] = ("expr", run_id_js) if run_id_js else ("lit", str(run_id))
    parts: list[tuple[str, str]] = [
        (
            "lit",
            "You are the committer for a ledger-only commit. You are the only "
            "stage that stages or commits anything. Stage exactly these "
            "unsettled rows' ledger files: [",
        )
    ]
    parts.extend(_list_parts(unsettled_row_ids, unsettled_row_ids_js))
    parts.append(
        (
            "lit",
            "], and nothing else. If any one of those files does not exist, "
            "skip it, report which one(s) you skipped in your reason, and "
            "commit the rest rather than failing the whole commit.",
        )
    )
    if is_drain:
        parts.append(
            (
                "lit",
                f" This is the drain commit: also run `backlog-grind-assemble grind-row "
                f"run-record --profile {profile} --run-id ",
            )
        )
        parts.append(run_id_part)
        parts.append(("lit", f" --repo-root {repo_root}` to write and stage state/queue-grind/{profile}/runs/"))
        if run_id_js:
            parts.append(("expr", run_id_js))
        else:
            parts.append(("lit", str(run_id)))
        parts.append(("lit", ".json in this same commit."))
        if record_js:
            parts.append(("lit", " Pass this JSON on stdin, byte for byte: "))
            parts.append(("expr", record_js))
            parts.append(("lit", "."))
    if is_drain:
        parts.append(("lit", f" Use commit subject `grind({profile}): drain run "))
        parts.append(run_id_part)
        parts.append(("lit", "` and a commit body naming these rows."))
    else:
        parts.append(("lit", f" Use commit subject `grind({profile}): ledger for "))
        parts.append(("expr", "unsettled.length"))
        parts.append(("lit", " row(s), run "))
        parts.append(run_id_part)
        parts.append(("lit", "` and a commit body naming these rows."))
    parts.append(("lit", _COMMIT_TAIL_LIT))
    return _compose_commit_agent_call(
        parts, label=label, phase_title=phase_title, agent_type_host=agent_type_host
    )


def compose_undo_call(
    *,
    label: str,
    phase_title: str,
    touched_files: Sequence[str] = (),
    touched_files_js: Optional[str] = None,
    created_files: Sequence[str] = (),
    created_files_js: Optional[str] = None,
    agent_type_host: Optional[str] = None,
) -> str:
    """`undo` (general-purpose, sonnet, low). Restores the fixer's own
    touched files from HEAD and removes the files it created.

    ``touched_files_js``/``created_files_js`` name JS expressions (the
    fixer's own returned touched/created lists) to interpolate at RUN time
    instead of the static lists."""
    parts: list[tuple[str, str]] = [("lit", "Restore these files from HEAD: [")]
    parts.extend(_list_parts(touched_files, touched_files_js))
    parts.append(("lit", "], and remove these files the fix created: ["))
    parts.extend(_list_parts(created_files, created_files_js))
    parts.append(("lit", "]. " + _NO_STAGING_CLAUSE))
    schema = {
        "type": "object",
        "required": ["outcome"],
        "properties": {"outcome": {"type": "string", "enum": ["undone"]}},
    }
    return _agent_call(
        _join_prompt_parts(parts),
        label=label,
        phase_title=phase_title,
        agent_type=GENERAL_PURPOSE_AGENT_TYPE,
        agent_type_host=agent_type_host,
        effort="low",
        schema=schema,
        is_expr=True,
    )
