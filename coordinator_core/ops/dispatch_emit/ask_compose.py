"""warp --ask script composer: the one .mjs from an ask (or an existing sizing) to a reviewed run.

The script sizes (raw ask only), asks the engine for a gate verdict, halts or runs the routed arm
(XS stage; S plan-author then stage; M+ plan-blitz then stage), executes the staged manifest rows
and composes the review wave once. Every early exit assigns the typed `_halted` and later phases
run under `if (!_halted)`: the terminal return is the script's one exit. Rows, briefs and pathspecs are Python-composed by
dispatch.ask_stage; the script only interprets the manifest. Pure composition: no spawn.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import date
from pathlib import Path
from typing import Callable, Optional, Sequence

from coordinator_core.git.git_state import head_sha
from coordinator_core.git.residue_classes import REPLY_KEY as _RESIDUE_REPLY_KEY, RESIDUE_DIRECTIVE as _RESIDUE_DIRECTIVE
from coordinator_core.warp_fire import SUBMIT_WORKFLOW_NAME, fire_command
from coordinator_core.ops.dispatch_emit import agent_sidecar as _sidecar
from coordinator_core.ops.dispatch_emit import prompt_fragments as _prompt_fragments
from coordinator_core.ops.dispatch_emit.nested_run_contract import STAGE_PENDING_KEY, STAGED_INLINE_SLOT, STAGED_RESUME_KEY
from coordinator_core.ops.dispatch_emit.reply_sidecar import (
    READ_STAGE_SIDECAR_JS,
    STAGE_REPLY_SCHEMA,
    VERIFY_REPLY_JS,
)
from coordinator_core.ops.dispatch_emit import emit as _emit
from coordinator_core.ops.dispatch_emit.row_returns import CONSUMED_RETURNS_PARAM as _CONSUMED_RETURNS_PARAM
from coordinator_core.ops.dispatch_emit.fanout_cost import AGENT_CALL_RE, GROUP_HEADER_TAIL, GROUP_MEMBER_RESULT_JS, FanoutCost, with_fanout
from coordinator_core.ops.dispatch_emit.falsifier_integrity_phase import REVIEWER_AGENT_TYPE
from coordinator_core.ops.dispatch_emit.ask_brief import EM_BRIEF_VAR, EmBrief, brief_decl_js
from coordinator_core.ops.dispatch_emit import ask_gate as _ask_gate  # noqa: F401 -- installs the fleet-mode provider
from coordinator_core.ops.dispatch_emit.sizing_fire import RESUME_LEDGER_HALT_BLOCKING_FIELD
from coordinator_core.ops.dispatch_emit.pre_halt_credit import CREDIT_MATCH_JS, SINCE_HALT_CREDIT_JS
from coordinator_core.ops.dispatch_emit.memo_rows import MEMO_CLOSE_KEY
from coordinator_core.ops._workflow_contract import agent_options_phase_titles
from coordinator_core.ops._anchor_check import GRAMMAR as _ANCHOR_GRAMMAR
from coordinator_core.ops.dispatch_emit.ask_contract import (
    ALL_LANDED_HALT_KIND,
    CLAIM_PARAM,
    GAPS_CLOSED_FIELD,
    HALT_GAP_RULE,
    HALT_DEPENDENTS_UNSTARTED,
    LEG_REFUSAL_HEAD,
    LEG_REFUSE,
    LEG_SKIP,
    RESUME_LEDGER_LEG_FIELD,
    ASK_MANIFEST_MARKER,
    ASK_PHASES,
    ASK_SOURCE_RELAYED,
    ASK_SOURCES,
    ASK_VERIFY_LABEL,
    ASK_VERIFY_PHASE,
    DELIVERY_NOTE_DIR,
    EMPTY_REGISTER_REASON,
    EMPTY_REGISTER_REFUSAL_HEAD,
    EMPTY_REGISTER_REFUSAL_TAIL,
    HALT_PARKED,
    parked_next_action_js,
    HALT_PLAN_FIELD,
    HALTED_RECORD_PARAM,
    PARKED_REPLY_KEY,
    PM_INTENT_FILE,
    RESUME_LEDGER_CREDITED_FIELD,
    RESUME_LEDGER_EM_DISCHARGED_FIELD,
    RESUME_LEDGER_HALT_SHA_FIELD,
    RESUME_LEDGER_KEY,
    RESUME_LEDGER_KIND_FIELD,
    RESUME_LEDGER_NOTES_FIELD,
    RESUME_LEDGER_REVISED_FIELD,
    RESUME_LEDGER_SINCE_HALT_SHAS_FIELD,
    RESUME_LEDGER_UNCLAIMED_FIELD,
    RESUME_LEDGER_UNSERVED_FIELD,
    RESUME_LEDGER_REMAINING_FIELD,
    RESUME_LEDGER_DERIVED_FIELD,
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
from coordinator_core.ops.dispatch_emit.ledger_recovery import (
    LEDGER_FIX_LABEL,
    LEDGER_FIX_PHASE,
    LEDGER_FIX_SCHEMA,
    LEDGER_REVERIFY_LABEL,
    RECOVERY_ATTEMPT_CAP,
    RECOVERY_RECORD_FILE,
    RECOVERY_WITHHELD_PARAM,
    REOPEN_PLAN_JS,
    ledger_fix_prompt,
)
from coordinator_core.ops.dispatch_emit import agent_route as _agent_route
from coordinator_core.ops.dispatch_emit import halt_refire as _halt_refire
from coordinator_core.ops.dispatch_emit import route_dispatch_js as _route_js
from coordinator_core.ops.dispatch_emit import route_preference as _route_pref
from coordinator_core.ops.dispatch_emit.design_route import DESIGN_ROUTE_KEY, STAGE_REFUSAL_PREFIX, next_action as _design_next_action
from coordinator_core.ops.dispatch_emit.ask_fanout_contract import FANOUT_OP, run_parent_copy_rel
from coordinator_core.ops.dispatch_emit.judgment_halt import pull_split_prompt, rule_and_refire_next_action_js
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
from coordinator_core.ops.dispatch_emit.ask_footprint import (
    FOOTPRINT_SCHEMA,
    admit_writes_js,
    footprint_owed,
    footprint_prompt,
)
from coordinator_core.ops.dispatch_emit.terminal_commit_params_channel import PARAMS_BLOCK_JS, params_path
from coordinator_core.ops.dispatch_emit.land_run_contract import OP_NAME as LAND_RUN_OP, RECONCILE_CLAUSE as LAND_RUN_RECONCILE_CLAUSE
from coordinator_core.ops.dispatch_emit.halted_run_route import ROUTE_OP as _HALTED_ROUTE_OP
from coordinator_core.ops.dispatch_emit.ask_stage_receipt import ALREADY_STAGED_KEY, STAGE_IN_FLIGHT_KEY
from coordinator_core.ops.dispatch_emit.wake_digest import (
    CAP_HELPER_JS,
    TERMINAL_COMMIT_CLI_HELPER_JS,
    TERMINAL_COMMIT_CLI_PROPERTY_JS,
    INTEGRATION_NOT_RUN_VAR,
    next_action_parts,
)
from coordinator_core.ops.dispatch_emit.work_label import build_work_label
from coordinator_core.ops.dispatch_emit.write_baseline import DIRTY_BEFORE_RUN_KEY, MEANT_TO_LAND_KEY, NO_CHANGE_REFUSAL
from coordinator_core.ops.review_mint.execute_review import (
    NO_SLICES_HALT,
    PREP_BUSY_HALT,
    PREP_MALFORMED_HALT,
    REVIEW_MISSING_HALT,
    SLICE_ID_UNKNOWN_HALT,
    compose_execute_review,
    WITHHELD_RULE,
    criterion_judge_brief,
    owner_repo_clause_js,
)
from coordinator_core.ops.review_mint.roster import EMIT_ROUTE_PLAN, parse_execute_review
from coordinator_core.ops.review_mint.wave_bookkeeping import review_wave_bookkeeping_stem
from coordinator_core.ops.requirement_register import REGISTER_PIN_DRIFT_KIND, REPIN_OP
from coordinator_core.ops.review_mint.wave_not_run import WAVE_NOT_RUN_VAR
from coordinator_core.ops.review_test_blocker import FAILING_DETAIL_FIELD, TEST_BLOCKER
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
_ROUTE_VERDICT_KEY = _route_pref.ROUTE_VERDICT_KEY
_VERDICT_RETURN = f", {_ROUTE_VERDICT_KEY}"
_OVERRIDE_SLOT ="<why your assembled route differs from route_preference; omit when it matches>"
_ANCHOR_CLAUSE = (
    "Repo root: {root} -- run `cd {root}` as its own standalone Bash call first; every "
    "repo-relative path below resolves against it."
)
#: Marker token a test asserts; other stage modules import the clause, never retype it.
SKILL_WEIGHT_MARKER = "skill-weight-budget"
SKILL_WEIGHT_HEADROOM_CLAUSE = (
    f"Mind the {SKILL_WEIGHT_MARKER}: skill and command prose writes are budgeted by the skill-weight "
    "leg at commit. Write a tripwire pointer, not prose, wherever a skill or command needs a rule. "
    "Never add or demand a test to pin prose (the wording of a skill, command or doc); pin behavior or a pointer's target instead."
)
_REVIEW_RESULT_NAMES = ("_reviewPrep", "_reviewWave", "_deliveryVerdict", "_deliveryNotRun", WAVE_NOT_RUN_VAR, "_reviewIntegration", INTEGRATION_NOT_RUN_VAR, "_reviewReverify")


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

#: Reconcile clause for prompts that run `dispatch.ask_stage`; its re-run is idempotent through the
#: stage receipt, whose `already_staged` reply is the landed manifest; a `stage_in_flight` reply
#: means the first call still runs, so the wait-and-re-run is bounded by `ASK_STAGE_IN_FLIGHT_ROUNDS`.
ASK_STAGE_IN_FLIGHT_ROUNDS = 3
ASK_STAGE_RECONCILE_CLAUSE = (
    f"If the {OP_ASK_STAGE} command replies with error code -32004 and its error.data carries "
    "reconcile_command, run reconcile_command once: a status whose state is finished with outcome "
    "result is the stage's reply. In every other -32004 case, run the same "
    f"{OP_ASK_STAGE} command exactly once more and return its reply verbatim; a reply carrying "
    f"{ALREADY_STAGED_KEY} is the landed manifest. A reply carrying {STAGE_IN_FLIGHT_KEY} means the "
    "first call still runs: run the -32004's reconcile_command once with its wait_secs, then re-run "
    f"the stage command once more, at most {ASK_STAGE_IN_FLIGHT_ROUNDS} times, then return the last "
    "reply verbatim. A -32004 is never itself the stage's failure."
)


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
        if LAND_RUN_OP in prompt_expr:
            reconcile = LAND_RUN_RECONCILE_CLAUSE
        elif OP_ASK_STAGE in prompt_expr:
            reconcile = ASK_STAGE_RECONCILE_CLAUSE
        else:
            reconcile = _emit._INDETERMINATE_RECONCILE_CLAUSE
        if reconcile not in prompt_expr:
            clause = _lit(" " + reconcile) + " + " + clause
        if _emit._INVOKE_LITERAL_CLAUSE not in prompt_expr:
            clause = _lit(_emit._INVOKE_LITERAL_CLAUSE) + " + " + clause
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
#: apply-rulings and pull-split: light judgment (edit a plan in place per a recorded ruling, slice rows by
#: disjoint writes); haiku loses the census/corrects/declared_absent semantics the edit must preserve.
_RULINGS_AGENT_TYPE = _MECHANICAL_AGENT_TYPE
_RULINGS_MODEL = "sonnet"
_PLAN_AUTHOR_EFFORT = "low"
_XL_EXIT_EFFORT = "low"
#: Models of DoE-defined types (commit, docs executor, sizing-scout) come from `emit._AGENT_MODELS`.
_COMMIT_AGENT_TYPE = _emit._COMMIT_AGENT_TYPE
_PLAN_REVIEW_AGENT_TYPE = "coordinator:staff-eng"
_PLAN_REVIEW_EFFORT = "low"
_ANCHOR_BRIEF_VERSION = "ask-compose-anchor/1"
#: Inserted ahead of the readiness-op sentence in the review and revise prompts.
_ANCHOR_PREMISES_SENTENCE = (
    "Declare `anchor_premises:` in the plan's frontmatter: each premise the route rests on "
    "(where data lives, which caller runs, what a path resolves to) as {id, claim, check: "
    "{command, expect}} with the command in census form, or [] when it rests on none. Run each "
    "check, then record its verdict through the `plan.premise_record` op with `anchor: <id>` and "
    "`line: <1-based plan line of that anchor's command:>` on the row (brief_version "
    f"`{_ANCHOR_BRIEF_VERSION}`). "
    f"The gate evaluates a check in-process only as {_ANCHOR_GRAMMAR}: grep -c stages joined by "
    "&&, one file each, no pipes, redirects or other shell operators; any other check is pulled "
    "as anchor-premise-no-verdict. "
)
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
        "pending_rulings": {"type": ["array", "null"], "items": _STR},
        "resume_ready": {"type": ["boolean", "null"]},
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
#: The readiness reply's judgment fields, copied through a revise leg so a pull halt can name its rulings.
_PULL_JUDGMENT_PROPS = {
    "rulings": {"type": "array", "items": {"type": "object"}},
    "blocking_findings": {"type": "number"},
    "split_recommended": {"type": "boolean"},
}
_REVISE_SCHEMA = _obj(["plan_rel"], {"plan_rel": _STR, "pull_class": _STR, "verdict": _STR, **_PULL_JUDGMENT_PROPS})
#: The pull split-proposal agent's reply: the xl-exit split shape `_append_split_stage` consumes.
_PULL_SPLIT_SCHEMA = _obj(
    ["pick", "slices"],
    {
        "pick": {"type": "string", "enum": ["split"]},
        "slices": {"type": "array", "items": {"type": "object"}},
        "parent_id": _STR,
        "runs": {"type": "array", "items": {"type": "object"}},
        "fire": {"type": "array", "items": {"type": "object"}},
        "error": _STR,
    },
)
#: The resume-with-rulings leg's reply: the ruling ids it applied to the plan.
_RULED_SCHEMA = _obj(["plan_rel", "applied"], {"plan_rel": _STR, "applied": {"type": "array", "items": _STR}})
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
        _RESIDUE_REPLY_KEY: {"type": ["array", "null"], "items": {"type": "string"}},
    },
)
_DOC_PATHS_JS = (
    "(typeof _docs !== 'undefined' && _docs && Array.isArray(_docs.doc_paths)) ? _docs.doc_paths : []"
)
# The scoped-test verdict as a params key; null before the test leg has run (a pre-test halt).
_TARGETED_TESTS_JS = (
    "targeted_tests: (_testResult && typeof _testResult.status === 'string') ? { status: _testResult.status, "
    "tests_run: _testResult.tests_run ?? null, tests_failed: _testResult.tests_failed ?? null, baseline: _testResult.baseline ?? null } : null,"
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
                    "owed_by": {"type": "array", "items": _STR},
                    "root_read": {"type": ["string", "array"], "items": _STR},
                    "wired": {"type": "boolean"},
                    "superseded_by": _STR,
                    "post_commit_acts": {"type": "array", "items": {"type": "string", "enum": list(POST_COMMIT_ACTS)}},
                    "remaining": {"type": "array", "items": _STR},
                    "evidence": _STR,
                    "met_by": {"type": ["string", "array"], "items": _STR},
                    GAPS_CLOSED_FIELD: {"type": "array", "items": _obj(["gap", "how"], {"gap": _STR, "how": _STR})},
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
    "commit -- the commit itself, reporting the landed sha, a memo naming it, the run's baton archived "
    "(baton_archive), or the plan stamped implemented (plan_implemented); the terminal commit writes both "
    "lifecycle acts -- named in "
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
    "that chunk id exactly; set withheld_by on no other item. An item whose only undelivered parts are "
    "owed rows (the stage manifest's owed ids, named beside the gated chunks) is 'not_met' with owed_by set to exactly those row ids; an item with any other gap "
    "carries no owed_by. The commit stage runs after this check, so an uncommitted "
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
_OWED_IDS_JS = "(((_manifest?.owed ?? []).map((o) => o.id).join(', ')) || 'none')"
_WITHHELD_SET_JS = "[...(_manifest?.gated ?? []), ...(_manifest?.slot_withheld ?? [])]"
_GATED_LIST_JS = _WITHHELD_SET_JS + ".map((g) => g.id)"
_WITHHELD_ROWS_JS = (
    "(" + _WITHHELD_SET_JS + ".length ? '\\nWithheld rows: ' + " + _WITHHELD_SET_JS + ".map((g) => g.id + ' (' + (g.title || 'untitled') + ')' "
    "+ ((g.register_rows ?? []).length ? ' [register ' + g.register_rows.join(', ') + ']' : '')).join('; ') + '. ' + "
    + json.dumps(WITHHELD_RULE)
    + " : '')"
)
#: A halted run's recorded next_action: params carry only keys `workflow.revise` reads, and `note`
#: satisfies its non-empty precondition (the halt reason becomes the run's note).
REVISE_NEXT_ACTION_JS = (
    "{ kind: 'revise_and_refire', op: 'workflow.revise', params: { run_id: _runId, "
    "note: 'halted: ' + String(_halted?.halted ?? 'unknown') } }"
)
#: An XS-MINT-MISROUTED halt repeats under any unchanged refire, so its next_action names the
#: plan_path stage instead of `workflow.revise`. No engine op consumes this kind yet (op is null): the
#: reading EM stages `plan_path` itself.
XS_MISROUTED_CODE = "XS-MINT-MISROUTED"
XS_MISROUTED_NEXT_ACTION_JS = (
    "{ kind: 'stage_by_plan_path', op: null, params: { run_id: _runId, sizing: _sizingRel, plan_path: _planRel, "
    "note: 'halted: ' + String(_halted?.halted ?? 'unknown') } }"
)
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
        "wave_test_targets": {"type": "object", "additionalProperties": {"type": "array", "items": _STR}},
        "no_test_target": {"type": ["object", "null"]},
        "doc_writes": {"type": "array", "items": _STR},
        "marker_path": _STR,
        "rulable_ids": {"type": "array", "items": _STR},
        "ledger_item_ids": {"type": "array", "items": _STR},
        "withheld_ids": {"type": "array", "items": _STR},
        "slot_withheld": {"type": "array", "items": _obj(["id"], {"id": _STR, "title": _STR, "register_rows": {"type": "array", "items": _STR}})},
        "gated": {"type": "array", "items": _obj(["id"], {"id": _STR, "reason": _STR, "gate": _STR, "owner_repo": _STR, "owner_root": _STR, "closure_key": {}, "title": _STR, "register_rows": {"type": "array", "items": _STR}})},
        "owed": {"type": "array", "items": _obj(["id"], {"id": _STR, "reason": _STR, "detail": _STR})},
        "engine_rows": {"type": "array", "items": _obj(["id"], {"id": _STR, "owner_repo": _STR, "owner_root": _STR, "root_class": _STR, "engine": {"type": "boolean"}})},
    },
)

#: A stage act's reply is a handle or a refusal; `_adoptStageReply` reads the sidecar through its reader and verifies it
#: against the handle before the manifest is adopted. `_STAGE_REPLY_OR_NULL` is for a conditional act.
_STAGE_REPLY_OR_NULL = {**STAGE_REPLY_SCHEMA, "type": ["object", "null"]}
_STAGE_REPLY_PROP = {"stage_reply": _STAGE_REPLY_OR_NULL}

_PLAN_STAGED_SCHEMA = _obj(
    ["plan_rel", "stage_reply"], {"plan_rel": _STR, "stage_reply": STAGE_REPLY_SCHEMA, **_FALSIFIER_PROP}
)
_REVISE_STAGED_SCHEMA = _obj(
    ["plan_rel"],
    {"plan_rel": _STR, "pull_class": _STR, "verdict": _STR, **_PULL_JUDGMENT_PROPS, **_STAGE_REPLY_PROP},
)
_SPINE_REVIEW_STAGED_SCHEMA = _obj(
    ["plan_rel"],
    {
        "plan_rel": _STR, "pull_class": _STR, "verdict": _STR, **_STAGE_REPLY_PROP,
        **_REVIEW_SIGNALS_PROP, **_FALSIFIER_PROP,
    },
)
#: The ledger-resume re-plan agent's return: the rows it added, and the stage it runs over exactly those.
_REPLAN_STAGED_SCHEMA = _obj(
    ["plan_rel", "added_rows"],
    {"plan_rel": _STR, "added_rows": {"type": "array", "items": _STR}, **_STAGE_REPLY_PROP},
)
#: The pre-sized gate agent's return: the gate reply plus the stage it runs as its closing act.
_GATE_STAGED_SCHEMA = {
    **_GATE_SCHEMA,
    "properties": {**_GATE_SCHEMA["properties"], **_STAGE_REPLY_PROP},
}
#: The size-reword agent's return: the reworded criterion, its own sizing-assemble result, and the
#: gate reply (plus stage reply) it runs as closing acts.
_REWORD_SCHEMA = _obj(
    ["exit_criterion"],
    {
        **_GATE_STAGED_SCHEMA["properties"],
        **ASSEMBLE_SCHEMA["properties"],
        _route_pref.ROUTE_VERDICT_KEY: {"type": ["object", "null"]},
        "exit_criterion": _STR,
    },
)
#: The ask scout's return with its relayed stage reply held to the stage-reply schema.
_ASK_SCOUT_STAGED_SCHEMA = {
    **ASK_SCOUT_SCHEMA,
    "properties": {
        **{k: v for k, v in ASK_SCOUT_SCHEMA["properties"].items() if k != "manifest"},
        **_STAGE_REPLY_PROP,
    },
}
#: The blitz-stage-read leg's return: the stage op's reply, run only when readyOp never ran.
_BLITZ_STAGE_READ_SCHEMA = _obj([], dict(_STAGE_REPLY_PROP))


_LEDGER_PREFIX_VAR = "_LEDGER_PREFIX"
#: JS expression: the gate's design brief ahead of an agent's task text; '' when the gate carried none.
_DESIGN_PREFIX_JS = f"(_gate?.{DESIGN_BRIEF_KEY} ? String(_gate.{DESIGN_BRIEF_KEY}) + '\\n\\n' : '')"

#: An engine-owned fold site returns the stage reply under `STAGE_REPLY_SCHEMA`; `_adoptStageReply`
#: (see `_stage_reply_js`) admits a manifest only when its sidecar matches the handle and
#: `_wholeManifestGap` finds no gap. A refused stage lands in `_stageRefusal`; a sidecar that does not
#: match its handle lands in `_diskHalt`; a not-whole manifest lands in `_relayGap` (halts by name).
_ADMIT_MANIFEST_JS = (
    "  let _stageRefusal = null;\n"
    "  let _stageOwedEm = null;\n"
    "  let _relayGap = null;\n"
    "  let _parked = null;\n"
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
    "  // Any non-empty readyOpError is the stage op's own refusal, except plan-blitz's 'did not run'\n"
    "  // sentinel (the op never ran; the one stage leg decides). The design-review prefix is matched\n"
    "  // later, on the adopted text.\n"
    "  const _adoptReadyError = (e) => {\n"
    "    const _re = String(e.readyOpError ?? '').trim();\n"
    "    if (!_stagedManifest && !_stageRefusal && _re !== '' && !/did not run/.test(_re)) _stageRefusal = _re;\n"
    "  };"
)
_STAGE_SIDECAR_HALT = "stage reply sidecar does not match its handle: "
_ADOPT_STAGE_REPLY_HEAD_JS = (
    "  let _stagePending = null;\n"
    "  let _noFoldCause = null;\n"
    # A soft (plan-blitz readyOp) reply's `error` is the pass-through's prose: `_adoptReadyError` owns
    # the refusal there. The manifest is only ever the sidecar text that matched its handle.
    "  const _adoptStageReply = async (r, soft) => {\n"
    "    if (r === null || r === undefined || typeof r !== 'object' || Array.isArray(r)) { if (!_noFoldCause) _noFoldCause = 'stage_reply absent from the agent return'; return; }\n"
    "    if (r.error && r.manifest_handle) { if (!soft && !_relayGap) _relayGap = "
    "'stage reply carries both error and manifest_handle'; return; }\n"
    "    if (r.error) { if (!soft && !_stageRefusal) { _stageRefusal = String(r.error); "
    "if (Array.isArray(r.owed_em_ids) && r.owed_em_ids.length) _stageOwedEm = { ids: r.owed_em_ids.map(String), "
    "plan: typeof r.owed_plan === 'string' ? r.owed_plan : null }; } return; }\n"
    f"    if (r.{PARKED_REPLY_KEY} && !r.manifest_handle) {{ if (!_parked) _parked = String(r.{PARKED_REPLY_KEY}); return; }}\n"
    "    if (!r.manifest_handle) { if (!soft && !_relayGap) _relayGap = 'manifest_handle'; return; }\n"
    "    let got;\n"
)
_ADOPT_STAGE_REPLY_READER_JS = "    got = await _readStageSidecar(r.manifest_handle, REPO_ROOT);\n"
# Trap: a launcher-child script holds no reader branch at all, so `workflow(` is absent from its bytes.
_ADOPT_STAGE_REPLY_LAUNCHER_JS = (
    "    got = _adoptStagedInline(r.manifest_handle);\n"
    "    if (got === null) { if (!_stagePending) _stagePending = { handle: r.manifest_handle }; return; }\n"
)
# Trap: depends on _verifyReplyText, and the slot line must precede it; the text still passes the verify before JSON.parse.
_ADOPT_STAGED_INLINE_JS = """function _adoptStagedInline(h) {
  const s = _STAGED_INLINE;
  if (!s || typeof s !== 'object' || !s.handle || typeof s.handle !== 'object' || h === null || typeof h !== 'object') return null;
  if (s.handle.path !== h.path || s.handle.sha256 !== h.sha256 || s.handle.bytes !== h.bytes) return null;
  const bad = _verifyReplyText(s.text, h);
  if (bad !== null) return { fail: bad };
  try {
    return { manifest: JSON.parse(s.text) };
  } catch (e) {
    return { fail: 'reply text is not valid JSON' };
  }
}
"""
# The inline's resume block says the launcher already sized, planned and staged: the twin skips every pre-stage leg.
# Trap: the guard keys on the block alone, never on arm; the manifest's verify is `_adoptStageReply`'s, so a failed
# verify halts with `_STAGE_SIDECAR_HALT` and the legs stay skipped.
_STAGED_RESUME_JS = (
    "  const _STAGED_RESUME = (() => { const s = _STAGED_INLINE; "
    "if (!s || typeof s !== 'object' || Array.isArray(s)) return null; "
    f"const r = s.{STAGED_RESUME_KEY}; "
    "return (r && typeof r === 'object' && typeof r.arm === 'string') ? r : null; })();\n"
)
_ADOPT_STAGE_REPLY_TAIL_JS = (
    f"    if (got.fail) {{ if (!_diskHalt) _diskHalt = {_js_string_literal(_STAGE_SIDECAR_HALT)} + got.fail; return; }}\n"
    "    const gap = _wholeManifestGap(got.manifest);\n"
    "    if (gap) { if (!_relayGap) _relayGap = gap; return; }\n"
    "    _stagedManifest = got.manifest;\n"
    "  };"
)
_ADOPT_STAGE_REPLY_JS = _ADOPT_STAGE_REPLY_HEAD_JS + _ADOPT_STAGE_REPLY_READER_JS + _ADOPT_STAGE_REPLY_TAIL_JS
_ADOPT_STAGE_REPLY_LAUNCHER_CHILD_JS = _ADOPT_STAGE_REPLY_HEAD_JS + _ADOPT_STAGE_REPLY_LAUNCHER_JS + _ADOPT_STAGE_REPLY_TAIL_JS


def _stage_reply_js(launcher_child: bool = False) -> str:
    """The script block that turns a stage reply into `_stagedManifest`: the runtime reads the sidecar's reader
    with no agent, and the text is verified against the handle before `JSON.parse`. `launcher_child` renders the
    reader-free form: the staged twin carries the verified text in the `STAGED_INLINE_SLOT` line, and a resume block
    there (`STAGED_RESUME_KEY`) is read into `_STAGED_RESUME`."""
    if launcher_child:
        return (
            VERIFY_REPLY_JS + STAGED_INLINE_SLOT + "\n" + _ADOPT_STAGED_INLINE_JS + _STAGED_RESUME_JS
            + _ADOPT_STAGE_REPLY_LAUNCHER_CHILD_JS
        )
    return VERIFY_REPLY_JS + READ_STAGE_SIDECAR_JS + _ADOPT_STAGE_REPLY_JS


#: The run reads its commit token from the Workflow `args` the emit's fire args carry, so the
#: script's bytes never hold it (any agent or peer can read the script).
_RUN_TOKEN_DECL_JS = (
    "  const _RUN_TOKEN = (() => { try { const a = (typeof args === 'string') ? JSON.parse(args) : "
    "(typeof args === 'undefined' ? null : args); return (a && typeof a.runToken === 'string' "
    "&& /^[0-9a-f]{32}$/.test(a.runToken)) ? a.runToken : null; } catch (_e) { return null; } })();"
)

# Trap: reads `_STAGED_INLINE`, so the slot line must precede it; the first emitted script's slot is null, so it holds no token.
_RUN_TOKEN_DECL_LAUNCHER_CHILD_JS = (
    "  const _RUN_TOKEN = (() => { try { const a = (typeof args === 'string') ? JSON.parse(args) : "
    "(typeof args === 'undefined' ? null : args); "
    "const t = (a && typeof a.runToken === 'string') ? a.runToken "
    ": ((typeof _STAGED_INLINE === 'object' && _STAGED_INLINE && typeof _STAGED_INLINE.runToken === 'string') "
    "? _STAGED_INLINE.runToken : null); "
    "return (typeof t === 'string' && /^[0-9a-f]{32}$/.test(t)) ? t : null; } catch (_e) { return null; } })();"
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


def _test_instruction_js(review_edits_base: str, repo_root: Optional[str] = None) -> str:
    """JS expression for the verify agent's scoped-test instructions, scoped at run time to the manifest's test targets.

    The plan is authored inside the run, so the scope cannot resolve at compose time; the stage
    resolves `test_targets` through `pathspec.terminal_test_scope`. Trap: the runner is told to
    report raw evidence and never gate, and the targets are scoped, never a tier. The measured-rev
    step precedes the targets so a HEAD-measured test reads the candidate commit; the env is set
    in the test process environment, never as a shell prefix. Trap: the env is prompt-enforced
    only; `measured_rev_drift` at the landing gates backstops file changes, not a runner that ignores the sha.
    """
    from coordinator_core.ops.dispatch_emit.ask_contract import MEASURED_REV_ENV, MEASURED_REV_OP

    return _cat(
        f"First run `coordinator-invoke {MEASURED_REV_OP}` with run_id ",
        "js:_runId",
        " and paths [",
        "js:(_manifest.review_declared_paths ?? []).join(', ')",
        f"]; it returns a candidate commit sha. Run every scoped test target with {MEASURED_REV_ENV} "
        "set to that sha in the test process environment. If the op refuses, run the tests without it "
        "and report the refusal as raw evidence. ",
        "Run the scoped test targets: [",
        "js:(_manifest.test_targets ?? []).join(', ')",
        "]. "
        + _emit._review_edits_clause(review_edits_base)
        + " "
        + _emit._test_runner_clause(repo_root)
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
        "    const _rv = (typeof _reviewReverify !== 'undefined' && _reviewReverify && typeof _reviewReverify === 'object') ? _reviewReverify : null;\n"
        "    const _rvExcluded = new Set([...(_rv?.pre_existing ?? []), ...(_rv?.kept ?? [])].map(String));\n"
        "    const _rvResolved = _rv?.status_after === 'pass';\n"
        "    const _rvDetail = new Map();\n"
        f"    const _rvRaw = _rv?.{FAILING_DETAIL_FIELD};\n"
        "    if (Array.isArray(_rvRaw)) { for (const d of _rvRaw) { if (d && d.node_id) _rvDetail.set(String(d.node_id), d.message == null ? '' : String(d.message)); } }\n"
        "    const _failingTests = [...new Set((Array.isArray(_rv?.failing) ? _rv.failing : (typeof _rv?.failing === 'string' ? [_rv.failing] : [])).map(String))]"
        ".filter((n) => !_rvResolved && !_rvExcluded.has(n)).sort().map((n) => ({ node_id: n, message: _rvDetail.get(n) ?? '' }));\n"
        "    const _gapLines = _failingTests.map((t) => t.node_id + ': ' + (t.message.trim().split('\\n')[0] ?? '').trim().slice(0, 300));\n"
        "    const _slicesJs = _blockedSlices.map((w) => ({ slice_id: w.slice_id ?? null, findings: w.findings, "
        "applied: w.applied ?? 0, sidecar_path: w.sidecar_path ?? null }));\n"
        "    if (_failingTests.length) {\n"
        "      const _claimed = (typeof _manifest !== 'undefined' && _manifest?.ledger_item_ids?.length) ? _manifest.ledger_item_ids : [];\n"
        "      _halted = { halted: " + json.dumps(HALT_REVIEW_BLOCKED) + ", blocker: " + json.dumps(TEST_BLOCKER) + ", run_id: _runId, "
        "failing: _failingTests, gaps: _gapLines, "
        + HALT_PLAN_FIELD + ": (typeof _planRel !== 'undefined' ? _planRel : null), "
        "items: _claimed.map((id) => ({ id, remaining: _gapLines, evidence: _gapLines.join('; ') })), "
        "slices: _slicesJs, "
        "next_action: 'The refire runs an executor fix leg against these failing tests: ' + _gapLines.join('; ') + '; ' + "
        + _refire_js(sizing_rel) + " };\n"
        "    } else if (_blockedSlices.length) _halted = { halted: " + json.dumps(HALT_REVIEW_BLOCKED) + ", run_id: _runId, "
        "unapplied_findings: _blockedSlices.reduce((n, w) => n + (w.findings - (w.applied ?? 0)), 0), "
        "slices: _slicesJs, "
        "next_action: 'The refire re-runs the review wave over the blocked slices: ' + "
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


_WAVE_TESTS_GATE_FN = "_waveTestsGreen"


def _wave_tests_green_js(
    agent_type_host: Optional[str], *, repo_root: Optional[str], run_id: str, em_brief: bool = False
) -> str:
    """`_waveTestsGreen(n, ids)`: the checkpoint's pre-commit gate over wave n's cumulative declared tests.

    An empty scope passes. Otherwise one test-runner leg runs the scope, and only the returned
    `status` gates (`pass` / `pass-with-skips` land); a throw, a missing sidecar or any other
    status records `_checkpointWithheld` and withholds. Trap: the runner reports, the script gates.
    `_checkpointWithheld` is read only by the run log line and the node regression test; no result
    field carries it. With `em_brief` the runner prompt opens with `_EM_BRIEF`, like the row verify leg.
    """
    brief = f"{EM_BRIEF_VAR} + " if em_brief else ""
    agent_type = _js_string_literal(_emit._degrade_agent_type(_emit._TEST_AGENT_TYPE, agent_type_host))
    sidecar = _sidecar.sidecar_clause_js("ask", run_id, "'verify:wave-' + n")
    phase = _js_string_literal(_emit._EXECUTE_PHASE_TITLE)
    return (
        "  const _checkpointWithheld = [];\n"
        f"  async function {_WAVE_TESTS_GATE_FN}(n, ids) {{\n"
        "    const scope = ((_manifest && _manifest.wave_test_targets) ?? {})[String(n)] ?? [];\n"
        "    if (!scope.length) return true;\n"
        "    let r = null;\n"
        "    try {\n"
        "      r = await agent(\n"
        f"        {brief}'Run and report on the scoped test target(s) for the wave ' + n + ' checkpoint: ' + "
        "scope.join(', ') + '. ' + "
        f"{_js_string_literal(_emit._test_runner_clause(repo_root))} + {sidecar},\n"
        f"        {{ label: 'verify:wave-' + n, phase: {phase}, agentType: {agent_type}, "
        f"{_emit._model_opt(_emit._TEST_AGENT_TYPE)}, schema: _ROW_VERIFY_SCHEMA }}\n"
        "      );\n"
        "    } catch (e) {\n"
        "      if (_USAGE_LIMIT_RE.test(String((e && e.message) ?? e))) _haltOnUsageLimit(e);\n"
        "      r = null;\n"
        "    }\n"
        "    const status = (r && r.status) || null;\n"
        "    if ((status === 'pass' || status === 'pass-with-skips') "
        "&& typeof r.sidecar_path === 'string' && r.sidecar_path.trim()) return true;\n"
        "    _checkpointWithheld.push({ wave: n, ids, status });\n"
        "    log('checkpoint of wave ' + n + ' withheld: scoped tests ' + (status || 'returned no verdict'));\n"
        "    return false;\n"
        "  }"
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
        f"next_action: {_lit(_USAGE_LIMIT_RESUME_CALL)} }};\n"
        "  }"
    )


_USAGE_LIMIT_RESUME_CALL = "After the limit resets, call Workflow with this scriptPath and resumeFromRunId set to this run id."


def _refire_next_action_helper_js(sizing_rel: Optional[str]) -> str:
    """`_refireNextAction()`: finalises `_halted.next_action` into the one-refire envelope before `_halted` leaves the script."""
    return _halt_refire.refire_next_action_js(
        call_js=f"(_hk === {_lit(HALT_USAGE_LIMIT)} ? {_lit(_USAGE_LIMIT_RESUME_CALL)} : {_refire_js(sizing_rel)})",
        legacy_js="_halted.next_action",
        kind_js="_halted.halted",
        detail_js="(typeof _legacy.detail === 'string' ? _legacy.detail : null)",
    )


def _halt_next_action_text_helper_js(sizing_rel: Optional[str]) -> str:
    """`_haltNextActionText()`: the halt's next step as text, '' when the run did not halt.

    Call after `_refireNextAction()`: it renders the one-refire envelope, not the legacy shape.
    """
    return (
        "  function _haltNextActionText() {\n"
        "    if (!_halted || typeof _halted !== 'object') return '';\n"
        "    const _na = _halted.next_action;\n"
        "    if (typeof _na === 'string') return _na;\n"
        "    if (_na && typeof _na === 'object') {\n"
        "      const _rev = _na.input && _na.input.revise && typeof _na.input.revise.call === 'string' ? _na.input.revise.call : null;\n"
        "      const _re = typeof _na.call === 'string' ? _na.call : (_na.input && typeof _na.input.refire === 'string' ? _na.input.refire : null);\n"
        "      return 'kind: ' + String(_na.kind ?? _halted.halted) + '; ' + (_rev ? _rev + '; ' : '') + (_re ? 'refire: ' + _re : '');\n"
        "    }\n"
        f"    if (_halted.halted === {_lit(HALT_REFUSAL)}) return 'Refire: ' + {_refire_js(sizing_rel)};\n"
        "    return '';\n"
        "  }"
    )


_PLAN_SLOT = "PLAN_PATH_SLOT_X"
_RUNTIME_PLAN_PATH_JS = "(_planRel ?? _sizingRel)"
_EVIDENCE_SLOT = "EVIDENCE_PATH_SLOT_X"
#: `<plan-stem>.evidence.yaml` beside the run's plan, which is unknown until run time.
_RUNTIME_EVIDENCE_PATH_JS = "((_planRel ?? _sizingRel).replace(/\\.[^./]+$/, '') + '.evidence.yaml')"
#: One source for the falsifier, judge and verify legs. Trap: a route-authored test shares the
#: route's own assumptions (a store root it threads in, a cwd it sits in), so it can pass while the
#: production path the ask names still fails.
_PRODUCTION_PATH_CLAUSE = (
    " When the ask asks to prove a behaviour holds, or names a production entry point or caller, "
    "drive that production entry point end-to-end, from a foreign cwd (not the repo root) where cwd "
    "matters. Verification that exercises only route-authored test fixtures for such an ask does not "
    "prove the criterion: report it as not proving the criterion, never as met."
)
_JUDGE_PATH_CLAUSE = (
    "\n\nThe plan_path below is the plan, or on the XS route (no plan is authored) the sizing: "
    "then its exit_criterion is the spec and there is no Verification section." + _PRODUCTION_PATH_CLAUSE
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
    + _PRODUCTION_PATH_CLAUSE
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
    "you edit, stage and commit nothing, and you never change a file to make a requirement true."
    + _PRODUCTION_PATH_CLAUSE
    + "\n\n"
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


def _prompt_parts(text: str) -> list[str]:
    """Split a `${js}`-templated prompt (`ledger_recovery.ledger_fix_prompt`) into `_cat` parts."""
    parts = re.split(r"\$\{([^}]+)\}", text)
    return [("js:" + p) if n % 2 else p for n, p in enumerate(parts) if p]


_LEDGER_RECOVER_JS = r"""
    const _recoveryCap = __CAP__;
    const _fpPosix = (p) => { let s = String(p).replace(/\\/g, '/'); while (s.startsWith('./')) s = s.slice(2); return s; };
    const _fpOwns = (r, p) => (r.writes ?? []).map(_fpPosix).includes(p) || (r.writes_under ?? []).some((d) => p.startsWith(_fpPosix(d).replace(/\/+$/, '') + '/'));
    const _fpOf = (row, failed) => {
      const out = [];
      const own = failed.map((t) => _fpPosix(String(t).split('::')[0])).filter((f) => _fpOwns(row, f));
      for (const p of [...(row.writes ?? []).map(_fpPosix), ...own]) if (!out.includes(p)) out.push(p);
      return out;
    };
    const _ledgerRecover = async () => {
      if (_gate.arm === __XS__) return;
      const blocking = _ledger ? _ledgerBlocking(_ledger) : [];
      if (!blocking.length) return;
      const rulable = _manifest?.rulable_ids ?? _gate.__RL__?.rulable_ids ?? [];
      if (blocking.some((i) => rulable.includes(i.id))) return;
      if (blocking.every((i) => Array.isArray(i.owed_by) && i.owed_by.length > 0)) return;
      const mrows = (_manifest.rows ?? []).filter((r) => r && typeof r === 'object');
      const rowOf = (cid) => mrows.find((r) => String(r.id) === cid) ?? {};
      const tr = __TEST__;
      const failed = [...(tr?.baseline?.caused ?? []), ...(tr?.baseline?.foreign ?? []), ...(tr?.baseline?.pre_existing ?? [])].map(String);
      const heldIds = [...(_manifest.gated ?? []), ...(_manifest.slot_withheld ?? [])].map((g) => String(g.id)).concat((_manifest.held_ids ?? []).map(String));
      const plan = _reopenPlan(blocking, mrows, failed, heldIds);
      const rec = {};
      for (const [cid, ids] of Object.entries(plan.held)) {
        rec[cid] = { rows: ids, attempts: 0, status: 'held', footprint: [], paths: [], last_reason: 'withheld at stage; never ran' };
      }
      for (const [cid, ids] of Object.entries(plan.reopen)) {
        rec[cid] = { rows: ids, attempts: 0, status: 'open', footprint: _fpOf(rowOf(cid), failed), paths: [], last_reason: '' };
      }
      if (!Object.keys(rec).length) return;
      const _fixOne = async (_cid) => {
        const st = rec[_cid];
        const _fixRow = rowOf(_cid);
        const _items = (_ledger.items ?? []).filter((i) => i && st.rows.includes(i.id));
        const remText = JSON.stringify(_items.map((i) => ({ id: i.id, remaining: i.remaining ?? [] })));
        const evText = JSON.stringify(_items.map((i) => ({ id: i.id, evidence: String(i.evidence ?? '').slice(0, 600) })));
        const failTxt = JSON.stringify({ failing: failed, summary: String(tr?.summary ?? ''), report: tr?.sidecar_path ?? null });
        const fpText = JSON.stringify(st.footprint);
        try { return await __FIXCALL__; }
        catch (e) {
          if (_USAGE_LIMIT_RE.test(String((e && e.message) ?? e))) { _haltOnUsageLimit(e); return null; }
          log('ledger-fix leg threw: ' + String((e && e.message) ?? e)); return null;
        }
      };
      for (let attempt = 1; attempt <= _recoveryCap && !_halted; attempt++) {
        const open = Object.keys(rec).filter((c) => rec[c].status === 'open');
        if (!open.length) break;
        const fixed = await parallel(open.map((cid) => () => _fixOne(cid)));
        if (_halted) return;
        open.forEach((cid, k) => {
          const st = rec[cid]; const r = fixed[k]; st.attempts = attempt;
          if (!r || typeof r !== 'object') { st.last_reason = 'fix leg returned no result'; return; }
          const row = rowOf(cid);
          const outside = (Array.isArray(r.paths) ? r.paths : []).map(_fpPosix).filter((p) => !st.footprint.includes(p) && !_fpOwns(row, p));
          if (outside.length) st.paths = [...new Set([...st.paths, ...outside])];
          st.last_reason = String(r.reason ?? '');
          if (r.status === 'BLOCKED') st.status = 'blocked';
          else if (outside.length) { st.status = 'blocked'; st.last_reason = 'fix wrote outside the footprint: ' + outside.join(', '); }
        });
        const stillOpen = open.filter((c) => rec[c].status === 'open');
        if (!stillOpen.length) continue;
        const ids = [...new Set(stillOpen.flatMap((c) => rec[c].rows))];
        const files = [...new Set(stillOpen.flatMap((c) => rec[c].footprint))];
        const verdicts = JSON.stringify(ids.map((id) => ({ id, remaining: ((_ledger.items ?? []).find((i) => i && i.id === id) ?? {}).remaining ?? [] })));
        const fileText = JSON.stringify(files);
        let rv = null;
        try { rv = await __REVERIFYCALL__; }
        catch (e) {
          if (_USAGE_LIMIT_RE.test(String((e && e.message) ?? e))) { _haltOnUsageLimit(e); return; }
          log('ledger-reverify leg threw: ' + String((e && e.message) ?? e));
        }
        if (_halted) return;
        if (rv && Array.isArray(rv.items)) {
          for (const it of rv.items) {
            if (!it || !ids.includes(it.id)) continue;
            const at = _ledger.items.findIndex((x) => x && x.id === it.id);
            if (at >= 0) _ledger.items[at] = it; else _ledger.items.push(it);
          }
          _normaliseLedger();
          __RECONCILE__
        }
        const t = rv && rv.test;
        if (t && typeof t.status === 'string') {
          const passed = (s) => s === 'pass' || s === 'pass-with-skips';
          const prior = __TEST__;
          const priorFiles = failed.map((f) => _fpPosix(String(f).split('::')[0]));
          if (!passed(t.status)) __TEST__ = t;
          else if (prior && !passed(prior.status) && priorFiles.length && priorFiles.every((f) => files.includes(f))) __TEST__ = t;
        }
        const still = new Set(_ledgerBlocking(_ledger).map((i) => i.id));
        for (const c of stillOpen) {
          if (!rec[c].rows.some((id) => still.has(id))) { rec[c].status = 'fixed'; continue; }
          rec[c].last_reason = JSON.stringify(rec[c].rows.map((id) => ((_ledger.items ?? []).find((i) => i && i.id === id) ?? {}).remaining ?? [])).slice(0, 300);
        }
      }
      for (const c of Object.keys(rec)) if (rec[c].status === 'open') rec[c].status = 'exhausted';
      const _out = ['exhausted', 'blocked', 'held'];
      for (const i of _ledgerBlocking(_ledger)) {
        const cs = Object.keys(rec).filter((c) => rec[c].rows.includes(i.id));
        if (cs.length && cs.every((c) => _out.includes(rec[c].status))) i.withheld_by = cs[0];
      }
      _recoveryWithheld = Object.keys(rec).filter((c) => _out.includes(rec[c].status));
      for (const c of _recoveryWithheld) if (!_incompleteChunks.includes(c)) _incompleteChunks.push(c);
      const recordText = JSON.stringify({ cap: _recoveryCap, chunks: rec });
      try { await __RECORDCALL__; }
      catch (e) {
        if (_USAGE_LIMIT_RE.test(String((e && e.message) ?? e))) { _haltOnUsageLimit(e); return; }
        log('recovery record leg threw: ' + String((e && e.message) ?? e));
      }
    };
    if (!_halted) await _ledgerRecover();
