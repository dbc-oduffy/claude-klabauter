"""warp --ask script composer: the one .mjs from an ask (or an existing sizing) to a reviewed run.

The script sizes (raw ask only), asks the engine for a gate verdict, halts or runs the routed arm
(XS stage; S plan-author then stage; M+ plan-blitz then stage), executes the staged manifest rows
and composes the review wave once. Every early exit assigns the typed `_halted` and later phases
run under `if (!_halted)`: the terminal return is the script's one exit. Rows, briefs and pathspecs are Python-composed by
dispatch.ask_stage; the script only interprets the manifest. Pure composition: no spawn.
"""

from __future__ import annotations

import json
import shlex
from datetime import date
from pathlib import Path
from typing import Callable, Optional, Sequence

from coordinator_core.git.git_state import head_sha
from coordinator_core.warp_fire import fire_command
from coordinator_core.ops.dispatch_emit import agent_sidecar as _sidecar
from coordinator_core.ops.dispatch_emit import emit as _emit
from coordinator_core.ops.dispatch_emit.fanout_cost import AGENT_CALL_RE, GROUP_HEADER_TAIL, GROUP_MEMBER_RESULT_JS, FanoutCost, with_fanout
from coordinator_core.ops.dispatch_emit.falsifier_integrity_phase import REVIEWER_AGENT_TYPE
from coordinator_core.ops.dispatch_emit.ask_brief import EM_BRIEF_VAR, EmBrief, brief_decl_js
from coordinator_core.ops.dispatch_emit.sizing_fire import RESUME_LEDGER_HALT_BLOCKING_FIELD
from coordinator_core.ops._workflow_contract import agent_options_phase_titles
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    HALT_DEPENDENTS_UNSTARTED,
    ASK_MANIFEST_MARKER,
    ASK_PHASES,
    ASK_SOURCE_RELAYED,
    ASK_SOURCES,
    ASK_VERIFY_LABEL,
    ASK_VERIFY_PHASE,
    DELIVERY_NOTE_DIR,
    HALT_PLAN_FIELD,
    HALTED_RECORD_PARAM,
    PM_INTENT_FILE,
    RESUME_LEDGER_EM_DISCHARGED_FIELD,
    RESUME_LEDGER_HALT_SHA_FIELD,
    RESUME_LEDGER_KEY,
    RESUME_LEDGER_KIND_FIELD,
    RESUME_LEDGER_NOTES_FIELD,
    RESUME_LEDGER_REMAINING_FIELD,
    RESUME_LEDGER_UNLANDED_FIELD,
    TERMINAL_RESUME_LEDGER_PARAM,
    RECORD_DIR_ROOT,
    RELAYED_ASK_FILE,
    HALT_DESIGN_REVIEW,
    HALT_REFUSAL,
    HALT_RESEARCH,
    HALT_ROOM,
    HALT_SPLIT,
    HALT_TOUCHPOINT,
    NO_PLAN_HAND_OFF_FLAG,
    OP_ASK_GATE,
    OP_ASK_STAGE,
    PM_EXIT_QUESTION_KEY,
    RUN_DIR_ROOT,
    SIZING_SCOUT_AGENT_TYPE,
    STOP_AFTER_PLAN,
    DOC_NEW_CMD,
    ASK_SCOUT_SCHEMA,
    ASSEMBLE_SCHEMA,
    ACCEPTANCE_ROWS_CLAUSE,
    RESEARCH_CLASSIFY_CLAUSE,
    SCOUT_RETURN_CLAUSE,
    SCOUT_SCHEMA,
    SIZING_ASSEMBLE_CMD,
    acceptance_rows_assemble_flags_js,
    research_assemble_flags_js,
    design_route_assemble_flags_js,
    DESIGN_ROUTE_CLAUSE,
    scout_assemble_flags_js,
)
from coordinator_core.ops.dispatch_emit.design_conformance import (
    ANCHOR_PREFIX as _DESIGN_ANCHOR_PREFIX,
    DEFAULT_MET_BY_CLAUSE,
    DESIGN_BRIEF_KEY,
)
from coordinator_core.ops.dispatch_emit import agent_route as _agent_route
from coordinator_core.ops.dispatch_emit import route_dispatch_js as _route_js
from coordinator_core.ops.dispatch_emit import route_preference as _route_pref
from coordinator_core.ops.dispatch_emit.design_route import DESIGN_ROUTE_KEY, STAGE_REFUSAL_PREFIX, next_action as _design_next_action
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import FANOUT_OP, run_parent_copy_rel
from coordinator_core.ops.dispatch_emit.ask_plan_blitz import STAGE_FN as _PLAN_BLITZ_FN
from coordinator_core.ops.dispatch_emit.requirements_ledger import (
    LEDGER_VIEW_FILE,
    NOTES_FILE,
    POST_COMMIT_ACTS,
    STATUSES as LEDGER_STATUSES,
)
from coordinator_core.ops.dispatch_emit.sizing_fire import (
    ARM_M_PLUS,
    ARM_ROADMAP,
    ARM_S,
    ARM_XS,
    SizingFireRefused,
    load_sizing,
    load_sizing_effective,
    resolve_arm,
)
from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import PARAMS_BLOCK_JS, params_path
from coordinator_core.ops.dispatch_emit.terminal_commit_reconcile import TERMINAL_COMMIT_RECONCILE_CLAUSE
from coordinator_core.ops.dispatch_emit.wake_digest import (
    CAP_HELPER_JS,
    TERMINAL_COMMIT_CLI_HELPER_JS,
    TERMINAL_COMMIT_CLI_PROPERTY_JS,
    next_action_parts,
)
from coordinator_core.ops.dispatch_emit.work_label import build_work_label
from coordinator_core.ops.review_mint.execute_review import (
    NO_SLICES_HALT,
    PREP_BUSY_HALT,
    PREP_MALFORMED_HALT,
    REVIEW_MISSING_HALT,
    SLICE_ID_UNKNOWN_HALT,
    compose_execute_review,
    criterion_judge_brief,
    owner_repo_clause_js,
)
from coordinator_core.ops.review_mint.roster import EMIT_ROUTE_PLAN, parse_execute_review
from coordinator_core.ops.review_mint.wave_bookkeeping import review_wave_bookkeeping_stem
from coordinator_core.ops.requirement_register import REGISTER_PIN_DRIFT_KIND, REPIN_OP
from coordinator_core.ops.review_mint.wave_not_run import WAVE_NOT_RUN_VAR
from coordinator_core.ops.workflow_scaffold import _js_string_literal
from coordinator_core.roadmap.post_stamp_clause import (
    EXIT_CRITERION_REFUSAL_HEAD,
    REMEDIATION_MARKER,
    SUITE_TIER_REMEDIATION,
)
from coordinator_core.session.record_homes import home_dir

_OWNER_ROWS_JS = "_crossRepoRows"
_SCOUT_SCHEMA = SCOUT_SCHEMA  # tests/test_ask_compose.py reads this name
_BLITZ_TRAIL = "blitz"
_INVOKE = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke"'
_OVERRIDE_SLOT = "<why your assembled route differs from route_preference; omit when it matches>"
_ANCHOR_CLAUSE = (
    "Repo root: {root} -- run `cd {root}` as its own standalone Bash call first; every "
    "repo-relative path below resolves against it."
)
_REVIEW_RESULT_NAMES = ("_reviewPrep", "_reviewWave", "_deliveryVerdict", "_deliveryNotRun", WAVE_NOT_RUN_VAR, "_reviewIntegration", "_reviewReverify")


class AskComposeRefused(ValueError):
    """The ask cannot be composed into a script; the message names the cause."""


#: Halt reason when the scout twice returned no integer exit_code, i.e. never ran sizing-assemble.
SCOUT_DID_NOT_ASSEMBLE_REASON = (
    "scout did not run sizing-assemble (no exit_code returned after one re-dispatch); "
    "the gate, plan, stage, execute and review phases were skipped"
)
SCOUT_DID_NOT_ASSEMBLE_RETRY = (
    "\n\nYour previous reply carried no exit_code: you did not run sizing-assemble. Run it as your "
    "closing command and return its exit code as exit_code."
)
#: Retry addendum head when the first reply carried a full sizing but no sizing-assemble result; the
#: engine-built command and the closing acts follow it.
SCOUT_VALUES_ASSEMBLE_RETRY = (
    "\n\nYour previous reply carried a full sizing but no sizing-assemble result. Keep those values "
    "unless one is wrong, and keep `name` unchanged. Run exactly this command now, changing nothing "
    "in it:\n\n"
)
_SCOUT_STRING_FIELDS = [k for k in SCOUT_SCHEMA["required"] if SCOUT_SCHEMA["properties"][k]["type"] == "string"]
_SCOUT_ARRAY_FIELDS = [k for k in SCOUT_SCHEMA["required"] if SCOUT_SCHEMA["properties"][k]["type"] == "array"]


def _lit(text: str) -> str:
    return _js_string_literal(text)


def _cat(*parts: str) -> str:
    """JS string-concatenation expression; a part is a quoted literal or a raw `js:` expression."""
    return " + ".join(p[3:] if p.startswith("js:") else _emit._resolve_markers_plus(p) for p in parts)


_UNKEYED_RUN = "unkeyed"


def _agent(
    prompt_expr: str,
    *,
    label: str,
    phase: str,
    agent_type: str,
    schema: dict,
    run_id: str = _UNKEYED_RUN,
    agent_model: Optional[str] = None,
    effort: Optional[str] = None,
) -> str:
    """One `agent(...)` call expression; the prompt ends with the sidecar clause keyed by ``run_id`` and ``label``.

    Trap: a composer call site that omits ``run_id`` files its sidecar under ``_UNKEYED_RUN``;
    ``test_ask_prompts_name_sidecar_dir`` pins that no composed ask script does.
    """
    effort_opt = f"effort: {_lit(effort)}, " if effort else ""
    clause = _lit(_sidecar.sidecar_clause(_sidecar.sidecar_dir("ask", run_id, label)))
    if "coordinator-invoke" in prompt_expr:
        reconcile = (
            TERMINAL_COMMIT_RECONCILE_CLAUSE
            if "dispatch.terminal_commit" in prompt_expr
            else _emit._INDETERMINATE_RECONCILE_CLAUSE
        )
        if reconcile not in prompt_expr:
            clause = _lit(" " + reconcile) + " + " + clause
    return (
        f"agent(({prompt_expr}) + {clause}, {{ label: {_lit(label)}, phase: {_lit(phase)}, "
        f"agentType: {_lit(agent_type)}, {_emit._model_opt(agent_type, agent_model)}, {effort_opt}"
        f"schema: {json.dumps(schema, sort_keys=True)} }})"
    )


#: Effort per stage; the model is the agent type's `emit._AGENT_MODELS` entry. Opus stages
#: (the S plan, the accept ruling, the ceo XL exit) use a type whose definition pins
#: `model: opus`, so `block_ungranted_opus_subagent` admits it and `enforce_agent_model_pin`
#: sees an equal model and a cheaper-or-equal effort.
_MECHANICAL_EFFORT = "low"
#: The gate legs: general-purpose at haiku, until a type that admits their multi-act prompt exists.
_MECHANICAL_AGENT_TYPE = "general-purpose"
_MECHANICAL_MODEL = "haiku"
_PLAN_AUTHOR_AGENT_TYPE = "coordinator:plan-author"
_PLAN_AUTHOR_EFFORT = "low"
_XL_EXIT_EFFORT = "low"
#: Models of DoE-defined types (commit, docs executor, sizing-scout) come from `emit._AGENT_MODELS`.
_COMMIT_AGENT_TYPE = _emit._COMMIT_AGENT_TYPE
_PLAN_REVIEW_AGENT_TYPE = "coordinator:staff-eng"
_PLAN_REVIEW_EFFORT = "low"
#: Every APM ruling in-run: a repo memo never outranks a live PM verbatim (an executor once
#: followed a memo over the ask on the strength of an APM ruling).
_KEEP_THE_ASK = (
    " The PM's verbatim words (the sizing's pm_verbatims, and {record_dir}/ask.md when it exists) "
    "outrank any in-repo memo, plan or doctrine: never rule against them on a memo's strength. "
    "Where they conflict, keep the ask and name the conflict in your ruling."
)


def _obj(required: list[str], properties: dict) -> dict:
    return {"type": "object", "required": required, "properties": properties}


_STR = {"type": "string"}
#: Worded positively: the refused spellings live only in suite_tier_refusal, never in a scout prompt.
EXIT_CRITERION_RULES_CLAUSE = (
    " The exit_criterion names no test tier and no tier-valued argument; "
    f"word verification as \"{SUITE_TIER_REMEDIATION}\"."
)
_GATE_SCHEMA = _obj(
    ["arm", "halt"],
    {
        "arm": {"type": ["string", "null"]},
        "halt": {"type": ["object", "null"]},
        "baton": {"type": ["object", "null"]},
        "sizing_path": {"type": ["string", "null"]},
        "tshirt": _STR,
        "route": _STR,
        "resume_plan": {"type": ["string", "null"]},
        "resume_rows": {"type": ["array", "null"], "items": _STR},
        "spine_plan": {"type": ["string", "null"]},
        RESUME_LEDGER_KEY: {"type": ["object", "null"]},
        DESIGN_BRIEF_KEY: {"type": ["string", "null"]},
        DESIGN_ROUTE_KEY: {"type": ["object", "null"]},
    },
)
_ONLY_ROWS_JS = (
    "...(_gate.resume_plan && _gate.resume_rows ? { only_rows: _gate.resume_rows } : {}), "
)
_RECORDED_SCHEMA = _obj(["ok"], {"ok": {"type": "boolean"}, "error": _STR})


def _ruling_schema(ruling_schema: dict) -> dict:
    """A ruling agent's return: its ruling plus the gate reply of its own record call."""
    return {
        **ruling_schema,
        "properties": {
            **_GATE_SCHEMA["properties"],
            **ruling_schema["properties"],
            "recorded": _RECORDED_SCHEMA,
        },
    }
_ACCEPT_SCHEMA = _obj(
    ["verdict", "pmOnly", "ruling"],
    {
        "verdict": {"type": "string", "enum": ["ruled", "pm-only"]},
        "pmOnly": {"type": "boolean"},
        "pmOnlyGround": _STR,
        "ruling": _STR,
        "statement": _STR,
    },
)
_PLAN_SCHEMA = _obj(["plan_rel"], {"plan_rel": _STR})
_REVISE_SCHEMA = _obj(["plan_rel"], {"plan_rel": _STR, "pull_class": _STR, "verdict": _STR})
_REVIEW_SIGNALS_PROP = {"review_signals": {"type": "array", "items": {"type": "string"}}}
_FALSIFIER_PROP = {"falsifier": {"type": ["object", "null"]}}
_SPINE_REVIEW_SCHEMA = _obj(
    ["plan_rel"], {"plan_rel": _STR, "pull_class": _STR, "verdict": _STR, **_REVIEW_SIGNALS_PROP}
)
_XL_SCHEMA = _obj(
    ["verdict", "pmOnly", "ruling"],
    {
        "verdict": {"type": "string", "enum": ["ruled", "pm-only"]},
        "pick": {"type": "string", "enum": ["roadmap", "accept_multi_session", "split"]},
        "slices": {
            "type": "array",
            "items": _obj(
                ["label", "ask", "rows"],
                {
                    "label": _STR,
                    "ask": _STR,
                    "rows": {"type": "array", "items": _STR},
                    "writes": {"type": "array", "items": _STR},
                },
            ),
        },
        "pmOnly": {"type": "boolean"},
        "pmOnlyGround": _STR,
        "ruling": _STR,
        "parent_id": _STR,
        "runs": {"type": "array", "items": {"type": "object"}},
        "fire": {"type": "array", "items": _STR},
        "error": _STR,
    },
)
_DOCS_SCHEMA = _obj(
    ["doc_paths"],
    {"doc_paths": {"type": "array", "items": _STR}, "note_path": _STR, "stale_docs": {"type": "array", "items": _STR}},
)
_COMMIT_SCHEMA = _obj(
    ["committed"],
    {
        "committed": {"type": "boolean"},
        "sha": {"type": ["string", "null"]},
        "already_landed": {"type": ["boolean", "null"]},
        "error": {"type": ["string", "null"]},
        "refused": {"type": ["string", "null"]},
        "sizing_shipped": {"type": ["object", "null"]},
        "close": {"type": ["object", "null"]},
        "fanout": {"type": ["object", "null"]},
        "siblings": {"type": ["object", "array", "null"]},
        "run_artifacts": {"type": ["object", "null"]},
        "run_artifact_residue": {"type": ["object", "null"]},
    },
)
_DOC_PATHS_JS = (
    "(typeof _docs !== 'undefined' && _docs && Array.isArray(_docs.doc_paths)) ? _docs.doc_paths : []"
)
# The scoped-test verdict as a params key; null before the test leg has run (a pre-test halt).
_TARGETED_TESTS_JS = (
    "targeted_tests: (_testResult && typeof _testResult.status === 'string') ? { status: _testResult.status, "
    "tests_failed: _testResult.tests_failed ?? null, baseline: _testResult.baseline ?? null } : null,"
)
_LEDGER_CHECK_SCHEMA = _obj(
    ["items"],
    {
        "all_of_pm_ask": {"type": "boolean"},
        "items": {
            "type": "array",
            "items": _obj(
                ["id", "status"],
                {
                    "id": _STR,
                    "status": {"type": "string", "enum": list(LEDGER_STATUSES)},
                    "essential": {"type": "boolean"},
                    "withheld_by": _STR,
                    "root_read": {"type": ["string", "array"], "items": _STR},
                    "wired": {"type": "boolean"},
                    "superseded_by": _STR,
                    "post_commit_acts": {"type": "array", "items": {"type": "string", "enum": list(POST_COMMIT_ACTS)}},
                    "remaining": {"type": "array", "items": _STR},
                    "evidence": _STR,
                    "met_by": {"type": ["string", "array"], "items": _STR},
                    "anchor": _STR,
                },
            ),
        },
        "conflicts": {"type": "array", "items": _STR},
    },
)
_LEDGER_ITEMS_RULES = (
    "Rows under the ledger's Out of scope heading are ruled out: give them no item. "
    "Return one entry in items for EVERY ledger item, by "
    "its id (the sizing's requirement_register row id), and on every engine-plane item named above "
    "set root_read to each root you judged it at: status 'met' when the change makes the "
    "item literally true, and wired true when the product actually reaches it (an entry a user, "
    "caller or the engine runs leads to it), not only when code for it exists; 'drifted' "
    "when it was lost or reinterpreted in transit -- a brief, plan, memo or stage narrowed, "
    "softened or re-read it (name where in evidence); 'superseded' only when a LATER revision item "
    "replaces it, named as superseded_by; 'post_commit' when everything the item asks that can be "
    "true before the commit is met and what remains is only an act that exists after the terminal "
    "commit -- the commit itself, reporting the landed sha, or a memo naming it -- named in "
    "post_commit_acts from (" + ", ".join(POST_COMMIT_ACTS) + "); the run's change is uncommitted "
    "by design, so such an act is never a reason for 'not_met'; otherwise 'not_met'. Set essential false only for an "
    "item that is an aside rather than something the PM asked to have delivered; when the ledger "
    "says all_of_pm_ask yes, every item is mandatory and essential does not apply. evidence is one "
    "or two sentences naming the file or behaviour. On every 'not_met' item return remaining: each "
    "part of the item not yet true, naming a post-commit act exactly (" + ", ".join(POST_COMMIT_ACTS) + ") "
    "and any other gap in plain words. On a design-registry item (anchor design-registry:<id>) judged "
    "'met', set met_by to the repo-relative path of the senior-front-end review file of its board, "
    "or of its ratification file; a design-registry item judged 'met' with no met_by is 'not_met'. "
    "On every item copy its anchor from the ledger row into anchor. "
)
_LEDGER_GATED_RULE = (
    ". An item whose only undelivered part is a gated chunk is 'not_met' with withheld_by set to "
    "that chunk id exactly; set withheld_by on no other item. The commit stage runs after this check, so an uncommitted "
    "change is expected here and 'commit' alone in remaining is not a reason to block. Return "
    "all_of_pm_ask as the ledger states it, and list any in-repo memo or doctrine that conflicts "
    "with the ask as conflicts."
)
_LEDGER_DOC_WRITES_RULE = (
    ". When a not_met item's only gap is writing one of those paths, name that path "
    "exactly as its own entry in remaining; the engine settles it from the docs step's "
    "report."
)
_GATED_IDS_JS = "(((_manifest?.gated ?? []).map((g) => g.id).join(', ')) || 'none')"
#: The stage-start read every judgment-bearing stage makes: the EM can revise the ask mid-run
#: (`workflow.revise`), so a stage that read the ask only at emit would act on a stale one.
_LEDGER_CLAUSE = (
    "Requirements ledger: read {ledger} at the start of this stage, before anything else. It holds "
    "every PM requirement for this run in arrival order: the PM's own words and each revision the "
    "EM sent while the run was in flight, and it can grow mid-run. Each item outranks every brief, "
    "plan and in-repo memo; a later revision governs over an earlier item it changes. A revision "
    "a peer session sent through workflow.revise (the row names its sender in `from`) is a ledger "
    "item with the same authority as an EM revision. Carry every item into what you hand on."
)
#: `_LEDGER_CLAUSE` for a relayed ask: the ledger's ask item is an EM's relay, not the PM's words.
_RELAYED_LEDGER_CLAUSE = _LEDGER_CLAUSE.replace(
    "every PM requirement for this run in arrival order: the PM's own words and each revision the "
    "EM sent",
    "every requirement for this run in arrival order: the PM's own words when the sizing records "
    "any, the relayed ask (an EM's relay, not the PM's words; the PM's verbatim words outrank it) "
    "and each revision the EM sent",
)
_MANIFEST_SCHEMA = _obj(
    ["run_dir", "plan_id", "rows", "review_declared_paths", "marker_path"],
    {
        "run_dir": _STR,
        "plan_id": {"type": ["string", "null"]},
        "error": {"type": ["string", "null"]},
        "owed_em_ids": {"type": "array", "items": _STR},
        "owed_plan": _STR,
        "rows": {
            "type": "array",
            "items": _obj(
                ["id", "agent_type", "model", "brief_path", "writes", "wave", "deps"],
                {
                    "id": _STR,
                    "agent_type": _STR,
                    "model": _STR,
                    "brief_path": _STR,
                    "writes": {"type": "array", "items": _STR},
                    "wave": {"type": "integer"},
                    "deps": {"type": "array", "items": _STR},
                    "group": _STR,
                },
            ),
        },
        "fanout": {"type": ["object", "null"]},
        "review_declared_paths": {"type": "array", "items": _STR},
        "test_targets": {"type": "array", "items": _STR},
        "no_test_target": {"type": ["object", "null"]},
        "doc_writes": {"type": "array", "items": _STR},
        "marker_path": _STR,
        "rulable_ids": {"type": "array", "items": _STR},
        "ledger_item_ids": {"type": "array", "items": _STR},
        "withheld_ids": {"type": "array", "items": _STR},
        "gated": {"type": "array", "items": _obj(["id"], {"id": _STR, "reason": _STR, "gate": _STR, "owner_repo": _STR, "owner_root": _STR, "closure_key": {}})},
        "owed": {"type": "array", "items": _obj(["id"], {"id": _STR, "reason": _STR, "detail": _STR})},
        "engine_rows": {"type": "array", "items": _obj(["id"], {"id": _STR, "owner_repo": _STR, "owner_root": _STR, "root_class": _STR, "engine": {"type": "boolean"}})},
    },
)

#: Requires every field `_wholeManifestGap` checks, so a trimmed relay fails the agent's own output
#: validation instead of reaching the fold. `head_sha` is the stage reply's execute-start HEAD
#: (null only when the op could not resolve it).
_MANIFEST_READ_SCHEMA = _obj(
    [*_MANIFEST_SCHEMA["required"], "test_targets", "head_sha"],
    {**_MANIFEST_SCHEMA["properties"], "head_sha": {"type": ["string", "null"]}},
)
#: `_MANIFEST_READ_SCHEMA` for an agent whose stage act is conditional: null passes (`required` and
#: `properties` bind only an object), a relayed object must be whole.
_RELAYED_MANIFEST_SCHEMA = {**_MANIFEST_READ_SCHEMA, "type": ["object", "null"]}

