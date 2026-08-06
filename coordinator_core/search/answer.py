"""coordinator_core.search.answer -- the seam that turns a grep-shaped Bash command
into an answer, or declines cleanly.

Purpose: single entry point for the PreToolUse(Bash) hook. Given a command string,
either return the exact text the command would have printed (plus a short provenance
note), or return None so existing guard behaviour proceeds untouched.

The contract is deliberately two-valued. There is no "partial answer" and no "answer
with a warning that the result may be wrong" -- an answer is faithful or it is not
offered. See `engine.Unanswerable` for why that asymmetry is the whole design.

Negative-spec:
  - Does NOT decide the hook's verdict shape. It returns text; the caller chooses the
    envelope. Keeping the policy decision out of here means this module stays testable
    without a harness payload.
  - Does NOT mutate anything on disk. Read-only by construction.
  - Does NOT fall back to a rewrite. Declining is the fallback.
"""

from __future__ import annotations

import os
from typing import List, Optional

from coordinator_core.bash_guards._command_tokenizer import (
    segments_from_tokens_with_pipe_flag as _segments,
)
from coordinator_core.bash_guards._shape_classifier import (
    Shape as _Shape,
    classify_command as _classify,
)
from coordinator_core.search.engine import (
    GREP_FAMILY,
    MAX_RENDER_BYTES,
    AnswerPlan,
    Unanswerable,
    build_stage,
    parse_grep_segment,
    run,
)


def plan_for(cmd: str) -> Optional[AnswerPlan]:
    """Build an AnswerPlan for `cmd`, or None if it is not answerable in-process."""
    if not cmd:
        return None
    classification = _classify(cmd.replace("\r", ""))
    if classification.tokens is None:
        return None
    if not classification.has_shape(_Shape.GREP_VIA_BASH):
        return None
    try:
        segments = _segments(classification.tokens)
    except Exception:
        return None
    if not segments:
        return None

    first_tokens, piped_into = segments[0]
    if piped_into or not first_tokens:
        return None  # `<cmd> | grep ...` -- the input does not exist until that runs
    if os.path.basename(first_tokens[0]) not in GREP_FAMILY:
        return None
    # Every later segment must be pipe-connected. A `;`/`&&`-joined segment is separate
    # work sequenced around the grep, and answering only the grep half would drop it.
    if not all(piped for _tokens, piped in segments[1:]):
        return None

    try:
        spec = parse_grep_segment(first_tokens)
        stages = [build_stage(tokens) for tokens, _piped in segments[1:]]
    except Unanswerable:
        return None
    return AnswerPlan(spec=spec, stages=stages)


def answer(cmd: str, cwd: str = ".") -> Optional[str]:
    """Return the rendered answer for `cmd`, or None to decline."""
    plan = plan_for(cmd)
    if plan is None:
        return None
    try:
        result = run(plan.spec, cwd=cwd, stop_after=plan.early_stop)
    except Unanswerable:
        return None
    except OSError:
        return None

    # A truncated search feeding a stage that aggregates or reads from the end produces
    # a confidently wrong number. Decline rather than caveat it -- the agent has no
    # reason to re-check a plausible-looking count.
    if result.truncated and not plan.tolerates_truncation:
        return None

    lines: List[str] = result.lines
    for stage in plan.stages:
        try:
            lines = stage.apply(lines)
        except Unanswerable:
            return None

    body = "\n".join(lines)
    clipped = False
    if len(body) > MAX_RENDER_BYTES:
        body = body[:MAX_RENDER_BYTES].rsplit("\n", 1)[0]
        clipped = True

    return _render(body, result, plan, clipped)


def _render(body: str, result, plan: AnswerPlan, clipped: bool) -> str:
    """Wrap the command's own output in a short, honest provenance note.

    The output comes FIRST and verbatim: the agent asked a question and this is the
    answer to it. Provenance goes underneath, where it informs the next invocation
    without displacing the result.
    """
    note = (
        "[searched in-process: %d file(s), %.0fms, no subprocess spawned]"
        % (result.files_scanned, result.elapsed_ms)
    )
    if result.truncated or clipped:
        note += (
            "\n[truncated at the %s -- narrow it with --include='*.py' or a tighter "
            "path to see the rest]" % (result.cap_hit or "render cap")
        )
    if not body:
        return "(no matches)\n\n" + note
    return body + "\n\n" + note