"""


def _ledger_recover_js(
    *, head: str, anchor: str, session_tail: str, record_dir: str, ledger_rel: str, run_id: str, judged: bool, test_var: str
) -> str:
    """The `_ledgerRecover()` definition and its one call, composed between the verify leg's ledger and the ledger halt.

    The fix legs run in parallel under `LEDGER_FIX_PHASE`; each re-verify judges only the reopened rows.
    Trap: it reads `_ledgerBlocking`, `_normaliseLedger` and `_reopenPlan`, which must precede it in the script.
    """
    fix_prompt = _cat(
        f"{head}\n\n{anchor}\n\nLedger-fix leg for chunk ",
        "js:_cid",
        f". Its brief is the file ",
        "js:(_fixRow.brief_path ?? '(none)')",
        ". ",
        *_prompt_parts(ledger_fix_prompt("remText", "evText", "failTxt", "fpText")),
        f" {SKILL_WEIGHT_HEADROOM_CLAUSE}{session_tail}",
    )
    fix_call = _agent(
        fix_prompt, label=LEDGER_FIX_LABEL, phase=LEDGER_FIX_PHASE, agent_type=_emit._EXECUTOR_AGENT_TYPE,
        schema=LEDGER_FIX_SCHEMA, run_id=run_id,
    ).replace(f"label: {_lit(LEDGER_FIX_LABEL)},", f"label: {_lit(LEDGER_FIX_LABEL)} + _cid,", 1)
    reverify_prompt = _cat(
        f"{head}\n\n{anchor}\n\nLedger re-verify leg. Fix legs just ran on the chunks behind these rows. "
        f"Re-read the requirements ledger at {ledger_rel}, then judge ONLY these rows now, against the current tree: ",
        "js:verdicts",
        ". Re-run only the scoped tests of these files (nothing wider): ",
        "js:fileText",
        ". Return `items` (one entry per judged row id, never another row) by the same contract: " + _LEDGER_ITEMS_RULES,
        " Also return `test` (the status, tests_run, tests_failed and sidecar_path of your run) when you ran tests.",
        f" {session_tail}",
    )
    reverify_schema = _obj(
        ["items"],
        {
            "items": _LEDGER_CHECK_SCHEMA["properties"]["items"],
            "test": json.loads(_emit.stage_schema_literal("test_result")),
        },
    )
    reverify_call = _agent(
        reverify_prompt, label=LEDGER_REVERIFY_LABEL, phase=ASK_VERIFY_PHASE, agent_type=_emit._EXECUTOR_AGENT_TYPE,
        schema=reverify_schema, run_id=run_id,
    )
    record_prompt = _cat(
        f"{head}\n\n{anchor}\n\nWrite the file {record_dir}/{RECOVERY_RECORD_FILE} with the Write tool (create its "
        "directory if missing), holding exactly this JSON text and nothing else: ",
        "js:recordText",
        ". Edit nothing else. Return {written: true} once it is on disk.",
    )
    record_call = _agent(
        record_prompt, label="ledger-recovery-record", phase=LEDGER_FIX_PHASE, agent_type=_emit._EXECUTOR_AGENT_TYPE,
        effort=_MECHANICAL_EFFORT, schema=_obj(["written"], {"written": {"type": "boolean"}}),
        run_id=run_id,
    )
    return (
        _LEDGER_RECOVER_JS.replace("__CAP__", str(RECOVERY_ATTEMPT_CAP))
        .replace("__RL__", RESUME_LEDGER_KEY)
        .replace("__XS__", _lit(ARM_XS))
        .replace("__TEST__", test_var)
        .replace("__FIXCALL__", fix_call)
        .replace("__REVERIFYCALL__", reverify_call)
        .replace("__RECORDCALL__", record_call)
        .replace("__RECONCILE__", "_reconcileJudged();" if judged else "")
    )


def _fire_tier_js() -> str:
    """JS expression for the execute wave's tier at fire time, from `_gate.tshirt`.

    The map is rendered from `emit._TSHIRT_TO_REVIEW_TIER`; an absent or unknown tshirt reads
    `full`, which keeps every roster agent.
    """
    return f"({json.dumps(_emit._TSHIRT_TO_REVIEW_TIER, sort_keys=True)}[_gate?.tshirt] ?? 'full')"


# A no-change refusal is a no-op halt (envelope-exempt in halt_refire); the paths it meant to land ride along.
_NO_CHANGE_HALT_JS = (
    f"if (_halted?.refused === {_lit(NO_CHANGE_REFUSAL)}) {{ Object.assign(_halted, {{ halted: 'no-op', "
    f"{MEANT_TO_LAND_KEY}: _commit?.{MEANT_TO_LAND_KEY} ?? null, "
    f"{DIRTY_BEFORE_RUN_KEY}: _commit?.{DIRTY_BEFORE_RUN_KEY} ?? null }}); }} "
)
_GATE_HALT_CAUSE_JS = "(_gate.halt ? 'gate halt: ' + String(_gate.halt.kind ?? _gate.halt.reason ?? 'halt') : 'apm ruling carried no foldable arm (non-XS arm or resume_plan)')"


def _fold_decline_js(clean_js: str, cause_js: Optional[str] = None) -> str:
    """JS `else` clause for a guarded fold site: records into `_noFoldCause` why nothing folded."""
    cause = cause_js or (
        f"(!({clean_js}) ? 'scout not clean (exit_code/halt/arm)' : "
        "'arm ' + String(_gate?.arm) + ' is outside the fold set')"
    )
    return f" else if (!_noFoldCause) {{ _noFoldCause = {cause}; }}"


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
        + "A path the manifest's write_baseline shows unchanged since stage was not delivered by this "
        "run: pre-existing dirt never meets a row, whatever the working tree holds. "
        "A HEAD-measured gate's result in this leg measures the run's candidate commit, so it is "
        "evidence about this run's change, never \"no change\"; when the measured-rev op refused, that "
        "gate measured HEAD, not the change, so the row is unmet with the refusal as its reason, never "
        "\"no change\". "
        "The manifest's withheld (gated) chunks close this prompt. Return items, all_of_pm_ask and "
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
    """The sizing at `sizing_rel` when its estimate is unsettled (intake mint or draft), else None."""
    if not sizing_rel:
        return None
    from coordinator_core.ops.sizing_intake_contract import needs_size_stage

    try:
        sizing = load_sizing(Path(repo_root), sizing_rel)
    except (SizingFireRefused, OSError, ValueError, KeyError):
        return None
    return sizing if needs_size_stage(sizing) else None


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


def _em_context_row_ids(repo_root: str, sizing_rel: Optional[str]) -> list[str]:
    """Ids of the sizing's register rows `requirements_ledger.judge` treats as EM context (non-blocking)."""
    from coordinator_core.ops.dispatch_emit.requirements_ledger import _is_design
    from coordinator_core.ops.sizing_intake_contract import EM_CONTEXT_MARKER

    if not sizing_rel:
        return []
    try:
        register = load_sizing_effective(Path(repo_root), sizing_rel).get("requirement_register")
    except (SizingFireRefused, OSError, ValueError, KeyError):
        return []
    rows = register.get("rows") if isinstance(register, dict) else None
    return [
        r["id"] for r in rows or []
        if isinstance(r, dict) and isinstance(r.get("id"), str)
        and str(r.get("source_text") or "").lstrip().startswith(EM_CONTEXT_MARKER)
        and not _is_design(r.get("anchor"))
    ]


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