_PLAN_STAGED_SCHEMA = _obj(
    ["plan_rel", "manifest"], {"plan_rel": _STR, "manifest": _MANIFEST_READ_SCHEMA, **_FALSIFIER_PROP}
)
_REVISE_STAGED_SCHEMA = _obj(
    ["plan_rel"],
    {"plan_rel": _STR, "pull_class": _STR, "verdict": _STR, "manifest": _RELAYED_MANIFEST_SCHEMA},
)
_SPINE_REVIEW_STAGED_SCHEMA = _obj(
    ["plan_rel"],
    {
        "plan_rel": _STR, "pull_class": _STR, "verdict": _STR, "manifest": _RELAYED_MANIFEST_SCHEMA,
        **_REVIEW_SIGNALS_PROP, **_FALSIFIER_PROP,
    },
)
#: The ledger-resume re-plan agent's return: the rows it added, and the stage it runs over exactly those.
_REPLAN_STAGED_SCHEMA = _obj(
    ["plan_rel", "added_rows"],
    {
        "plan_rel": _STR, "added_rows": {"type": "array", "items": _STR}, "manifest": _RELAYED_MANIFEST_SCHEMA,
        "error": {"type": ["string", "null"]},
    },
)
#: The pre-sized gate agent's return: the gate reply plus the stage it runs as its closing act.
_GATE_STAGED_SCHEMA = {
    **_GATE_SCHEMA,
    "properties": {
        **_GATE_SCHEMA["properties"],
        "manifest": _RELAYED_MANIFEST_SCHEMA,
        "stage_error": {"type": ["string", "null"]},
    },
}
#: The size-reword agent's return: the reworded criterion, its own sizing-assemble result, and the
#: gate reply (plus stage manifest) it runs as closing acts.
_REWORD_SCHEMA = _obj(
    ["exit_criterion"],
    {**_GATE_STAGED_SCHEMA["properties"], **ASSEMBLE_SCHEMA["properties"], "exit_criterion": _STR},
)
#: The ask scout's return with its relayed stage manifest held to the strict schema.
_ASK_SCOUT_STAGED_SCHEMA = {
    **ASK_SCOUT_SCHEMA,
    "properties": {
        **ASK_SCOUT_SCHEMA["properties"],
        "manifest": _RELAYED_MANIFEST_SCHEMA,
        "stage_error": {"type": ["string", "null"]},
    },
}
#: The blitz-stage-read leg's return: the manifest read from disk, or the stage op's own refusal.
_BLITZ_STAGE_READ_SCHEMA = _obj(
    [], {"manifest": _RELAYED_MANIFEST_SCHEMA, "stage_error": {"type": ["string", "null"]}}
)


_LEDGER_PREFIX_VAR = "_LEDGER_PREFIX"
#: JS expression: the gate's design brief ahead of an agent's task text; '' when the gate carried none.
_DESIGN_PREFIX_JS = f"(_gate?.{DESIGN_BRIEF_KEY} ? String(_gate.{DESIGN_BRIEF_KEY}) + '\\n\\n' : '')"

#: An engine-owned fold site returns the stage reply under `_MANIFEST_READ_SCHEMA`, so a trimmed relay
#: fails the agent's own output validation; `_adoptManifest` admits a relay only when
#: `_wholeManifestGap` finds no gap. A refused stage lands in `_stageRefusal`; a not-whole relay
#: lands in `_relayGap` (halts by name) unless `soft` (the plan-blitz arm, which re-reads on a gap).
_ADMIT_MANIFEST_JS = (
    "  let _stageRefusal = null;\n"
    "  let _stageOwedEm = null;\n"
    "  let _relayGap = null;\n"
    "  let _diskHalt = null;\n"
    "  const _wholeManifestGap = (m) => {\n"
    "    const ne = (v) => typeof v === 'string' && v.trim() !== '';\n"
    "    if (!m || typeof m !== 'object' || Array.isArray(m)) return 'manifest';\n"
    "    if (m.error) return 'error: ' + String(m.error);\n"
    "    for (const k of ['rows', 'review_declared_paths', 'test_targets']) { if (!Array.isArray(m[k])) return k; }\n"
    "    for (let i = 0; i < m.rows.length; i++) {\n"
    "      const r = m.rows[i];\n"
    "      if (!r || typeof r !== 'object') return 'rows[' + i + ']';\n"
    "      for (const k of ['id', 'brief_path', 'agent_type', 'model']) { if (!ne(r[k])) return 'rows[' + i + '].' + k; }\n"
    "      if (!Number.isInteger(r.wave)) return 'rows[' + i + '].wave';\n"
    "      for (const k of ['writes', 'deps']) { if (!Array.isArray(r[k])) return 'rows[' + i + '].' + k; }\n"
    "    }\n"
    "    for (const k of ['gated', 'owed']) {\n"
    "      if (m[k] === undefined || m[k] === null) continue;\n"
    "      if (!Array.isArray(m[k])) return k;\n"
    "      for (let i = 0; i < m[k].length; i++) { if (!m[k][i] || !ne(m[k][i].id)) return k + '[' + i + '].id'; }\n"
    "    }\n"
    "    if (!/^[0-9a-f]{40}$/.test(String(m.head_sha ?? ''))) return 'head_sha';\n"
    "    return null;\n"
    "  };\n"
    "  const _adoptManifest = (m, soft) => {\n"
    "    if (m === null || m === undefined || typeof m !== 'object' || Array.isArray(m)) return;\n"
    # A soft (relayed) reply's `error` is agent prose -- a relay that shortens an oversized
    # manifest writes its note there -- so it never refuses; the disk read that follows decides.
    "    if (m.error) { if (!soft && !_stageRefusal) { _stageRefusal = String(m.error); "
    "if (Array.isArray(m.owed_em_ids) && m.owed_em_ids.length) _stageOwedEm = { ids: m.owed_em_ids.map(String), "
    "plan: typeof m.owed_plan === 'string' ? m.owed_plan : null }; } return; }\n"
    "    const gap = _wholeManifestGap(m);\n"
    "    if (gap) { if (!soft && !_relayGap) _relayGap = gap; return; }\n"
    "    _stagedManifest = m;\n"
    "  };\n"
    "  // Any non-empty readyOpError is the stage op's own refusal, except plan-blitz's 'did not run'\n"
    "  // sentinel (the op never ran; the one stage leg decides). The design-review prefix is matched\n"
    "  // later, on the adopted text.\n"
    "  const _adoptReadyError = (e) => {\n"
    "    const _re = String(e.readyOpError ?? '').trim();\n"
    "    if (!_stagedManifest && !_stageRefusal && _re !== '' && !/did not run/.test(_re)) _stageRefusal = _re;\n"
    "  };"
)
#: The run reads its commit token from the Workflow `args` the emit's fire args carry, so the
#: script's bytes never hold it (any agent or peer can read the script).
_RUN_TOKEN_DECL_JS = (
    "  const _RUN_TOKEN = (() => { try { const a = (typeof args === 'string') ? JSON.parse(args) : "
    "(typeof args === 'undefined' ? null : args); return (a && typeof a.runToken === 'string' "
    "&& /^[0-9a-f]{32}$/.test(a.runToken)) ? a.runToken : null; } catch (_e) { return null; } })();"
)

#: A dispatched agent runs the definition loaded at session start, so a run that edits an
#: `agents/*.md` file would review or verify against stale text. Every prompt names the run's
#: edited definitions and has the agent read its own from disk.
_AGENT_DEFS_FN = "_agentDefsClause"
_AGENT_DEFS_MARKER = f"{_emit._SHARED_PATH_MARKER_DELIM}{_AGENT_DEFS_FN}(){_emit._SHARED_PATH_MARKER_DELIM}"
_AGENT_DEFS_HELPER_JS = (
    f"  function {_AGENT_DEFS_FN}() {{\n"
    "    const _edited = [...new Set([...(_writes ?? []), ...((_manifest && !_manifest.error && "
    "_manifest.review_declared_paths) || [])])].filter((p) => /(^|\\/)agents\\/[^/]+\\.md$/.test(String(p)));\n"
    "    if (!_edited.length) return '';\n"
    "    return 'Agent definitions this run edits: ' + _edited.join(', ') + '. The definition you were "
    "started with was loaded before this run and can be stale: if your own agent type is defined by "
    "one of those files, read that file from disk under the repo root now and follow its current text "
    "wherever it differs.\\n\\n';\n"
    "  }"
)


def _ledger_shadowed(blitz_fn: str, *, em_brief: bool) -> str:
    """`planBlitz` defined inside a scope whose `agent` prefixes the ledger clause, so every
    plan-blitz prompt reads the requirements ledger first. An EM-brief shadow inside the function
    calls `_askAgent`, rebound here to the ledger-prefixed agent: the brief and the ledger compose."""
    rebind = "    const _askAgent = agent;\n" if em_brief else ""
    return (
        "  const _outerAgent = agent;\n"
        f"  const {_PLAN_BLITZ_FN} = (() => {{\n"
        f"    const agent = (p, o) => _outerAgent({_DESIGN_PREFIX_JS} + {_LEDGER_PREFIX_VAR} + {_AGENT_DEFS_FN}() + p, o);\n"
        f"{rebind}{blitz_fn}\n"
        f"    return {_PLAN_BLITZ_FN};\n"
        "  })();"
    )


_ROW_VERIFY_PROMPT_OPEN = "`Run and report on the scoped test target(s) for ${id}: `"


def _test_instruction_js(review_edits_base: str) -> str:
    """JS expression for the verify agent's scoped-test instructions, scoped at run time to the manifest's test targets.

    The plan is authored inside the run, so the scope cannot resolve at compose time; the stage
    resolves `test_targets` through `pathspec.terminal_test_scope`. Trap: the runner is told to
    report raw evidence and never gate, and the targets are scoped, never a tier.
    """
    return _cat(
        "Run the scoped test targets: [",
        "js:(_manifest.test_targets ?? []).join(', ')",
        "]. "
        + _emit._review_edits_clause(review_edits_base)
        + " "
        + _emit._test_runner_clause()
        + " "
        + (_emit._baseline_context_clause(review_edits_base) + " " if _emit._is_full_sha(review_edits_base) else "")
        + "Report raw evidence; do not gate. Write your record and return its path as sidecar_path -- required.",
    )


_USAGE_LIMIT_RE = r"/(usage|session|rate|weekly|5-hour) limit|limit reached|resets \d{1,2}(:\d{2})?\s*(am|pm)?|quota/i"
HALT_USAGE_LIMIT = "usage_limit"
HALT_REVIEW_BLOCKED = "review_blocked"
HALT_CRITERION_NOT_MET = "criterion_not_met"
HALT_DELIVERY_FAIL = "delivery_fail"
HALT_NOT_CLOSEABLE = "not_closeable"
HALT_STOP_RULE = "stop_rule"

#: A slice reviewer (`slice-review-result`) is BLOCKED with `findings` > `applied`; whole-diff lenses and
#: null (degraded / verify-only) wave entries never match.
def _review_blocked_halt_js(sizing_rel: Optional[str]) -> str:
    """The BLOCKED-review halt; its next_action names the engine-ask refire, since journal replay
    hands back the cached BLOCKED wave."""
    return (
        "  if (!_halted) {\n"
        "    const _blockedSlices = (Array.isArray(_reviewWave) ? _reviewWave : []).filter("
        "(w) => w && w.verdict === 'BLOCKED' && (w.findings ?? 0) > (w.applied ?? 0));\n"
        "    if (_blockedSlices.length) _halted = { halted: " + json.dumps(HALT_REVIEW_BLOCKED) + ", run_id: _runId, "
        "unapplied_findings: _blockedSlices.reduce((n, w) => n + (w.findings - (w.applied ?? 0)), 0), "
        "slices: _blockedSlices.map((w) => ({ slice_id: w.slice_id ?? null, findings: w.findings, "
        "applied: w.applied ?? 0, sidecar_path: w.sidecar_path ?? null })), "
        "next_action: 'Fix the unapplied findings named in each slice sidecar, then refire: ' + "
        + _refire_js(sizing_rel) + " };\n"
        "  }"
    )


#: A test leg owed by the manifest whose verdict is absent, has no sidecar, or reads not_run never
#: reaches the commit as passed. `TEST_GUARD` is substituted with the leg's own owed-condition.
def _test_verdict_empty_halt_js(sizing_rel: Optional[str]) -> str:
    """The empty-test-verdict halt; its next_action names the engine-ask refire, which re-runs the
    verify leg, where journal replay would hand back the cached empty verdict."""
    return (
        "    if (!_halted && (TEST_GUARD) && !(_testResult && typeof _testResult.status === 'string' "
        "&& _testResult.status && _testResult.status !== 'not_run' "
        "&& typeof _testResult.sidecar_path === 'string' && _testResult.sidecar_path.trim())) "
        "{ _halted = { halted: " + json.dumps("test_verdict_empty") + ", run_id: _runId, "
        "detail: 'the test leg returned no verdict (empty sidecar or not_run); the run did not pass its tests', "
        "next_action: 'Refire to re-run the test leg: ' + " + _refire_js(sizing_rel) + " }; }"
    )


def _usage_limit_helper_js() -> str:
    """`_haltOnUsageLimit(e)`: a limit-shaped agent failure becomes a resumable `_halted`; any other error rethrows.

    The run id the harness resumes from is not visible to the script, so the halt carries a hint to
    read it from the Workflow result.
    """
    return (
        f"  const _USAGE_LIMIT_RE = {_USAGE_LIMIT_RE};\n"
        "  function _haltOnUsageLimit(e) {\n"
        "    const msg = String((e && (e.message ?? e)) ?? '');\n"
        "    if (!_USAGE_LIMIT_RE.test(msg)) throw e;\n"
        f"    _halted = {{ halted: {_lit(HALT_USAGE_LIMIT)}, run_id: _runId, detail: msg.slice(0, 300), "
        "resume_from_run_id: 'this Workflow run id (wf_...)', "
        "next_action: 'After the limit resets, call Workflow with this scriptPath and resumeFromRunId set to this run id.' };\n"
        "  }"
    )


_PLAN_SLOT = "PLAN_PATH_SLOT_X"
_RUNTIME_PLAN_PATH_JS = "(_planRel ?? _sizingRel)"
_EVIDENCE_SLOT = "EVIDENCE_PATH_SLOT_X"
#: `<plan-stem>.evidence.yaml` beside the run's plan, which is unknown until run time.
_RUNTIME_EVIDENCE_PATH_JS = "((_planRel ?? _sizingRel).replace(/\\.[^./]+$/, '') + '.evidence.yaml')"
_JUDGE_PATH_CLAUSE = (
    "\n\nThe plan_path below is the plan, or on the XS route (no plan is authored) the sizing: "
    "then its exit_criterion is the spec and there is no Verification section."
)
#: The scout's folded falsifier act. Trap: the scout assembled the criterion, so the probe must hit
#: the criterion's observable outcome and never the scout's own premise evidence.
_FALSIFIER_ACT = (
    "The observation runs through the production entry point the criterion names -- the command, "
    "import path or call site a user reaches -- and probes the criterion's observable outcome, never "
    "your premise evidence and never a unit test of one helper. Run it now against the unchanged tree "
    "and record what it prints as baseline_output: it must show the criterion FALSE today. Write no "
    "repo file. The how runs this repo's own code from the working tree, a module or test run from "
    "the repo root; the installed CLI and warm engine serve the published engine and cannot see "
    "this run's uncommitted change. It probes observable output, not an internal name the change "
    "may rename. Return how (one runnable observation, verbatim), baseline_output and "
    "expected_when_true (what the same observation shows once the criterion holds)."
)
#: The plan-authoring legs' falsifier act, for every arm with no in-run scout to fold it into (a presized
#: ask, an M+ spine review) and for a scout that returned none. Same return shape as the scout fold.
_PLAN_FALSIFIER_CLAUSE = (
    " Also author the baseline falsifier for the sizing's prime exit criterion (its exit_criterion.statement). "
    + _FALSIFIER_ACT + " Return it as `falsifier`."
)
#: JS arrow normalising a leg's `falsifier` reply into the `_falsifierSpec` shape; null unless all three fields are text.
_AS_PLAN_FALSIFIER_JS = (
    "const _asPlanFalsifier = (f) => (f && [f.how, f.baseline_output, f.expected_when_true]"
    ".every((s) => typeof s === 'string' && s.trim())) ? { how: f.how, baseline_output: f.baseline_output, "
    "expected_when_true: f.expected_when_true, authored_by: 'plan-author' } : null;"
)
#: Rides the judge prompt at run time; XS authors none, so `_falsifierSpec` may be null.
_FALSIFIER_SUFFIX_JS = (
    "(_falsifierSpec ? '\\n\\nrecorded falsifier (authored before any change by ' + (_falsifierSpec.authored_by ?? 'unknown') "
    "+ '; run its how verbatim at HEAD, "
    "compare to expected_when_true and baseline_output, and rule not_met when it still shows the baseline). "
    "When its how cannot run as written (a missing name, an engine that predates the change, a refused fixture), "
    "report falsifier verdict error with the cause in output_anchor; fail is only for a how that ran and still shows the baseline. "
    "Judge the criterion from your own observation: ' "
    "+ JSON.stringify(_falsifierSpec) : '')"
)


def _criterion_judge_brief(
    review, stage_schemas: dict, *, run_base_sha: str, intent: str, ledger_charter: str
) -> Optional[tuple[str, dict]]:
    """The roster judge's `(prompt, result schema)` for the verify agent, or None without a judge.

    `criterion_judge_brief` takes `plan_path` as a compile-time literal; the slot stands in and is
    spliced out for the run-time marker naming the plan (or XS sizing). The plan's own falsifier is
    unknown until the plan exists, so the run's pre-change `_falsifierSpec` rides the prompt's
    run-time suffix instead (null at XS).
    """
    brief = criterion_judge_brief(
        review,
        stage_schemas=stage_schemas,
        plan_path=_PLAN_SLOT,
        run_base_sha=run_base_sha,
        prompt_head=intent + _JUDGE_PATH_CLAUSE,
        ledger_charter=ledger_charter,
        evidence_path=_EVIDENCE_SLOT,
    )
    if brief is None:
        return None
    prompt, schema = brief
    if prompt.count(_PLAN_SLOT) != 2:
        raise AskComposeRefused("criterion judge prompt no longer names its plan path exactly twice")
    if prompt.count(_EVIDENCE_SLOT) != 1:
        raise AskComposeRefused("criterion judge prompt no longer names the evidence sidecar exactly once")
    marker = _emit._SHARED_PATH_MARKER_DELIM
    prompt = prompt.replace(_EVIDENCE_SLOT, f"{marker}{_RUNTIME_EVIDENCE_PATH_JS}{marker}")
    return prompt.replace(_PLAN_SLOT, f"{marker}{_RUNTIME_PLAN_PATH_JS}{marker}"), schema


_VERIFY_INTRO = (
    f"phase: {ASK_VERIFY_PHASE}\n\nYou are the run's one closing agent: it replaces the separate scoped-test, "
    "docs and criterion-judge agents. The run's change is on disk, uncommitted. Do these three steps in "
    "order. Your write set is exactly step 2's declared doc writes and its delivery note (and any verdict "
    "report step 2 names); in steps 1 and 3 "
    "you edit, stage and commit nothing, and you never change a file to make a requirement true.\n\n"
)
_VERIFY_NO_TESTS = "Run no tests: this run has no scoped test target to run. Omit `test`."
_VERIFY_NO_DOCS = "Write no docs on this run: omit doc_paths, note_path and stale_docs."


def _verify_call(
    *,
    head: str,
    anchor: str,
    test_js: str,
    docs_parts: Sequence[str],
    verdict_parts: Sequence[str],
    judged: bool,
    schema: dict,
    run_id: str,
) -> str:
    """The merged tail's one `agent(...)` call EXPRESSION: scoped tests, declared docs, then the
    criterion and ledger verdict, in that order inside one coordinator:executor agent.

    Trap: the verdict follows the agent's own test run, and its write set is bounded in the prompt
    to `_manifest.doc_writes` plus the delivery note; the engine's reconcile (not this prompt) is
    what keeps a path outside that set from clearing a ledger item.
    """
    docs_js = (
        f"js:(_docsWanted ? ({_cat(*docs_parts)}) : {_lit(_VERIFY_NO_DOCS)})" if docs_parts else _VERIFY_NO_DOCS
    )
    prompt = _cat(
        f"{head}\n\n{anchor}\n\n",
        _VERIFY_INTRO + "(1) TESTS. Start the scoped tests first and let them run in the background while "
        "you do step 2. ",
        f"js:(_testWanted ? ({test_js}) : {_lit(_VERIFY_NO_TESTS)})",
        "\n\n(2) DOCS. ",
        docs_js,
        "js:(_verdictReports.length ? ' Also write each of these files with the Write tool (create its directory "
        "if missing), holding exactly its text and nothing else: ' + JSON.stringify(_verdictReports) : '')",
        "\n\n(3) VERDICT. Once the test result is in, read it before you rule: a failing or degraded "
        "result is evidence for the verdict, never the verdict itself. ",
        *verdict_parts,
        "\n\nReturn one object: `test` (step 1's result, status and summary included; omit it when no "
        "tests ran), "
        + ("`judge` (the criterion verdict fields the judge brief above names, its status included), " if judged else "")
        + "`items`, `all_of_pm_ask` and `conflicts` (the ledger verdict), and `doc_paths`, `note_path` and "
        "`stale_docs` (step 2).",
    )
    return _agent(
        prompt, label=ASK_VERIFY_LABEL, phase=ASK_VERIFY_PHASE, agent_type=_emit._EXECUTOR_AGENT_TYPE,
        schema=schema, run_id=run_id,
    )


def _verify_schema(judge_schema: Optional[dict]) -> dict:
    """The merged leg's result schema: `ask_contract.ASK_VERIFY_RETURN_KEYS`, with the test result
    and the judge verdict nested because their `status` keys collide."""
    properties = {
        "test": json.loads(_emit.stage_schema_literal("test_result")),
        **_LEDGER_CHECK_SCHEMA["properties"],
        **_DOCS_SCHEMA["properties"],
    }
    required = ["items"]
    if judge_schema is not None:
        properties["judge"] = judge_schema
        required.append("judge")
    return _obj(required, properties)


def _fire_tier_js() -> str:
    """JS expression for the execute wave's tier at fire time, from `_gate.tshirt`.

    The map is rendered from `emit._TSHIRT_TO_REVIEW_TIER`; an absent or unknown tshirt reads
    `full`, which keeps every roster agent.
    """
    return f"({json.dumps(_emit._TSHIRT_TO_REVIEW_TIER, sort_keys=True)}[_gate?.tshirt] ?? 'full')"


def _folded_ledger_charter(
    *, relayed: bool, ledger_rel: str, record_dir: str, run_dir: str, in_run_commit: bool
) -> str:
    """The requirements-ledger check as a charter the criterion judge carries.

    The judge reads the plan and the sizing itself; the gated-chunk list and the engine-plane
    clause reach it as a run-time suffix of the verify prompt (`compose_ask_script`).
    """
    return (
        "Ledger check, folded into this verdict (no separate ledger-check agent runs): also judge "
        "the result against every requirement "
        + ("on record (the PM's, and the relayed ask, which is not the PM's words)" if relayed else "the PM has on record")
        + ", never against the plan alone. "
        f"Re-read the requirements ledger at {ledger_rel} now (revisions may have landed since the run "
        f"started), then {record_dir}/{PM_INTENT_FILE} when it exists, the change, and the briefs under "
        f"{run_dir}/briefs. "
        + _LEDGER_ITEMS_RULES
        + "The manifest's withheld (gated) chunks close this prompt. Return items, all_of_pm_ask and "
        "conflicts beside your criterion verdict"
        + _LEDGER_GATED_RULE
        + (_LEDGER_DOC_WRITES_RULE if in_run_commit else "")
    )


