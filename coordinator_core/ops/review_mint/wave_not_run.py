"""Contract for the per-slot not-run list of a composed review wave.

A whole-diff review slot that resolves to null records one entry
``{slot, agent_type, reason}`` in the script-scope list named by ``WAVE_NOT_RUN_VAR``;
the wake digest carries that list as ``inline_review.wave_not_run``.
"""

from coordinator_core.ops.workflow_scaffold import _js_string_literal

#: Script-scope list (declared by the emitter, assigned `= []` by the review block).
WAVE_NOT_RUN_VAR = "_waveNotRun"

REJECTED = "rejected: "
NO_REVIEW = "returned no review"
SKIPPED = "skipped: "


def not_run_entry_js(slot_index_js: str, agent_type: str, reason_js: str) -> str:
    """JS statement pushing one not-run entry.

    `slot_index_js` and `reason_js` are JS expressions spliced verbatim; `agent_type`
    is a plain string, emitted as a quoted literal.
    """
    return (
        f"{WAVE_NOT_RUN_VAR}.push({{ slot: {slot_index_js}, "
        f"agent_type: {_js_string_literal(agent_type)}, reason: {reason_js} }});"
    )
