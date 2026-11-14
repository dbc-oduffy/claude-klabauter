"""
coordinator_core.hooks.record_askuserquestion_answer -- PostToolUse(AskUserQuestion)
hook op that records each answer the PM gave as a ``pm_turns.jsonl`` turn.

WHY: ``baton_pm_turns.append_turn`` was fed only by the UserPromptSubmit path, so a ruling
given as an AskUserQuestion answer (a publish go-live, a cloud opt-in) never reached the log a
G-EM reads to verify a PM verbatim. This op is the second feeder; ``append_turn`` stays the only
writer (it owns locking and turn numbering).

One turn per answered question: ``prompt`` is the answer text verbatim, ``referent.excerpt`` is
the question text plus its option labels. Reads ``tool_input.questions`` and
``tool_response.answers`` (question text -> answer) from the hook payload; every input comes from
the payload, never from this process's environment or cwd.

NEGATIVE SPEC: records nothing for a payload carrying ``agent_id`` (a subagent's question is not
the PM's), for a missing or empty answer, or for a payload with no ``session_id``. Never blocks
and never raises: the return is always ``no_advisory()``.
"""

from __future__ import annotations

from typing import Any, List, Optional

from coordinator_core.hooks._envelope import no_advisory, payload_of
from coordinator_core.ipc import register_op
from coordinator_core.ops import baton_pm_turns


def _option_labels(question: dict) -> List[str]:
    options = question.get("options")
    if not isinstance(options, list):
        return []
    labels = []
    for option in options:
        label = option.get("label") if isinstance(option, dict) else option
        if isinstance(label, str) and label.strip():
            labels.append(label.strip())
    return labels


def _answer_text(raw: Any) -> Optional[str]:
    """A string answer as-is; a multi-select list joined with ", "; anything else None."""
    if isinstance(raw, list):
        raw = ", ".join(item for item in raw if isinstance(item, str) and item)
    if isinstance(raw, str) and raw.strip():
        return raw
    return None


def _referent_excerpt(question_text: str, question: Optional[dict]) -> str:
    labels = _option_labels(question) if question else []
    if not labels:
        return question_text
    return f"{question_text}\nOptions: {'; '.join(labels)}"


@register_op("hooks.record_askuserquestion_answer")
def _handler(params: dict, repo_root=None) -> dict:
    """PostToolUse(AskUserQuestion): append one pm_turn per answered question.

    Returns ``no_advisory()`` on every path, recorded or not.
    """
    try:
        payload = payload_of(params)
        if payload.get("agent_id"):
            return no_advisory()
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return no_advisory()
        cwd = payload.get("cwd")
        cwd = cwd if isinstance(cwd, str) and cwd else None

        response = payload.get("tool_response")
        answers = response.get("answers") if isinstance(response, dict) else None
        if not isinstance(answers, dict):
            return no_advisory()

        tool_input = payload.get("tool_input")
        questions = tool_input.get("questions") if isinstance(tool_input, dict) else None
        by_text = {
            q["question"]: q
            for q in (questions if isinstance(questions, list) else [])
            if isinstance(q, dict) and isinstance(q.get("question"), str)
        }

        for question_text, raw in answers.items():
            answer = _answer_text(raw)
            if answer is None or not isinstance(question_text, str):
                continue
            referent = None
            if question_text.strip():
                referent = {"excerpt": _referent_excerpt(question_text, by_text.get(question_text))}
            baton_pm_turns.append_turn(answer, session_id=session_id, cwd=cwd, referent=referent)
    except Exception:
        pass
    return no_advisory()