def _known_arm(repo_root: str, sizing_rel: Optional[str]) -> Optional[str]:
    """The arm of an existing sizing, or None when unreadable (the gate verb owns that refusal)."""
    if not sizing_rel:
        return None
    try:
        return resolve_arm(load_sizing(Path(repo_root), sizing_rel))
    except (SizingFireRefused, OSError, ValueError, KeyError):
        return None


def _intake_sizing(repo_root: str, sizing_rel: Optional[str]) -> Optional[dict]:
    """The sizing at `sizing_rel` when it is an unsized intake mint, else None."""
    if not sizing_rel:
        return None
    from coordinator_core.ops.sizing_intake_contract import is_intake

    try:
        sizing = load_sizing(Path(repo_root), sizing_rel)
    except (SizingFireRefused, OSError, ValueError, KeyError):
        return None
    return sizing if is_intake(sizing) else None


def _source_memo_clause(repo_root: str, prompt: Optional[str], sizing_rel: Optional[str]) -> str:
    """The planner clause naming the inbox memo(s) the ask or its sizing names; empty when none."""
    from coordinator_core.ops.dispatch_emit.source_memo_ref import inbox_memo_refs, sizing_texts

    texts: list = [prompt]
    if sizing_rel:
        try:
            texts.extend(sizing_texts(load_sizing(Path(repo_root), sizing_rel)))
        except (SizingFireRefused, OSError, ValueError, KeyError):
            pass
    found: dict[str, None] = {}
    for t in texts:
        if isinstance(t, str):
            for ref in inbox_memo_refs(t):
                found.setdefault(ref)
    refs = list(found)
    if not refs:
        return ""
    if len(refs) == 1:
        return (
            f"Source memo: set plan frontmatter `source_memo: {refs[0]}`. The close-out memo-lifecycle "
            "leg resolves it against the landed sha; author no row that edits, closes or sends it.\n\n"
        )
    listed = ", ".join(f"`{r}`" for r in refs)
    return (
        f"Source memos named: {listed}. The close-out memo-lifecycle leg resolves one: set plan "
        f"frontmatter `source_memo: {refs[0]}` and name the rest in an open question. Author no row "
        "that edits, closes or sends any of them.\n\n"
    )


def _xl_exit_open(repo_root: str, sizing_rel: Optional[str]) -> bool:
    """An existing sizing whose XL exit the APM picks in-run: `pm-decision`, no `xl_exit`, ceo mode."""
    if not sizing_rel:
        return False
    from coordinator_core.ops.sizing_acceptance import apm_picks_xl_exit

    try:
        sizing = load_sizing_effective(Path(repo_root), sizing_rel)
    except (SizingFireRefused, OSError, ValueError, KeyError):
        return False
    return (
        sizing.get("route") == "pm-decision"
        and sizing.get("xl_exit") is None
        and apm_picks_xl_exit(sizing.get("interaction_mode"))
    )


def _read_plan_blitz() -> str:
    from coordinator_core.ops.dispatch_emit.ask_plan_blitz import load_plan_blitz_text

    return load_plan_blitz_text()


def load_review_signal_selects(plugin_root: Optional[str] = None) -> dict[str, str]:
    """`{signal: selects}` from DoE's contract/review-signals.json under the resolved plugin root, or refuse by name.

    An unresolved root yields `{}` (no signal routing); a missing or unparseable file refuses.
    """
    from coordinator_core.ops.dispatch_emit.ask_plan_blitz import AskPlanBlitzRefused

    if plugin_root is None:
        from coordinator_core.warm.caller_context import resolve_caller_context

        plugin_root = resolve_caller_context().plugin_root
    if not plugin_root:
        # Reachable only with an injected plan-blitz text: the plugin-read path refuses an unresolved root first.
        return {}
    source = Path(plugin_root) / "contract" / "review-signals.json"
    try:
        signals = json.loads(source.read_text(encoding="utf-8"))["signals"]
        return {str(name): str(entry["selects"]) for name, entry in signals.items()}
    except FileNotFoundError:
        raise AskPlanBlitzRefused(f"no review-signals.json at {source}") from None
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        raise AskPlanBlitzRefused(f"unreadable review-signals.json at {source}: {exc}") from None


_PHASE_CALL_TITLES = {"execute": _emit._EXECUTE_PHASE_TITLE}


def _carried_titles(lines: Sequence[str]) -> set[str]:
    """Phase titles some composed agent call carries as its `phase:` option."""
    return agent_options_phase_titles("\n".join(lines))


def _phase_titles(
    *,
    carried: set[str],
    blitz_phases: list[str],
    review_titles: list[str],
    through_plan: bool = False,
    extra: Sequence[str] = (),
) -> list[str]:
    """One title per serial group, in run order: the contract phases and the review titles that an
    agent in this script carries, plus plan-blitz's own titles (DoE's, passed through).

    Agents that run together carry one title (the verify leg), so a concurrent group reads as one
    row, and a title no agent carries is never listed.
    """
    titles: list[str] = []

    def add(title: str) -> None:
        if title not in titles:
            titles.append(title)

    for phase in ASK_PHASES:
        if phase == "review":
            for title in review_titles:
                if title in carried:
                    add(title)
            continue
        title = _PHASE_CALL_TITLES.get(phase, phase)
        if title in carried:
            add(title)
        if phase == "plan":
            for title in blitz_phases:
                add(title)
            if through_plan:
                return titles
    for title in extra:
        add(title)
    return titles


_REVIEW_NOOP_EXIT = "return { halted: 'no-op',"
_REVIEW_NOOP_TAIL = "wave: null, integration: null }; }"
_REVIEW_MISSING_GUARD = "  if (_waveRaw.some(r => r && r.prep && !r.prep.error"
_REVIEW_PREP_BIND = "  _reviewPrep = (() => {\n"
_REVIEW_SLICES_GUARD = "  if (!_reviewPrep || (!(Array.isArray(_reviewPrep.slices) ? _reviewPrep.slices : []).length && !_verifyOnly)) {"


def _single_exit_review(blocks: list[str]) -> str:
    """The review blocks with the prep guards' no-op `return` turned into a `_halted` assignment.

    The guards sit in the review-wave block (`prep_in_wave`), so every later block runs under
    `if (!_halted)` and the no-op reaches the terminal return like any other halt.
    """
    prep = blocks[0]
    if prep.count(_REVIEW_NOOP_EXIT) != 1 or _REVIEW_NOOP_TAIL not in prep or _REVIEW_SLICES_GUARD not in prep:
        raise AskComposeRefused("review prep block no longer has the no-op exit shape single-exit composition rewrites")
    busy_exit = f"return {{ halted: '{PREP_BUSY_HALT}'"
    missing_exit = f"return {{ halted: '{REVIEW_MISSING_HALT}'"
    malformed_exit = f"return {{ halted: '{PREP_MALFORMED_HALT}'"
    malformed_guard = "  if (_malformed.length) {"
    slice_id_exit = f"return {{ halted: '{SLICE_ID_UNKNOWN_HALT}'"
    slice_id_guard = "  if (_badSliceIdAt >= 0) {"
    if (
        prep.count(malformed_exit) != 1
        or prep.count(malformed_guard) != 1
        or prep.count(busy_exit) != 1
        or prep.count(missing_exit) != 1
        or prep.count(_REVIEW_MISSING_GUARD) != 1
        or prep.count(_REVIEW_PREP_BIND) != 1
        or prep.count(slice_id_exit) != 1
        or prep.count(slice_id_guard) != 1
    ):
        raise AskComposeRefused("review prep block no longer has the prep-busy and review-missing exit shape single-exit composition rewrites")
    prep = prep.replace(busy_exit, busy_exit.replace("return ", "_halted = ", 1))
    prep = prep.replace(malformed_exit, malformed_exit.replace("return ", "_halted = ", 1))
    prep = prep.replace(malformed_guard, "  if (!_halted && _malformed.length) {", 1)
    prep = prep.replace(missing_exit, missing_exit.replace("return ", "_halted = ", 1))
    prep = prep.replace(_REVIEW_MISSING_GUARD, _REVIEW_MISSING_GUARD.replace("if (", "if (!_halted && ", 1), 1)
    prep = prep.replace(_REVIEW_PREP_BIND, "  if (!_halted) _reviewPrep = (() => {\n", 1)
    prep = prep.replace(slice_id_exit, slice_id_exit.replace("return ", "_halted = ", 1))
    prep = prep.replace(slice_id_guard, "  if (!_halted && _badSliceIdAt >= 0) {", 1)
    prep = prep.replace(_REVIEW_NOOP_EXIT, "_halted = { halted: 'no-op',")
    prep = prep.replace(_REVIEW_SLICES_GUARD, "  if (!_halted && (!_reviewPrep || (!(Array.isArray(_reviewPrep.slices) ? _reviewPrep.slices : []).length && !_verifyOnly))) {", 1)
    prep = prep.replace(f"return {{ halted: '{NO_SLICES_HALT}'", f"_halted = {{ halted: '{NO_SLICES_HALT}'", 1)
    rest = "\n\n".join(blocks[1:])
    return prep + ("\n\n  if (!_halted) {\n" + rest + "\n  }" if rest else "")


def _row_sidecar_js(run_id: str, label_prefix: str) -> str:
    """JS expression for the sidecar clause of a per-row leg whose label is ``label_prefix + r.id`` at fire time."""
    return _sidecar.sidecar_clause_js("ask", run_id, f"{label_prefix} + r.id")


def _reviewer_row_js(anchor: str, label_prefix: str, run_id: str) -> list[str]:
    """The execute-loop branch for a blinded falsifier-integrity reviewer row: the agent is given
    only the repo anchor and its brief path (no preamble, no ledger clause -- the brief is the
    phase's own spec prompt); its structured verdict is queued in `_verdictReports` as JSON for
    the row's dispatch report path, and the closing verify agent writes it. `_runRow` reads the
    returned `DONE:` line.
    Trap: the read-only reviewer never holds the Write; the verify agent does."""
    prompt = _cat(
        f"{anchor}\n\n", "Your brief is the file ", "js:r.brief_path",
        " -- read it completely, then execute it as written.", "js:" + _row_sidecar_js(run_id, label_prefix),
    )
    report = (
        f"{_lit(_emit._DISPATCH_REPORT_DIR + '/')} + {_RUNTIME_PLAN_PATH_JS}.split('/').pop().slice(0, -3)"
        " + '/' + r.id + '.md'"
    )
    reviewer = (
        f"agent({prompt}, {{ label: {label_prefix} + r.id, phase: {_lit(_emit._EXECUTE_PHASE_TITLE)}, "
        f"agentType: r.agent_type, model: r.model, schema: {_emit.stage_schema_literal('falsifier_integrity_result')} }})"
    )
    return [
        f"      if (r.agent_type === {_lit(REVIEWER_AGENT_TYPE)}) _rows[r.id] = _runRow(r.id, "
        "(r.deps ?? []).map((d) => _rows[d]), null, async () => {",
        f"        const _v = await {reviewer};",
        "        if (!_v || !_v.verdict) { return 'BLOCKED: the reviewer returned no verdict'; }",
        f"        _verdictReports.push({{ path: {report}, text: JSON.stringify(_v) }});",
        "        return 'DONE: ' + r.id + ' verdict ' + _v.verdict;",
        "      });",
    ]


#: Adapter from a manifest row's `route` (`agent_route.Route.to_json`) to `_routedRun`'s route shape.
_ROUTE_ADAPTER_JS = (
    "  const _routeOf = (r) => ({ workClass: r.route.work_class, "
    "triage: r.route.triage ? r.route.triage.agent_type : null, "
    "primary: r.route.primary.agent_type, targets: Object.keys(r.route.targets) });"
)


def _routing_enabled(repo_root: str) -> bool:
    """True when the repo carries a route table. The manifest is staged at run time, so the script
    cannot see a row's `route` at compose time; a repo with no table emits no routing bytes."""
    try:
        return _agent_route.load_route_table(repo_root) is not None
    except (_agent_route.RouteTableError, OSError):  # fail open: a malformed table was already refused at stage
        return False


def _routed_row_js(work_label: str, exec_phase: str) -> str:
    """JS expression dispatching routed row `r` through `_routedRun` (never `_groupRun`)."""
    return (
        "_routedRun(_routeOf(r), (t, extra) => agent(_rowPrompt(r) + extra, "
        f"{{ label: {work_label} + r.id, phase: {exec_phase}, agentType: t, "
        f"model: r.route.targets[t] ?? (r.route.triage && r.route.triage.agent_type === t ? r.route.triage.model : r.model), stallMs: {_emit._EXECUTOR_STALL_MS} }}))"
    )


def _row_runner_js() -> str:
    """Declarations `_emit._run_row_helper_js` reads, with no plan-scoped halting (one run, one plan)."""
    return "\n".join(
        [
            _emit._row_state_decls_js({}, ()),
            f"  const _ROW_VERIFY_SCHEMA = {_emit.stage_schema_literal('row_verification_result')};",
            _emit._run_row_helper_js(None, None, "_rowVerifyWanted"),
        ]
    )


_SIZING_SKILL_SOURCE = ("skills", "sizing", "SKILL.md")


def _sizing_procedure_pointer() -> str:
    """` by Reading <sizing SKILL.md> and following it`, or "" when the plugin root or the file
    does not resolve. Agents hold no Skill tool, so the procedure is named as a file, never a skill."""
    try:
        from coordinator_core.warm.caller_context import resolve_caller_context

        root = resolve_caller_context().plugin_root
    except Exception:  # fail open: an unresolvable plugin root drops the pointer, never the ask
        return ""
    if not root:
        return ""
    source = Path(root).joinpath(*_SIZING_SKILL_SOURCE)
    if not source.is_file():
        return ""
    return f", by Reading `{source.as_posix()}` and following it"


