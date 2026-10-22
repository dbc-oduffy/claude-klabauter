"""Pure JS builders for a pulled plan's resumable halt and its over-threshold split proposal.

Leaf module: it never imports ask_compose, so the composer can import it at load.
"""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit.ask_fanout_contract import FANOUT_OP
from coordinator_core.ops.workflow_scaffold import _js_string_literal

NEXT_ACTION_KIND = "rule_and_refire"
REVISE_OP = "workflow.revise"

_INVOKE = '"${COORDINATOR_SETTINGS_HOME:-$HOME/.coordinator-claude-settings}/bin/coordinator-invoke"'


def rule_and_refire_next_action_js(plan_js: str, rulings_js: str, refire_js: str) -> str:
    """JS object literal `{kind, plan, rulings, revise, refire}`; each argument is a JS expression.

    `revise` instructs one `workflow.revise` note per ruling id, each led by its `J<n>` id so the
    resumed run can tell a ruling from an in-flight revision of the ask.
    """
    revise = (
        "{ op: " + _js_string_literal(REVISE_OP)
        + ", note_per_ruling: (" + rulings_js + ").map((r) => r.id)"
        + ", call: " + _js_string_literal(f"{_INVOKE} {REVISE_OP} (run_id; one note per ruling id, each starting with its J<n> id: ")
        + " + (" + rulings_js + ").map((r) => r.id).join(', ') + ')' }"
    )
    return (
        "{ kind: " + _js_string_literal(NEXT_ACTION_KIND)
        + ", plan: " + plan_js
        + ", rulings: " + rulings_js
        + ", revise: " + revise
        + ", refire: " + refire_js + " }"
    )


def pull_split_prompt(plan_js: str, sizing_js: str) -> str:
    """Prompt text (a JS string-concatenation expression) for the pull split-proposal agent.

    `plan_js` and `sizing_js` are JS expressions for the pulled plan path and the sizing path.
    The reply shape is the xl-exit agent's `{pick: 'split', slices, ...}`, consumed unchanged by
    `_append_split_stage`.
    """
    lit = _js_string_literal
    confirm = "(typeof args !== 'undefined' && !!args && args.confirm_split === true)"
    return (
        lit(
            "phase: pull-split\n\nThe plan-blitz pull drew more blocking findings than one baton "
            "carries. Read the pulled plan at "
        )
        + " + " + plan_js
        + " + " + lit(
            " and partition its `## Tasks` spine rows into slices whose `writes` are disjoint, "
            "so each slice ships alone as its own ask. Return pick 'split' and slices: "
            "[{label, ask, rows, writes?}], where ask is that slice's full request, rows lists "
            "the spine row ids it carries, and writes its repo-relative file paths. Every spine row "
            "goes in exactly one slice; none is left out or repeated, and rows that share a write "
            "stay in one slice. Delete no row and no premise to shrink a slice. "
        )
        + " + (" + confirm + " ? " + lit(
            "confirm_split is set: return the slices only and run nothing. "
        ) + " : " + lit(
            f"Then run `{_INVOKE} {FANOUT_OP} '"
            "{\"slices\": <your slices, as JSON>, \"parent_sizing\": \""
        ) + " + " + sizing_js + " + " + lit(
            "\"}'` (a single quote inside the JSON is written '\\''), and return its JSON reply as "
            "parent_id, runs and fire (error empty on success; if it replies `{\"error\": ...}`, "
            "return that message as `error`, with parent_id empty and runs and fire empty). "
        ) + ")"
    )