def _strip_comment_lines(js: str) -> str:
    """``js`` without the lines holding only a ``//`` comment.

    Strings, templates, regexes and block comments are untouched.

    Trap: regex-vs-division is decided from the previous significant character, as in
    ``prompt_fragments._scan_literals``; a regex literal that defeats it can hide a quote and
    derail the scan, so the only deletions are whole comment lines the scan reached in code mode.
    """
    n, i, prev = len(js), 0, ""
    stack: list = []  # "T" = inside a template body; an int = brace depth inside a `${ }`
    cuts: list[tuple[int, int]] = []
    while i < n:
        c = js[i]
        if stack and stack[-1] == "T":
            if c == "\\":
                i += 2
            elif c == "`":
                stack.pop()
                prev, i = "`", i + 1
            elif js.startswith("${", i):
                stack.append(0)
                prev, i = "{", i + 2
            else:
                i += 1
        elif c in "'\"":
            j = i + 1
            while j < n and js[j] != c and js[j] != "\n":
                j += 2 if js[j] == "\\" else 1
            prev, i = c, j + 1
        elif c == "`":
            stack.append("T")
            i += 1
        elif c == "/" and js.startswith("//", i):
            eol = js.find("\n", i)
            eol = n if eol < 0 else eol
            ls = js.rfind("\n", 0, i) + 1
            if not js[ls:i].strip():
                cuts.append((ls, min(eol + 1, n)))
            i = eol
        elif c == "/" and js.startswith("/*", i):
            k = js.find("*/", i + 2)
            i = n if k < 0 else k + 2
        elif c == "/" and (prev == "" or prev in _prompt_fragments._PREV_OPENERS or js[max(0, i - 8) : i].rstrip().endswith(("return", "typeof"))):
            j, in_cls = i + 1, False
            while j < n and (in_cls or js[j] != "/") and js[j] != "\n":
                if js[j] == "\\":
                    j += 1
                elif js[j] == "[":
                    in_cls = True
                elif js[j] == "]":
                    in_cls = False
                j += 1
            prev, i = "/", j + 1
        else:
            if c == "{" and stack:
                stack[-1] += 1
            elif c == "}" and stack:
                if stack[-1] == 0:
                    stack.pop()
                else:
                    stack[-1] -= 1
            if not c.isspace():
                prev = c
            i += 1
    out, last = [], 0
    for a, z in cuts:
        out.append(js[last:a])
        last = z
    out.append(js[last:])
    return "".join(out)


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
    out_of_group_guard = "  if (_outOfGroupAt >= 0) {"
    if (
        prep.count(malformed_exit) != 1
        or prep.count(malformed_guard) != 1
        or prep.count(busy_exit) != 1
        or prep.count(missing_exit) != 1
        or prep.count(_REVIEW_MISSING_GUARD) != 1
        or prep.count(_REVIEW_PREP_BIND) != 1
        or prep.count(slice_id_exit) != 2
        or prep.count(slice_id_guard) != 1
        or prep.count(out_of_group_guard) != 1
    ):
        raise AskComposeRefused("review prep block no longer has the prep-busy and review-missing exit shape single-exit composition rewrites")
    prep = prep.replace(busy_exit, busy_exit.replace("return ", "_halted = ", 1))
    prep = prep.replace(malformed_exit, malformed_exit.replace("return ", "_halted = ", 1))
    prep = prep.replace(malformed_guard, "  if (!_halted && _malformed.length) {", 1)
    prep = prep.replace(missing_exit, missing_exit.replace("return ", "_halted = ", 1))
    prep = prep.replace(_REVIEW_MISSING_GUARD, _REVIEW_MISSING_GUARD.replace("if (", "if (!_halted && ", 1), 1)
    prep = prep.replace(_REVIEW_PREP_BIND, "  if (!_halted) _reviewPrep = (() => {\n", 1)
    prep = prep.replace(slice_id_exit, slice_id_exit.replace("return ", "_halted = ", 1))
    prep = prep.replace(out_of_group_guard, "  if (!_halted && _outOfGroupAt >= 0) {", 1)
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