def compose_ask_script(
    *,
    repo_root: str,
    prompt: Optional[str],
    sizing_rel: Optional[str],
    run_id: str,
    session_id: Optional[str],
    review_roster_fragment: Optional[dict] = None,
    review_stage_schemas: Optional[dict] = None,
    wrap_stage: Optional[Callable[[str], tuple[str, list[str]]]] = None,
    plan_blitz_text: Optional[str] = None,
    script_path: Optional[str] = None,
    plan_blitz_args: Optional[dict] = None,
    writes: Sequence[str] = (),
    cross_repo_approved: bool = False,
    roadmap_blitz_text: Optional[str] = None,
    agent_type_host: Optional[str] = None,
    baton: Optional[dict] = None,
    accept_pending: bool = False,
    preamble: Optional[str] = None,
    em_brief: Optional[EmBrief] = None,
    stop_after: Optional[str] = None,
    in_run_commit: bool = False,
    ask_source: str = "pm",
    required_reviewers: Sequence[str] = (),
    run_base_sha: Optional[str] = None,
    route_preference: Optional[_route_pref.RoutePreference] = None,
) -> str:
    """The .mjs text for one ask: a raw `prompt`, or an existing `sizing_rel` (size phase omitted).

    A non-empty `route_preference` adds the EM's steer to the size-stage scout prompt, ahead of the
    ask text, and a `route_override_reason` slot to each gate command the scout runs after its own
    sizing-assemble. Empty or absent, every string is unchanged.

    `run_base_sha`, when given, is the base for the review blocks, the criterion judge brief and
    the test instruction; absent, the base is HEAD at compose time.

    A `sizing_rel` that is an intake mint (`is_intake`) is sized in-run like a raw ask, into that
    same file: its placeholder route never picks the arm and its minted `pm_verbatims` are kept.

    `ask_source="relayed"` marks `prompt` as an EM's relay: every prompt that would call the ask
    the PM's words names it a relayed ask instead, and the run reads `relayed-ask.md`, not `ask.md`.

    `required_reviewers` (`plugin:agent` types) join the roster's `named` stage at every tier.
    The review roster and stage schemas load from DoE when not injected; `wrap_stage` and
    `plan_blitz_text` default to `ask_plan_blitz.wrap_stage` over the resolved plugin asset, which
    is embedded only when the arm can be M+. `script_path` is the repo-relative path the script
    is written to, carried into `next_action.params` for `dispatch.terminal_commit`.
    `plan_blitz_args` is spread first into the planBlitz call, so `mode`, `repoRoot` and `batons`
    always win; `writes` seeds the emit-time write set the gate and stage ops receive.
    `baton` ({"path", "deliverable_id"}) is passed to the size scaffold and the gate verb. The
    accept phase (an APM ruling recorded in-run, then a re-gate) composes when `accept_pending`
    or on a raw ask; it never writes a `pm_quote`. `preamble` heads every row executor's prompt,
    as on the plan route; it is refused when the script carries an M+ or roadmap branch, whose
    executors this script never prompts. `em_brief` is declared once as `_EM_BRIEF` and prefixes
    every agent prompt the script composes, plan-blitz's included (through `wrap_stage`'s
    `agent_prefix_var`, which shadows `agent` over the script-level `_askAgent`); the roadmap
    arm refuses it. A gate verdict's `resume_plan` is staged at S and revises at M+ instead of
    authoring. A raw ask sizes through the scout agent, whose closing acts run
    `sizing-assemble --write` and `dispatch.ask_gate`; a gate halt carries the assemble decision's `pm_exit_question`.
    `stop_after=STOP_AFTER_PLAN` ends the script at the plan wave with the single exit
    `{planned, sizing, run_id}` (XS: `{route: 'dispatch', sizing}`); stage, execute and review
    are not composed.
    `in_run_commit` composes the docs and commit stages: the run lands its own terminal commit and
    reports the close-job record; without it the EM runs the commit. The run token is read at
    runtime from `args.runToken`; no token bytes are ever emitted.
    On a raw ask in ceo mode an XL `pm-decision` sizing gets its exit picked in-run by the APM;
    `roadmap_blitz_text` (or the plugin asset) embeds the roadmap arm that pick can lead to.
    """
    # Deferred: pm_adjudication pulls grind_stages, which a --plan emit must not load.
    from coordinator_core.ops.dispatch_emit.pm_adjudication import (
        ADJUDICATOR_AGENT_TYPE,
        IRREVERSIBLE_GATE_SOURCE,
    )

    if bool(prompt) == bool(sizing_rel):
        raise AskComposeRefused("compose_ask_script takes exactly one of prompt / sizing_rel")
    if ask_source not in ASK_SOURCES:
        raise AskComposeRefused(f"ask_source takes one of {ASK_SOURCES}, got {ask_source!r}")
    relayed = ask_source == ASK_SOURCE_RELAYED
    has_route_pref = route_preference is not None and not route_preference.is_empty()
    ledger_clause_text = _RELAYED_LEDGER_CLAUSE if relayed else _LEDGER_CLAUSE
    if stop_after not in (None, STOP_AFTER_PLAN):
        raise AskComposeRefused(f"stop_after takes {STOP_AFTER_PLAN!r} or nothing, got {stop_after!r}")

    if review_roster_fragment is None or review_stage_schemas is None:
        from coordinator_core.ops.dispatch_emit.op import _load_review_inputs

        review_roster_fragment, review_stage_schemas = _load_review_inputs(EMIT_ROUTE_PLAN)
    review = parse_execute_review(review_roster_fragment, required_reviewers=list(required_reviewers))

    intake = _intake_sizing(repo_root, sizing_rel)
    if relayed and sizing_rel and intake is None:
        raise AskComposeRefused("a relayed ask is a raw prompt or an intake slice; a planned sizing carries no relayed text")
    # An intake sizing's estimate and route are placeholders: the in-run size stage judges them.
    sized_in_run = not sizing_rel or intake is not None
    known_arm = None if intake is not None else _known_arm(repo_root, sizing_rel)
    if preamble and known_arm in (None, ARM_M_PLUS, ARM_ROADMAP):
        raise AskComposeRefused(
            f"--preamble reaches only XS/S row executors; this script's arm is {known_arm or 'unsized'}, "
            "whose executors run from a later emit. Pass --preamble to that plan emit instead."
        )
    blitz_fn: str = ""
    blitz_phases: list[str] = []
    if known_arm in (None, ARM_M_PLUS, ARM_ROADMAP):
        if wrap_stage is None:
            from coordinator_core.ops.dispatch_emit import ask_plan_blitz

            wrap_stage = ask_plan_blitz.wrap_stage
        blitz_text = plan_blitz_text if plan_blitz_text is not None else _read_plan_blitz()
        blitz_fn, blitz_phases = (
            wrap_stage(blitz_text, agent_prefix_var=EM_BRIEF_VAR) if em_brief else wrap_stage(blitz_text)
        )
        if f"async function {_PLAN_BLITZ_FN}(" not in blitz_fn:
            raise AskComposeRefused(f"wrapped plan-blitz stage does not define async function {_PLAN_BLITZ_FN}")

    xl_open = _xl_exit_open(repo_root, sizing_rel)
    # A raw ask is sized in-run, so any size can reach the XL exit.
    xl_possible = sized_in_run or xl_open
    rb_fn: str = ""
    rb_phases: list[str] = []
    if xl_possible and not em_brief:
        from coordinator_core.ops.dispatch_emit import ask_roadmap
        from coordinator_core.ops.dispatch_emit.ask_plan_blitz import AskPlanBlitzRefused

        try:
            rb_text = roadmap_blitz_text if roadmap_blitz_text is not None else ask_roadmap.load_roadmap_blitz_text()
            rb_fn, rb_phases = ask_roadmap.wrap_roadmap_stage(rb_text)
        except (AskPlanBlitzRefused, OSError):
            # Unembedded, an in-run roadmap pick halts naming the `--ask --sizing` re-fire.
            rb_fn, rb_phases = "", []

    if known_arm == ARM_ROADMAP:
        if em_brief:
            raise AskComposeRefused(
                "the roadmap arm takes no EM brief; drop args.brief, args.brief_file and args.context "
                "(CLI: --brief/--brief-file/--context), or re-submit once the sizing routes to a plan arm"
            )
        from coordinator_core.ops.dispatch_emit.ask_roadmap import compose_roadmap_script

        return compose_roadmap_script(
            repo_root=repo_root,
            sizing_rel=sizing_rel,
            run_id=run_id,
            interaction_mode=load_sizing_effective(Path(repo_root), sizing_rel).get("interaction_mode"),
            roadmap_text=roadmap_blitz_text,
            plan_blitz_fn=blitz_fn,
            plan_blitz_phases=blitz_phases,
            plan_blitz_args=plan_blitz_args,
            script_path=script_path,
        )

    run_dir = f"{RUN_DIR_ROOT}/{run_id}"
    record_dir = f"{RECORD_DIR_ROOT}/{run_id}"
    parent_copy = run_parent_copy_rel(run_id)
    manifest_rel = f"{record_dir}/manifest.json"
    anchor = _ANCHOR_CLAUSE.format(root=repo_root)
    brief_prefix = f"{_emit._SHARED_PATH_MARKER_DELIM}{EM_BRIEF_VAR}{_emit._SHARED_PATH_MARKER_DELIM}" if em_brief else ""
    head = brief_prefix + _AGENT_DEFS_MARKER + _emit._BRIEF_PRECEDENCE_CLAUSE
    session_tail = _emit._dispatching_session_id_paragraph(session_id)
    agent_type = _emit._EXECUTOR_AGENT_TYPE

    def prompt_of(*lines_and_exprs: str) -> str:
        return _cat(f"{head}\n\n{anchor}\n\n", *lines_and_exprs)

    ledger_rel = f"{record_dir}/{LEDGER_VIEW_FILE}"
    ledger_clause = "\n\n" + ledger_clause_text.format(ledger=ledger_rel)
    intent_clause = (
        f"\n\nFirst read {record_dir}/{PM_INTENT_FILE} when it exists: the PM's own words and the exit "
        "criterion. They outrank every brief, plan and in-repo memo; judge the change against them, "
        "and treat a claim the change leaves untrue, or a brief that weakened it, as a finding."
        + (
            f" Then read {record_dir}/{RELAYED_ASK_FILE}: the ask as an agent relayed it, not the PM's "
            "words; serve it, but the PM's words outrank it."
            if relayed
            else ""
        )
    ) + ledger_clause
    review_head = head + intent_clause + session_tail
    run_base_sha = run_base_sha or head_sha(repo_root) or ""
    review_blocks = compose_execute_review(
        review,
        stage_schemas=review_stage_schemas,
        plan_path=manifest_rel,
        run_base_sha=run_base_sha,
        repo_root=repo_root,
        declared_paths_js="_manifest.review_declared_paths",
        prompt_head=review_head,
        run_key=run_id,
        prep_suffix_js="' plan: ' + (_planRel ?? _sizingRel) + ' session_id: ' + _SESSION_ID",
        prep_in_wave=True,
        host_degraded=agent_type_host == _emit._AGENT_TYPE_HOST_DEGRADED,
        owner_rows_js=_OWNER_ROWS_JS,
        tier_js=_fire_tier_js(),
        executor_rows_js="(_manifest?.rows ?? []).length",
        tshirt_js="_gate?.tshirt",
        reverify=True,
    )
    review_titles = [title for title, _ in review_blocks]
    ledger_charter = _folded_ledger_charter(
        relayed=relayed,
        ledger_rel=ledger_rel,
        record_dir=record_dir,
        run_dir=run_dir,
        in_run_commit=in_run_commit,
    )
    judge_brief = _criterion_judge_brief(
        review,
        review_stage_schemas,
        run_base_sha=run_base_sha,
        intent=intent_clause,
        ledger_charter=ledger_charter,
    )
    judged = judge_brief is not None

    b: list[str] = []
    b.append(f"  const REPO_ROOT = {_lit(repo_root)};")
    b.append(f"  const _runId = {_lit(run_id)};")
    b.append(f"  const _SESSION_ID = {_lit(session_id or '')};")
    b.append(f"  let _sizingRel = {_lit(sizing_rel) if sizing_rel else 'null'};")
    writes_literal = json.dumps(list(writes))
    b.append(f"  let _writes = {writes_literal};")
    b.append(f"  const _crossRepoApproved = {'true' if cross_repo_approved else 'false'};")
    b.append("  let _planRel = null;")
    b.append("  let _gated = [];")
    b.append("  let _manifest = null;")
    b.append("  let _stagedManifest = null;")
    b.append(_ADMIT_MANIFEST_JS)
    b.append(f"  let {_OWNER_ROWS_JS} = [];")
    b.append("  let _viaRoadmap = false;")
    b.append("  let _splitExit = null;")
    b.append(_RUN_TOKEN_DECL_JS)
    b.append("  let _nextAction = null;")
    b.append("  let _docs = null;")
    b.append("  let _commit = null;")
    b.append("  let _haltCommit = null;")
    if in_run_commit:
        b.append(f"  {TERMINAL_COMMIT_CLI_HELPER_JS}")
        b.append(f"  {PARAMS_BLOCK_JS}")
        b.append(_halt_record_commit_fn(
            head=head, anchor=anchor, script_path=script_path, repo_root=repo_root, run_id=run_id,
        ))
    b.append("  let _ledger = null;")
    b.append(_AGENT_DEFS_HELPER_JS)
    if rb_fn:
        from coordinator_core.ops.dispatch_emit.ask_roadmap import ROADMAP_DECLS

        b.extend(ROADMAP_DECLS)
        b.append(rb_fn)
    if em_brief:
        b.append(f"  {brief_decl_js(em_brief)}")
        b.append("  const _askAgent = agent;")
    source_memo_clause = _source_memo_clause(repo_root, prompt, sizing_rel)
    b.append(
        f"  const {_LEDGER_PREFIX_VAR} = "
        f"{_lit(ledger_clause_text.format(ledger=ledger_rel) + chr(10) * 2 + source_memo_clause)};"
    )
    if blitz_fn:
        b.append(_ledger_shadowed(blitz_fn, em_brief=bool(em_brief)))
    runner_js = _row_runner_js()
    if em_brief:
        if runner_js.count(_ROW_VERIFY_PROMPT_OPEN) != 1:
            raise AskComposeRefused("row runner's verify prompt no longer has the shape the EM brief prefixes")
        runner_js = runner_js.replace(_ROW_VERIFY_PROMPT_OPEN, f"{EM_BRIEF_VAR} + {_ROW_VERIFY_PROMPT_OPEN}")
    b.append(runner_js)
    b.append(f"  const {_emit._REPO_ROOT_VAR} = REPO_ROOT;")
    b.append(f"  let {_emit._RUN_BASE_VAR} = {_lit(run_base_sha)};")
    b.append(_emit._checkpoint_commit_js(
        agent_type_host, repo_root=repo_root, session_id=session_id, seam=None,
        runtime_plan_js=_RUNTIME_PLAN_PATH_JS,
    ))
    b.append(_usage_limit_helper_js())
    for name in (*_REVIEW_RESULT_NAMES, _emit._TEST_RESULT_VAR):
        b.append(f"  let {name} = {'[]' if name == WAVE_NOT_RUN_VAR else 'null'};")
    if judged:
        b.append(f"  let {_emit._FALSIFIER_RESULT_VAR} = null;")

    def _gate_payload(record_js: str = "", sizing_js: str = "_sizingRel") -> str:
        return (
            "js:JSON.stringify({ sizing_path: " + sizing_js + ", run_id: _runId, writes: _writes"
            + (", baton: " + json.dumps(baton["path"]) if baton else "")
            + (f", record: {record_js}" if record_js else "")
            + " })"
        )

    fold_stage = not stop_after
    fold_falsifier = judged and not stop_after
    if fold_falsifier:
        b.append("  let _planFalsifier = null;")
        b.append("  " + _AS_PLAN_FALSIFIER_JS)
    def _stage_act_parts(base: str, sizing: str, gated: str) -> tuple[str, ...]:
        """The closing act of the agent that just ran the gate: stage the arms with no plan agent to fold into.

        ``base`` is the params object text carrying run_id, writes, cross_repo_approved and session_id;
        ``sizing`` and ``gated`` are the values the XS params add. Each of the three is a literal or a
        ``js:`` expression.
        """
        return (
            " When the gate reply carries its own `manifest` (a ledger resume or an S resume stages itself), "
            "return it verbatim as manifest, every field and every row, trimming nothing, and run no stage command. "
            "As your very last act otherwise, stage the run, but only when the gate reply's halt is null and its arm "
            f"is XS: run `{_INVOKE} {OP_ASK_STAGE} '<params>'` once, <params> being the JSON object ",
            base,
            " with sizing_path ",
            sizing,
            " and gated ",
            gated,
            " added. Return the JSON reply verbatim as manifest, every field and every row, trimming nothing; "
            "when it replies {\"error\": ...}, return that message as stage_error with manifest null. On any "
            "other arm run no stage command and return no manifest.",
        )

    def _gate_agent_call(label: str, phase_title: str, stage_act: bool = False) -> str:
        gate_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            f"Run `{_INVOKE} {OP_ASK_GATE} '",
            _gate_payload(),
            "'` and return its JSON reply verbatim as arm, halt, baton and (when present) resume_plan, resume_rows, resume_ledger and spine_plan, plus design_brief and design_route when present; "
            "when the command is denied or fails, return halt {kind: 'refusal', reason: <its error text>}, never arm "
            "and halt both null. Also read the sizing at ",
            "js:_sizingRel",
            " and return its estimate.tshirt as tshirt and its route as route.",
            *(
                _stage_act_parts(
                    "js:JSON.stringify({ run_id: _runId, writes: _writes, cross_repo_approved: "
                    "_crossRepoApproved, session_id: _SESSION_ID })",
                    "js:JSON.stringify(_sizingRel)",
                    "js:JSON.stringify(_gated)",
                )
                if stage_act and fold_stage
                else ()
            ),
        )
        return _agent(
            gate_prompt, label=label, phase=phase_title, agent_type=_MECHANICAL_AGENT_TYPE,
            agent_model=_MECHANICAL_MODEL,
            schema=_GATE_STAGED_SCHEMA if stage_act and fold_stage else _GATE_SCHEMA,
            effort=_MECHANICAL_EFFORT, run_id=run_id,
        )

    if sized_in_run:
        b.append("  phase('size');")
        _sizings_prefix = Path(home_dir(".", "sizings")).as_posix() + "/"
        # One path form for both the scout's `--write` template and the runtime `_sizingPathFor`: a
        # template without the run suffix lets the file land where `_sizingRel` (refire line, stage
        # and plan legs) does not look.
        _sizing_stem_head = f"{date.today().isoformat()}-"
        _sizing_stem_tail = f"-{run_id.rsplit('-', 1)[-1]}.yaml"
        sizing_write_rel = (
            sizing_rel if intake is not None
            else f"{_sizings_prefix}{_sizing_stem_head}<name>{_sizing_stem_tail}"
        )
        _deliverable_js = (
            f" + {_lit(' --deliverable-id ' + shlex.quote(baton['deliverable_id']))}" if baton and baton.get("deliverable_id") else ""
        )
        deliverable_flag = (
            f" --deliverable-id {shlex.quote(baton['deliverable_id'])}" if baton and baton.get("deliverable_id") else ""
        )
        relayed_flag = (
            f" --relayed-ask-file {shlex.quote(record_dir + '/' + RELAYED_ASK_FILE)}" if relayed else ""
        )
        assemble_template = (
            f"{SIZING_ASSEMBLE_CMD} --tshirt <tshirt> --intent <intent> --name <name> "
            "--premise-provenance <premise_provenance> --premise-evidence <premise_evidence> "
            f"--exit-criterion <exit_criterion>{deliverable_flag} [--jtbd-unclear when jtbd_unclear is true] "
            "[--well-trodden-step-change when well_trodden_step_change is true] "
            "[--research-class/-appetite/-source/-depth/-target/-question from research when research is returned] "
            "[--design-route <design_route> when design_route is returned] "
            f"--write {shlex.quote(f'{repo_root}/{sizing_write_rel}')} --json {NO_PLAN_HAND_OFF_FLAG}{relayed_flag}"
        )
        gate_template = (
            f"{_INVOKE} {OP_ASK_GATE} '"
            f'{{"sizing_path":"{sizing_write_rel}","run_id":"{run_id}","writes":[<your writes>]'
            + (f',"baton":{json.dumps(baton["path"])}' if baton else "")
            + (f',"route_override_reason":"{_OVERRIDE_SLOT}"' if has_route_pref else "")
            + "}'"
        )
        scout_closing_tail = (
            "\n\nReturn its exit code as exit_code, its stdout verbatim as stdout and its stderr as "
            "stderr. Do not edit, retry or repair it. (2) Only when exit_code is 0, run the next command "
            "with <name> filled as above and <your writes> replaced by the JSON array you return as writes:\n\n"
            + gate_template
            + "\n\nand return its JSON reply verbatim as arm, halt, baton, sizing_path and (when present) resume_plan, "
            f"resume_rows, resume_ledger and spine_plan, plus design_brief and design_route when present; then read estimate.tshirt and route from the sizing at {sizing_write_rel} "
            "and return them as tshirt and route (tshirt replaces your own judged value). "
            "On a non-zero exit_code run no gate command and return no arm or halt."
        )
        if fold_falsifier:
            scout_closing_tail += (
                " (3) Only when the gate reply's halt is null and its arm is neither null nor XS, author the "
                "baseline falsifier for the exit_criterion you assembled. " + _FALSIFIER_ACT
                + " Return it as falsifier. On any other arm, or a halt, author none and return no falsifier."
            )
        if fold_stage:
            _scout_base = json.dumps(
                {"run_id": run_id, "cross_repo_approved": bool(cross_repo_approved), "session_id": session_id or ""}
            )[:-1] + ', "writes": <your writes array>}'
            scout_closing_tail += "".join(
                _stage_act_parts(_scout_base, json.dumps(sizing_write_rel), "<your gated array>")
            )
        scout_closing = (
            " Your closing acts, in order, once your fields are settled. (1) Run sizing-assemble once: "
            "replace each <field> in the command below with your own value, POSIX single-quoted (an "
            "embedded ' written '\\'''), <name> being lowercase kebab-case ([a-z0-9-] only):\n\n"
            + assemble_template
            + scout_closing_tail
        )
        def _assemble_cmd_js(path_js: str) -> str:
            """JS expression building the sizing-assemble command over `_asmFields`, writing to `path_js`."""
            return (
                f"{_lit(SIZING_ASSEMBLE_CMD)}"
                + scout_assemble_flags_js("_asmFields", "_q", _deliverable_js)
                + research_assemble_flags_js("_asmFields", "_q")
                + design_route_assemble_flags_js("_asmFields", "_q")
                + acceptance_rows_assemble_flags_js("_asmFields", "_q")
                + f" + ' --write ' + _q({path_js}) + ' --json ' + {_lit(NO_PLAN_HAND_OFF_FLAG)}"
                + (f" + {_lit(' --relayed-ask-file ' + shlex.quote(record_dir + '/' + RELAYED_ASK_FILE))}" if relayed else "")
            )

        scout_prompt = prompt_of(
            "Size this ask afresh, never reusing or editing an existing sizing"
            + _sizing_procedure_pointer()
            + ". Judge first; write no file except through your "
            "closing `sizing-assemble`. "
            + SCOUT_RETURN_CLAUSE
            + EXIT_CRITERION_RULES_CLAUSE
            + " " + RESEARCH_CLASSIFY_CLAUSE
            + " " + ACCEPTANCE_ROWS_CLAUSE
            + " " + DESIGN_ROUTE_CLAUSE
            + scout_closing
            + ledger_clause
            + " When the ask below does not carry the PM's whole ask, read the session's PM turns "
            f"(`{_INVOKE} baton.pm_turns`) for the prior ones; the EM brief, when present, may note "
            "which prior turns are pertinent."
            + ("\n\n" + _route_pref.scout_block(route_preference).rstrip("\n") if has_route_pref else "")
            + (
                "\n\nRelayed ask (an agent's relay, not the PM's words):\n"
                if relayed
                else "\n\nAsk:\n"
            )
            + (prompt or str((intake or {}).get("intent") or ""))
        )
        b.append("  const _q = (v) => \"'\" + String(v ?? '').replace(/'/g, \"'\\\\''\") + \"'\";")
        b.append(
            "  const _slugOf = (r) => String(r.name || 'ask').toLowerCase().replace(/[^a-z0-9]+/g, '-')"
            ".replace(/^-+|-+$/g, '') || 'ask';"
        )
        if intake is not None:
            b.append(f"  const _sizingPathFor = (r) => REPO_ROOT + '/' + {_lit(sizing_rel)};")
        else:
            b.append(
                f"  const _sizingPathFor = (r) => REPO_ROOT + '/' + '{_sizings_prefix}' + "
                f"{_lit(_sizing_stem_head)} + _slugOf(r) + {_lit(_sizing_stem_tail)};"
            )
        b.append(
            "  const _boundRelOf = (v) => typeof v === 'string' && v !== '' && !v.startsWith('/') "
            "&& !/^[A-Za-z]:/.test(v) && !v.includes('\\\\') && !v.split('/').includes('..') ? v : null;"
        )
        # An agent's Bash tool folds stderr into stdout, so sizing-assemble's stderr notes trail the
        # JSON; the decision is the span from the first `{` to the last column-0 `}`. A compact
        # one-line decision has no column-0 `}`, so the spans widen to the last `}`, then to the
        # first whole line that parses as a decision: a written sizing must not read as "not run".
        b.append(
            "  const _parseDecision = (stdout) => { try { const _out = String(stdout ?? ''); "
            "const _try = (t) => { try { const d = JSON.parse(t); "
            "return d && typeof d === 'object' && typeof d.route === 'string' ? d : null; } catch (_e) { return null; } }; "
            "const _s = _out.indexOf('{'); "
            "return _try(_out.slice(_s, _out.lastIndexOf('\\n}') + 2)) "
            "?? _try(_out.slice(_s, _out.lastIndexOf('}') + 1)) "
            "?? _out.split('\\n').map((l) => l.trim()).filter((l) => l.startsWith('{')).map(_try)"
            ".find((d) => d) ?? null; } "
            "catch (_e) { return null; } };"
        )
        # Proof sizing-assemble ran: an integer exit_code, and at exit 0 a parsed decision or a gate
        # reply (an arm or a halt; ask_gate reads the sizing from disk, so it answers only once the file
        # exists). A non-zero exit is proof too: the reword and failed-exit paths own it.
        b.append(
            "  const _assembleProven = (r) => Number.isInteger(r.exit_code) && (r.exit_code !== 0 "
            "|| !!_parseDecision(r.stdout) || typeof r.arm === 'string' "
            "|| !!(r.halt && typeof r.halt === 'object'));"
        )
        b.append(
            "  const _scoutHasValues = (r) => "
            f"{json.dumps(_SCOUT_STRING_FIELDS)}.every((k) => typeof r[k] === 'string' && r[k].trim() !== '') "
            f"&& {json.dumps(_SCOUT_ARRAY_FIELDS)}.every((k) => Array.isArray(r[k]));"
        )
        b.append(
            f"  const _scoutedFirst = await {_agent(scout_prompt, label='size', phase='size', agent_type=SIZING_SCOUT_AGENT_TYPE, schema=_ASK_SCOUT_STAGED_SCHEMA if fold_stage else ASK_SCOUT_SCHEMA, run_id=run_id)};"
        )
        # One re-dispatch whenever assemble is unproven. With a full sizing in hand the retry carries
        # the engine-built command over those values; otherwise the scout is told to run it itself.
        # The retry merges over the first reply so a value it omits persists; a second miss falls
        # through to the halts below.
        values_retry = _cat(
            SCOUT_VALUES_ASSEMBLE_RETRY,
            "js:(() => { const _asmFields = { ..._scoutedFirst }; return " + _assemble_cmd_js("_sizingPathFor(_scoutedFirst)") + "; })()",
            scout_closing_tail,
        )
        b.append(
            "  const _scouted = _assembleProven(_scoutedFirst) ? _scoutedFirst : { ..._scoutedFirst, ...(await "
            f"{_agent(scout_prompt + ' + (_scoutHasValues(_scoutedFirst) ? (' + values_retry + ') : ' + _lit(SCOUT_DID_NOT_ASSEMBLE_RETRY) + ')', label='size-retry', phase='size', agent_type=SIZING_SCOUT_AGENT_TYPE, schema=_ASK_SCOUT_STAGED_SCHEMA if fold_stage else ASK_SCOUT_SCHEMA, run_id=run_id)}) }};"
        )
        b.append("  const _scoutAssembled = Number.isInteger(_scouted.exit_code);")
        # Captured before the reword path mutates `_scouted`: a folded manifest or falsifier is
        # valid only for the criterion and sizing the scout's own gate reply judged.
        b.append("  const _scoutClean = _scouted.exit_code === 0 && !_scouted.halt && !!_scouted.arm;")
        b.append("  _writes = _scouted.writes ?? [];")
        b.append("  _gated = _scouted.gated ?? [];")
        b.append("  const _sizingPath = _boundRelOf(_scouted.sizing_path) ? REPO_ROOT + '/' + _scouted.sizing_path : _sizingPathFor(_scouted);")
        assemble_cmd_js = _assemble_cmd_js("_sizingPath")
        _reword_sizing_js = "_sizingPath.slice(REPO_ROOT.length + 1)"
        reword_closing = (
            "\n\nThen, in order: (1) run exactly this one command, replacing the quoted placeholder "
            "'<your reworded exit_criterion>' with your reworded exit_criterion, POSIX single-quoted (an "
            "embedded ' written '\\''), and change nothing else:\n\n",
            "js:_reassembleCmd",
            "\n\nReturn its exit code as exit_code, its stdout verbatim as stdout and its stderr as stderr. "
            f"Do not edit, retry or repair it. (2) Only when exit_code is 0, run `{_INVOKE} {OP_ASK_GATE} '",
            _gate_payload(sizing_js=_reword_sizing_js),
            "'` and return its JSON reply verbatim as arm, halt, baton, sizing_path and (when present) resume_plan, resume_rows, resume_ledger, "
            "spine_plan, design_brief and design_route; when the command is denied or fails, return halt {kind: 'refusal', reason: <its error "
            "text>}, never arm and halt both null. Also read the sizing at ",
            "js:" + _reword_sizing_js,
            " and return its estimate.tshirt as tshirt and its route as route. On a non-zero exit_code run no "
            "gate command and return no arm or halt."
            + (
                f" When your reworded sizing routes differently from route_preference, add "
                f'`"route_override_reason":"{_OVERRIDE_SLOT}"` to that JSON object.'
                if has_route_pref
                else ""
            ),
            *(
                _stage_act_parts(
                    "js:JSON.stringify({ run_id: _runId, writes: _writes, cross_repo_approved: "
                    "_crossRepoApproved, session_id: _SESSION_ID })",
                    "js:JSON.stringify(" + _reword_sizing_js + ")",
                    "js:JSON.stringify(_gated)",
                )
                if fold_stage
                else ()
            ),
        )
        # One reword: a remediable exit-criterion refusal from the scout's closing sizing-assemble goes
        # back to the scout, which rewords, re-assembles, re-gates and stages as its closing acts.
        # `_scouted` is mutated in place so the halt and decision lines below read the final attempt; a
        # second refusal falls through to the halt.
        reword_prompt = prompt_of(
            "sizing-assemble refused your exit_criterion. Judge nothing else and write no file except "
            "through the closing commands below. Reword the exit_criterion so it clears the refusal and return it "
            "as exit_criterion."
            + EXIT_CRITERION_RULES_CLAUSE,
            "\n\nRefusal:\n",
            "js:_asmOut.slice(0, 1500)",
            "\n\nRefused exit_criterion:\n",
            "js:String(_scouted.exit_criterion ?? '')",
            *reword_closing,
        )
        b.append("  let _rewordStaged = false;")
        b.append(
            "  const _asmOut = String(_scouted.stderr ?? '') + '\\n' + String(_scouted.stdout ?? '');"
        )
        b.append(
            f"  if (_scouted.exit_code !== 0 && _asmOut.includes({_lit(EXIT_CRITERION_REFUSAL_HEAD)}) "
            f"&& _asmOut.includes({_lit(REMEDIATION_MARKER)})) {{"
        )
        b.append("    const _asmFields = { ..._scouted, exit_criterion: '<your reworded exit_criterion>' };")
        b.append(f"    const _reassembleCmd = {assemble_cmd_js};")
        b.append(
            f"    const _reworded = await {_agent(reword_prompt, label='size-reword', phase='size', agent_type=SIZING_SCOUT_AGENT_TYPE, schema=_REWORD_SCHEMA, run_id=run_id)};"
        )
        b.append(
            "    _scouted.exit_criterion = _reworded.exit_criterion; _scouted.exit_code = _reworded.exit_code; "
            "_scouted.stdout = _reworded.stdout; _scouted.stderr = _reworded.stderr;"
        )
        b.append("    if (_scouted.exit_code === 0) {")
        b.append(
            "      _sizingRel = _boundRelOf(_reworded.sizing_path) ?? _sizingPath.slice(REPO_ROOT.length + 1);"
        )
        b.append(
            "      Object.assign(_scouted, { arm: _reworded.arm ?? null, halt: _reworded.halt ?? null, "
            "baton: _reworded.baton ?? null, sizing_path: _sizingRel, tshirt: _reworded.tshirt, route: _reworded.route, "
            "resume_plan: _reworded.resume_plan ?? null, resume_rows: _reworded.resume_rows ?? null, resume_ledger: _reworded.resume_ledger ?? null, spine_plan: _reworded.spine_plan ?? null, "
            f"{DESIGN_BRIEF_KEY}: _reworded.{DESIGN_BRIEF_KEY} ?? null, "
            f"{DESIGN_ROUTE_KEY}: _reworded.{DESIGN_ROUTE_KEY} ?? null, "
            "manifest: _reworded.manifest ?? null });"
        )
        b.append("      _rewordStaged = !_scouted.halt && !!_scouted.arm;")
        b.append("    }")
        b.append("  }")
        b.append("  let _decision = null;")
        b.append("  _decision = _parseDecision(_scouted.stdout);")
        b.append(
            "  if (!_scoutAssembled) { _halted = { halted: "
            f"{_lit(HALT_REFUSAL)}, reason: {_lit(SCOUT_DID_NOT_ASSEMBLE_REASON)}, "
            "detail: [_scouted.stderr, _scouted.stdout].map((s) => String(s ?? '').trim())"
            ".filter((s) => s).join('\\n').slice(0, 600), run_id: _runId }; }"
        )
        b.append(
            "  else if (_scouted.exit_code !== 0) { _halted = { halted: "
            f"{_lit(HALT_REFUSAL)}, reason: 'sizing-assemble failed (exit ' + _scouted.exit_code + '); "
            "the gate, plan, stage, execute and review phases were skipped', "
            "detail: [_scouted.stderr, _scouted.stdout].map((s) => String(s ?? '').trim())"
            ".filter((s) => s).join('\\n').slice(0, 600), run_id: _runId }; }"
        )
        b.append(
            "  else { _sizingRel = _boundRelOf(_scouted.sizing_path) ?? _sizingPath.slice(REPO_ROOT.length + 1); }"
        )
        # Exit 0 proves nothing when the scout never ran sizing-assemble (stdout 'not run'). Proof is
        # a parsed assemble decision (always carries `route`) or a gate reply (an arm or a halt):
        # ask_gate reads the sizing from disk, so it answers only once the file exists. Scouts
        # abridge long stdout, so the gate reply alone must suffice.
        b.append(
            "  if (!_halted && (_decision === null || typeof _decision !== 'object' "
            "|| typeof _decision.route !== 'string') && typeof _scouted.arm !== 'string' "
            "&& !(_scouted.halt && typeof _scouted.halt === 'object')) { _halted = { halted: "
            f"{_lit(HALT_REFUSAL)}, reason: 'scout failure: sizing-assemble did not run: the scout replied exit 0 but its stdout is "
            "not a sizing-assemble decision, so the sizing file was never written; the gate, plan, stage, execute "
            "and review phases were skipped', "
            "detail: [_scouted.stdout, _scouted.stderr].map((s) => String(s ?? '').trim())"
            ".filter((s) => s).join('\\n').slice(0, 600), run_id: _runId }; }"
        )
        b.append(f"  const _pmExit = (_decision && _decision[{_lit(PM_EXIT_QUESTION_KEY)}]) || null;")

    b.append("  phase('gate');")
    if sized_in_run:
        # The scout's closing acts ran the gate; a failed assemble leaves nothing to carry.
        b.append(
            "  let _gate = _halted ? { arm: null, halt: null } : { arm: _scouted.arm ?? null, "
            "halt: _scouted.halt ?? null, baton: _scouted.baton ?? null, tshirt: _scouted.tshirt, "
            "route: _scouted.route, resume_plan: _scouted.resume_plan ?? null, "
            "resume_rows: _scouted.resume_rows ?? null, resume_ledger: _scouted.resume_ledger ?? null, spine_plan: _scouted.spine_plan ?? null, "
            f"{DESIGN_BRIEF_KEY}: _scouted.{DESIGN_BRIEF_KEY} ?? null, "
            f"{DESIGN_ROUTE_KEY}: _scouted.{DESIGN_ROUTE_KEY} ?? null }};"
        )
        if fold_falsifier:
            b.append("  let _scoutFalsifier = null;")
            b.append(
                f"  if (_scoutClean && _gate.arm !== {_lit(ARM_XS)} && _scouted.falsifier && "
                "[_scouted.falsifier.how, _scouted.falsifier.baseline_output, "
                "_scouted.falsifier.expected_when_true].every((s) => typeof s === 'string' && s.trim())) "
                "{ _scoutFalsifier = { how: _scouted.falsifier.how, baseline_output: _scouted.falsifier.baseline_output, "
                "expected_when_true: _scouted.falsifier.expected_when_true, authored_by: 'sizing-scout' }; }"
            )
    else:
        # An existing sizing has no scout to close for it.
        b.append(f"  let _gate = await {_gate_agent_call('gate', 'gate', stage_act=True)};")
    if fold_stage:
        # Adopted only under the plan folds' guard (rows array, no error), for the arms the act covers.
        clean_js = "(_scoutClean || _rewordStaged)" if sized_in_run else "!_gate.halt"
        reply_js = "_scouted" if sized_in_run else "_gate"
        b.append(
            f"  if ({clean_js} && (_gate.arm === {_lit(ARM_XS)} || (_gate.arm === {_lit(ARM_S)} && _gate.resume_plan) "
            f"|| _gate.{RESUME_LEDGER_KEY})) {{ _adoptManifest({reply_js}.manifest); }}"
        )
        b.append(
            f"  if ({clean_js} && {reply_js}.stage_error) {{ _adoptManifest({{ error: {reply_js}.stage_error }}); }}"
        )

    def _record_clause(record_js: str, fields: str) -> list[str]:
        """The ruling agent's own record call; the engine refuses a gated ruling before it writes."""
        return [
            " When, and only when, your verdict is 'ruled', run this one command once, with " + fields
            + " set in its `record` member, and return its JSON reply verbatim as arm, halt, baton, "
            "recorded and (when present) resume_plan, resume_rows, resume_ledger and spine_plan, beside your verdict, pmOnly and ruling. "
            f"On 'pm-only' run nothing. `{_INVOKE} {OP_ASK_GATE} '",
            _gate_payload(record_js),
            "'` That command is the only way you write the sizing: never edit it by hand.",
        ]

    # Accept and xl-exit stay separate from the scout and the plan reviewer: reviewer independence
    # (the delegate rules a criterion it did not write) and the APM delegate's own record write
    # through the gated op. Each runs only when a halt is owed, so most runs spawn neither.
    if accept_pending or xl_possible:
        apm_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            "phase: accept\n\nYou are the PM's delegate for one exit criterion. Nobody escalates to the "
            "human here: a scope, direction or priority matter is yours as the APM. Read the sizing at ",
            "js:_sizingRel",
            " and judge its exit_criterion.statement: is it a done-state this ask can be held to? Rule "
            "it as written (verdict 'ruled', statement omitted), or amend it by returning the better "
            "one-sentence statement. When no statement is on record, author one from the sizing's "
            "intent: verdict 'ruled' with the one-sentence done-state as statement. Return the ruling verbatim as ruling. Never rule on a merge, "
            "publish, push to main or cross-repo commit gate: return verdict 'pm-only' with "
            "pmOnlyGround and pmOnly true ONLY when the matter is important AND urgent AND has no clear "
            "right answer, or needs such an external or irreversible action. Being unsure is not a "
            "ground. You stage and commit nothing." + _KEEP_THE_ASK.format(record_dir=record_dir) + ledger_clause,
            *_record_clause(
                "{ kind: 'exit_criterion', ruling_ref: _runId }",
                "`apm_ruling` (your ruling, verbatim) and, only when you amended or authored a "
                "statement, `statement` (that statement)",
            ),
        )
        apm_call = _agent(
            apm_prompt, label="accept", phase="accept", agent_type=ADJUDICATOR_AGENT_TYPE,
            schema=_ruling_schema(_ACCEPT_SCHEMA), run_id=run_id,
        )
        xl_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            "phase: xl-exit\n\nYou are the PM's delegate for one XL exit; this sizing runs in ceo mode, "
            "so nobody escalates to the human here. Read the sizing at ",
            "js:_sizingRel",
            " (intent, estimate, exit_criterion, research) and pick its exit: 'accept_multi_session' when "
            "the work is one deliverable a single multi-session plan can carry; 'roadmap' when it spans two "
            "or more named workstreams or needs an initiative or goal link; 'split' when its intent or "
            "register names two or more independent, separately shippable initiatives (each would ship "
            "alone), which then run as sibling runs. For 'split' also return slices: [{label, ask, rows, "
            "writes?}], one per initiative, where ask is that initiative's full request, rows lists "
            "requirement_register row ids, and writes (when you can name them) its repo-relative file "
            "paths. Every register row id, a verbatim row included, goes in exactly one slice: assign a "
            "verbatim row to the slice it most serves; none is left out or repeated. 'roadmap' stays the "
            "pick for one initiative spanning workstreams. Return verdict 'ruled', the "
            "pick, and your one-paragraph reasoning verbatim as ruling. Return verdict 'pm-only' with "
            "pmOnly true and pmOnlyGround ONLY when the job-to-be-done itself is unstated, or the work "
            "needs a merge, publish, push to main or cross-repo commit. Being unsure is not a ground. ",
            "js:((typeof args !== 'undefined' && !!args && args.confirm_split === true) ? '' : "
            + _lit(
                f"When you pick 'split' with slices, also run `{_INVOKE} {FANOUT_OP} '"
                "{\"slices\": <your slices, as JSON>, \"parent_sizing\": \""
            )
            + " + _sizingRel + "
            + _lit(
                "\"}'` (a single quote inside the JSON is written '\\''), and return its JSON reply as parent_id, "
                "runs and fire (error empty on success; if it replies `{\"error\": ...}`, return that message as "
                "`error`, with parent_id empty and runs and fire empty). On success, read "
                f"scratch/warp/<parent_id>/parent.json and write its content verbatim to {parent_copy} "
                "(create the directory first). "
            )
            + ")",
            "You stage and commit nothing, apart from that write." + _KEEP_THE_ASK.format(record_dir=record_dir) + ledger_clause,
            *_record_clause(
                "{ kind: 'xl_exit', ruling_ref: _runId }",
                "`xl_exit` (your pick) and `apm_ruling` (your ruling, verbatim)",
            ),
        )
        xl_call = _agent(
            xl_prompt, label="xl-exit", phase="xl-exit", agent_type=ADJUDICATOR_AGENT_TYPE,
            schema=_ruling_schema(_XL_SCHEMA), effort=_XL_EXIT_EFFORT, run_id=run_id,
        )
        # A record writes neither estimate.tshirt nor route, so both carry over from the first gate.
        take_gate = "_gate = { ..._gate, arm: %s.arm, halt: %s.halt, baton: %s.baton ?? null, resume_plan: %s.resume_plan ?? null, resume_rows: %s.resume_rows ?? null, resume_ledger: %s.resume_ledger ?? null, spine_plan: %s.spine_plan ?? null, design_route: %s.design_route ?? _gate.design_route ?? null };"
        irreversible = f"new RegExp({_lit(IRREVERSIBLE_GATE_SOURCE)}, 'i')"
        b.append("  let _splitSlices = null;")
        b.append("  let _splitFan = null;")
        b.append(
            # Each round discharges one halt in-run and re-gates: an XL sizing in ceo mode can
            # owe both the exit pick and the exit-criterion acceptance. No progress ends the loop.
            "  for (let _round = 0; _round < 3 && _gate.halt; _round++) {\n"
            "    let _progressed = false;\n"
            f"    if (_gate.halt.kind === {_lit(HALT_TOUCHPOINT)}) {{\n"
            "    phase('accept');\n"
            "    let _apm = null;\n"
            f"    try {{ _apm = await {apm_call}; }} catch (_e) {{ _apm = null; }}\n"
            f"    const _apmGated = {irreversible}.test(_apm?.ruling ?? '');\n"
            "    if (_apm && _apm.verdict === 'ruled' && _apm.pmOnly !== true && !_apmGated && _apm.ruling "
            "&& _apm.recorded && _apm.recorded.ok === true && 'arm' in _apm && 'halt' in _apm) {\n"
            f"      {take_gate % (('_apm',) * 8)}\n"
            "      _progressed = true;\n"
            "    }\n"
            f"    if (_gate.halt && _gate.halt.kind === {_lit(HALT_TOUCHPOINT)} && _apm) {{\n"
            "      _gate = { ..._gate, halt: { ..._gate.halt, apm: { verdict: _apm.verdict, "
            "pmOnlyGround: _apm.pmOnlyGround ?? (_apmGated ? 'external-or-irreversible' : null), ruling: _apm.ruling ?? null } } };\n"
            "    }\n"
            f"    }} else if ({'true' if xl_possible else 'false'} && _gate.halt.kind === {_lit(HALT_ROOM)} && _gate.halt.apm_xl_exit === true) {{\n"
            "    phase('xl-exit');\n"
            "    let _xl = null;\n"
            f"    try {{ _xl = await {xl_call}; }} catch (_e) {{ _xl = null; }}\n"
            f"    const _xlGated = {irreversible}.test(_xl?.ruling ?? '');\n"
            "    if (_xl && _xl.verdict === 'ruled' && _xl.pmOnly !== true && !_xlGated && _xl.ruling "
            "&& ['roadmap', 'accept_multi_session', 'split'].includes(_xl.pick) "
            "&& _xl.recorded && _xl.recorded.ok === true && 'arm' in _xl && 'halt' in _xl) {\n"
            f"      {take_gate % (('_xl',) * 8)}\n"
            "      if (_xl.pick === 'split' && Array.isArray(_xl.slices)) { _splitSlices = _xl.slices; _splitFan = _xl; }\n"
            "      _progressed = true;\n"
            "    }\n"
            f"    if (!_progressed && _xl) {{\n"
            "      _gate = { ..._gate, halt: { ..._gate.halt, apm: { verdict: _xl.verdict, pick: _xl.pick ?? null, "
            "pmOnlyGround: _xl.pmOnlyGround ?? (_xlGated ? 'external-or-irreversible' : null), ruling: _xl.ruling ?? null } } };\n"
            "    }\n"
            "    }\n"
            "    if (!_progressed) break;\n"
            "  }"
        )
    # Placed after the halt spread so an undefined `halt.reason` cannot erase it: a halt record
    # always carries a non-empty reason, and a no-arm reply with no halt says so.
    _gate_reason_js = (
        "reason: String((_gate.halt && _gate.halt.reason) || (_gate.halt ? 'the gate halted ('"
        " + _gate.halt.kind + ') without a reason' : 'the gate returned no arm and no halt'"
        + (
            " + (_scouted && String(_scouted.stdout ?? '').trim() ? ' (scout stdout: ' + "
            "String(_scouted.stdout).trim().slice(0, 200) + ')' : '')"
            if sized_in_run
            else ""
        )
        + "))"
    )
    if not sized_in_run:
        b.append(
            "  if (_gate.halt || !_gate.arm) { _halted = { halted: (_gate.halt && _gate.halt.kind) || "
            f"{_lit(HALT_REFUSAL)}, ..._gate.halt, {_gate_reason_js}, sizing: _sizingRel, run_id: _runId }}; }}"
        )
    else:
        b.append(
            "  if (_gate.halt || !_gate.arm) { if (!_halted) _halted = { halted: (_gate.halt && _gate.halt.kind) || "
            f"{_lit(HALT_REFUSAL)}, ..._gate.halt, {_gate_reason_js}, sizing: _sizingRel, run_id: _runId"
            f", ...(_pmExit ? {{ {PM_EXIT_QUESTION_KEY}: _pmExit }} : {{}}) }}; }}"
        )
    # A ledger resume re-judges the blocking rows of a run whose executors already landed. It
    # re-stages the plan (the terminal commit needs the manifest) and takes the XS tail: no plan
    # author, review wave or scoped test runs, and execution is skipped by `_resumeLedger`.
    # A resume at M+ whose still-blocking rows owe work keeps its arm: the re-plan leg below adds rows
    # for that work and only those rows are staged and dispatched. Any other ledger resume takes the XS tail.
    # The re-plan leg is composed only when `fold_stage`; a stop_after compose never reaches it.
    _rl0 = f"_gate.{RESUME_LEDGER_KEY}"
    b.append("  let _ledgerExec = false;")
    b.append("  let _ledgerAdded = [];")
    b.append(f"  const _resumeLedger = !_halted && !!{_rl0};")
    b.append(
        f"  const _ledgerUnlanded = _resumeLedger ? ({_rl0}.{RESUME_LEDGER_UNLANDED_FIELD} ?? []) : [];"
    )
    b.append(
        f"  const _ledgerOwes = _resumeLedger && (_gate.arm === {_lit(ARM_M_PLUS)} || _gate.arm === {_lit(ARM_S)})"
        f" && ({_rl0}.blocking ?? [])"
        f".some((id) => (((({_rl0}.{RESUME_LEDGER_REMAINING_FIELD} ?? {{}})[id]) ?? {{}}).remaining ?? []).length > 0);"
    )
    b.append(
        f"  if (_resumeLedger) {{ _planRel = {_rl0}.plan; if (_ledgerUnlanded.length) {{ _ledgerExec = true; }} "
        f"else if (!_ledgerOwes) {{ _gate = {{ ..._gate, arm: {_lit(ARM_XS)} }}; }} "
        f"if (_gate.arm === {_lit(ARM_S)}) {{ _gate = {{ ..._gate, resume_plan: _planRel }}; }} }}"
    )
    b.append(
        f"  const _allLandedResume = _resumeLedger && {_rl0}.{RESUME_LEDGER_KIND_FIELD} === {_lit(ALL_LANDED_HALT_KIND)};"
    )
    if fold_stage:
        # A ledger verdict carries the gate op's own stage of the resumed plan as `manifest`; the re-plan
        # agent stages the plan itself over exactly the rows it added, so no standalone stage leg runs.
        replan_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            "Ledger-resume re-plan leg. An earlier run halted at the ledger check; these requirement rows "
            "stayed not_met: ",
            f"js:{_rl0}.blocking.join(', ')",
            f". Read each row's register text in {ledger_rel}. Work still owed, by row: ",
            f"js:JSON.stringify({_rl0}.{RESUME_LEDGER_REMAINING_FIELD} ?? {{}})",
            ". Revise notes, oldest first: ",
            f"js:JSON.stringify({_rl0}.{RESUME_LEDGER_NOTES_FIELD} ?? [])",
            ". Revise the plan at ",
            "js:_planRel",
            " IN PLACE with Edit -- never scaffold a new plan, never change its path -- adding one new row, "
            "disposition open, per piece of owed work, each with bounded declared `writes` covering the "
            "paths the work lies on and a done condition that is a runnable check. Do not edit, reopen or "
            "remove any existing row: the rows that landed stay landed. Return the plan's repo-relative "
            "path as plan_rel and the ids of exactly the rows you added as added_rows. Commit nothing. "
            f"As your very last act, when you added at least one row, run `{_INVOKE} {OP_ASK_STAGE} '<params>'` "
            "once, <params> being the JSON object ",
            "js:JSON.stringify({ run_id: _runId, plan_path: _planRel, writes: _writes, "
            "cross_repo_approved: _crossRepoApproved, session_id: _SESSION_ID, "
            "...(_sizingRel ? { commit_sizing_path: _sizingRel } : {}) })",
            " with only_rows set to the ids of exactly the rows you added",
            "js:(_ledgerUnlanded.length ? ' together with these unlanded rows: ' + JSON.stringify(_ledgerUnlanded) : '')",
            ", and return its JSON reply "
            "verbatim as manifest, every field and every row, trimming nothing. If it replies "
            "{\"error\": ...}, return that message as error and manifest null. When you added no row, "
            f"stage nothing and return manifest null.{session_tail}",
        )
        replan_call = _agent(
            replan_prompt, label="ledger-replan", phase="plan", agent_type=_PLAN_AUTHOR_AGENT_TYPE,
            schema=_REPLAN_STAGED_SCHEMA, effort=_PLAN_AUTHOR_EFFORT, run_id=run_id,
        )
        b.append(
            "  if (_ledgerOwes && !_stageRefusal) { const _gateStaged = _stagedManifest; const _gateGap = _relayGap; "
            "_stagedManifest = null; _relayGap = null; "
            f"const _replanned = await {replan_call}; "
            "_ledgerAdded = (Array.isArray(_replanned?.added_rows) ? _replanned.added_rows : [])"
            ".filter((s) => typeof s === 'string' && s.trim()); "
            "if (_ledgerAdded.length) { _ledgerExec = true; "
            "_adoptManifest(_replanned?.manifest ?? (_replanned?.error ? { error: _replanned.error } : null)); } "
            f"else {{ if (!_ledgerUnlanded.length) {{ _gate = {{ ..._gate, arm: {_lit(ARM_XS)} }}; }} "
            "_stagedManifest = _gateStaged; _relayGap = _gateGap; } }"
        )

        if blitz_fn:
            # The plan-blitz arm's readyOpReply has no engine schema, so DoE's reviewer can still trim it:
            # this is the only leg that re-reads, and it stages first only when readyOp never ran.
            blitz_read_prompt = _cat(
                f"{head}\n\n{anchor}\n\n",
                "js:(entry.readyOpReply == null ? "
                + _lit(f"Run `{_INVOKE} {OP_ASK_STAGE} '")
                + " + JSON.stringify({ run_id: _runId, plan_path: _planRel, writes: _writes, "
                "cross_repo_approved: _crossRepoApproved, session_id: _SESSION_ID, "
                "...(_sizingRel ? { commit_sizing_path: _sizingRel } : {}) }) + "
                + _lit(
                    "'` once. If it replies {\"error\": ...}, return that message as stage_error with "
                    "manifest null and read nothing. Otherwise "
                )
                + " : '')",
                f"Read the file {manifest_rel} with the Read tool and return its JSON content verbatim as "
                "manifest, every field and every row, trimming nothing. When the file is absent or "
                "unreadable, return manifest null and no stage_error. Commit nothing.",
            )
            blitz_read_call = _agent(
                blitz_read_prompt, label="blitz-stage-read", phase="plan", agent_type=_MECHANICAL_AGENT_TYPE,
                agent_model=_MECHANICAL_MODEL, schema=_BLITZ_STAGE_READ_SCHEMA, effort=_MECHANICAL_EFFORT,
                run_id=run_id,
            )
            b.append("  const _blitzStageRead = async (entry) => {")
            b.append(f"    const _r = await {blitz_read_call};")
            b.append("    if (_r?.stage_error) { _adoptManifest({ error: _r.stage_error }); return; }")
            b.append(
                f"    if (!_r?.manifest) {{ _diskHalt = 'no stage manifest on disk: ' + {_lit(manifest_rel)}; return; }}"
            )
            b.append("    const _g = _wholeManifestGap(_r.manifest);")
            b.append(
                "    if (_g) { _diskHalt = 'stage manifest on disk is not whole: ' + _g; return; }"
            )
            b.append("    _stagedManifest = _r.manifest;")
            b.append("  };")

        def blitz_stage_read(entry: str, indent: str) -> str:
            """Read the staged manifest on a ready entry whose relay was not whole, staging first when
            readyOp never ran (no reply and no error, or plan-blitz's own 'did not run' error). A readyOp
            error of its own halts."""
            return (
                f"{indent}if ({entry} && typeof {entry} === 'object' && _planRel && !_stagedManifest && !_stageRefusal "
                f"&& ({entry}.readyOpReply != null || {entry}.readyOpError == null "
                f"|| /did not run/.test(String({entry}.readyOpError)))) {{ await _blitzStageRead({entry}); }}"
            )

    if xl_possible:
        _append_split_stage(b)

    fold_stage = not stop_after

    def stage_fold(lead: str) -> tuple[str, ...]:
        """Prompt parts that make the plan agent stage its own plan and return the reply as `manifest`."""
        return (
            f"{lead} run `{_INVOKE} {OP_ASK_STAGE} '",
            "js:JSON.stringify({ run_id: _runId, plan_path: '<plan_rel>', writes: _writes, "
            "cross_repo_approved: _crossRepoApproved, session_id: _SESSION_ID, " + _ONLY_ROWS_JS +
            "...(_sizingRel ? { commit_sizing_path: _sizingRel } : {}) })",
            "'` with <plan_rel> replaced by the plan's repo-relative path, and return its JSON reply "
            "verbatim as `manifest`, every field and every row, trimming nothing. If it replies "
            "`{\"error\": ...}`, return a `manifest` holding that message as its `error`, run_dir and "
            "marker_path empty, rows, review_declared_paths and test_targets empty, plan_id and head_sha "
            "null -- never an empty manifest without the error.",
        )

    # A raw ask's scout folds the falsifier itself; the plan legs author it only when the scout returned none.
    plan_falsifier_parts = (
        ("js:(_scoutFalsifier ? '' : " + json.dumps(_PLAN_FALSIFIER_CLAUSE) + ")",)
        if sized_in_run
        else (_PLAN_FALSIFIER_CLAUSE,)
    )
    plan_author = _cat(
        "js:" + _DESIGN_PREFIX_JS,
        f"{head}\n\n{anchor}\n\n",
        "Author the plan for the sizing at ",
        "js:_sizingRel",
        f": scaffold it with `{DOC_NEW_CMD} --type plan --sizing-object <that sizing path> --title "
        "\"<title>\" --out docs/plans/<sizing-stem>.md` (that launcher, never `python3` on the "
        "script; never author the frontmatter block by hand), then change its existing `scope_mode:` line to `spec-dispatch`, derive its "
        "spine from the sizing, then FILL the "
        "scaffold: no PLACEHOLDER, `path/to/file` or `<REPLACE:` marker may remain anywhere in the "
        "plan -- stage refuses a plan that still carries one. Each row states an outcome: the PM's "
        "verbatim words it serves, what is true when it is done, and the files it may touch; the "
        "executor designs the change, so never write the code, a diff or an 'implement exactly' "
        "instruction into a row. The sizing's pm_verbatims outrank any in-repo memo or doctrine. "
        "Return its repo-relative path as plan_rel."
        + ledger_clause
        + " Every ledger item is one the plan's rows must serve; quote each in the row that serves it."
        + (
            " Read the sizing's `scout_evidence` entries, if it has any, before deriving the spine."
            if plan_blitz_args
            else ""
        ),
        *(plan_falsifier_parts if fold_falsifier else ()),
        *(stage_fold(" Once the plan is filled and carries no marker,") if fold_stage else ()),
    )
    b.append(f"  if (!_halted && _gate.arm === {_lit(ARM_S)}) {{")
    b.append("    if (_gate.resume_plan) { _planRel = _gate.resume_plan; } else {")
    b.append("    phase('plan');")
    plan_call = _agent(
        plan_author, label="plan", phase="plan", agent_type=_PLAN_AUTHOR_AGENT_TYPE,
        schema=_PLAN_STAGED_SCHEMA if fold_stage else _PLAN_SCHEMA,
        effort=_PLAN_AUTHOR_EFFORT, run_id=run_id,
    )
    if fold_stage:
        b.append(f"    const _authored = await {plan_call};")
        b.append("    _planRel = _authored.plan_rel;")
        b.append("    _adoptManifest(_authored.manifest);")
        if fold_falsifier:
            b.append("    _planFalsifier = _asPlanFalsifier(_authored.falsifier);")
    else:
        b.append(f"    _planRel = (await {plan_call}).plan_rel;")
    b.append("    }")
    b.append("  }")
    if blitz_fn:
        b.append(f"  if (!_halted && _gate.arm === {_lit(ARM_M_PLUS)}) {{")
        b.append("    phase('plan');")
        # gateReportPath binds from the runtime `_sizingRel`: a raw ask's sizing is minted in-run.
        blitz_spread = f"...{json.dumps(plan_blitz_args, sort_keys=True)}, " if plan_blitz_args else ""
        signal_selects = load_review_signal_selects()
        review_prompt = _cat(
            "js:" + _DESIGN_PREFIX_JS,
            f"{head}\n\n{anchor}\n\n",
            "The ask gate minted a dispatch spine from the sizing's concrete acceptance rows at ",
            "js:_gate.spine_plan",
            ". Review it as the plan reviewer: every row's writes are real, bounded paths, its done "
            "condition is a runnable check, and together the rows serve the sizing's exit criterion. "
            "Apply every finding IN PLACE with Edit -- never scaffold a new plan, never change its "
            "path -- then return its repo-relative path as plan_rel. Then run the "
            "`plan.readiness_verdict` op on the reviewed plan and return that reply's pull_class and "
            "verdict alongside plan_rel. Also declare the spine's `review_signals` in its frontmatter "
            "(ids from this vocabulary, empty when none: "
            + ", ".join(sorted(signal_selects))
            + ") and return them as review_signals."
            + ledger_clause,
            " Every ledger item is one the plan's rows must serve; quote each in the row that serves it.",
            *(plan_falsifier_parts if fold_falsifier else ()),
            *(
                stage_fold(" When the final verdict is `ready`, also stage the plan:")
                + (" Otherwise leave `manifest` null.",)
                if fold_stage
                else ()
            ),
        )
        review_call = _agent(
            review_prompt, label="plan-review", phase="plan", agent_type=_PLAN_REVIEW_AGENT_TYPE,
            schema=_SPINE_REVIEW_STAGED_SCHEMA if fold_stage else _SPINE_REVIEW_SCHEMA,
            effort=_PLAN_REVIEW_EFFORT, run_id=run_id,
        )
        b.append("    let _spineFallback = null;")
        b.append("    if (_gate.spine_plan && !_gate.resume_plan) {")
        b.append(f"      const _spineReview = await {review_call};")
        if fold_falsifier:
            b.append("      _planFalsifier = _asPlanFalsifier(_spineReview?.falsifier);")
        b.append("      _spineFallback = _spineReview?.plan_rel || _gate.spine_plan;")
        b.append(
            f"      const _signalSelects = {json.dumps(signal_selects, sort_keys=True)};"
        )
        b.append(
            "      const _wrongReviewer = (_spineReview?.review_signals ?? []).some((s) => "
            f"_signalSelects[s] !== undefined && _signalSelects[s] !== {_lit(_PLAN_REVIEW_AGENT_TYPE)});"
        )
        b.append("      if (!_wrongReviewer) {")
        b.append("      if (_spineReview?.verdict === 'ready') {")
        b.append("        _planRel = _spineFallback;")
        if fold_stage:
            b.append("        _adoptManifest(_spineReview.manifest);")
        b.append("      } else {")
        b.append(
            "        _halted = { halted: " + _lit(HALT_REFUSAL) + ", reason: 'spine plan review verdict ' + "
            "String(_spineReview?.verdict ?? 'none') + ' (not ready)', review: _spineReview ?? null, "
            "sizing: _sizingRel, run_id: _runId };"
        )
        b.append("      }")
        b.append("      }")
        b.append("    }")
        b.append("    if (!_planRel && !_halted) {")

        # plan-blitz-ready-op-contract: omitted once staged and in a stop_after script (never stages).
        ready_op_js = (
            f"...(!_stagedManifest ? {{ readyOp: {{ op: {_lit(OP_ASK_STAGE)}, params: {{ run_id: _runId, "
            "writes: _writes, cross_repo_approved: _crossRepoApproved, session_id: _SESSION_ID, "
            + _ONLY_ROWS_JS
            + "...(_sizingRel ? { commit_sizing_path: _sizingRel } : {}) }, "
            + f"schema: {json.dumps(_MANIFEST_READ_SCHEMA, sort_keys=True)} }} }} : {{}}), "
            if fold_stage
            else ""
        )

        def _blitz_call(plan_path_js: str) -> str:
            return (
                f"await {_PLAN_BLITZ_FN}({{ {blitz_spread}mode: 'single', {ready_op_js}repoRoot: REPO_ROOT, waveIndex: 0, "
                "gateReportPath: REPO_ROOT + '/' + _sizingRel, "
                f"trailDir: {_lit(run_dir + '/' + _BLITZ_TRAIL)}, batons: [{{ ...(_gate.baton ?? {{}}), "
                "sized: true, sizingObject: _sizingRel, tshirt: _gate.tshirt, route: _gate.baton?.route ?? _gate.route, "
                f"planPath: {plan_path_js}, executionOpen: true }}] }});"
            )

        b.append(f"    const _blitz = {_blitz_call('_gate.resume_plan ?? _spineFallback ?? null')}")
        b.append("    const _ready = (_blitz?.ready ?? [])[0];")
        if fold_stage:
            b.append(
                "    if (_ready && typeof _ready === 'object' && !_stagedManifest) { _adoptManifest(_ready.readyOpReply, true); _adoptReadyError(_ready); }"
            )
        b.append("    _planRel = (_ready && typeof _ready === 'object') ? _ready.planPath : _ready;")
        if fold_stage:
            b.append(blitz_stage_read("_ready", "    "))
        b.append("    const _pulledEntry = (_blitz?.pulled ?? []).find((p) => p && p.planPath);")
        b.append("    let _repeatBlocking = null;")
        b.append("    if (!_planRel && _pulledEntry) {")
        revise_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            "plan-blitz pulled the plan at ",
            "js:_pulledEntry.planPath",
            " at its prep gate. Revise that plan IN PLACE with Edit -- never scaffold a new one, never "
            "change its path -- so every finding below is resolved, then return its repo-relative path "
            "as plan_rel. Then run the `plan.readiness_verdict` op on the revised plan, passing the "
            "pull's coverage and reviews values below when they are not null, apply every "
            "mechanical_repairs entry in place with Edit, then re-run the op with the plan alone "
            "(no coverage, no reviews: the repairs answered those findings, and the same values "
            "would pull the plan again), and return that reply's pull_class and verdict "
            "alongside plan_rel.\n\nPull reason:\n",
            "js:String(_pulledEntry.reason ?? '')",
            "\n\nPull coverage: ",
            "js:JSON.stringify(_pulledEntry.coverage ?? null)",
            "\nPull reviews: ",
            "js:JSON.stringify(_pulledEntry.reviews ?? null)",
            "\n\nGate report (read it first):\n",
            "js:String(_pulledEntry.prepGateReport ?? '(none)')",
            ledger_clause,
            " Every ledger item is one the plan's rows must serve; quote each in the row that serves it.",
            *(
                stage_fold(
                    " When the final verdict is `ready` and pull_class is `mechanical`, also stage the plan:"
                )
                + (" Otherwise leave `manifest` null.",)
                if fold_stage
                else ()
            ),
        )
        b.append(
            f"      const _revised = await {_agent(revise_prompt, label='plan-revise', phase='plan', agent_type=_PLAN_AUTHOR_AGENT_TYPE, schema=_REVISE_STAGED_SCHEMA if fold_stage else _REVISE_SCHEMA, effort=_PLAN_AUTHOR_EFFORT, run_id=run_id)};"
        )
        b.append(
            "      if (_revised?.pull_class === 'mechanical' && _revised?.verdict === 'ready') "
            "{ _planRel = _revised.plan_rel || _pulledEntry.planPath;"
            + (
                " if (_planRel === _revised.plan_rel) { _adoptManifest(_revised.manifest); }"
                if fold_stage
                else ""
            )
            + " } else {"
        )
        b.append(f"      const _reblitz = {_blitz_call('_revised?.plan_rel || _pulledEntry.planPath')}")
        b.append("      const _reready = (_reblitz?.ready ?? [])[0];")
        if fold_stage:
            b.append(
                "      if (_reready && typeof _reready === 'object' && !_stagedManifest) { _adoptManifest(_reready.readyOpReply, true); _adoptReadyError(_reready); }"
            )
        b.append("      _planRel = (_reready && typeof _reready === 'object') ? _reready.planPath : _reready;")
        if fold_stage:
            b.append(blitz_stage_read("_reready", "      "))
        b.append("      if (!_planRel) { _blitz.regate = _reblitz; }")
        b.append(
            "      const _rePulled = (_reblitz?.pulled ?? []).find((p) => p && p.planPath);"
        )
        b.append(
            "      if (!_planRel && Array.isArray(_pulledEntry.blocking) && Array.isArray(_rePulled?.blocking) "
            "&& _pulledEntry.blocking.length > 0 && _rePulled.blocking.length === _pulledEntry.blocking.length "
            "&& _rePulled.blocking.every((r, i) => r === _pulledEntry.blocking[i])) "
            "{ _repeatBlocking = _rePulled.blocking; }"
        )
        b.append("      }")
        b.append("    }")
        b.append(
            f"    if (!_planRel) {{ _halted = {{ halted: {_lit(HALT_REFUSAL)}, "
            "reason: 'plan-blitz reported no ready plan', blitz: _blitz, sizing: _sizingRel, run_id: _runId };"
        )
        b.append(
            "    const _driftPull = [...(_blitz?.regate?.pulled ?? []), ...(_blitz?.pulled ?? [])].find((p) => p && p.planPath "
            f"&& (String(p.reason ?? '') + String(p.prepGateReport ?? '')).includes({_lit(REGISTER_PIN_DRIFT_KIND)}));"
        )
        b.append("    if (_driftPull) {")
        b.append(
            "      const _pc = (_driftPull.reviews ?? _driftPull.reviewVerdicts ?? []).find((r) => r && r.reviewer === 'premise-check');"
        )
        b.append("      const _pcRows = _driftPull.premise?.rows ?? _driftPull.premiseRows;")
        b.append(
            "      const _premise = _pc ? { verdict: _pc.verdict, ...(_pc.ledger ? { ledger: _pc.ledger } : {}) } "
            ": Array.isArray(_pcRows) ? { rows: _pcRows } : null;"
        )
        b.append("      const _repinParams = { plan: _driftPull.planPath, premise_check: _premise };")
        b.append(
            f"      _halted.next_action = {{ kind: 'repin_register', op: {_lit(REPIN_OP)}, params: _repinParams, "
            f"call: {_lit(f'{_INVOKE} {REPIN_OP} ' + chr(39))} + JSON.stringify(_repinParams).replace(/'/g, \"'\\\\''\") + \"'\", "
            f"refire: {_refire_js(sizing_rel)}, "
            "note: _premise ? undefined : 'the pull carries no premise-check verdict: replace premise_check with the regate premise-check review {verdict[, ledger]}' };"
        )
        b.append("    }")
        b.append(
            f"    if (_repeatBlocking && !_halted.next_action) {{ _halted = {{ halted: {_lit(HALT_REFUSAL)}, "
            "reason: 'plan-blitz pulled twice on the same blocking set', blocking: _repeatBlocking, "
            "next_action: { kind: 'rule_on_blocking_rows', blocking: _repeatBlocking, "
            "note: 'a second pull blocked on the same rows: rule on them (revise the plan or accept them); the same plan pulls again' }, "
            "blitz: _blitz, sizing: _sizingRel, run_id: _runId }; }"
        )
        b.append("    }")
        b.append("    }")
        b.append("  }")
    else:
        b.append(
            f"  if (!_halted && _gate.arm === {_lit(ARM_M_PLUS)}) {{ _halted = {{ halted: {_lit(HALT_REFUSAL)}, "
            "reason: 'sizing resolved to M+ at emit time but the plan-blitz stage was not embedded', "
            "sizing: _sizingRel, run_id: _runId }; }"
        )

    if stop_after:
        if in_run_commit:
            _append_halt_record_commit(b)
        b.append(
            "  return _halted ? { halted_by: _halted, halt_commit: _haltCommit, sizing: _sizingRel, run_id: _runId, "
            f"{DESIGN_ROUTE_KEY}: _gate?.{DESIGN_ROUTE_KEY} ?? null }} : "
            f"(_gate.arm === {_lit(ARM_XS)} ? {{ route: 'dispatch', sizing: _sizingRel, {DESIGN_ROUTE_KEY}: _gate?.{DESIGN_ROUTE_KEY} ?? null }} : "
            f"{{ planned: [_planRel], sizing: _sizingRel, run_id: _runId, {DESIGN_ROUTE_KEY}: _gate?.{DESIGN_ROUTE_KEY} ?? null }});"
        )
        return _finish_script(
            b,
            manifest_rel,
            "Size, gate and plan from an ask, then stop before stage.",
            _phase_titles(
                carried=_carried_titles(b),
                blitz_phases=blitz_phases,
                review_titles=[],
                through_plan=True,
            ),
        )

    if xl_possible:
        b.append(f"  if (!_halted && _gate.arm === {_lit(ARM_ROADMAP)}) {{")
        b.append("    _viaRoadmap = true;")
        if rb_fn and blitz_fn:
            from coordinator_core.ops.dispatch_emit.ask_roadmap import roadmap_stage_lines

            b.extend(
                roadmap_stage_lines(
                    repo_root=repo_root, run_id=run_id, interaction_mode="ceo",
                    plan_blitz_args=plan_blitz_args, script_path=script_path,
                )
            )
        else:
            b.append(
                f"    _halted = {{ halted: {_lit(HALT_REFUSAL)}, reason: 'the sizing resolved to the roadmap arm, "
                "which this script does not embed: resubmit Workflow({name: \"coordinator:engine-ask\", "
                "args: {sizing: \"' + _sizingRel + '\"}})', "
                "sizing: _sizingRel, run_id: _runId };"
            )
        b.append("  }")
    if judged:
        b.append("  let _falsifierSpec = null;")
    b.append("  let _allLanded = false;")
    b.append("  if (!_viaRoadmap) {")
    b.append("  if (!_halted) {")
    # Stage is folded into the last act of the agent that closes the gate, plan or plan-blitz ready
    # path; there is no standalone stage or read leg. Each engine-owned fold site returns its relay under
    # `_MANIFEST_READ_SCHEMA` and `_adoptManifest` admits it only when `_wholeManifestGap` finds no gap;
    # a not-whole relay halts by name. The plan-blitz arm alone re-reads the disk manifest through
    # `_blitzStageRead` when DoE's schema-less readyOpReply arrives trimmed. The falsifier is authored
    # by the scout or the plan legs, never a leg of its own.
    b.append(
        "  let _stageHalt = _stageRefusal ? 'stage refused: ' + _stageRefusal : "
        "(_diskHalt ?? (_relayGap ? 'stage manifest relay is not whole: ' + _relayGap : null));"
    )
    b.append("  if (_stagedManifest) { _manifest = _stagedManifest; }")
    if judged and fold_falsifier:
        spec_js = "_scoutFalsifier ?? _planFalsifier" if sized_in_run else "_planFalsifier"
        b.append(f"  if (_gate.arm && _gate.arm !== {_lit(ARM_XS)}) {{ _falsifierSpec = {spec_js}; }}")
    b.append(
        f"  if (!_stagedManifest) {{ _manifest = {{ error: '{NO_MANIFEST_PREFIX}: the agent that closes the "
        "gate, plan or plan-blitz ready path returned none' }; }"
    )
    b.append(
        "  if (!_stagedManifest && _stageHalt) { _manifest = { error: _stageHalt, owed_em_ids: _stageOwedEm?.ids ?? [], "
        "owed_plan: _stageOwedEm?.plan ?? null }; }"
    )
    b.append("  if (!_manifest.error) { for (const g of (_manifest.gated ?? [])) { _incompleteChunks.push(g.id); } }")
    b.append("  if (!_manifest.error) { for (const o of (_manifest.owed ?? [])) { _incompleteChunks.push(o.id); } }")
    b.append(
        f"  if (!_manifest.error) {{ {_OWNER_ROWS_JS} = (_manifest.gated ?? []).filter((g) => g.owner_repo)"
        ".map((g) => ({ id: g.id, owner_repo: g.owner_repo, owner_root: g.owner_root ?? '', "
        "closure_key: g.closure_key ?? null })).concat(_manifest.engine_rows ?? []); }"
    )
    # Is anything left to dispatch, or only to judge and commit: a resume whose rows all landed
    # stages no rows, only owed/gated ones, and joins the verify-and-commit tail.
    b.append(
        "  if (!_manifest.error && !(_manifest.rows ?? []).length && Array.isArray(_gate.resume_rows) "
        "&& ((_manifest.owed ?? []).length || (_manifest.gated ?? []).length)) "
        f"{{ _allLanded = true; _gate = {{ ..._gate, arm: {_lit(ARM_XS)} }}; }}"
    )
    b.append(
        f"  if (_manifest.error && /^(?:stage refused: )?{STAGE_REFUSAL_PREFIX}/.test(String(_manifest.error))) {{ _halted = {{ "
        f"halted: {_lit(HALT_DESIGN_REVIEW)}, kind: {_lit(HALT_DESIGN_REVIEW)}, reason: String(_manifest.error), "
        f"board_url: _gate?.{DESIGN_ROUTE_KEY}?.board?.url ?? null, {DESIGN_ROUTE_KEY}: _gate?.{DESIGN_ROUTE_KEY} ?? null, "
        f"sizing: _sizingRel, plan: _planRel, run_id: _runId, next_action: {_design_next_action_js(sizing_rel)} }}; }}"
    )
    b.append(
        "  else if (_manifest.error || (!(_manifest.rows ?? []).length && !_allLanded && !_resumeLedger)) { _halted = { halted: "
        f"{_lit(HALT_REFUSAL)}, kind: {_lit(HALT_REFUSAL)}, reason: _manifest.error || 'stage staged no rows', "
        "sizing: _sizingRel, plan: _planRel, run_id: _runId, "
        f"...({_owed_handback_js(sizing_rel)} ? {{ next_action: {_owed_handback_js(sizing_rel)} }} : "
        f"{_all_em_next_action_js(sizing_rel)}) }}; }}"
    )
    b.append("  }")

    b.append("  const _verdictReports = [];")
    b.append("  if (!_halted && (!_resumeLedger || _ledgerExec)) {")
    # The base is the staged manifest's head_sha (execute-start HEAD, written by the stage op without a
    # spawn); no agent reads HEAD.
    b.append(f"  {_emit._RUN_BASE_VAR} = /^[0-9a-f]{{40}}$/.test(String(_manifest?.head_sha ?? '')) ? _manifest.head_sha : '';")
    b.append(
        f"  if (!_runBase || !{_RUNTIME_PLAN_PATH_JS}) {{ _halted = {{ halted: "
        f"{_lit(HALT_REFUSAL)}, kind: {_lit(HALT_REFUSAL)}, "
        f"reason: 'checkpoint trailer unavailable: ' + (!{_RUNTIME_PLAN_PATH_JS} ? 'no plan path for Checkpoint-Plan' : 'no run base for Checkpoint-Base') "
        "+ '; every checkpoint commit would be refused', "
        "sizing: _sizingRel, plan: _planRel, run_id: _runId }; }"
    )
    b.append("  }")
    b.append("  if (!_halted && (!_resumeLedger || _ledgerExec)) {")
    b.append(f"  phase({_lit(_emit._EXECUTE_PHASE_TITLE)});")
    b.append("  const _rows = {};")
    b.append(
        f"  const _rowVerifyWanted = !(_gate.arm !== {_lit(ARM_XS)} && (_manifest.test_targets ?? []).length);"
    )
    b.append("  const _waves = [...new Set(_manifest.rows.map((r) => r.wave))].sort((a, b) => a - b);")
    b.append("  if (_manifest.fanout) log('Fanout: ' + JSON.stringify(_manifest.fanout));")
    row_prompt = _cat(
        f"{brief_prefix}{_AGENT_DEFS_MARKER}{_emit._prompt_head(preamble)}\n\n{anchor}\n\n",
        "Your brief is the file ",
        "js:r.brief_path",
        f" -- read it completely, then execute it as written.{ledger_clause}{session_tail}",
        "js:" + _row_sidecar_js(run_id, _lit(build_work_label(''))),
    )
    work_label = _lit(build_work_label(''))
    exec_phase = _lit(_emit._EXECUTE_PHASE_TITLE)
    # Rows sharing a `group` share one memoised agent; each member keeps its own `_runRow` and reads
    # its own `<ID>:` line out of the shared reply, so a member the agent omitted reads incomplete.
    b.append(GROUP_MEMBER_RESULT_JS)
    b.append(f"  const _rowPrompt = (r) => {row_prompt};")
    b.append("  const _groups = {};")
    b.append(
        "  function _groupPrompt(g) {\n"
        "    const ms = _manifest.rows.filter((x) => x.group === g);\n"
        "    return 'You own ' + ms.length + ' assignments, in this order: ' + ms.map((m) => m.id).join(', ') "
        f"+ {_js_string_literal(GROUP_HEADER_TAIL)} + ms.map("
        "(m) => '=== Assignment ' + m.id + ' ===\\n' + _rowPrompt(m) + '\\n\\n').join('');\n"
        "  }"
    )
    b.append(
        "  async function _groupRun(r) {\n"
        f"    _groups[r.group] ??= agent(_groupPrompt(r.group), {{ label: {work_label} + r.group, phase: {exec_phase}, "
        f"agentType: r.agent_type, model: r.model, stallMs: {_emit._EXECUTOR_STALL_MS} }});\n"
        "    return _memberResult(await _groups[r.group], r.id);\n"
        "  }"
    )
    routed = _routing_enabled(repo_root)
    if routed:
        b.append(_route_js.routed_run_js())
        b.append(_ROUTE_ADAPTER_JS)
    b.append("  for (const w of _waves) {")
    b.append("    for (const r of _manifest.rows.filter((x) => x.wave === w)) {")
    b.extend(_reviewer_row_js(anchor, work_label, run_id))
    routed_arm = f"r.route ? {_routed_row_js(work_label, exec_phase)} : " if routed else ""
    b.append(
        f"      else _rows[r.id] = _runRow(r.id, (r.deps ?? []).map((d) => _rows[d]), null, async () => {routed_arm}r.group ? _groupRun(r) : agent(_rowPrompt(r), "
        f"{{ label: {work_label} + r.id, phase: {exec_phase}, "
        f"agentType: r.agent_type, model: r.model, stallMs: {_emit._EXECUTOR_STALL_MS} }}), "
        "{ title: r.id, paths: r.writes ?? [], declared: r.writes ?? [] });"
    )
    b.append("    }")
    # Wave barrier: wave w+1 launches only once wave w's commit:wave-w leg has settled.
    b.append("    const _waveIds = _manifest.rows.filter((x) => x.wave === w).map((x) => x.id);")
    b.append("    await Promise.allSettled(_waveIds.map((i) => _rows[i]));")
    b.append("    await _commitWave(w, _waveIds);")
    b.append("  }")
    b.append(
        "  if (_commitFailures.length) log('Wave commit failures (the terminal commit covers their paths): ' "
        "+ _commitFailures.join('; '));"
    )
    b.append("  await Promise.all(Object.values(_rows));")
    # verify:<row> (emit.py's _runRow) pushes only behind _rowVerifyWanted: the terminal test leg
    # covers every row's scope, except on the XS arm where its test waits on review applying a fix.
    b.append("  await Promise.all(_verifications);")
    b.append("  }")
    b.append(
        "  if (!_halted) { const _r = _heldDependentsReason(); if (_r) "
        f"_halted = {{ halted: {_lit(HALT_DEPENDENTS_UNSTARTED)}, detail: _r }}; }}"
    )

    review_text = _single_exit_review(
        [_emit._unconst(block, _REVIEW_RESULT_NAMES) for _, block in review_blocks]
    )
    b.append(
        "  const _noReviewPaths = !!((_manifest?.rows ?? []).length && "
        "!(_manifest?.review_declared_paths ?? []).length);"
    )
    # The review wave shadows `agent` inside its own block (design brief prefix only) rather than
    # renaming its call sites: the leg register, phase-title and brief checks find legs by the
    # literal `agent(` token, so a renamed call site is invisible to every one of them.
    b.append("  const _reviewOuterAgent = agent;")
    b.append(f"  {{ const agent = (p, o) => _reviewOuterAgent({_DESIGN_PREFIX_JS} + p, o);")
    b.append(
        f"  if (!_halted && _gate.arm !== {_lit(ARM_XS)} && !_noReviewPaths) {{\n" + review_text + "\n  }"
    )
    b.append("  }")
    # XS skips review; an empty wave (zero slices, no delivery verdict) is the form terminal_commit's
    # inline_review already accepts, so the run still reaches commit without a fabricated verdict.
    b.append(f"  if (!_halted && _gate.arm === {_lit(ARM_XS)}) {{ _reviewWave = []; }}")
    b.append(
        "  if (!_halted && _noReviewPaths && _gate.arm !== "
        f"{_lit(ARM_XS)}) {{ log('execute_review skipped: no row declares a path, nothing to review'); "
        "_reviewWave = []; }"
    )
    b.append(_review_blocked_halt_js(sizing_rel))
    test_guard = (
        f"(_gate.arm !== {_lit(ARM_XS)} || _reviewApplied > 0) && (_manifest.test_targets ?? []).length"
    )
    review_applied = (
        "const _reviewApplied = (Array.isArray(_reviewWave) ? _reviewWave : [])"
        ".reduce((n, w) => n + ((w && w.applied) || 0), 0);"
    )
    # A spine whose writes map to no runnable test target (`pathspec.NoTestTargetError`, e.g. a
    # .ts-only change in a repo the locator cannot see tests for) is degraded, never a pass.
    no_target = (
        f"  if (!_halted && _manifest && _manifest.no_test_target) {{ {_emit._TEST_RESULT_VAR} = "
        "{ status: 'degraded', summary: 'No terminal test run: ' + _manifest.no_test_target.reason }; }"
    )
    review_vars = _emit.review_stage_vars(
        review,
        bookkeeping_stem_literal=_lit(review_wave_bookkeeping_stem(run_id, None)),
        plan_id_literal="(_manifest.plan_id ?? null)",
    )
    na_kind, na_op, na_params = next_action_parts(
        has_commit_request=True,
        review_vars=review_vars,
        test_var=_emit._TEST_RESULT_VAR,
        falsifier_var=_emit._FALSIFIER_RESULT_VAR if judged else None,
        verification_var="_verifications",
        test_absent_status="not_run",
        script_path=script_path,
        session_id=session_id,
    )
    if judged:
        verdict_parts: tuple[str, ...] = (
            judge_brief[0],
            "js:(" + owner_repo_clause_js(_OWNER_ROWS_JS) + ")",
            "js:" + _FALSIFIER_SUFFIX_JS,
            "\n\nThe stage manifest withheld these chunks (gated): ",
            "js:" + _GATED_IDS_JS,
        )
    else:
        verdict_parts = (
            "Rule the run against every requirement "
            + ("on record (the PM's, and the relayed ask, which is not the PM's words)" if relayed else "the PM has on record")
            + ", never against the "
            f"plan alone. Re-read the requirements ledger at {ledger_rel} now (revisions may have landed "
            f"since the run started), then {record_dir}/{PM_INTENT_FILE} when it exists, then the run's change "
            "(`git status --porcelain` and `git diff`)",
            "js:" + owner_repo_clause_js(_OWNER_ROWS_JS),
            ", the plan when one exists (",
            "js:(_planRel ?? 'none')",
            f"), and the briefs under {run_dir}/briefs. " + _LEDGER_ITEMS_RULES
            + "The stage manifest withheld these chunks (gated): ",
            "js:" + _GATED_IDS_JS,
            _LEDGER_GATED_RULE,
            *((_LEDGER_DOC_WRITES_RULE,) if in_run_commit else ()),
        )
    verify_call = _verify_call(
        head=head,
        anchor=anchor,
        test_js=_test_instruction_js(run_base_sha or "HEAD"),
        docs_parts=_docs_prompt_parts(run_id=run_id, manifest_rel=manifest_rel) if in_run_commit else (),
        verdict_parts=(*verdict_parts, DEFAULT_MET_BY_CLAUSE, "js:" + _SFE_WAVE_SIDECARS_JS, "js:" + _RESUME_LEDGER_CLAUSE_JS),
        judged=judged,
        schema=_verify_schema(judge_brief[1] if judged else None),
        run_id=run_id,
    )
    unjudged = (
        "{ status: 'indeterminate', differs_from_baseline: null, observation: 'unjudged: verify leg returned "
        "no verdict', reason: 'verify leg returned no judge verdict', sidecar_path: null }"
    )
    verify_lines = [
        "  if (!_halted && _manifest && !_manifest.error) {",
        f"    phase({_lit(ASK_VERIFY_PHASE)});",
        "    " + review_applied,
        f"    const _testWanted = !!({test_guard});",
        *([f"    const _docsWanted = !!({_DOCS_PRE_TAIL_GUARD});"] if in_run_commit else []),
        "    let _verify = null;",
        f"    try {{ _verify = await {verify_call}; }} catch (e) {{ const _m = String((e && e.message) ?? e); "
        "if (_USAGE_LIMIT_RE.test(_m)) { _haltOnUsageLimit(e); } else { log('verify leg threw: ' + _m); } }",
        f"    if (_testWanted && _verify && _verify.test) {{ {_emit._TEST_RESULT_VAR} = _verify.test; }}",
        *(
            [
                "    if (_docsWanted && _verify && Array.isArray(_verify.doc_paths)) { _docs = { doc_paths: "
                "_verify.doc_paths, note_path: _verify.note_path ?? null, stale_docs: "
                "Array.isArray(_verify.stale_docs) ? _verify.stale_docs : [] }; }"
            ]
            if in_run_commit
            else []
        ),
        *(
            [
                f"    {_emit._FALSIFIER_RESULT_VAR} = (_verify && _verify.judge) ? "
                f"{_emit._JUDGE_VERDICT_RULES_JS}(_verify.judge) : {unjudged};"
            ]
            if judged
            else []
        ),
        "    _ledger = (_verify && Array.isArray(_verify.items)) ? { all_of_pm_ask: _verify.all_of_pm_ask === true, "
        "items: _verify.items, conflicts: Array.isArray(_verify.conflicts) ? _verify.conflicts : [] } : null;",
        "  }",
    ]
    verify_lines[-1:-1] = [_test_verdict_empty_halt_js(sizing_rel).replace("TEST_GUARD", test_guard)]
    _rl = f"_gate.{RESUME_LEDGER_KEY}"
    # The "writes are exactly these paths" clause below is prompt-only; the terminal commit's declared-path
    # check bounds what counts as met, not what the leg touches.
    remaining_prompt = _cat(
        f"{head}\n\n{anchor}\n\n",
        "Ledger-resume remaining-work leg. An earlier run halted; its revise notes may ask for work with no "
        "requirement row blocking. Rows still not_met and not ruled out_of_scope by the revise: ",
        f"js:{_rl}.blocking.join(', ')",
        f". Read each row's register text in {ledger_rel}. Work still owed, by row: ",
        f"js:JSON.stringify({_rl}.{RESUME_LEDGER_REMAINING_FIELD} ?? {{}})",
        ". Revise notes, oldest first: ",
        f"js:JSON.stringify({_rl}.{RESUME_LEDGER_NOTES_FIELD} ?? [])",
        f". Also read {record_dir}/{NOTES_FILE} when it exists: the notes of this refire, newer than the ones above. "
        "Do the owed work now. Your writes are exactly these declared paths: ",
        "js:(_manifest?.rows ?? []).flatMap((r) => r.writes ?? []).join(', ')",
        ". A row whose work you cannot finish, or whose owed work lies on a path outside those writes, stays "
        "not_met: write nothing for it and say so in `summary`. Commit and stage nothing."
        f"{session_tail}",
    )
    remaining_call = _agent(
        remaining_prompt, label="ledger-remaining", phase=ASK_VERIFY_PHASE,
        agent_type=_emit._EXECUTOR_AGENT_TYPE, schema=_obj(["summary"], {"summary": _STR}),
        run_id=run_id,
    )
    verify_lines[2:2] = [
        f"    const _reviseNoted = _resumeLedger && ({_rl}.{RESUME_LEDGER_NOTES_FIELD} ?? []).length > 0;\n"
        f"    if (_resumeLedger && !_ledgerExec && (({_rl}.blocking ?? []).length || _reviseNoted)) {{\n"
        f"      if (!_allLandedResume || _reviseNoted) {{ try {{ await {remaining_call}; }} catch (e) {{ const _m = String((e && e.message) ?? e); "
        "if (_USAGE_LIMIT_RE.test(_m)) { _haltOnUsageLimit(e); } else { log('ledger-remaining leg threw: ' + _m); } } }\n"
        "    }"
    ]
    b.extend(verify_lines)
    b.append(no_target)
    b.append("  if (!_halted && _manifest && !_manifest.error) {")
    b.append(
        "    const _reconciled = [];\n"
        f"    const _designPrefix = {json.dumps(_DESIGN_ANCHOR_PREFIX)};\n"
        "    for (const i of (_ledger?.items ?? [])) {\n"
        "      if (!i || i.status !== 'met' || !String(i.anchor ?? '').startsWith(_designPrefix)) continue;\n"
        "      const _mb = (Array.isArray(i.met_by) ? i.met_by : [i.met_by]).some((m) => typeof m === 'string' && m.trim() && m.trim().toLowerCase() !== 'none');\n"
        "      if (!_mb) { i.status = 'not_met'; i.essential = true; "
        "i.remaining = ['met_by: the senior-front-end review or ratification file of the design board']; }\n"
        "    }\n"
        "    for (const i of (_ledger?.items ?? [])) {\n"
        "      if (!i || i.status !== 'not_met' || !Array.isArray(i.remaining) || !i.remaining.length) continue;\n"
        "      const rest = i.remaining.filter((a) => !((_manifest.doc_writes ?? []).includes(a) "
        "&& (_docs?.doc_paths ?? []).includes(a)));\n"
        "      if (rest.length === i.remaining.length) continue;\n"
        "      if (rest.length) i.remaining = rest; else { _reconciled.push(i.id); i.status = 'met'; delete i.remaining; }\n"
        "    }"
    )
    if judged:
        v = _emit._FALSIFIER_RESULT_VAR
        b.append(
            f"    if ({v}?.status === 'not_met' && (_ledger?.items ?? []).length "
            "&& (_ledger.items).every((i) => i && i.status === 'met') "
            f"&& !String({v}.reason ?? '').startsWith('not wired up: ') "
            f"&& [undefined, 'pass', 'not_present'].includes({v}.falsifier?.verdict) "
            f"&& (!Array.isArray({v}.clauses) || {v}.clauses.every((c) => !c || c.status === 'met'))) "
            f"{{ {v} = {{ ...{v}, status: 'met', differs_from_baseline: null, "
            "reason: _reconciled.join(', ') ? 'met after reconcile: ' + _reconciled.join(', ') : 'met: every ledger item met', "
            "reconciled_from: 'not_met' }; }"
        )
    b.append(
        f"    const _postCommitActs = {json.dumps(list(POST_COMMIT_ACTS))};\n"
        "    const _ledgerBlocking = (l) => (l?.items ?? []).filter((i) => {\n"
        "      if (!i) return false;\n"
        "      const rem = Array.isArray(i.remaining) ? i.remaining : [];\n"
        "      const status = i.status === 'not_met' && rem.length && rem.every((a) => _postCommitActs.includes(a)) "
        "? 'post_commit' : i.status;\n"
        "      const acts = status === 'post_commit' && i.status === 'not_met' ? rem : i.post_commit_acts;\n"
        "      if (status === 'drifted') return true;\n"
        "      if (status === 'not_met' && typeof i.withheld_by === 'string' && i.withheld_by) "
        "return !(_manifest?.gated ?? []).map((g) => g.id).includes(i.withheld_by);\n"
        "      if (status === 'not_met') return l.all_of_pm_ask === true || i.essential !== false;\n"
        "      if (status === 'post_commit') return !Array.isArray(acts) || !acts.length "
        "|| acts.some((a) => !_postCommitActs.includes(a));\n"
        "      return false;\n"
        "    });"
    )
    # `out_of_scope` accepts only register rows still open at stage; any other blocking id is named
    # apart, with the ledger re-check as its remedy.
    b.append(
        f"    const _rulable = _manifest?.rulable_ids ?? _gate.{RESUME_LEDGER_KEY}?.rulable_ids ?? [];\n"
        f"    const _ledgerIds = _gate.{RESUME_LEDGER_KEY}?.ledger_item_ids ?? _manifest?.ledger_item_ids ?? null;\n"
        f"    const _withheldIds = _gate.{RESUME_LEDGER_KEY}?.withheld_ids ?? _manifest?.withheld_ids ?? null;\n"
        "    const _allRuledOut = Array.isArray(_ledgerIds) && !_ledgerIds.length "
        "&& Array.isArray(_withheldIds) && _withheldIds.length > 0;\n"
        "    const _emptyVerdict = !!_ledger && (_ledger.items ?? []).length === 0 && !_allRuledOut;\n"
        "    const _owedIds = _emptyVerdict && Array.isArray(_ledgerIds) ? _ledgerIds : [];\n"
        "    const _blkIds = (_emptyVerdict ? _owedIds : (_ledger ? _ledgerBlocking(_ledger) : []))"
        ".map((i) => (i && typeof i === 'object') ? i.id : i).filter(Boolean);\n"
        "    const _blkRulable = _blkIds.filter((id) => _rulable.includes(id));\n"
        "    const _blkForeign = _blkIds.filter((id) => !_rulable.includes(id));"
    )
    b.append(
        "    if (!_halted && (!_ledger || _emptyVerdict || _ledgerBlocking(_ledger).length)) "
        "{ _halted = { halted: 'ledger-check', "
        "items: _emptyVerdict ? _owedIds.map((id) => ({ id, status: 'not_met' })) : (_ledger ? _ledgerBlocking(_ledger) : []), "
        f"conflicts: _ledger?.conflicts ?? [], ledger: {_lit(ledger_rel)}, sizing: _sizingRel, run_id: _runId, "
        f"{HALT_PLAN_FIELD}: _planRel, "
        "next_action: (_blkIds.length ? 'Ledger rows ' + _blkIds.join(', ') + (_emptyVerdict ? ' were owed a verdict and the verify leg returned none' : '') + ' block the commit.' "
        ": !_ledger ? 'The verify leg returned no items list, so the ledger holds no verdict: fix the verify leg before re-running.' "
        ": 'The verify leg returned an empty items list and no ledger row ids are on record to hold it against: fix the verify leg before re-running.') "
        "+ (_blkRulable.length ? ' Run coordinator-invoke workflow.revise with run_id ' + _runId "
        "+ ' with out_of_scope: [' + _blkRulable.join(', ') + '] for the rows ruled out; `verbatim` carries the PM words ruling them out, recorded as the ruling and adding no row. The re-fire resumes at the ledger check.' : '') "
        "+ (_blkForeign.length ? ' ' + _blkForeign.join(', ') + ' are not in the stage manifest rulable_ids: re-run the ledger check.' : '') "
        "+ (_blkIds.length ? ' Re-fire the same sizing: it executes the remaining work of the rows it keeps, briefed with the revise notes, '"
        "+ 'and a row is met only on work changed since this halt.' : '') }; }"
    )
    b.append("  }")
    b.append("  }")
    b.append(f"  {CAP_HELPER_JS}")
    if not in_run_commit:
        b.append(f"  {TERMINAL_COMMIT_CLI_HELPER_JS}")
    _fv = _emit._FALSIFIER_RESULT_VAR
    criterion_open = " && !_criterionOpen" if judged else ""
    # The chunk gate's stop rule and the held-dependents check assign `_halted` a bare string; the
    # halt-record commit and `_mkRevise` read it as the typed `{halted, ...}` shape.
    b.append(
        "  if (_halted && typeof _halted !== 'object') { _halted = { halted: "
        f"(_haltedByHold ? {_lit(HALT_DEPENDENTS_UNSTARTED)} : {_lit(HALT_STOP_RULE)}), "
        "reason: String(_halted), sizing: _sizingRel, run_id: _runId }; }"
    )
    if judged:
        b.append(f"  const _criterionOpen = !!({_fv} && {_fv}.status === 'not_met');")
    roadmap_fan = bool(rb_fn and in_run_commit)
    if roadmap_fan:
        b.append(
            "  const _roadmapFan = !!(_viaRoadmap && !_halted && _nextAction && _nextAction.kind === 'terminal_commit');"
        )
    b.append(
        f"  const _closeable = !!({'_roadmapFan || (' if roadmap_fan else '('}!_halted && _manifest && !_manifest.error"
        f"{criterion_open} && !(_deliveryVerdict && _deliveryVerdict.verdict === 'FAIL')));"
    )
    b.append(
        "  const _mkRevise = () => ({ run_id: _runId, sizing: _sizingRel, "
        "reason: (_halted && _halted.halted) ? _halted.halted : (!_manifest || _manifest.error) ? 'manifest-error' : "
        + (
            f"_criterionOpen ? (String({_fv}.reason ?? '').startsWith('not wired up: ') ? String({_fv}.reason).slice(0, 200) : "
            f"'criterion not_met' + ({_fv}.reason ? ': ' + String({_fv}.reason).slice(0, 200) : '')) : "
            if judged
            else ""
        )
        + "'delivery verdict FAIL', "
        f"revise: {_lit(f'{_INVOKE} workflow.revise (run_id; verbatim for a PM requirement, note for free text)')}, "
        f"refire: {_refire_js(sizing_rel)} }});"
    )
    stopped_na = (
        "{ kind: 'revise_and_refire', op: 'workflow.revise', params: { run_id: _runId, sizing: _sizingRel, halted: _halted } }"
        if in_run_commit
        else "{ kind: 'none', op: null, params: null }"
    )
    b.append(
        f"  _nextAction = {'_roadmapFan ? _nextAction : ' if roadmap_fan else ''}_closeable "
        f"? {{ kind: {na_kind}, op: {na_op}, params: {na_params} }} "
        f": (_halted?.next_action?.kind === 'repin_register' ? _halted.next_action : {stopped_na});"
    )
    b.append("  let _reviseAndRefire = _closeable ? null : _mkRevise();")
    b.append(
        f"  if (_resumeLedger && _nextAction && _nextAction.params && _gate.{RESUME_LEDGER_KEY}.inline_review "
        f"&& typeof _gate.{RESUME_LEDGER_KEY}.inline_review === 'object') "
        f"{{ _nextAction.params.inline_review = {{ ..._gate.{RESUME_LEDGER_KEY}.inline_review }}; }}"
    )
    # An end that is not closeable and carries no halt would return with neither a commit nor a
    # halted_by; it halts here so the halt-record commit lands the run record.
    unclosable_kind = (
        f"_criterionOpen ? {_lit(HALT_CRITERION_NOT_MET)} : " if judged else ""
    ) + f"(_deliveryVerdict && _deliveryVerdict.verdict === 'FAIL') ? {_lit(HALT_DELIVERY_FAIL)} : {_lit(HALT_NOT_CLOSEABLE)}"
    b.append(
        "  if (!_closeable && !_halted && !(_commit && _commit.committed === true)) { "
        f"_halted = {{ halted: {unclosable_kind}, reason: _reviseAndRefire.reason, "
        "sizing: _sizingRel, run_id: _runId }; }"
    )
    b.append(
        "  if (_nextAction.params && _ledger) { _nextAction.params.ledger_verdict = { items: (_ledger.items ?? [])"
        ".map((i) => ({ id: i.id, status: i.status, essential: i.essential !== false, wired: i.wired === true, "
        "...(i.superseded_by ? { superseded_by: i.superseded_by } : {}), "
        "...(i.withheld_by ? { withheld_by: i.withheld_by } : {}), "
        "...(i.post_commit_acts ? { post_commit_acts: i.post_commit_acts } : {}), "
        "...(i.met_by ? { met_by: i.met_by } : {}), "
        "...(i.root_read != null ? { root_read: i.root_read } : {}), "
        "...(i.remaining ? { remaining: i.remaining } : {}) })) }; }"
    )
    b.append(
        f"  if (_nextAction.params && _resumeLedger) {{ _nextAction.params.{TERMINAL_RESUME_LEDGER_PARAM} = "
        f"{{ run_id: _runId, {RESUME_LEDGER_HALT_SHA_FIELD}: _gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_HALT_SHA_FIELD} ?? null, "
        f"blocking: _gate.{RESUME_LEDGER_KEY}.blocking ?? [] }}; "
        f"if (Array.isArray(_gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_HALT_BLOCKING_FIELD})) "
        f"{{ _nextAction.params.{TERMINAL_RESUME_LEDGER_PARAM}.blocking = "
        f"_gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_HALT_BLOCKING_FIELD}; }} }}"
    )
    # A review agent may return a sidecar as an absolute Windows path; the commit command must carry
    # repo-relative forward slashes only, or a re-escaping commit agent corrupts the JSON.
    b.append(
        "  if (_nextAction.params && _nextAction.params.inline_review) { "
        "const _root = REPO_ROOT.replace(/\\\\/g, '/').replace(/\\/+$/, '') + '/'; "
        "const _rel = (p) => { const s = String(p).replace(/\\\\/g, '/'); "
        "return s.toLowerCase().startsWith(_root.toLowerCase()) ? s.slice(_root.length) : s; }; "
        "const _ir = _nextAction.params.inline_review; "
        "if (Array.isArray(_ir.wave_sidecar_paths)) { _ir.wave_sidecar_paths = _ir.wave_sidecar_paths.map(_rel); } "
        "if (Array.isArray(_ir.wave_reviews)) { _ir.wave_reviews = _ir.wave_reviews.map((w) => w && typeof w.sidecar_path === 'string' ? { ...w, sidecar_path: _rel(w.sidecar_path) } : w); } "
        "if (typeof _ir.prep_sidecar === 'string') { _ir.prep_sidecar = _rel(_ir.prep_sidecar); } }"
    )
    if in_run_commit:
        _append_docs_and_commit(
            b, head=head, anchor=anchor, repo_root=repo_root, run_id=run_id, manifest_rel=manifest_rel,
            script_path=script_path,
        )
        _append_halt_record_commit(b)
    b.append(
        f"  return {{ arm: _gate.arm, {DESIGN_ROUTE_KEY}: _gate?.{DESIGN_ROUTE_KEY} ?? null, sizing: _sizingRel, plan: _planRel, run_id: _runId, "
        "split: _splitExit ? _splitExit.split : null, fire: _splitExit ? _splitExit.fire : "
        f"(_halted && _halted.halted === {_lit(HALT_RESEARCH)} && Array.isArray(_halted.fire) ? _halted.fire : "
        "(_commit?.close?.siblings?.released ?? [])), "
        f"manifest: {_lit(manifest_rel)}, rows: (_manifest?.rows ?? []).map((r) => r.id), "
        "incomplete: _incompleteChunks, "
        "withheld: ((_manifest && !_manifest.error) ? (_manifest.gated ?? []) : []).map((g) => ({ id: g.id, owner_repo: g.owner_repo ?? '', closure_key: g.closure_key ?? null })), "
        "owed: ((_manifest && !_manifest.error) ? (_manifest.owed ?? []) : []).map((o) => ({ id: o.id, reason: o.reason ?? '', detail: o.detail ?? '' })), "
        f"owed_next_action: {_owed_handback_js(sizing_rel)} || null, "
        "blocked: _blockedChunks, held_by: _heldBy, unanswered: _unansweredBriefs, "
        "stopped_by: _stoppedBy, not_started: _notStarted, halted_by: _halted, halt_commit: _haltCommit, "
        "review: { prep: _reviewPrep, wave: _reviewWave, delivery: _deliveryVerdict, "
        "integration: _reviewIntegration, reverify: _reviewReverify }, "
        + (
            "roadmap: _viaRoadmap ? { gate: _rb, ready: _ready, fanout: _commit?.fanout ?? null, "
            "not_fired: _commit?.fanout?.not_fired ?? _unfired } : null, "
            "fanout: _commit?.fanout ?? null, "
            if rb_fn else ""
        )
        + "ledger: _ledger, docs: _docs, commit: _commit, next_action: _nextAction, revise_and_refire: _reviseAndRefire, "
        + (
            "get terminal_commit_cli() { return this.next_action.kind === 'terminal_commit' "
            "? _terminalCommitCli(this.next_action.params) : null; }, };"
            if in_run_commit
            else f"{TERMINAL_COMMIT_CLI_PROPERTY_JS} }};"
        )
    )

    return _finish_script(
        b,
        manifest_rel,
        (
            f"Plan, stage, execute, review from accepted sizing {Path(sizing_rel).stem}."
            if not sized_in_run
            else "One in-session run from an ask to a reviewed result: size, gate, plan, stage, execute, review."
        ),
        _phase_titles(
            carried=_carried_titles(b),
            blitz_phases=blitz_phases,
            review_titles=review_titles,
            extra=[
                *(["xl-exit"] if accept_pending or sized_in_run else []),
                *(["roadmap-blitz", *rb_phases] if rb_fn else []),
                ASK_VERIFY_PHASE,
                *(("commit",) if in_run_commit else ()),
            ],
        ),
    )


def _refire_js(sizing_rel: Optional[str]) -> str:
    """JS expression for the engine-ask submission call, rendered by `warp_fire.fire_command`."""
    if sizing_rel:
        return _lit(fire_command(sizing_rel))
    marker = "\u0000"
    head, tail = fire_command(marker).split(json.dumps(marker))
    return (
        f"(_sizingRel ? {_lit(head)} + JSON.stringify(_sizingRel) + {_lit(tail)} : {_lit(fire_command(None))})"
    )


def _design_next_action_js(sizing_rel: Optional[str]) -> str:
    """JS expression for the design-review halt's next_action, rendered by `design_route.next_action`."""
    if sizing_rel:
        return _lit(_design_next_action(sizing_rel))
    marker = "\u0000"
    parts = _design_next_action(marker).split(marker)
    return "(" + " + (_sizingRel ?? '') + ".join(_lit(p) for p in parts) + ")"


def _owed_handback_js(sizing_rel: Optional[str]) -> str:
    """JS expression: names the EM-owned rows a hands-off run cannot perform and the refire that
    resumes them, or '' when none are owed."""
    return (
        "(((_manifest && !_manifest.error ? _manifest.owed : null) ?? []).length "
        "? 'Owed, not run: ' + _manifest.owed.map((o) => o.id + ': ' + (o.detail ?? '')).join('; ') "
        f"+ '. Then refire: ' + {_refire_js(sizing_rel)} : '')"
    )