#: Prefixes the review-wave prompt head: a symbol unused in this repo can still be called from a sibling repo.
_CROSS_REPO_CALLER_CLAUSE = (
    "\n\nBefore deleting or removing any symbol (function, method, class, constant, even a private one) as "
    "dead code, search the sibling repos for callers of it. If any sibling repo still calls it, keep the "
    "symbol and report it as a finding instead."
)


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
    launcher_child: bool = False,
) -> str:
    """The .mjs text for one ask: a raw `prompt`, or an existing `sizing_rel` (size phase omitted).

    A non-empty `route_preference` adds the EM's steer to the size-stage scout prompt, ahead of the
    ask text, and a `route_override_reason` slot to each gate command the scout runs after its own
    sizing-assemble. Empty or absent, every string is unchanged.

    `launcher_child` renders a script a launcher starts: the script holds no `workflow()` reader, so it can never
    nest; it returns `stage_pending` until its staged twin fills the inline slot with the verified manifest.

    `run_base_sha`, when given, is the base for the review blocks, the criterion judge brief and
    the test instruction; absent, the base is HEAD at compose time.

    A `sizing_rel` that is an intake mint or at `draft` (`needs_size_stage`) is sized in-run like a raw ask, into that
    same file: its placeholder route never picks the arm and its minted `pm_verbatims` are kept.

    `ask_source="relayed"` marks `prompt` as an EM's relay: every prompt that would call the ask
    the PM's words names it a relayed ask instead, and the run reads `relayed-ask.md`, not `ask.md`.

    `required_reviewers` (`plugin:agent` types) join the roster's `named` stage at every tier.
    The review roster and stage schemas load from DoE when not injected; `wrap_stage` and
    `plan_blitz_text` default to `ask_plan_blitz.wrap_stage` over the resolved plugin asset, which
    is embedded only when the arm can be M+. `script_path` is the repo-relative path the script
    is written to, carried into `next_action.params` for `dispatch.land_run`.
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
    parked halt (`next_action` names /mise-prep on the plan; XS: `{route: 'dispatch', sizing}`); stage, execute and review
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
        blitz_fn = _strip_comment_lines(blitz_fn)
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
    review_head = head + intent_clause + _CROSS_REPO_CALLER_CLAUSE + "\n\n" + SKILL_WEIGHT_HEADROOM_CLAUSE + session_tail
    run_base_sha = run_base_sha or head_sha(repo_root) or ""
    review_blocks = compose_execute_review(
        review,
        stage_schemas=review_stage_schemas,
        plan_path=manifest_rel,
        run_base_sha=run_base_sha,
        repo_root=repo_root,
        declared_paths_js="_reviewPaths",
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
        delivery_notes_js="_deliveryNotes",
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
    b.append(_stage_reply_js(launcher_child))
    b.append(f"  let {_OWNER_ROWS_JS} = [];")
    b.append("  let _viaRoadmap = false;")
    b.append("  let _splitExit = null;")
    b.append(_RUN_TOKEN_DECL_LAUNCHER_CHILD_JS if launcher_child else _RUN_TOKEN_DECL_JS)
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
    b.append("  let _consumedReturns = {};")
    b.append("  let _recoveryWithheld = [];")
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
        runtime_plan_js=_RUNTIME_PLAN_PATH_JS, pre_commit_gate_js=_WAVE_TESTS_GATE_FN,
    ))
    b.append(_wave_tests_green_js(agent_type_host, repo_root=repo_root, run_id=run_id, em_brief=bool(em_brief)))
    b.append(_usage_limit_helper_js())
    b.append(_refire_next_action_helper_js(sizing_rel))
    b.append(_halt_next_action_text_helper_js(sizing_rel))
    for name in (*_REVIEW_RESULT_NAMES, _emit._TEST_RESULT_VAR):
        b.append(f"  let {name} = {'[]' if name == WAVE_NOT_RUN_VAR else 'false' if name == INTEGRATION_NOT_RUN_VAR else 'null'};")
    if judged:
        b.append(f"  let {_emit._FALSIFIER_RESULT_VAR} = null;")

    def _gate_payload(record_js: str = "", sizing_js: str = "_sizingRel") -> str:
        return (
            "js:JSON.stringify({ sizing_path: " + sizing_js + ", run_id: _runId, writes: _writes"
            + (", baton: " + json.dumps(baton["path"]) if baton else "")
            + (f", record: {record_js}" if record_js else "")
            + ", ...(_RUN_TOKEN ? { require_live_token: true } : {}) })"
        )

    fold_stage = not stop_after
    fold_falsifier = judged and not stop_after
    if fold_falsifier:
        b.append("  let _planFalsifier = null;")
        b.append("  " + _AS_PLAN_FALSIFIER_JS)
    def _stage_params_js(sizing_js: str, gated_js: str) -> str:
        """The complete XS stage params as one ``js:`` expression: sizing_path and gated inside it, never plan_path."""
        return (
            "js:JSON.stringify({ run_id: _runId, writes: _writes, cross_repo_approved: "
            f"_crossRepoApproved, session_id: _SESSION_ID, sizing_path: {sizing_js}, gated: {gated_js} }})"
        )

    def _stage_act_parts(*params: str) -> tuple[str, ...]:
        """The closing act of the agent that just ran the gate: stage the arms with no plan agent to fold into.

        ``params`` are the parts of the complete params object (sizing_path and gated already inside it,
        never plan_path), each a literal or a ``js:`` expression, so the agent splices nothing.
        """
        return (
            " When the gate reply carries its own `manifest_handle` (a ledger resume or an S resume stages itself), "
            "return {manifest_handle: <it verbatim>} as stage_reply; when it carries `manifest` holding an `error`, return "
            "{error: <that text>} as stage_reply; either way run no stage command. "
            "As your very last act otherwise, stage the run, but only when the gate reply's halt is null and its arm "
            f"is XS and it carries no resume_plan (a resumed plan is staged elsewhere): run `{_INVOKE} {OP_ASK_STAGE} '<params>'` once, <params> being exactly this JSON object, "
            "with no key added, dropped or renamed: ",
            *params,
            ". Return its JSON reply verbatim as stage_reply (a manifest_handle, or the {\"error\": ...} refusal). "
            "On any other arm run no stage command and return no stage_reply.",
        )

    def _gate_agent_call(label: str, phase_title: str, stage_act: bool = False) -> str:
        gate_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            f"Run `{_INVOKE} {OP_ASK_GATE} '",
            _gate_payload(),
            "'` and return its JSON reply verbatim as arm, halt, baton and (when present) resume_plan, resume_rows, resume_ledger, pending_rulings and spine_plan, plus resume_ready, design_brief and design_route when present; "
            "when the command is denied or fails, return halt {kind: 'refusal', reason: <its error text>}, never arm "
            "and halt both null. Also read the sizing at ",
            "js:_sizingRel",
            " and return its estimate.tshirt as tshirt and its route as route.",
            *(
                _stage_act_parts(_stage_params_js("_sizingRel", "_gated"))
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

    # A launcher child whose twin carries a resume block runs from the inline manifest: every pre-stage leg sits
    # behind `_NO_RESUME`, and `_gate` is seeded from the block instead of a leg's reply.
    _NO_RESUME = "!_STAGED_RESUME" if launcher_child else "true"
    _gate_seed_js = (
        "{ arm: _STAGED_RESUME.arm, halt: null, baton: null, tshirt: null, route: null, "
        f"resume_plan: (_STAGED_RESUME.arm !== {_lit(ARM_XS)} && typeof _STAGED_RESUME.plan_rel === 'string' "
        "&& _STAGED_RESUME.plan_rel) ? _STAGED_RESUME.plan_rel : null, "
        "resume_rows: null, resume_ledger: null, pending_rulings: null, resume_ready: null, spine_plan: null, "
        f"{DESIGN_BRIEF_KEY}: null, {DESIGN_ROUTE_KEY}: null, {_ROUTE_VERDICT_KEY}: null }}"
    )
    if launcher_child:
        b.append(
            "  if (_STAGED_RESUME) {\n"
            "    if (typeof _STAGED_RESUME.sizing_rel === 'string' && _STAGED_RESUME.sizing_rel) "
            "_sizingRel = _STAGED_RESUME.sizing_rel;\n"
            f"    if (_STAGED_RESUME.arm !== {_lit(ARM_XS)} && typeof _STAGED_RESUME.plan_rel === 'string' "
            "&& _STAGED_RESUME.plan_rel) _planRel = _STAGED_RESUME.plan_rel;\n"
            "    await _adoptStageReply({ manifest_handle: _STAGED_INLINE.handle });\n"
            "  }"
        )
    if sized_in_run:
        if launcher_child:
            b.append("  let _scouted = null;")
            b.append("  let _scoutClean = false;")
            b.append("  let _pmExit = null;")
            b.append("  let _rewordStaged = false;")
            b.append("  if (!_STAGED_RESUME) {")
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
            f"--write {shlex.quote(f'{repo_root}/{sizing_write_rel}')} --warp-run {shlex.quote(run_id)} --json {NO_PLAN_HAND_OFF_FLAG}{relayed_flag}"
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
            f"resume_rows, resume_ledger, pending_rulings and spine_plan, plus resume_ready, design_brief and design_route{_VERDICT_RETURN if has_route_pref else ''} when present; then read estimate.tshirt and route from the sizing at {sizing_write_rel} "
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
                {
                    "run_id": run_id, "cross_repo_approved": bool(cross_repo_approved),
                    "session_id": session_id or "", "sizing_path": sizing_write_rel,
                }
            )[:-1] + ', "writes": <your writes array>, "gated": <your gated array>}'
            scout_closing_tail += "".join(_stage_act_parts(_scout_base))
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
                + f" + ' --write ' + _q({path_js}) + ' --warp-run ' + _q(_runId) + ' --json ' + {_lit(NO_PLAN_HAND_OFF_FLAG)}"
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
            "  " + ("" if launcher_child else "const ") + "_scouted = _assembleProven(_scoutedFirst) ? _scoutedFirst : { ..._scoutedFirst, ...(await "
            f"{_agent(scout_prompt + ' + (_scoutHasValues(_scoutedFirst) ? (' + values_retry + ') : ' + _lit(SCOUT_DID_NOT_ASSEMBLE_RETRY) + ')', label='size-retry', phase='size', agent_type=SIZING_SCOUT_AGENT_TYPE, schema=_ASK_SCOUT_STAGED_SCHEMA if fold_stage else ASK_SCOUT_SCHEMA, run_id=run_id)}) }};"
        )
        b.append("  const _scoutAssembled = Number.isInteger(_scouted.exit_code);")
        # Captured before the reword path mutates `_scouted`: a folded manifest or falsifier is
        # valid only for the criterion and sizing the scout's own gate reply judged.
        b.append(f"  {'' if launcher_child else 'const '}_scoutClean = _scouted.exit_code === 0 && !_scouted.halt && !!_scouted.arm;")
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
            f"pending_rulings, resume_ready, spine_plan, design_brief and design_route{_VERDICT_RETURN if has_route_pref else ''}; when the command is denied or fails, return halt {{kind: 'refusal', reason: <its error "
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
                _stage_act_parts(_stage_params_js(_reword_sizing_js, "_gated"))
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
        if not launcher_child:
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
            "resume_plan: _reworded.resume_plan ?? null, resume_rows: _reworded.resume_rows ?? null, resume_ledger: _reworded.resume_ledger ?? null, pending_rulings: _reworded.pending_rulings ?? null, resume_ready: _reworded.resume_ready ?? null, spine_plan: _reworded.spine_plan ?? null, "
            f"{DESIGN_BRIEF_KEY}: _reworded.{DESIGN_BRIEF_KEY} ?? null, "
            f"{DESIGN_ROUTE_KEY}: _reworded.{DESIGN_ROUTE_KEY} ?? null, "
            f"{_ROUTE_VERDICT_KEY}: _reworded.{_ROUTE_VERDICT_KEY} ?? null, "
            "stage_reply: _reworded.stage_reply ?? null });"
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
        b.append(
            f"  {'' if launcher_child else 'const '}_pmExit = (_decision && _decision[{_lit(PM_EXIT_QUESTION_KEY)}]) || null;"
        )
        if launcher_child:
            b.append("  }")

    b.append("  phase('gate');")
    if sized_in_run:
        # The scout's closing acts ran the gate; a failed assemble leaves nothing to carry.
        b.append(
            "  let _gate = " + ("_STAGED_RESUME ? " + _gate_seed_js + " : (" if launcher_child else "")
            + "_halted ? { arm: null, halt: null } : { arm: _scouted.arm ?? null, "
            "halt: _scouted.halt ?? null, baton: _scouted.baton ?? null, tshirt: _scouted.tshirt, "
            "route: _scouted.route, resume_plan: _scouted.resume_plan ?? null, "
            "resume_rows: _scouted.resume_rows ?? null, resume_ledger: _scouted.resume_ledger ?? null, pending_rulings: _scouted.pending_rulings ?? null, resume_ready: _scouted.resume_ready ?? null, spine_plan: _scouted.spine_plan ?? null, "
            f"{DESIGN_BRIEF_KEY}: _scouted.{DESIGN_BRIEF_KEY} ?? null, "
            f"{DESIGN_ROUTE_KEY}: _scouted.{DESIGN_ROUTE_KEY} ?? null, "
            f"{_ROUTE_VERDICT_KEY}: _scouted.{_ROUTE_VERDICT_KEY} ?? null }}" + (")" if launcher_child else "") + ";"
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
        # An existing sizing has no scout to close for it, so an XS one with no writes gets its footprint here.
        if footprint_owed(arm=known_arm, writes=writes, sized_in_run=False):
            fp_call = _agent(
                prompt_of(footprint_prompt(sizing_rel=sizing_rel, baton_path=baton["path"] if baton else None)),
                label="footprint", phase="gate", agent_type=SIZING_SCOUT_AGENT_TYPE,
                schema=FOOTPRINT_SCHEMA, run_id=run_id,
            )
            b.append(
                f"  if ({_NO_RESUME} && !_writes.length) {{ const _fp = await {fp_call}; "
                f"_writes = (Array.isArray(_fp?.writes) ? _fp.writes : []).filter({admit_writes_js()}); }}"
            )
        _gate_call = f"await {_gate_agent_call('gate', 'gate', stage_act=True)}"
        if launcher_child:
            _gate_call = f"_STAGED_RESUME ? {_gate_seed_js} : ({_gate_call})"
        b.append(f"  let _gate = {_gate_call};")
    if fold_stage:
        # Adopted only under the plan folds' guard (rows array, no error), for the arms the act covers.
        clean_js = "(_scoutClean || _rewordStaged)" if sized_in_run else "!_gate.halt"
        arms_js = (
            f"((_gate.arm === {_lit(ARM_XS)} && !_gate.resume_plan) || (_gate.arm === {_lit(ARM_S)} && _gate.resume_plan) "
            f"|| _gate.resume_ready || _gate.{RESUME_LEDGER_KEY})"
        )
        if sized_in_run:
            b.append(
                f"  if ({clean_js} && {arms_js}) {{ await _adoptStageReply(_scouted.stage_reply); }}"
                f"{_fold_decline_js(clean_js)}"
            )
        else:
            b.append(
                "  if (_gate && typeof _gate.arm === 'string') { _gate = { ..._gate, arm: _gate.arm.trim().toLowerCase() }; "
                f"if ({clean_js} && {arms_js}) {{ await _adoptStageReply(_gate.stage_reply); }}"
                f"{_fold_decline_js(clean_js)} }}"
            )

    def _record_clause(record_js: str, fields: str) -> list[str]:
        """The ruling agent's own record call; the engine refuses a gated ruling before it writes."""
        return [
            " When, and only when, your verdict is 'ruled', run this one command once, with " + fields
            + " set in its `record` member, and return its JSON reply verbatim as arm, halt, baton, "
            "recorded and (when present) resume_plan, resume_rows, resume_ledger, pending_rulings and spine_plan, plus resume_ready, beside your verdict, pmOnly and ruling. "
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
            *(
                (
                    " Here the gate reply is your record command's reply.",
                    *_stage_act_parts(_stage_params_js("_sizingRel", "_gated")),
                )
                if fold_stage
                else ()
            ),
        )
        accept_schema = _ruling_schema(_ACCEPT_SCHEMA)
        if fold_stage:
            accept_schema = {**accept_schema, "properties": {**accept_schema["properties"], **_STAGE_REPLY_PROP}}
        apm_call = _agent(
            apm_prompt, label="accept", phase="accept", agent_type=ADJUDICATOR_AGENT_TYPE,
            schema=accept_schema, run_id=run_id,
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
        take_gate = "_gate = { ..._gate, arm: %s.arm, halt: %s.halt, baton: %s.baton ?? null, resume_plan: %s.resume_plan ?? null, resume_rows: %s.resume_rows ?? null, resume_ledger: %s.resume_ledger ?? null, pending_rulings: %s.pending_rulings ?? null, resume_ready: %s.resume_ready ?? null, spine_plan: %s.spine_plan ?? null, design_route: %s.design_route ?? _gate.design_route ?? null };"
        irreversible = f"new RegExp({_lit(IRREVERSIBLE_GATE_SOURCE)}, 'i')"
        b.append("  let _splitSlices = null;")
        b.append("  let _splitFan = null;")
        b.append(
            # Each round discharges one halt in-run and re-gates: an XL sizing in ceo mode can
            # owe both the exit pick and the exit-criterion acceptance. No progress ends the loop.
            f"  for (let _round = 0; _round < 3 && {_NO_RESUME} && _gate.halt; _round++) {{\n"
            "    let _progressed = false;\n"
            f"    if (_gate.halt.kind === {_lit(HALT_TOUCHPOINT)}) {{\n"
            "    phase('accept');\n"
            "    let _apm = null;\n"
            f"    try {{ _apm = await {apm_call}; }} catch (_e) {{ _apm = null; }}\n"
            f"    const _apmGated = {irreversible}.test(_apm?.ruling ?? '');\n"
            "    if (_apm && _apm.verdict === 'ruled' && _apm.pmOnly !== true && !_apmGated && _apm.ruling "
            "&& _apm.recorded && _apm.recorded.ok === true && 'arm' in _apm && 'halt' in _apm) {\n"
            f"      {take_gate % (('_apm',) * 10)}\n"
            + (
                f"      if (!_gate.halt && _gate.arm === {_lit(ARM_XS)} && !_gate.resume_plan) {{ await _adoptStageReply(_apm.stage_reply); }}"
                f"{_fold_decline_js('!_gate.halt', _GATE_HALT_CAUSE_JS)}\n"
                if fold_stage
                else ""
            )
            + "      _progressed = true;\n"
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
            f"      {take_gate % (('_xl',) * 10)}\n"
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
    b.append("  let _xsTail = false;")
    b.append("  let _ledgerAdded = [];")
    b.append(f"  const _resumeLedger = !_halted && !!{_rl0};")
    b.append(
        f"  const _ledgerUnlanded = _resumeLedger ? ({_rl0}.{RESUME_LEDGER_UNLANDED_FIELD} ?? []) : [];"
    )
    b.append(
        f"  const _ledgerDerived = _resumeLedger ? ({_rl0}.{RESUME_LEDGER_DERIVED_FIELD} ?? []) : [];"
    )
    b.append(
        f"  const _ledgerUnserved = _resumeLedger && (({_rl0}.{RESUME_LEDGER_UNSERVED_FIELD} ?? []).length > 0);"
    )
    b.append(
        f"  const _ledgerOwes = _resumeLedger && _gate.arm === {_lit(ARM_M_PLUS)}"
        f" && ((({_rl0}.blocking ?? [])"
        f".some((id) => (((({_rl0}.{RESUME_LEDGER_REMAINING_FIELD} ?? {{}})[id]) ?? {{}}).remaining ?? []).length > 0)"
        f" && ({_rl0}.{RESUME_LEDGER_LEG_FIELD}?.action !== {_lit(LEG_SKIP)}))"
        f" || ({_rl0}.{RESUME_LEDGER_UNCLAIMED_FIELD} ?? []).length > 0) || _ledgerUnserved;"
    )
    b.append(
        f"  if (_resumeLedger) {{ _planRel = {_rl0}.plan; if (_ledgerUnlanded.length || _ledgerDerived.length) {{ _ledgerExec = true; }} "
        "else if (!_ledgerOwes) { _xsTail = true; } "
        f"if (_gate.arm === {_lit(ARM_S)} && !_xsTail) {{ _gate = {{ ..._gate, resume_plan: _planRel }}; }} }}"
    )
    b.append(
        f"  const _allLandedResume = _resumeLedger && {_rl0}.{RESUME_LEDGER_KIND_FIELD} === {_lit(ALL_LANDED_HALT_KIND)};"
    )
    b.append(
        f"  const _preHaltCredit = _resumeLedger && !_allLandedResume && _ledgerUnlanded.length === 0"
        f" && ({_rl0}.{RESUME_LEDGER_NOTES_FIELD} ?? []).length > 0;"
    )
    if fold_stage:
        # An XS sizing whose plan edge names an existing plan stages that plan: the XS mint refuses a sizing with an edge.
        xs_plan_stage_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            f"Run `{_INVOKE} {OP_ASK_STAGE} '",
            "js:JSON.stringify({ run_id: _runId, plan_path: _gate.resume_plan, "
            "cross_repo_approved: _crossRepoApproved, session_id: _SESSION_ID, " + _ONLY_ROWS_JS
            + "...(_sizingRel ? { commit_sizing_path: _sizingRel } : {}) })",
            "'` once and return its JSON reply verbatim as stage_reply (a manifest_handle, or the "
            "{\"error\": ...} refusal). Commit nothing.",
        )
        xs_plan_stage_call = _agent(
            xs_plan_stage_prompt, label="xs-plan-stage", phase="gate", agent_type=_MECHANICAL_AGENT_TYPE,
            agent_model=_MECHANICAL_MODEL, schema=_BLITZ_STAGE_READ_SCHEMA, effort=_MECHANICAL_EFFORT,
            run_id=run_id,
        )
        b.append(
            f"  if (!_halted && {_NO_RESUME} && !_resumeLedger && !_stagedManifest && !_stageRefusal "
            f"&& _gate.arm === {_lit(ARM_XS)} && _gate.resume_plan) "
            f"{{ _planRel = _gate.resume_plan; const _xsPlanStaged = await {xs_plan_stage_call}; "
            "await _adoptStageReply(_xsPlanStaged?.stage_reply); }"
        )
    if fold_stage:
        # A ledger verdict carries the gate op's own stage of the resumed plan as `manifest`; the re-plan
        # agent stages the plan itself over exactly the rows it added, so no standalone stage leg runs.
        replan_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            "Ledger-resume re-plan leg. An earlier run halted at the ledger check; these requirement rows "
            "stayed not_met: ",
            f"js:{_rl0}.blocking.join(', ')",
            f". Read each row's register text in {ledger_rel}. {_GAPS_OPEN_LEAD}",
            f"js:JSON.stringify({_rl0}.{RESUME_LEDGER_REMAINING_FIELD} ?? {{}})",
            f". {HALT_GAP_RULE} Items already credited as landed by rows that landed after the halt (landed work: add no row for them, "
            "never re-plan them): ",
            f"js:JSON.stringify({_rl0}.{RESUME_LEDGER_CREDITED_FIELD} ?? [])",
            ". Revise notes, oldest first: ",
            f"js:JSON.stringify({_rl0}.{RESUME_LEDGER_NOTES_FIELD} ?? [])",
            ". Register rows the plan's register_claims.rows omit (a revise minted them after the plan was written; "
            "each is owed work and needs a row of its own that serves it): ",
            f"js:JSON.stringify({_rl0}.{RESUME_LEDGER_UNCLAIMED_FIELD} ?? [])",
            ". Register rows no plan row serves (minted by a revise after the halt, or whose remaining work "
            "names paths no row could derive; each is owed work): ",
            f"js:JSON.stringify({_rl0}.{RESUME_LEDGER_UNSERVED_FIELD} ?? [])",
            ". Every row you add carries register_rows naming the register id it serves, and its writes may "
            "name any path the work needs (the landing fence admits the writes of serving rows)",
            ". Revise the plan at ",
            "js:_planRel",
            " IN PLACE with Edit -- never scaffold a new plan, never change its path -- adding one new row, "
            "disposition open, per piece of owed work, each with bounded declared `writes` covering the "
            "paths the work lies on and a done condition that is a runnable check. Do not edit, reopen or "
            "remove any existing row: the rows that landed stay landed. Return the plan's repo-relative "
            "path as plan_rel and the ids of exactly the rows you added as added_rows. Commit nothing. "
            f"As your very last act, when you added at least one row, run `{_INVOKE} {OP_ASK_STAGE} '<params>'` "
            "once, <params> being the JSON object ",
            "js:JSON.stringify({ run_id: _runId, plan_path: _planRel, "
            "cross_repo_approved: _crossRepoApproved, session_id: _SESSION_ID, ledger_resume: true, "
            f"...(({_rl0}.{RESUME_LEDGER_UNCLAIMED_FIELD} ?? []).length ? {{ claim_register_rows: true }} : {{}}), "
            "...(_sizingRel ? { commit_sizing_path: _sizingRel } : {}) })",
            " with only_rows set to the ids of exactly the rows you added",
            "js:(_ledgerUnlanded.length ? ' together with these unlanded rows: ' + JSON.stringify(_ledgerUnlanded) : '')",
            ", and return its JSON reply "
            "verbatim as stage_reply (a manifest_handle, or the {\"error\": ...} refusal). When you added no row, "
            f"stage nothing and return stage_reply null.{session_tail}",
        )
        replan_call = _agent(
            replan_prompt, label="ledger-replan", phase="plan", agent_type=_PLAN_AUTHOR_AGENT_TYPE,
            schema=_REPLAN_STAGED_SCHEMA, effort=_PLAN_AUTHOR_EFFORT, run_id=run_id,
        )
        b.append(
            "  if (_ledgerOwes && !_stageRefusal) { const _gateStaged = _stagedManifest; const _gateGap = _relayGap; "
            "const _gateDisk = _diskHalt; _stagedManifest = null; _relayGap = null; _diskHalt = null; "
            f"const _replanned = await {replan_call}; "
            "_ledgerAdded = (Array.isArray(_replanned?.added_rows) ? _replanned.added_rows : [])"
            ".filter((s) => typeof s === 'string' && s.trim()); "
            "if (_ledgerAdded.length) { _ledgerExec = true; "
            "await _adoptStageReply(_replanned?.stage_reply); } "
            "else { if (!_ledgerUnlanded.length) { _xsTail = true; } "
            "_stagedManifest = _gateStaged; _relayGap = _gateGap; _diskHalt = _gateDisk; "
            f"if (_ledgerUnserved) {{ _halted = {{ halted: {_lit(HALT_REFUSAL)}, "
            f"reason: 'unserved register row(s) ' + JSON.stringify({_rl0}.{RESUME_LEDGER_UNSERVED_FIELD}) "
            "+ ': the re-plan added no row; re-size the sizing, or fire a fresh ask whose asks[].writes "
            "cover the work', sizing: _sizingRel, run_id: _runId }; } } }"
        )

        if blitz_fn:
            # The one stage leg of the plan-blitz arm: it runs only when readyOp never ran, and the
            # sidecar it names is paged and verified like any other stage reply.
            blitz_read_prompt = _cat(
                f"{head}\n\n{anchor}\n\n",
                f"Run `{_INVOKE} {OP_ASK_STAGE} '",
                "js:JSON.stringify({ run_id: _runId, plan_path: _planRel, "
                "cross_repo_approved: _crossRepoApproved, session_id: _SESSION_ID, "
                "...(_sizingRel ? { commit_sizing_path: _sizingRel } : {}) })",
                "'` once and return its JSON reply verbatim as stage_reply (a manifest_handle, or the "
                "{\"error\": ...} refusal). Commit nothing.",
            )
            blitz_read_call = _agent(
                blitz_read_prompt, label="blitz-stage-read", phase="plan", agent_type=_MECHANICAL_AGENT_TYPE,
                agent_model=_MECHANICAL_MODEL, schema=_BLITZ_STAGE_READ_SCHEMA, effort=_MECHANICAL_EFFORT,
                run_id=run_id,
            )
            b.append("  const _blitzStageRead = async () => {")
            b.append(f"    const _r = await {blitz_read_call};")
            b.append(
                f"    if (!_r?.stage_reply) {{ _diskHalt = 'no stage manifest on disk: ' + {_lit(manifest_rel)}; return; }}"
            )
            b.append("    await _adoptStageReply(_r.stage_reply);")
            b.append("  };")

        def blitz_stage_read(entry: str, indent: str) -> str:
            """Stage a ready entry whose readyOp never ran (no reply and no error, or plan-blitz's own 'did not
            run' error). A reply that carried neither a refusal nor a verifiable handle halts as not whole; a
            readyOp error of its own halts."""
            return (
                f"{indent}if ({entry} && typeof {entry} === 'object' && _planRel && !_resumeLedger && !_stagedManifest && !_stageRefusal "
                f"&& !_diskHalt && !_relayGap && !_parked) {{\n"
                f"{indent}  if ({entry}.readyOpReply?.{PARKED_REPLY_KEY} && !{entry}.readyOpReply.manifest_handle "
                f"&& !{entry}.readyOpReply.error) {{ _parked = String({entry}.readyOpReply.{PARKED_REPLY_KEY}); }}\n"
                f"{indent}  else if ({entry}.readyOpReply?.error && !{entry}.readyOpReply.manifest_handle) "
                f"{{ await _adoptStageReply({entry}.readyOpReply, false); }}\n"
                f"{indent}  else if ({entry}.readyOpReply != null) {{ _relayGap = 'manifest_handle'; }}\n"
                f"{indent}  else if ({entry}.readyOpError == null || /did not run/.test(String({entry}.readyOpError))) "
                "{ await _blitzStageRead(); }\n"
                f"{indent}}}"
            )

    if xl_possible:
        _append_split_stage(b)

    fold_stage = not stop_after

    def stage_fold(lead: str) -> tuple[str, ...]:
        """Prompt parts that make the plan agent stage its own plan and return the reply as `stage_reply`."""
        return (
            f"{lead} run `{_INVOKE} {OP_ASK_STAGE} '",
            "js:JSON.stringify({ run_id: _runId, plan_path: '<plan_rel>', "
            "cross_repo_approved: _crossRepoApproved, session_id: _SESSION_ID, " + _ONLY_ROWS_JS +
            "...(_sizingRel ? { commit_sizing_path: _sizingRel } : {}) })",
            "'` with <plan_rel> replaced by the plan's repo-relative path, and return its JSON reply "
            "verbatim as `stage_reply` (a `manifest_handle`, or the `{\"error\": ...}` refusal).",
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
    b.append(f"  if (!_halted && {_NO_RESUME} && _gate.arm === {_lit(ARM_S)}) {{")
    b.append("    if (_resumeLedger) { /* planRel already the ledger plan */ } else if (_gate.resume_plan) { _planRel = _gate.resume_plan; } else {")
    b.append("    phase('plan');")
    plan_call = _agent(
        plan_author, label="plan", phase="plan", agent_type=_PLAN_AUTHOR_AGENT_TYPE,
        schema=_PLAN_STAGED_SCHEMA if fold_stage else _PLAN_SCHEMA,
        effort=_PLAN_AUTHOR_EFFORT, run_id=run_id,
    )
    if fold_stage:
        b.append(f"    const _authored = await {plan_call};")
        b.append("    _planRel = _authored.plan_rel;")
        b.append("    await _adoptStageReply(_authored.stage_reply);")
        if fold_falsifier:
            b.append("    _planFalsifier = _asPlanFalsifier(_authored.falsifier);")
    else:
        b.append(f"    _planRel = (await {plan_call}).plan_rel;")
    b.append("    }")
    b.append("  }")
    if blitz_fn:
        b.append(f"  if (!_halted && {_NO_RESUME} && _gate.arm === {_lit(ARM_M_PLUS)}) {{")
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
            "path -- then return its repo-relative path as plan_rel. "
            + _ANCHOR_PREMISES_SENTENCE
            + "Then run the "
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
                + (" Otherwise leave `stage_reply` null.",)
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
            b.append("        await _adoptStageReply(_spineReview.stage_reply);")
        b.append("      } else {")
        b.append(
            "        _halted = { halted: " + _lit(HALT_REFUSAL) + ", reason: 'spine plan review verdict ' + "
            "String(_spineReview?.verdict ?? 'none') + ' (not ready)', review: _spineReview ?? null, "
            "sizing: _sizingRel, run_id: _runId };"
        )
        b.append("      }")
        b.append("      }")
        b.append("    }")
        b.append("    if (_gate.resume_ready && _gate.resume_plan) { _planRel = _gate.resume_plan; }")
        b.append("    if (!_planRel && !_halted) {")

        # plan-blitz-ready-op-contract: omitted once staged and in a stop_after script (never stages).
        ready_op_js = (
            f"...(!_stagedManifest ? {{ readyOp: {{ op: {_lit(OP_ASK_STAGE)}, params: {{ run_id: _runId, "
            "cross_repo_approved: _crossRepoApproved, session_id: _SESSION_ID, "
            + _ONLY_ROWS_JS
            + "...(_sizingRel ? { commit_sizing_path: _sizingRel } : {}) }, "
            + f"schema: {json.dumps(STAGE_REPLY_SCHEMA, sort_keys=True)} }} }} : {{}}), "
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

        ruled_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            "phase: apply-rulings\n\nThis run resumes the pulled plan at ",
            "js:_gate.resume_plan",
            ". Read the requirements ledger (path below) at run time. Its revision rows that "
            "`workflow.revise` recorded on the halted run and whose text names a ruling id `J<n>` are "
            "the EM's rulings on that plan's blocking findings; a revision of the original ask that "
            "names no such id is not a ruling. Apply each ruling to the plan IN PLACE with Edit: a "
            "prep-gate class is fixed in the plan, a review is revised or accepted, a CONTRADICTED "
            "premise gets a `corrects:` entry and an UNRESOLVED premise a `declared_absent:` entry. "
            "Never scaffold a new plan, never change its path, and never delete a census premise or a "
            "review finding to clear one. Return the plan's repo-relative path as plan_rel and the "
            "ruling ids you applied as applied (an empty list, with no edit, when the ledger carries "
            "no ruling). Apply rulings: ",
            "js:_gate.pending_rulings ? _gate.pending_rulings.join(', ') : '(unlisted: read the ledger)'",
            "." + ledger_clause,
        )
        ruled_call = _agent(
            ruled_prompt, label="apply-rulings", phase="plan", agent_type=_RULINGS_AGENT_TYPE,
            agent_model=_RULINGS_MODEL, schema=_RULED_SCHEMA, effort=_MECHANICAL_EFFORT, run_id=run_id,
        )
        b.append(
            "    if (_gate.resume_plan && (_gate.pending_rulings == null || _gate.pending_rulings.length > 0)) "
            f"{{ const _ruled = await {ruled_call}; _gate = {{ ..._gate, resume_rulings: (_ruled?.applied ?? []) }}; "
            "} else if (_gate.resume_plan) { _gate = { ..._gate, resume_rulings: [] }; }"
        )
        b.append(f"    const _blitz = {_blitz_call('_gate.resume_plan ?? _spineFallback ?? null')}")
        b.append("    const _ready = (_blitz?.ready ?? [])[0];")
        if fold_stage:
            b.append(
                "    if (_ready && typeof _ready === 'object' && !_stagedManifest) { await _adoptStageReply(_ready.readyOpReply, true); _adoptReadyError(_ready); }"
            )
        b.append("    _planRel = (_ready && typeof _ready === 'object') ? _ready.planPath : _ready;")
        if fold_stage:
            b.append(blitz_stage_read("_ready", "    "))
        b.append("    const _pulledEntry = (_blitz?.pulled ?? []).find((p) => p && p.planPath);")
        b.append("    let _repeatBlocking = null;")
        b.append("    let _pullPlan = null;")
        b.append("    let _pullRulings = [];")
        b.append("    let _pullBlocking = null;")
        b.append("    let _pullSplit = false;")
        if not (accept_pending or xl_possible):
            b.append("    let _splitSlices = null;")
            b.append("    let _splitFan = null;")
        b.append("    if (!_planRel && _pulledEntry) {")
        revise_prompt = _cat(
            f"{head}\n\n{anchor}\n\n",
            "plan-blitz pulled the plan at ",
            "js:_pulledEntry.planPath",
            " at its prep gate. Revise that plan IN PLACE with Edit -- never scaffold a new one, never "
            "change its path -- so every finding below is resolved, then return its repo-relative path "
            "as plan_rel. "
            + _ANCHOR_PREMISES_SENTENCE
            + "Then run the `plan.readiness_verdict` op on the revised plan, passing the "
            "pull's coverage and reviews values below when they are not null, apply every "
            "mechanical_repairs entry in place with Edit, then re-run the op with the plan alone "
            "(no coverage, no reviews: the repairs answered those findings, and the same values "
            "would pull the plan again), and return that reply's pull_class and verdict "
            "alongside plan_rel. Also return that reply's rulings, blocking_findings and "
            "split_recommended verbatim; when the pull's coverage or reviews still stand unanswered "
            "after your repairs, take them from the run that carried those values instead.\n\nPull reason:\n",
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
                + (" Otherwise leave `stage_reply` null.",)
                if fold_stage
                else ()
            ),
        )
        b.append(
            f"      const _revised = await {_agent(revise_prompt, label='plan-revise', phase='plan', agent_type=_PLAN_AUTHOR_AGENT_TYPE, schema=_REVISE_STAGED_SCHEMA if fold_stage else _REVISE_SCHEMA, effort=_PLAN_AUTHOR_EFFORT, run_id=run_id)};"
        )
        b.append("      _pullPlan = _revised?.plan_rel || _pulledEntry.planPath;")
        b.append(
            "      _pullRulings = Array.isArray(_revised?.rulings) && _revised.rulings.length ? _revised.rulings : "
            "(Array.isArray(_pulledEntry.blocking) ? _pulledEntry.blocking : []).map((f, i) => "
            "({ id: 'J' + (i + 1), source: 'blocking', subject: String(f).slice(0, 300), ask: String(f) }));"
        )
        b.append("      _pullBlocking = typeof _revised?.blocking_findings === 'number' ? _revised.blocking_findings : null;")
        b.append(
            "      if (_revised?.pull_class === 'mechanical' && _revised?.verdict === 'ready') "
            "{ _planRel = _revised.plan_rel || _pulledEntry.planPath;"
            + (
                " if (_planRel === _revised.plan_rel) { await _adoptStageReply(_revised.stage_reply); }"
                if fold_stage
                else ""
            )
            + " } else {"
        )
        split_prompt = (
            _cat(f"{head}\n\n{anchor}\n\n")
            + " + "
            + pull_split_prompt("_pullPlan", "_sizingRel")
        )
        split_call = _agent(
            split_prompt, label="pull-split", phase="plan", agent_type=_RULINGS_AGENT_TYPE,
            agent_model=_RULINGS_MODEL, schema=_PULL_SPLIT_SCHEMA, effort=_MECHANICAL_EFFORT, run_id=run_id,
        )
        b.append(
            "      if (_revised?.split_recommended === true && _splitSlices == null "
            f"&& !(String(_pulledEntry.reason ?? '') + String(_pulledEntry.prepGateReport ?? '')).includes({_lit(REGISTER_PIN_DRIFT_KIND)})) {{"
        )
        b.append("      let _pullFan = null;")
        b.append(f"      try {{ _pullFan = await {split_call}; }} catch (_e) {{ _haltOnUsageLimit(_e); _pullFan = null; }}")
        b.append(
            "      if (_pullFan && _pullFan.pick === 'split' && Array.isArray(_pullFan.slices) && _pullFan.slices.length) "
            "{ _splitSlices = _pullFan.slices; _splitFan = _pullFan; _pullSplit = true; }"
        )
        b.append("      }")
        b.append("      if (!_planRel && !_pullSplit) {")
        b.append(f"      const _reblitz = {_blitz_call('_revised?.plan_rel || _pulledEntry.planPath')}")
        b.append("      const _reready = (_reblitz?.ready ?? [])[0];")
        if fold_stage:
            b.append(
                "      if (_reready && typeof _reready === 'object' && !_stagedManifest) { await _adoptStageReply(_reready.readyOpReply, true); _adoptReadyError(_reready); }"
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
        b.append("      }")
        b.append("    }")
        from coordinator_core.ops.dispatch_emit.ask_roadmap import no_ready_reason_js as _no_ready_reason_js

        b.append(
            f"    if (!_planRel) {{ _halted = {{ halted: {_lit(HALT_REFUSAL)}, "
            f"reason: {_no_ready_reason_js('(_blitz?.regate ?? _blitz)?.pulled')}, blitz: _blitz, sizing: _sizingRel, run_id: _runId }};"
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
        pull_refire = _refire_js(sizing_rel)
        pull_na = rule_and_refire_next_action_js("_pullPlan", "_pullRulings", pull_refire)
        b.append(
            f"    if (_repeatBlocking && !_halted.next_action) {{ _halted = {{ halted: {_lit(HALT_REFUSAL)}, "
            "reason: 'plan-blitz pulled twice on the same blocking set', blocking: _repeatBlocking, "
            "plan: _pullPlan, rulings: _pullRulings, blocking_findings: _pullBlocking, "
            f"next_action: {{ ...{pull_na}, kind: 'rule_on_blocking_rows', blocking: _repeatBlocking, "
            "note: 'a second pull blocked on the same rows: rule on them (revise the plan or accept them); the same plan pulls again' }, "
            "blitz: _blitz, sizing: _sizingRel, run_id: _runId }; }"
        )
        b.append(
            "    if (_pullSplit && !_halted.next_action) { _halted = { halted: "
            f"{_lit(HALT_SPLIT)}, reason: 'plan-blitz pulled the plan with ' + _pullBlocking + ' blocking findings: split into smaller batons', "
            "plan: _pullPlan, rulings: _pullRulings, blocking_findings: _pullBlocking, "
            "blitz: _blitz, sizing: _sizingRel, run_id: _runId }; }"
        )
        b.append(
            "    else if (_pullPlan && !_halted.next_action) { _halted = { ..._halted, "
            "plan: _pullPlan, rulings: _pullRulings, blocking_findings: _pullBlocking, "
            f"next_action: {pull_na} }}; }}"
        )
        b.append("    }")
        b.append("    }")
        b.append("  }")
        _append_split_stage(b)
    else:
        b.append(
            f"  if (!_halted && _gate.arm === {_lit(ARM_M_PLUS)}) {{ _halted = {{ halted: {_lit(HALT_REFUSAL)}, "
            "reason: 'sizing resolved to M+ at emit time but the plan-blitz stage was not embedded', "
            "sizing: _sizingRel, run_id: _runId }; }"
        )

    if stop_after:
        b.append(
            f"  if (!_halted && _gate.arm !== {_lit(ARM_XS)} && !_xsTail && _planRel) {{ _halted = {{ halted: {_lit(HALT_PARKED)}, "
            "reason: 'parked after plan: stop_after=plan', "
            f"plan: _planRel, sizing: _sizingRel, run_id: _runId, next_action: {parked_next_action_js('_planRel')} }}; }}"
        )
        b.append(
            f"  if (!_halted && _gate.arm !== {_lit(ARM_XS)} && !_xsTail) {{ _halted = {{ halted: {_lit(HALT_REFUSAL)}, "
            "reason: 'stop_after=plan but no plan landed', sizing: _sizingRel, run_id: _runId }; }"
        )
        if in_run_commit:
            _append_halt_record_commit(b)
        b.append("  _refireNextAction();")
        b.append(
            "  return _halted ? { halted_by: _halted, halt_commit: _haltCommit, sizing: _sizingRel, run_id: _runId, "
            f"{DESIGN_ROUTE_KEY}: _gate?.{DESIGN_ROUTE_KEY} ?? null }} : "
            f"{{ route: 'dispatch', sizing: _sizingRel, {DESIGN_ROUTE_KEY}: _gate?.{DESIGN_ROUTE_KEY} ?? null }};"
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
            chatty=bool((plan_blitz_args or {}).get("chatty")),
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
    b.append("  let _memoCloseOnly = false;")
    b.append("  if (!_viaRoadmap) {")
    b.append("  if (!_halted) {")
    # Stage is folded into the last act of the agent that closes the gate, plan or plan-blitz ready
    # path; there is no standalone stage or read leg. Each fold site returns the stage op's reply (a
    # handle or a refusal) as `stage_reply`; `_adoptStageReply` adopts the manifest only from sidecar
    # pages that match the handle and pass `_wholeManifestGap`, and a mismatch halts by name. The
    # plan-blitz arm alone stages through `_blitzStageRead`, and only when readyOp never ran. The
    # falsifier is authored by the scout or the plan legs, never a leg of its own.
    b.append(
        f"  if (_parked && !_halted) _halted = {{ halted: {_lit(HALT_PARKED)}, "
        "reason: 'parked after plan: workflow.revise stop_after=plan', "
        f"plan: _planRel, sizing: _sizingRel, run_id: _runId, next_action: {parked_next_action_js('_planRel')} }};"
    )
    b.append(
        "  let _stageHalt = _stageRefusal ? 'stage refused: ' + _stageRefusal : "
        "(_diskHalt ?? (_relayGap ? 'stage manifest relay is not whole: ' + _relayGap : null));"
    )
    if launcher_child:
        b.append(
            f"  if (_stagePending && !_stagedManifest) {{ return {{ {STAGE_PENDING_KEY}: _stagePending }}; }}"
        )
    b.append("  if (_stagedManifest) { _manifest = _stagedManifest; }")
    if judged and fold_falsifier:
        spec_js = "_scoutFalsifier ?? _planFalsifier" if sized_in_run else "_planFalsifier"
        b.append(f"  if (_gate.arm && _gate.arm !== {_lit(ARM_XS)} && !_xsTail) {{ _falsifierSpec = {spec_js}; }}")
    b.append(
        f"  if (!_stagedManifest) {{ _manifest = {{ error: '{NO_MANIFEST_PREFIX}: the agent that closes the "
        "gate, plan or plan-blitz ready path returned none (' + (_noFoldCause ?? 'no fold site was reached') + ')' }; }"
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
        "{ _allLanded = true; _xsTail = true; }"
    )
    # `_allLanded` is set too: it is the flag the pinned refusal-halt condition already honors.
    b.append(
        "  if (!_manifest.error && !(_manifest.rows ?? []).length && !(_manifest.owed ?? []).length "
        f"&& !(_manifest.gated ?? []).length && Array.isArray(_manifest.{MEMO_CLOSE_KEY}) && _manifest.{MEMO_CLOSE_KEY}.length) "
        "{ _memoCloseOnly = true; _allLanded = true; _xsTail = true; }"
    )
    b.append(
        f"  if (_manifest.error && /^(?:stage refused: )?{STAGE_REFUSAL_PREFIX}/.test(String(_manifest.error))) {{ _halted = {{ "
        f"halted: {_lit(HALT_DESIGN_REVIEW)}, kind: {_lit(HALT_DESIGN_REVIEW)}, reason: String(_manifest.error), "
        f"board_url: _gate?.{DESIGN_ROUTE_KEY}?.board?.url ?? null, {DESIGN_ROUTE_KEY}: _gate?.{DESIGN_ROUTE_KEY} ?? null, "
        f"sizing: _sizingRel, plan: _planRel, run_id: _runId, next_action: {_design_next_action_js(sizing_rel)} }}; }}"
    )
    b.append(
        "  else if (!_parked && (_manifest.error || (!(_manifest.rows ?? []).length && !_allLanded && !_resumeLedger))) { _halted = { halted: "
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
    b.append("  let _deliveryNotes = '';")
    b.append("  if (!_halted && (!_resumeLedger || _ledgerExec)) {")
    b.append(f"  phase({_lit(_emit._EXECUTE_PHASE_TITLE)});")
    b.append("  const _rows = {};")
    b.append(
        f"  const _rowVerifyWanted = !(_gate.arm !== {_lit(ARM_XS)} && !_xsTail && (_manifest.test_targets ?? []).length);"
    )
    b.append("  const _waves = [...new Set(_manifest.rows.map((r) => r.wave))].sort((a, b) => a - b);")
    b.append("  if (_manifest.fanout) log('Fanout: ' + JSON.stringify(_manifest.fanout));")
    row_prompt = _cat(
        f"{brief_prefix}{_AGENT_DEFS_MARKER}{_emit._prompt_head(preamble)}\n\n{anchor}\n\n",
        "Your brief is the file ",
        "js:r.brief_path",
        f" -- read it completely, then execute it as written.\n\n{SKILL_WEIGHT_HEADROOM_CLAUSE}{ledger_clause}{session_tail}",
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
        "    const ms = _manifest.rows.filter((x) => x.group === g && !x.kept_from);\n"
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
    b.append(
        "  function _keptRun(r) {\n"
        "    log('Kept from ' + r.kept_from + ': ' + r.id);\n"
        "    return 'DONE: ' + r.id + ' kept from ' + r.kept_from + ' (report on disk)';\n"
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
        f"      else _rows[r.id] = _runRow(r.id, (r.deps ?? []).map((d) => _rows[d]), null, async () => r.kept_from ? _keptRun(r) : {routed_arm}r.group ? _groupRun(r) : agent(_rowPrompt(r), "
        f"{{ label: {work_label} + r.id, phase: {exec_phase}, "
        f"agentType: r.agent_type, model: r.model, stallMs: {_emit._EXECUTOR_STALL_MS} }}), "
        "{ title: r.id, paths: r.writes ?? [], declared: r.writes ?? [] });"
    )
    b.append("    }")
    # Wave barrier: wave w+1 launches only once wave w's commit:wave-w leg has settled.
    b.append("    const _waveIds = _manifest.rows.filter((x) => x.wave === w).map((x) => x.id);")
    b.append("    await Promise.allSettled(_waveIds.map((i) => _rows[i]));")
    # Trap: the last wave takes no checkpoint leg; dispatch.terminal_commit lands its paths, so a
    # checkpoint there is a commit agent immediately followed by the terminal commit.
    b.append("    if (w !== _waves[_waves.length - 1]) await _commitWave(w, _waveIds);")
    b.append("  }")
    b.append(
        "  if (_commitFailures.length) log('Wave commit failures (the terminal commit covers their paths): ' "
        "+ _commitFailures.join('; '));"
    )
    b.append("  await Promise.all(Object.values(_rows));")
    # The settled executor replies (their DONE notes), already in hand: no spawn, no file read.
    b.append(
        "  _deliveryNotes = (await Promise.all(_manifest.rows"
        f".filter((r) => r.agent_type !== {_lit(REVIEWER_AGENT_TYPE)})"
        ".map(async (r) => { const t = await _rows[r.id]; "
        "return typeof t === 'string' && t.trim() ? r.id + ': ' + t.trim().replace(/\\s+/g, ' ').slice(0, 600) : ''; })))"
        ".filter((x) => x).join(' | ');"
    )
    # Trap: the engine caps; no truncation here. A reply that fails the DONE test is never persisted.
    b.append(
        "  { const _consumedIds = new Set(_manifest.rows.flatMap((r) => (r.deps ?? []).filter((d) => d !== r.id)));\n"
        "    for (const r of _manifest.rows) {\n"
        f"      if (r.agent_type === {_lit(REVIEWER_AGENT_TYPE)} || !_consumedIds.has(r.id)) continue;\n"
        "      const t = await _rows[r.id];\n"
        "      if (t == null) continue;\n"
        "      const s = typeof t === 'string' ? t : JSON.stringify(t);\n"
        "      const j = JSON.stringify(t);\n"
        "      if (s.trim() && !_DEP_NON_DONE_RE.test(j) && _DEP_ANY_STATUS_RE.test(j)) _consumedReturns[r.id] = s;\n"
        "    }\n"
        "  }"
    )
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
        "  const _reviewPaths = [...(_manifest?.review_declared_paths ?? []), "
        f"...(_gate.{RESUME_LEDGER_KEY}?.remaining_leg?.writes ?? [])];\n"
        "  const _noReviewPaths = !_reviewPaths.length;"
    )
    # The review wave shadows `agent` inside its own block (design brief prefix only) rather than
    # renaming its call sites: the leg register, phase-title and brief checks find legs by the
    # literal `agent(` token, so a renamed call site is invisible to every one of them.
    b.append("  const _reviewOuterAgent = agent;")
    b.append("  let _footprintReviewed = false; // true only when this fire ran the review block; XS / _xsTail / no-paths set _reviewWave = [] without reviewing")
    b.append(f"  {{ const agent = (p, o) => _reviewOuterAgent({_DESIGN_PREFIX_JS} + p, o);")
    b.append(
        f"  if (!_halted && _gate.arm !== {_lit(ARM_XS)} && !_xsTail && !_noReviewPaths) {{\n"
        + review_text + "\n  _footprintReviewed = true;\n  }"
    )
    b.append("  }")
    # XS skips review; an empty wave (zero slices, no delivery verdict) is the form terminal_commit's
    # inline_review already accepts, so the run still reaches commit without a fabricated verdict.
    b.append(f"  if (!_halted && (_gate.arm === {_lit(ARM_XS)} || _xsTail)) {{ _reviewWave = []; }}")
    b.append(f"  if (!_halted && (_gate.arm === {_lit(ARM_XS)} || _xsTail)) {{ {INTEGRATION_NOT_RUN_VAR} = true; }}")
    b.append(
        "  if (!_halted && _noReviewPaths && !_xsTail && _gate.arm !== "
        f"{_lit(ARM_XS)}) {{ log('execute_review skipped: no row declares a path, nothing to review'); "
        "_reviewWave = []; }"
    )
    b.append(
        f"  if (!_halted && _noReviewPaths && !_xsTail && _gate.arm !== {_lit(ARM_XS)}) "
        f"{{ {INTEGRATION_NOT_RUN_VAR} = true; }}"
    )
    b.append(_review_blocked_halt_js(sizing_rel))
    test_guard = (
        f"((_gate.arm !== {_lit(ARM_XS)} && !_xsTail) || _reviewApplied > 0) && (_manifest.test_targets ?? []).length"
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
            "; owed rows: ",
            "js:" + _OWED_IDS_JS,
            "js:" + _WITHHELD_ROWS_JS,
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
            "; owed rows: ",
            "js:" + _OWED_IDS_JS,
            _LEDGER_GATED_RULE,
            *((_LEDGER_DOC_WRITES_RULE,) if in_run_commit else ()),
        )
    verify_call = _verify_call(
        head=head,
        anchor=anchor,
        test_js=_test_instruction_js(run_base_sha or "HEAD", repo_root),
        docs_parts=_docs_prompt_parts(run_id=run_id, manifest_rel=manifest_rel) if in_run_commit else (),
        verdict_parts=(*verdict_parts, DEFAULT_MET_BY_CLAUSE, "js:" + _SFE_WAVE_SIDECARS_JS, "js:" + _RESUME_LEDGER_CLAUSE_JS, "js:" + _MEMO_CLOSE_CLAUSE_JS),
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
        f"    if (!_halted) try {{ _verify = await {verify_call}; }} catch (e) {{ const _m = String((e && e.message) ?? e); "
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
                f"{_emit._JUDGE_VERDICT_RULES_JS}(_verify.judge, {_GATED_LIST_JS}) : {unjudged};"
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
    # The leg's footprint is the only path set named; dispatch.land_run withholds a changed path outside it.
    remaining_prompt = _cat(
        f"{head}\n\n{anchor}\n\n",
        "Ledger-resume remaining-work leg. An earlier run halted; its revise notes may ask for work with no "
        "requirement row blocking. Rows still not_met and not ruled out_of_scope by the revise: ",
        f"js:{_rl}.blocking.join(', ')",
        f". Read each row's register text in {ledger_rel}. {_GAPS_OPEN_LEAD}",
        f"js:JSON.stringify({_rl}.{RESUME_LEDGER_REMAINING_FIELD} ?? {{}})",
        f". {HALT_GAP_RULE} Revise notes, oldest first: ",
        f"js:JSON.stringify({_rl}.{RESUME_LEDGER_NOTES_FIELD} ?? [])",
        ". Rows a revise added after the earlier run (new requirements, owed work you must do now):",
        f"js:JSON.stringify({_rl}.{RESUME_LEDGER_REVISED_FIELD} ?? [])",
        f". Also read {record_dir}/{NOTES_FILE} when it exists: the notes of this refire, newer than the ones above. "
        "Do the owed work now. Your writes are exactly these declared paths: ",
        f"js:({_rl}.{RESUME_LEDGER_LEG_FIELD}?.writes ?? []).join(', ')",
        ". A row whose work you cannot finish, or whose owed work lies on a path outside those writes, stays "
        "not_met: write nothing for it and say so in `summary`. Commit and stage nothing. "
        f"{SKILL_WEIGHT_HEADROOM_CLAUSE}{session_tail}",
    )
    remaining_call = _agent(
        remaining_prompt, label="ledger-remaining", phase=ASK_VERIFY_PHASE,
        agent_type=_emit._EXECUTOR_AGENT_TYPE, schema=_obj(["summary"], {"summary": _STR}),
        run_id=run_id,
    )
    verify_lines[2:2] = [
        f"    const _reviseNoted = _resumeLedger && ({_rl}.{RESUME_LEDGER_NOTES_FIELD} ?? []).length > 0;\n"
        f"    const _revisedRows = _resumeLedger && ({_rl}.{RESUME_LEDGER_REVISED_FIELD} ?? []).length > 0;\n"
        f"    if (_resumeLedger && !_ledgerExec && (({_rl}.blocking ?? []).length || _reviseNoted)) {{\n"
        f"      const _leg = {_rl}.{RESUME_LEDGER_LEG_FIELD} ?? null;\n"
        f"      if (_leg && _leg.action === {_lit(LEG_REFUSE)}) {{ _halted = {{ halted: {_lit(HALT_REFUSAL)}, "
        f"reason: {_lit(LEG_REFUSAL_HEAD)} + (_leg.reason ?? ''), sizing: _sizingRel, run_id: _runId, {HALT_PLAN_FIELD}: _planRel }}; }}\n"
        f"      else if (_leg && _leg.action === {_lit(LEG_SKIP)}) {{ log('ledger-remaining leg skipped: ' + (_leg.reason ?? '')); }}\n"
        f"      else if (!_allLandedResume || _reviseNoted || _revisedRows) {{ try {{ await {remaining_call}; }} catch (e) {{ const _m = String((e && e.message) ?? e); "
        "if (_USAGE_LIMIT_RE.test(_m)) { _haltOnUsageLimit(e); } else { log('ledger-remaining leg threw: ' + _m); } } }\n"
        "    }"
    ]
    b.extend(verify_lines)
    b.append(no_target)
    b.append("  if (!_halted && _manifest && !_manifest.error) {")
    b.append(
        "    const _reconciled = [];\n"
        f"    const _designPrefix = {json.dumps(_DESIGN_ANCHOR_PREFIX)};\n"
        "    const _normaliseLedger = () => {\n"
        "    for (const i of (_ledger?.items ?? [])) {\n"
        "      if (!i || i.status !== 'met' || !String(i.anchor ?? '').startsWith(_designPrefix)) continue;\n"
        "      const _mb = (Array.isArray(i.met_by) ? i.met_by : [i.met_by]).some((m) => typeof m === 'string' && m.trim() && m.trim().toLowerCase() !== 'none');\n"
        "      if (!_mb) { i.status = 'not_met'; i.essential = true; "
        "i.remaining = ['met_by: the senior-front-end review or ratification file of the design board']; }\n"
        "    }\n"
        "    if (_preHaltCredit) {\n"
        f"      const _blk = _gate.{RESUME_LEDGER_KEY}.blocking ?? [];\n"
        f"      const _creditMatch = {CREDIT_MATCH_JS};\n"
        f"      const _sinceCredit = {SINCE_HALT_CREDIT_JS};\n"
        f"      const _sinceHalt = [...(_gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_SINCE_HALT_SHAS_FIELD} ?? []), ..._checkpointShas];\n"
        "      for (const i of (_ledger?.items ?? [])) {\n"
        "        if (!i || i.status !== 'met' || !_blk.includes(i.id)) continue;\n"
        f"        const _rec = ((_gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_REMAINING_FIELD} ?? {{}})[i.id]) ?? {{}};\n"
        f"        const _known = [...(_gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_NOTES_FIELD} ?? []), String(_rec.evidence ?? ''), "
        "String(Array.isArray(_rec.remaining) ? _rec.remaining.join(' ') : (_rec.remaining ?? ''))].join(' ');\n"
        "        if (!_creditMatch(i.met_by, _known) && !_sinceCredit(i.met_by, _sinceHalt, "
        f"_manifest?.review_declared_paths ?? [], _footprintReviewed && {WAVE_NOT_RUN_VAR}.length === 0)) {{ "
        "i.status = 'not_met'; i.essential = true; "
        "i.remaining = ['met_by: the commit sha that carries the credited work']; }\n"
        "      }\n"
        "    }\n"
        "    for (const i of (_ledger?.items ?? [])) {\n"
        "      if (!i || i.status !== 'not_met' || !Array.isArray(i.remaining) || !i.remaining.length) continue;\n"
        "      const rest = i.remaining.filter((a) => !((_manifest.doc_writes ?? []).includes(a) "
        "&& (_docs?.doc_paths ?? []).includes(a)));\n"
        "      if (rest.length === i.remaining.length) continue;\n"
        "      if (rest.length) i.remaining = rest; else { _reconciled.push(i.id); i.status = 'met'; delete i.remaining; }\n"
        "    }\n"
        "    };\n"
        "    _normaliseLedger();"
    )
    if judged:
        v = _emit._FALSIFIER_RESULT_VAR
        b.append(
            "    const _reconcileJudged = () => {\n"
            f"    if ({v}?.status === 'not_met' && (_ledger?.items ?? []).length "
            "&& (_ledger.items).every((i) => i && i.status === 'met') "
            f"&& !String({v}.reason ?? '').startsWith('not wired up: ') "
            f"&& [undefined, 'pass', 'not_present'].includes({v}.falsifier?.verdict) "
            f"&& (!Array.isArray({v}.clauses) || {v}.clauses.every((c) => !c || c.status === 'met'))) "
            f"{{ {v} = {{ ...{v}, status: 'met', differs_from_baseline: null, "
            "reason: _reconciled.join(', ') ? 'met after reconcile: ' + _reconciled.join(', ') : 'met: every ledger item met', "
            "reconciled_from: 'not_met' }; }\n"
            "    };\n"
            "    _reconcileJudged();"
        )
    b.append(
        f"    const _postCommitActs = {json.dumps(list(POST_COMMIT_ACTS))};\n"
        f"    const _emContextIds = {json.dumps(_em_context_row_ids(repo_root, sizing_rel))};\n"
        "    const _ledgerBlocking = (l) => (l?.items ?? []).filter((i) => {\n"
        "      if (!i || _emContextIds.includes(i.id)) return false;\n"
        "      const rem = Array.isArray(i.remaining) ? i.remaining : [];\n"
        "      const status = i.status === 'not_met' && rem.length && rem.every((a) => _postCommitActs.includes(a)) "
        "? 'post_commit' : i.status;\n"
        "      const acts = status === 'post_commit' && i.status === 'not_met' ? rem : i.post_commit_acts;\n"
        "      if (status === 'drifted') return true;\n"
        "      if (status === 'not_met' && typeof i.withheld_by === 'string' && i.withheld_by) "
        "return !((_manifest?.gated ?? []).map((g) => g.id).includes(i.withheld_by) || _recoveryWithheld.includes(i.withheld_by));\n"
        "      if (status === 'not_met') return l.all_of_pm_ask === true || i.essential !== false;\n"
        "      if (status === 'post_commit') return !Array.isArray(acts) || !acts.length "
        "|| acts.some((a) => !_postCommitActs.includes(a));\n"
        "      return false;\n"
        "    });"
    )
    if known_arm != ARM_XS:
        b.append(REOPEN_PLAN_JS)
        b.append(
            _ledger_recover_js(
                head=head, anchor=anchor, session_tail=session_tail, record_dir=record_dir, ledger_rel=ledger_rel,
                run_id=run_id, judged=judged, test_var=_emit._TEST_RESULT_VAR,
            )
        )
    # `out_of_scope` accepts only register rows still open at stage; any other blocking id is named
    # apart, with the ledger re-check as its remedy.
    b.append(
        f"    const _rulable = _manifest?.rulable_ids ?? _gate.{RESUME_LEDGER_KEY}?.rulable_ids ?? [];\n"
        "    // An empty register refuses only when nothing was minted: minted manifest ids outrank a pre-mint resume verdict.\n"
        f"    const _ledgerIds = _manifest?.ledger_item_ids?.length ? _manifest.ledger_item_ids : (_gate.{RESUME_LEDGER_KEY}?.ledger_item_ids ?? _manifest?.ledger_item_ids ?? null);\n"
        f"    const _withheldIds = _gate.{RESUME_LEDGER_KEY}?.withheld_ids ?? _manifest?.withheld_ids ?? null;\n"
        "    const _allRuledOut = Array.isArray(_ledgerIds) && !_ledgerIds.length "
        "&& Array.isArray(_withheldIds) && _withheldIds.length > 0;\n"
        "    const _emptyVerdict = !!_ledger && (_ledger.items ?? []).length === 0 && !_allRuledOut;\n"
        "    const _owedIds = _emptyVerdict && Array.isArray(_ledgerIds) ? _ledgerIds : [];\n"
        "    const _blkIds = (_emptyVerdict ? _owedIds : (_ledger ? _ledgerBlocking(_ledger) : []))"
        ".map((i) => (i && typeof i === 'object') ? i.id : i).filter(Boolean);\n"
        "    const _blkRulable = _blkIds.filter((id) => _rulable.includes(id));\n"
        "    const _blkForeign = _blkIds.filter((id) => !_rulable.includes(id));\n"
        f"    const _uncoveredPaths = _gate.{RESUME_LEDGER_KEY}?.{RESUME_LEDGER_LEG_FIELD}?.unwritable ?? [];\n"
        "    const _addTaskRemedy = _uncoveredPaths.length ? ' The remaining work names paths no plan row writes: ' "
        "+ _uncoveredPaths.join(', ') + '. Add a row first: coordinator-invoke plan.tasks.mutate verb add-task "
        "(params plan_path=' + (_planRel ?? '<the plan>') + ', task) with a unique id, a title and declared `writes` covering them; "
        "a re-fire alone re-runs only the rows already there.' : '';"
    )
    b.append(
        "    if (!_halted && _emptyVerdict && Array.isArray(_ledgerIds) && !_ledgerIds.length "
        "&& Array.isArray(_withheldIds) && !_withheldIds.length) "
        f"{{ _halted = {{ halted: {_lit(HALT_REFUSAL)}, "
        f"reason: {_lit(EMPTY_REGISTER_REASON + EMPTY_REGISTER_REFUSAL_HEAD)} + _sizingRel + {_lit(EMPTY_REGISTER_REFUSAL_TAIL)}, "
        f"sizing: _sizingRel, run_id: _runId, {HALT_PLAN_FIELD}: _planRel }}; }}"
    )
    b.append(
        "    const _owedById = new Map((_manifest?.owed ?? []).map((o) => [o.id, o]));\n"
        "    const _blkItems = (_emptyVerdict || !_ledger) ? [] : _ledgerBlocking(_ledger);\n"
        "    const _allOwed = _blkItems.length > 0 "
        "&& _blkItems.every((i) => Array.isArray(i.owed_by) && i.owed_by.length > 0 && i.owed_by.every((id) => _owedById.has(id))) "
        "&& _blockedChunks.length === 0 && _incompleteChunks.every((id) => _owedById.has(id));\n"
        "    const _owedNamed = [...new Set(_blkItems.flatMap((i) => i.owed_by ?? []))]"
        ".map((id) => id + ' (' + (_owedById.get(id)?.reason ?? '') + '): ' + (_owedById.get(id)?.detail ?? ''));"
    )
    b.append(
        "    if (!_halted && (!_ledger || _emptyVerdict || _ledgerBlocking(_ledger).length)) "
        "{ _halted = { halted: 'ledger-check', "
        "items: _emptyVerdict ? _owedIds.map((id) => ({ id, status: 'not_met' })) : (_ledger ? _ledgerBlocking(_ledger) : []), "
        f"conflicts: _ledger?.conflicts ?? [], ledger: {_lit(ledger_rel)}, sizing: _sizingRel, run_id: _runId, "
        f"{HALT_PLAN_FIELD}: _planRel, "
        "next_action: _allOwed ? 'Everything left is EM or operator work: ' + _owedNamed.join('; ') "
        "+ '. Perform each, record it (em-performed: plan.tasks.mutate verb em-discharge; operator: verb resolve, "
        "disposition coded, disposition_ref <sha>), then refire to re-judge ' + _blkIds.join(', ') + '.' "
        "+ (_blkRulable.length ? ' Run coordinator-invoke workflow.revise with run_id ' + _runId "
        "+ ' with out_of_scope: [' + _blkRulable.join(', ') + '] for the rows ruled out; `verbatim` carries the PM words ruling them out, recorded as the ruling and adding no row.' "
        f"+ ' If the halt commit landed every row, close it after the revise: ' + {_lit(_HALTED_ROUTE_OP)} + ' {{run_id: ' + _runId + ', sha: <the halt commit>}}.' : '') "
        ": ((_blkIds.length ? 'Ledger rows ' + _blkIds.join(', ') + (_emptyVerdict ? ' were owed a verdict and the verify leg returned none' : '') + ' block the commit.' "
        ": !_ledger ? 'The verify leg returned no items list, so the ledger holds no verdict: the refire re-runs the verify leg.' "
        ": 'The verify leg returned an empty items list and no ledger row ids are on record to hold it against: the refire re-runs the verify leg.') "
        "+ (_blkRulable.length ? ' Run coordinator-invoke workflow.revise with run_id ' + _runId "
        "+ ' with out_of_scope: [' + _blkRulable.join(', ') + '] for the rows ruled out; `verbatim` carries the PM words ruling them out, recorded as the ruling and adding no row. The re-fire resumes at the ledger check.' "
        f"+ ' If the halt commit landed every row, close it after the revise: ' + {_lit(_HALTED_ROUTE_OP)} + ' {{run_id: ' + _runId + ', sha: <the halt commit>}}.' : '') "
        "+ (_blkForeign.length ? ' ' + _blkForeign.join(', ') + ' are not in the stage manifest rulable_ids: re-run the ledger check. "
        f"A ruling that is not yet a row is recorded with workflow.revise run_id ' + _runId + ' verbatim + from + {CLAIM_PARAM}: true, which claims it and resumes at the ledger check.' : '') "
        "+ _addTaskRemedy "
        "+ (_blkIds.length ? ' Re-fire the same sizing: it executes the remaining work of the rows it keeps, briefed with the revise notes, '"
        "+ 'and work committed at or before the halt meets a row when the verdict met_by names that commit.' : '')) }; }"
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
        + "(_commit && _commit.committed === true && (_incompleteChunks.length > 0 || _blockedChunks.length > 0)) "
        "? 'landed-rows-open' : 'delivery verdict FAIL', "
        f"revise: {_lit(f'{_INVOKE} workflow.revise (run_id; verbatim for a PM requirement, note for free text)')}, "
        f"refire: {_refire_js(sizing_rel)} }});"
    )
    stopped_na = (
        f"(String(_halted?.halted ?? '') + String(_halted?.reason ?? '')).includes({_lit(XS_MISROUTED_CODE)}) "
        f"? {XS_MISROUTED_NEXT_ACTION_JS} : {REVISE_NEXT_ACTION_JS}"
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
        f"  if (_recoveryWithheld.length && _nextAction && _nextAction.params) "
        f"{{ _nextAction.params.{RECOVERY_WITHHELD_PARAM} = [..._recoveryWithheld]; }}"
    )
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
        "...(i.remaining ? { remaining: i.remaining } : {}), "
        f"...(i.{GAPS_CLOSED_FIELD} ? {{ {GAPS_CLOSED_FIELD}: i.{GAPS_CLOSED_FIELD} }} : {{}}) }})) }}; }}"
    )
    b.append(
        f"  if (_nextAction.params && _resumeLedger) {{ _nextAction.params.{TERMINAL_RESUME_LEDGER_PARAM} = "
        f"{{ run_id: _runId, {RESUME_LEDGER_HALT_SHA_FIELD}: _gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_HALT_SHA_FIELD} ?? null, "
        f"blocking: _gate.{RESUME_LEDGER_KEY}.blocking ?? [], "
        f"{RESUME_LEDGER_NOTES_FIELD}: _gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_NOTES_FIELD} ?? [], "
        f"{RESUME_LEDGER_REMAINING_FIELD}: _gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_REMAINING_FIELD} ?? {{}} }}; "
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
    b.append("  _refireNextAction();")
    b.append(
        f"  return {{ arm: _gate.arm, {DESIGN_ROUTE_KEY}: _gate?.{DESIGN_ROUTE_KEY} ?? null, {_ROUTE_VERDICT_KEY}: _gate?.{_ROUTE_VERDICT_KEY} ?? null, sizing: _sizingRel, plan: _planRel, run_id: _runId, "
        "split: _splitExit ? _splitExit.split : null, fire: _splitExit ? _splitExit.fire : "
        f"(_halted && _halted.halted === {_lit(HALT_RESEARCH)} && Array.isArray(_halted.fire) ? _halted.fire : "
        "(_commit?.close?.siblings?.released ?? [])), "
        f"manifest: {_lit(manifest_rel)}, rows: (_manifest?.rows ?? []).map((r) => r.id), "
        "incomplete: _incompleteChunks, "
        "withheld: ((_manifest && !_manifest.error) ? (_manifest.gated ?? []) : []).map((g) => ({ id: g.id, owner_repo: g.owner_repo ?? '', closure_key: g.closure_key ?? null })), "
        "owed: ((_manifest && !_manifest.error) ? (_manifest.owed ?? []) : []).map((o) => ({ id: o.id, reason: o.reason ?? '', detail: o.detail ?? '' })), "
        f"owed_next_action: {_owed_handback_js(sizing_rel)} || _haltNextActionText() || null, "
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
                *((LEDGER_FIX_PHASE,) if known_arm != ARM_XS else ()),
                *(("commit",) if in_run_commit else ()),
            ],
        ),
        chatty=bool((plan_blitz_args or {}).get("chatty")),
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


def _finish_script(
    b: list[str], manifest_rel: str, description: str, phase_titles: list[str], chatty: bool = False
) -> str:
    """The script text from its composed body lines, refused over the Workflow runner's byte cap."""
    meta = with_fanout(_emit._meta_block("warp-ask", description, phase_titles, chatty=chatty), composed_fanout(b))
    script = (
        f"{_emit._NODE_CHECK_DOES_NOT_APPLY_COMMENT}\n{meta}\n"
        f"{ASK_MANIFEST_MARKER}{manifest_rel}\n"
        + "\n".join(b)
        + "\n"
    )
    script = _prompt_fragments.hoist_repeated_fragments(script, anchor=ASK_MANIFEST_MARKER)
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
#: Appended to the verify prompt of a memo-close-only run; `_memoCloseOnly` gates it.
_MEMO_CLOSE_CLAUSE_JS = (
    "(_memoCloseOnly ? ' MEMO-CLOSE: no executor ran; the source memo sha reply and resolve are paid by the "
    "close-out memo-lifecycle leg after commit, so a requirement row whose only remaining acts are sha_reply "
    "and lifecycle acts is not_met with remaining naming them. Memo rows: ' + "
    f"JSON.stringify(_manifest.{MEMO_CLOSE_KEY}) + '.' : '')"
)
#: Lead-in of the refire prompts' per-row halt remaining lines; each line is a gap that is still true.
_GAPS_OPEN_LEAD = "Gaps still open, by row (each line is still true; close it, never pin it): "
#: Tells the resume ledger judge how a met blocking row proves each halt gap line closed.
_GAPS_CLOSED_INSTRUCTION = (
    f" On every blocking row you judge met, return {GAPS_CLOSED_FIELD} with one {{gap, how}} per halt gap line: "
    "gap copied exactly, how naming the file or behaviour that makes it no longer hold. "
    "A row whose diff asserts a gap line's state is not_met."
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
    "landed work satisfies its register text. ' : _preHaltCredit ? 'A blocking row is met when the work at HEAD, "
    "including commits before the halt commit, satisfies its register text as the revise notes rule it; its "
    "met_by must name the commit sha(s) carrying that work, which may be a checkpoint this run made since the halt. ' : 'A blocking row is met on work changed since the halt "
    f"commit ' + (_gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_HALT_SHA_FIELD} ?? '(unknown)') + ', or on work at HEAD committed at or before it when met_by names that commit sha. ') + '"
    f"Revise notes: ' + JSON.stringify(_gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_NOTES_FIELD} ?? []) + "
    "'. Return an item for each of them and no other item. Halt gap lines, by row: ' + "
    f"JSON.stringify(_gate.{RESUME_LEDGER_KEY}.{RESUME_LEDGER_REMAINING_FIELD} ?? {{}}) + ' ' + {json.dumps(HALT_GAP_RULE)} + "
    f"{json.dumps(_GAPS_CLOSED_INSTRUCTION)} "
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
    `dispatch.land_run`'s, so the prompt carries none of the tail's own results.
    """
    note_rel = f"{DELIVERY_NOTE_DIR}/{run_id}.md"
    from coordinator_core.ops.dispatch_emit.reach_claims import REACH_CLAIM_FORM

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
        "ask outranks it. A `DELIVERY*.md` among the declared doc writes is the delivery note the ask "
        "named: write it at that path with the same content as (b), and do not leave it to the EM. ",
        f"(b) Write the delivery note at {note_rel}: every requirements-ledger item ({RECORD_DIR_ROOT}/"
        f"{run_id}/{LEDGER_VIEW_FILE}) with what the change did for it, what shipped (files "
        "and why), each review finding and how it was resolved, and every decision "
        "or deviation taken in-run with its reason -- read them off the row briefs under ",
        f"{RUN_DIR_ROOT}/{run_id}/briefs",
        ", which are never committed. " + REACH_CLAIM_FORM + " Never edit a plan, sizing or other process record to carry "
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
    git-commit-agent running `dispatch.land_run` with the run token. The docs were written
    earlier, by the verify agent. The commit lands the product plus the run's
    run_artifacts candidate set; the docs stage's `doc_paths` ride the params block.
    A landed commit turns `_nextAction` to the close-job record the reply carries: the EM has nothing to run
    only when its `fire` list (the sibling slices the close released) is empty. When
    `close.siblings.blocked_rows` is non-empty the record is `refire_after_sibling` instead: `op` is the
    submit workflow name, `params.blocked_on` names the blocking sibling runs and `params.refire` the call.

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
    # Trap: the commit op can return a close block keyed to a sibling run; the emitted script
    # re-keys it to its own script basename so the reply never reports another run's close.
    b.append(
        f"    const _ownCloseKey = String(_nextAction.params.script_path ?? {_lit(script_path or '')})"
        ".replace(/\\\\/g, '/').split('/').pop().replace(/\\.[^.]*$/, '');"
    )
    b.append(f"    try {{ _commit = await {commit_call}; }} catch (e) {{ _haltOnUsageLimit(e); }}")
    b.append(
        "    if (_ownCloseKey && _commit && _commit.committed === true && _commit.close "
        "&& typeof _commit.close === 'object' && _commit.close.run_id !== _ownCloseKey) { "
        "_commit = { ..._commit, close: { ..._commit.close, run_id: _ownCloseKey, "
        "relayed_run_id: _commit.close.run_id ?? null } }; }"
    )
    b.append(
        "    if (_commit && _commit.committed === true) { const _blockedRows = _commit.close?.siblings?.blocked_rows ?? [];"
    )
    b.append(
        "      for (const _uk of Object.keys(_commit.incomplete_reasons ?? {})) "
        "{ if (!_incompleteChunks.includes(_uk)) _incompleteChunks.push(_uk); }"
    )
    b.append(
        "      if (_blockedRows.length > 0) { _nextAction = { kind: 'refire_after_sibling', op: "
        + _lit(SUBMIT_WORKFLOW_NAME)
        + ", params: { run_id: _runId, sizing: _sizingRel, "
        "blocked_on: _blockedRows.map((r) => ({ row: r.row, run_id: r.run_id, label: r.label, status: r.status })), "
        "refire: _blockedRows[0].refire, close: _commit.close ?? null, "
        "fire: _commit.close?.siblings?.released ?? [] } }; }"
    )
    b.append(
        "      else if (_incompleteChunks.length > 0 || _blockedChunks.length > 0) { "
        "_nextAction = { kind: 'revise_and_refire', op: 'workflow.revise', params: { run_id: _runId, "
        "note: 'landed with rows open: ' + [...new Set([..._incompleteChunks, ..._blockedChunks])].join(', ') }, "
        "close: _commit.close ?? null, fire: _commit.close?.siblings?.released ?? [] }; "
        "_reviseAndRefire = _mkRevise(); }"
    )
    b.append(
        "      else { _nextAction = { kind: 'close_job', op: null, "
        "params: { run_id: _runId, close: _commit.close ?? null, fire: _commit.close?.siblings?.released ?? [] } }; } }"
    )
    b.append(
        "    else { if (!_halted) _halted = { halted: 'commit', refused: _commit?.refused ?? null, "
        "already_landed: _commit?.already_landed ?? null, "
        "error: _commit?.error ?? null, blob_drift: _commit?.blob_drift ?? null, "
        "plan: _planRel ?? null, sizing: _sizingRel ?? null }; " + _NO_CHANGE_HALT_JS +
        "if (_incompleteChunks.length === 0) { _nextAction.params.halted = _halted; _reviseAndRefire = null; } "
        f"else {{ _nextAction = {REVISE_NEXT_ACTION_JS}; _reviseAndRefire = _mkRevise(); }} }}"
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
        "blob_drift: _commit?.blob_drift ?? null, sizing: _sizingRel, run_id: _runId }; " + _NO_CHANGE_HALT_JS + "}",
        "  }",
    ]


def _commit_stage_call(
    *, head: str, anchor: str, repo_root: str, run_id: str, script_path: Optional[str] = None
) -> str:
    """The `commit` agent call (git-commit-agent, haiku) running `dispatch.land_run`.

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
        f"prefix, no edit to it. `{LAND_RUN_OP}` derives the paths from the run's commit "
        "request, so no `paths` array is owed.\n\n",
        f"coordinator-invoke {LAND_RUN_OP} --params-file {params_file.as_posix()} --repo {Path(repo_root).as_posix()}",
        "\n\nThe engine reads the params block below. Do not copy, edit or pass it.\n\n",
        "js:_terminalCommitParamsBlock({ ..._nextAction.params, " + _TARGETED_TESTS_JS + " "
        f"script_path: _nextAction.params.script_path ?? {_lit(script_path or '')}, "
        "run_id: _runId, run_token: _RUN_TOKEN, "
        f"doc_paths: {_DOC_PATHS_JS} }})",
        f"\n\n{LAND_RUN_OP} enumerates every file this run authored and lands it; you run the "
        "one command and return its reply.\n\nReturn from its JSON reply committed, sha, already_landed, error, refused, "
        f"sizing_shipped, close, fanout, siblings, run_artifacts, run_artifact_residue and {_RESIDUE_REPLY_KEY}, verbatim (null "
        f"where absent). A non-empty {_RESIDUE_REPLY_KEY} is returned with \"{_RESIDUE_DIRECTIVE}\". Run nothing else.",
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
    """`async function _commitHaltRecord()`: a git-commit-agent runs `dispatch.land_run` in
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
        f"coordinator-invoke {LAND_RUN_OP} --params-file {params_file.as_posix()} --repo {Path(repo_root).as_posix()}",
        "\n\nThe engine reads the params block below. Do not copy, edit or pass it.\n\n",
        f"js:_terminalCommitParamsBlock({{ script_path: {_lit(script_path or '')}, run_id: _runId, "
        f"run_token: _RUN_TOKEN, {HALTED_RECORD_PARAM}: _halted, {_CONSUMED_RETURNS_PARAM}: _consumedReturns, "
        f"doc_paths: {_DOC_PATHS_JS}, "
        + _TARGETED_TESTS_JS + " "
        "...(_nextAction && _nextAction.kind === 'terminal_commit' && _nextAction.params && _nextAction.params.inline_review "
        "? { run_outcome: { inline_review: _nextAction.params.inline_review, "
        "incomplete_chunks: _nextAction.params.incomplete_chunks ?? [], "
        "landed_chunks: _nextAction.params.landed_chunks ?? [], "
        "ledger_verdict: _nextAction.params.ledger_verdict ?? null, "
        "plan_path: _nextAction.params.plan_path ?? _planRel ?? null } } : {}) })",
        f"\n\n{LAND_RUN_OP} enumerates every file this run authored and lands it; you run the "
        "one command and return its reply.\n\nReturn from its JSON reply committed, sha, already_landed, error, refused, "
        f"run_artifacts, run_artifact_residue and {_RESIDUE_REPLY_KEY}, verbatim (null where absent). "
        f"A non-empty {_RESIDUE_REPLY_KEY} is returned with \"{_RESIDUE_DIRECTIVE}\". Run nothing else.",
    )
    call = _agent(
        prompt, label="halt-commit", phase="commit", agent_type=_COMMIT_AGENT_TYPE,
        schema=_COMMIT_SCHEMA, effort=_MECHANICAL_EFFORT, run_id=run_id,
    )
    return f"  async function _commitHaltRecord() {{ _refireNextAction(); return await {call}; }}"


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