NO_MANIFEST_PREFIX = "no folded stage manifest"

def _all_em_next_action_js(sizing_rel: Optional[str]) -> str:
    """JS expression: `{next_action}` for a stage refusal that derived zero dispatchable rows with
    rows owed by the EM (the stage reply's `owed_em_ids`), else `{}`. Names the exact em-discharge
    call per owed id, then the refire."""
    return (
        "((_manifest?.owed_em_ids ?? []).length ? { next_action: "
        "'Every stage performer is the EM; nothing to dispatch. Perform each row, then record it: ' + "
        "_manifest.owed_em_ids"
        ".map((id) => 'coordinator-invoke plan.tasks.mutate {\"verb\":\"em-discharge\",\"plan_path\":' "
        "+ JSON.stringify(_manifest.owed_plan ?? _planRel ?? '') + ',\"id\":' + JSON.stringify(id) + ',\"text\":\"<what you did>\"}').join('; ') "
        f"+ '. Then refire: ' + {_refire_js(sizing_rel)} }} : {_no_manifest_next_action_js(sizing_rel)})"
    )


def _no_manifest_next_action_js(sizing_rel: Optional[str]) -> str:
    """JS expression: `{next_action}` for a halt whose manifest is missing, else `{}`. The refire
    re-runs the gate op, which re-stages the plan from the sizing, so it is the resume route."""
    return (
        f"(String(_manifest?.error ?? '').startsWith({_lit(NO_MANIFEST_PREFIX)}) ? {{ next_action: "
        "'Resume: the stage manifest is rebuilt when the gate re-stages the plan' + (_planRel ? ' ' + _planRel : '') "
        f"+ '. Refire: ' + {_refire_js(sizing_rel)} }} : {{}})"
    )


def composed_fanout(body_lines: Sequence[str]) -> FanoutCost:
    """Cost of the agent legs a script composes at emit: one per `agent(` call site outside the
    execute loop's row dispatch. Rows are counted at stage time (the manifest's `fanout`), so the
    basis is always `lower-bound`. `units` and `agents` here exclude rows, so the script's own meta
    understates the run; the row-inclusive count is the manifest `fanout`."""
    legs = sum(
        len(AGENT_CALL_RE.findall(ln)) for ln in body_lines if "_rows[r.id] = _runRow(" not in ln and "_groups[" not in ln
    )
    return FanoutCost(units=legs, agents=legs, basis="lower-bound")


def _finish_script(b: list[str], manifest_rel: str, description: str, phase_titles: list[str]) -> str:
    """The script text from its composed body lines, refused over the Workflow runner's byte cap."""
    meta = with_fanout(_emit._meta_block("warp-ask", description, phase_titles), composed_fanout(b))
    script = (
        f"{_emit._NODE_CHECK_DOES_NOT_APPLY_COMMENT}\n{meta}\n"
        f"{ASK_MANIFEST_MARKER}{manifest_rel}\n"
        + "\n".join(b)
        + "\n"
    )
    if len(script.encode("utf-8")) > _emit._WORKFLOW_SCRIPT_BYTE_CAP:
        raise AskComposeRefused(
            f"composed ask script is over the Workflow runner's {_emit._WORKFLOW_SCRIPT_BYTE_CAP}-byte cap"
        )
    return script


#: Repo-relative sidecars of the review wave's senior-front-end returns, or 'none'; the ledger
#: check defaults a design row's met_by to one of them.
_SFE_WAVE_SIDECARS_JS = (
    "((() => { const _r = REPO_ROOT.replace(/\\\\/g, '/').replace(/\\/+$/, '') + '/'; "
    "const _p = (Array.isArray(_reviewWave) ? _reviewWave : []).filter((w) => w && typeof w.sidecar_path === 'string' "
    "&& /(?:^|:)senior-front-end$/.test(String(w.agent_type ?? ''))).map((w) => { "
    "const s = w.sidecar_path.replace(/\\\\/g, '/'); "
    "return s.toLowerCase().startsWith(_r.toLowerCase()) ? s.slice(_r.length) : s; }); "
    "return _p.length ? _p.join(', ') : 'none'; })())"
)
#: Appended to the verify prompt: on a ledger resume the executors' work already landed, so the
#: verdict covers exactly the rows the halted run blocked on.
_RESUME_LEDGER_CLAUSE_JS = (
    "(_resumeLedger ? ' RESUME: ' + (_allLandedResume ? 'every row of the plan landed in an earlier run and no "
    "executor ran in this one. ' : 'an earlier run halted at the ledger check; a remaining-work leg then ran "
    "against the rows it kept. ') + "
    f"(_gate.{RESUME_LEDGER_KEY}.blocking.length ? 'Judge only these requirement rows, now, against ' + "
    "(_allLandedResume ? 'the landed work of the plan at HEAD: ' : 'the current tree: ') + "
    f"_gate.{RESUME_LEDGER_KEY}.blocking.join(', ') + '. ' + (_allLandedResume ? 'A blocking row is met when the "
    "landed work satisfies its register text. ' : 'A blocking row is met only on work changed since the halt "
    f"commit ' + (_gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_HALT_SHA_FIELD} ?? '(unknown)') + '; the diff the halt already judged is not_met. ') + '"
    f"Revise notes: ' + JSON.stringify(_gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_NOTES_FIELD} ?? []) + "
    "'. Return an item for each of them and no other item.' "
    ": 'No requirement row is left to judge. Return items: [].') + "
    f"(Object.keys(_gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_EM_DISCHARGED_FIELD} ?? {{}}).length "
    "? ' EM-discharged rows (performed by the EM, recorded via em-discharge): ' + "
    f"Object.entries(_gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_EM_DISCHARGED_FIELD}).map(([k, v]) => k + ': ' + v).join('; ') + '.' : '') "
    ": '')"
)
#: The verify agent writes docs before any ledger or criterion verdict exists, so docs are asked
#: for only on what is settled before it runs.
_DOCS_PRE_TAIL_GUARD = (
    "!_halted && !_resumeLedger && _manifest && !_manifest.error && !_viaRoadmap && _RUN_TOKEN "
    "&& !(_deliveryVerdict && _deliveryVerdict.verdict === 'FAIL')"
)


def _docs_prompt_parts(*, run_id: str, manifest_rel: str) -> tuple[str, ...]:
    """Step 2 of the merged verify prompt, as `_cat` parts: the declared docs and the delivery note.

    It edits only `_manifest.doc_writes`; the delivery note's `## Verdicts` section is
    `dispatch.terminal_commit`'s, so the prompt carries none of the tail's own results.
    """
    note_rel = f"{DELIVERY_NOTE_DIR}/{run_id}.md"
    return (
        "This run's change is on disk, uncommitted. Read the sizing at ",
        "js:_sizingRel",
        "js:(_planRel ? ', the plan at ' + _planRel : '')",
        f", and the run manifest at {manifest_rel}. The review's verdicts, verbatim:\n",
        "js:_cap(JSON.stringify({ delivery: _deliveryVerdict, integration: _reviewIntegration, "
        "wave: _reviewWave }), 12000)",
        "\n\nThe files you may write are exactly this run's declared doc writes: ",
        "js:JSON.stringify(_manifest.doc_writes ?? [])",
        ". Edit no other file: a README or wiki page the change makes stale that is not in that list "
        "goes in stale_docs, unedited. (a) Update every path the sizing's `doc_consumers` lists (each "
        "restates the rule this ask changes; a `:<line>` or `#<anchor>` suffix names where), and amend "
        "or supersede the text at each `ask_conflicts` path so it no longer contradicts the ask -- the "
        "ask outranks it. "
        f"(b) Write the delivery note at {note_rel}: every requirements-ledger item ({RECORD_DIR_ROOT}/"
        f"{run_id}/{LEDGER_VIEW_FILE}) with what the change did for it, what shipped (files "
        "and why), each review finding and how it was resolved, and every decision "
        "or deviation taken in-run with its reason -- read them off the row briefs under ",
        f"{RUN_DIR_ROOT}/{run_id}/briefs",
        ", which are never committed. Never edit a plan, sizing or other process record to carry "
        "this. Write no verdicts section: the ledger checker's, the tests' and the criterion's results "
        "are appended to the note by the commit. Never write under scratch/, never stage or commit. "
        "Return as doc_paths every "
        "repo-relative file you wrote or edited, the note's path as note_path, and stale_docs.",
    )


def _append_docs_and_commit(
    b: list[str], *, head: str, anchor: str, repo_root: str, run_id: str, manifest_rel: str,
    script_path: Optional[str] = None,
) -> None:
    """The run's commit stage, under a live terminal-commit `_nextAction` and no halt: a
    git-commit-agent running `dispatch.terminal_commit` with the run token. The docs were written
    earlier, by the verify agent. The commit lands the product plus the run's
    run_artifacts candidate set; the docs stage's `doc_paths` ride the params block.
    A landed commit turns `_nextAction` to the close-job record the reply carries: the EM has nothing to run
    only when its `fire` list (the sibling slices the close released) is empty.

    Trap: the commit command must stay one bare `coordinator-invoke` segment -- the
    `block_subagent_commit` run-token leg admits nothing else. The params ride in the prompt's
    fenced block, which the leg reads back from the spawn transcript; the agent types only the
    forward-slash file path, never JSON.
    """
    live = "_closeable && _nextAction && _nextAction.kind === 'terminal_commit' && _RUN_TOKEN"
    commit_call = _commit_stage_call(
        head=head, anchor=anchor, repo_root=repo_root, run_id=run_id, script_path=script_path
    )
    b.append(f"  if ({live}) {{")
    b.append("    phase('commit');")
    b.append(f"    try {{ _commit = await {commit_call}; }} catch (e) {{ _haltOnUsageLimit(e); }}")
    b.append(
        "    if (_commit && _commit.committed === true) { _nextAction = { kind: 'close_job', op: null, "
        "params: { run_id: _runId, close: _commit.close ?? null, fire: _commit.close?.siblings?.released ?? [] } }; }"
    )
    b.append(
        "    else { if (!_halted) _halted = { halted: 'commit', refused: _commit?.refused ?? null, "
        "already_landed: _commit?.already_landed ?? null, "
        "error: _commit?.error ?? null, blob_drift: _commit?.blob_drift ?? null, "
        "plan: _planRel ?? null, sizing: _sizingRel ?? null }; "
        "if (_incompleteChunks.length === 0) { _nextAction.params.halted = _halted; _reviseAndRefire = null; } "
        "else { _nextAction = { kind: 'revise_and_refire', op: 'workflow.revise', params: { run_id: _runId, "
        "sizing: _sizingRel, halted: _halted } }; _reviseAndRefire = _mkRevise(); } }"
    )
    b.append("  }")


def roadmap_commit_decls(*, head: str, anchor: str, repo_root: str, run_id: str) -> list[str]:
    """Bindings the standalone roadmap script needs for its commit leg; the run-embedded roadmap
    arm already declares every one of them."""
    if params_path(repo_root, run_id) is None:
        raise AskComposeRefused(f"run id {run_id!r} has no terminal-commit params path")
    return [
        "  let _nextAction = null;",
        "  let _commit = null;",
        _RUN_TOKEN_DECL_JS,
        f"  {PARAMS_BLOCK_JS}",
        _usage_limit_helper_js(),
    ]


def roadmap_commit_lines(
    *, head: str, anchor: str, repo_root: str, run_id: str, script_path: Optional[str] = None
) -> list[str]:
    """The standalone roadmap script's commit leg: the same `commit` agent the run-embedded
    commit stage fires, under a live fan-out `_nextAction`. A refusal halts by name."""
    call = _commit_stage_call(
        head=head, anchor=anchor, repo_root=repo_root, run_id=run_id, script_path=script_path
    )
    return [
        "  if (!_halted && _nextAction && _nextAction.kind === 'terminal_commit' && _RUN_TOKEN) {",
        "    phase('commit');",
        f"    try {{ _commit = await {call}; }} catch (e) {{ _haltOnUsageLimit(e); }}",
        "    if (!_halted && !(_commit && _commit.committed === true)) { _halted = { halted: 'commit', "
        "refused: _commit?.refused ?? null, error: _commit?.error ?? null, "
        "blob_drift: _commit?.blob_drift ?? null, sizing: _sizingRel, run_id: _runId }; }",
        "  }",
    ]


def _commit_stage_call(
    *, head: str, anchor: str, repo_root: str, run_id: str, script_path: Optional[str] = None
) -> str:
    """The `commit` agent call (git-commit-agent, haiku) running `dispatch.terminal_commit`.

    The params block always carries `script_path` (the `_nextAction.params` value wins, else
    `script_path`, else ''): `prompt_params` refuses a block without it, and the run-token hook then
    never writes the params file."""
    params_file = params_path(repo_root, run_id)
    if params_file is None:
        raise AskComposeRefused(f"run id {run_id!r} has no terminal-commit params path")
    commit_prompt = _cat(
        f"{head}\n\n{anchor}\n\n",
        "phase: commit\n\nThis is a run's commit stage (`coordinator:git-commit-agent`, § A run's commit "
        "stage). Run exactly this one command as its own Bash call: no `cd &&`, no wrapper, no env "
        "prefix, no edit to it. `dispatch.terminal_commit` derives the paths from the run's commit "
        "request, so no `paths` array is owed.\n\n",
        f"coordinator-invoke dispatch.terminal_commit --params-file {params_file.as_posix()}",
        "\n\nThe engine reads the params block below. Do not copy, edit or pass it.\n\n",
        "js:_terminalCommitParamsBlock({ ..._nextAction.params, " + _TARGETED_TESTS_JS + " "
        f"script_path: _nextAction.params.script_path ?? {_lit(script_path or '')}, "
        "run_id: _runId, run_token: _RUN_TOKEN, "
        f"doc_paths: {_DOC_PATHS_JS} }})",
        "\n\ndispatch.terminal_commit enumerates every file this run authored and lands it; you run the "
        "one command and return its reply.\n\nReturn from its JSON reply committed, sha, already_landed, error, refused, "
        "sizing_shipped, close, fanout, siblings, run_artifacts and run_artifact_residue, verbatim (null "
        "where absent). Run nothing else.",
    )
    # Stays its own agent: a commit is denied to every agent but git-commit-agent
    # (`block_subagent_commit` run-token leg), so it cannot fold into the docs agent's last act.
    return _agent(
        commit_prompt, label="commit", phase="commit", agent_type=_COMMIT_AGENT_TYPE,
        schema=_COMMIT_SCHEMA, effort=_MECHANICAL_EFFORT, run_id=run_id,
    )


def _halt_record_commit_fn(
    *, head: str, anchor: str, script_path: Optional[str], repo_root: object, run_id: str
) -> str:
    """`async function _commitHaltRecord()`: a git-commit-agent runs `dispatch.terminal_commit` in
    `halted_by` mode, landing the run record dir plus the run_artifacts candidate set. Declared among the script's hoisted
    helpers so the commit stage's own label scan sees one `commit` agent after the tail.

    Trap: the command stays one bare `coordinator-invoke` segment, as in the commit stage, and the
    params ride the same fenced block: the agent types only the file path, never JSON.
    """
    params_file = params_path(repo_root, run_id)
    if params_file is None:
        raise AskComposeRefused(f"run id {run_id!r} has no terminal-commit params path")
    prompt = _cat(
        f"{head}\n\n{anchor}\n\n",
        "phase: commit\n\nThis run halted. Run exactly this one command as its own Bash call: no "
        "`cd &&`, no wrapper, no env prefix, no edit to it.\n\n",
        f"coordinator-invoke dispatch.terminal_commit --params-file {params_file.as_posix()}",
        "\n\nThe engine reads the params block below. Do not copy, edit or pass it.\n\n",
        f"js:_terminalCommitParamsBlock({{ script_path: {_lit(script_path or '')}, run_id: _runId, "
        f"run_token: _RUN_TOKEN, {HALTED_RECORD_PARAM}: _halted, doc_paths: {_DOC_PATHS_JS}, "
        + _TARGETED_TESTS_JS + " "
        "...(_nextAction && _nextAction.kind === 'terminal_commit' && _nextAction.params && _nextAction.params.inline_review "
        "? { run_outcome: { inline_review: _nextAction.params.inline_review, "
        "incomplete_chunks: _nextAction.params.incomplete_chunks ?? [], "
        "landed_chunks: _nextAction.params.landed_chunks ?? [], "
        "ledger_verdict: _nextAction.params.ledger_verdict ?? null, "
        "plan_path: _nextAction.params.plan_path ?? _planRel ?? null } } : {}) })",
        "\n\ndispatch.terminal_commit enumerates every file this run authored and lands it; you run the "
        "one command and return its reply.\n\nReturn from its JSON reply committed, sha, already_landed, error, refused, "
        "run_artifacts and run_artifact_residue, verbatim (null where absent). Run nothing else.",
    )
    call = _agent(
        prompt, label="halt-commit", phase="commit", agent_type=_COMMIT_AGENT_TYPE,
        schema=_COMMIT_SCHEMA, effort=_MECHANICAL_EFFORT, run_id=run_id,
    )
    return f"  async function _commitHaltRecord() {{ return await {call}; }}"


def _append_split_stage(b: list[str]) -> None:
    """The in-run split exit: a `split` halt carries the fanout the xl-exit agent already ran, or stays a halt for the EM.

    With `args.confirm_split === true`, or no proposed slices, the halt keeps kind `split` and
    carries the proposed slices. Otherwise the xl-exit agent's own reply (`_splitFan`) holds the
    `dispatch.ask_fanout` result and `_splitExit` is the script's single exit. A refusal keeps the
    halt: the roadmap arm is never reached from a split.
    """
    b.append(
        f"  if (_halted && _halted.halted === {_lit(HALT_SPLIT)}) {{\n"
        "    const _confirmSplit = typeof args !== 'undefined' && !!args && args.confirm_split === true;\n"
        "    const _proposed = Array.isArray(_splitSlices) ? _splitSlices : [];\n"
        "    _halted = { ..._halted, slices: _proposed, confirm_split: _confirmSplit };\n"
        "    if (!_confirmSplit && _proposed.length) {\n"
        "      const _fan = _splitFan;\n"
        "      if (_fan && !_fan.error && _fan.parent_id && Array.isArray(_fan.runs)) {\n"
        "        _splitExit = { split: { parent_id: _fan.parent_id, runs: _fan.runs }, "
        "fire: _fan.fire ?? [], sizing: _sizingRel, run_id: _runId };\n"
        "        _halted = { ..._halted, split: _splitExit.split, fire: _splitExit.fire };\n"
        "      } else {\n"
        "        _halted = { ..._halted, reason: 'split refused: ' + String((_fan && _fan.error) || 'no reply').slice(0, 600) };\n"
        "      }\n"
        "    }\n"
        "  }"
    )


def _append_halt_record_commit(b: list[str]) -> None:
    """The halt's record-only commit stage, composed only for a run that minted a token
    (`_RUN_TOKEN`) and skipped once a product commit landed."""
    b.append("  if (_halted && _RUN_TOKEN && !(_commit && _commit.committed === true)) {")
    b.append("    phase('commit');")
    b.append("    try { _haltCommit = await _commitHaltRecord(); } catch (e) { _haltOnUsageLimit(e); }")
    b.append("  }")
