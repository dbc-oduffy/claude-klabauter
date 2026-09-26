"""
coordinator_core.hooks.nudge_harness_directive_dispatch — Stop-hook advisory op.

Purpose: Catch the failure mode that four rounds of ratified prose have not
discharged — an EM ending its turn having declined to dispatch, having asked
the PM for permission to dispatch, having ASCRIBED a dispatch restriction to
the PM as the PM's own standing instruction, or having asked the PM's
permission to commit/stage work it already owns — because it read a
*conditional* harness system-prompt line as an unconditional bar written by
its operator. Four tells in total (A-D below); Tell D exists because the
operator's own global doctrine names committing and dispatching in the same
breath as things the harness makes feel gated when they are not — verbatim:
"the human needs to give explicit permission before doing even simple,
non-destructive things like committing or dispatching an agent; that may
feel like it comes from the human, but it does not — it's from the general
Anthropic harness."

This op is a LATE backstop, not the fix: it fires at end-of-turn, after the
misattribution has already been spoken. Per DR-123 (`docs/decisions/
DR-123-the-per-turn-dispatch-restatement-is-ret.md`, DoE-claude), the work of
countering the harness line is now split three ways: the binding statement
lives at SessionStart (`coordinator/snippets/agent-role-em.md` § How You
Dispatch); the mid-conversation salience carrier is the once-per-session
UserPromptSubmit line (`_DISPATCH_DEFAULT_LINE` in `runtime-tripwire-em-
check.py`, coordinator/DoE-side, not this module); and this op is the third,
late backstop. The two tells this op originally shipped with (citing the
directive; asking permission) both missed the actual 2026-08-02 failure
shape — misattribution to the PM; Tell C below closes that gap. Tell D
closes a sibling gap observed the same day: the identical permission
reflex, aimed at committing instead of dispatching.

The harness line in question is a hardcoded constant in the Claude Code binary
(`tengu_heron_brook`, verified against v2.1.220), emitted as a pair::

    Do not call the AgentTool unless the user requested it
    Do not use workflows or deep-research unless the user requested it

It is gated on the MODEL (mid-conversation-system capability), not on any
settings.json key — there is no local or install-time setting that suppresses
it, which is precisely why an operator-side artifact has to exist.

Why a Stop hook rather than more doctrine: the harness block is delivered as a
*mid-conversation* system injection, so it sits near the live turn, while
coordinator's rebuttal (`docs/wiki/harness-directive-conflicts.md`,
`snippets/em-operating-doctrine.md` § Dispatch Is Encouraged, DR-082, DR-108)
lands once at SessionStart, tens of KB upstream. Recency wins arguments that
correctness does not. This op restores the balance by speaking at end-of-turn,
where the mistake is observable.

Design-as-offers: the message leads with the better alternative ("dispatch it"),
not with the violation, and names the explicit exit for a genuine
direction-class question. It fires AT MOST ONCE per session.

Negative-spec:
    - This op never fires on a subagent's Stop (agent_id present) — a dispatched
      worker holds no dispatch authority and must not be nudged to fan out.
    - It never fires when ``stop_hook_active`` is set: that Stop was itself
      caused by a hook block, and re-firing would wedge the session in a loop.
    - It is NOT a general "did the EM dispatch enough" auditor. It matches only
      the four observed tells; silence is the correct output for everything
      else.
    - Tell D (commit-permission) never fires on a genuine merge-to-main ask, a
      push/PR/remote (outward-facing) ask, a report of an already-completed
      commit, or a scoping question about which files to include — each is a
      correct ask/report, not this failure mode. See Tell D's own comment for
      the discriminators.
    - This module has no ``@register_op``-decorated async handler, unlike the
      dual-path convention established in ``nudge_em_code_dispatch.py``. That is
      deliberate, not an omission: Stop events are not routed through the IPC
      daemon path at all, so there is nothing for a daemon-side handler to
      register against. Transport here is the DoE-resident stdin/stderr shim
      (``op(payload)`` only) — see ``nudge_em_code_dispatch.py``'s own banner
      comment for the fuller rationale on why the two paths must not be
      conflated.

Spec backlink: docs/wiki/harness-directive-conflicts.md § Why prose alone has not held
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from coordinator_core._hook_envelope import context_only
from coordinator_core.lifecycle import git_common_dir


_TELL_CITES_DIRECTIVE = re.compile(
    r"\bAgent\s?Tool\b"
    r"|unless (?:the )?(?:user|you) (?:requested|asks? for|request(?:ed)?) it",
    re.IGNORECASE,
)

_TELL_ASKS_PERMISSION = re.compile(
    r"\b(?:want me|shall i|should i|would you like me|do you want me|ok(?:ay)? (?:for me )?)"
    r"[^.?!\n]{0,60}?"
    r"\b(?:dispatch|delegate|fan[- ]out|spawn|subagent|sub-agent)\b",
    re.IGNORECASE,
)

# Tell C — the EM ASCRIBING a dispatch restriction to the PM as the PM's own
#      this is what discriminates a RESTRICTION ("your rule against
#: Quantity nouns that make a nearby rule-noun a MEASUREMENT rather than an
_QUANTITY_WORDS = r"budget|count|limit|cap|quota|allowance|spend|overhead"

_POSSESSIVE_PM_ATTRIBUTION = re.compile(
    r"\byour\s+(?:standing\s+)?[^.?!\n]{0,60}?\b(?:instruction|rule|directive|order|policy)\b"
    r"|\bas you(?:'ve| have)?\s+instructed\b"
    r"|\byou(?:'ve| have)\s+instructed\b"
    r"|\byours\s+to\s+\w+"
    r"|\b(?:this\s+)?(?:session|conversation)\s+was\s+started\s+with\s+a\s+standing\s+\w+"
    r"|\b(?:this|the)\s+(?:session|conversation|turn)'s\s+(?:standing\s+)?"
    # rule-noun disqualifies the match, which is what catches the MODIFIER word
    r"(?:(?!" + _QUANTITY_WORDS + r")[^.?!\n]){0,40}?"
    r"\b(?:instruction|rule|directive|order|policy)\b"
    r"(?!\s+(?:" + _QUANTITY_WORDS + r"))",
    re.IGNORECASE,
)

_TELL_C_DISPATCH_TERM = re.compile(
    r"dispatch\w*|delegat\w*|fan[- ]?out\w*|spawn\w*|sub-?agent\w*|agent[-\s]?tool\w*",
    re.IGNORECASE,
)

_TELL_C_RESTRICTION_CUE = re.compile(
    r"\bdon'?t\b|\bdo not\b|\bdidn'?t\b|\bdid not\b"
    r"|\bdeclin\w*|\bheld\b|\bholding\b|\bavoid\w*|\brefrain\w*|\bwithhold\w*"
    r"|\bnot to\b|\bno\b|\bcannot\b|\bcan'?t\b|\bwon'?t\b|\bskip\w*",
    re.IGNORECASE,
)


def _tell_misattributes_to_pm(text: str) -> bool:
    for sentence in re.split(r"[.?!\n]+", text):
        if not sentence.strip():
            continue
        if not _POSSESSIVE_PM_ATTRIBUTION.search(sentence):
            continue
        if _TELL_C_DISPATCH_TERM.search(sentence) and _TELL_C_RESTRICTION_CUE.search(sentence):
            return True
    return False


# already owns. Modelled directly on Tell B (_TELL_ASKS_PERMISSION above): same
# (Commit Gate: only the EXECUTOR never commits) makes the commit step
# `_COMMIT_GATE_OR_OUTWARD_CUE` is a defensive backstop: a sentence naming
# A third negative, `_COMMIT_SCOPING_CUE`, catches the scoping question —
# which asks about the CONTENTS of a commit the EM is already going to make,
_TELL_ASKS_COMMIT_PERMISSION = re.compile(
    r"\b(?:want me|shall i|should i|would you like me|do you want me|ok(?:ay)? (?:for me )?)"
    r"[^.?!\n]{0,60}?"
    r"\b(?:commit|committing|stage|staging)\b",
    re.IGNORECASE,
)

_COMMIT_GATE_OR_OUTWARD_CUE = re.compile(
    r"\bmerge\w*\b|\bpush\w*\b|\bpr\b|\bremote\b|\bupstream\b",
    re.IGNORECASE,
)

_COMMIT_SCOPING_CUE = re.compile(
    r"\bwhich\b[^.?!\n]{0,20}\bfiles?\b"
    r"|\bwhat\b[^.?!\n]{0,20}\bfiles?\b"
    r"|\bleave\b[^.?!\n]{0,30}\bout\b"
    r"|\bout of scope\b"
    r"|\bshould\b[^.?!\n]{0,20}\binclude\b",
    re.IGNORECASE,
)


def _tell_asks_commit_permission(text: str) -> bool:
    """Return True iff `text` asks the PM's permission to commit/stage work
    the EM already owns, within any single sentence.

    Sentence-scoped, matching `_tell_misattributes_to_pm`'s shape. A match is
    suppressed when the same sentence also carries a merge/push/PR/remote cue
    (a genuine PM gate or outward-facing ask) or a scoping cue (a question
    about commit CONTENTS, not permission to commit).
    """
    for sentence in re.split(r"[.?!\n]+", text):
        if not sentence.strip():
            continue
        if not _TELL_ASKS_COMMIT_PERMISSION.search(sentence):
            continue
        if _COMMIT_GATE_OR_OUTWARD_CUE.search(sentence):
            continue
        if _COMMIT_SCOPING_CUE.search(sentence):
            continue
        return True
    return False


_META_DISCUSSION = re.compile(
    r"tengu_heron_brook|nudge-harness-directive|harness-directive-conflicts",
    re.IGNORECASE,
)

_NUDGE_MESSAGE = """\
[nudge] Dispatch/commit of your work is EM remit, not a permission ask.
[nudge] AgentTool is a hardcoded default, not your PM's, unless requested.
[nudge] Direction-class? Say so; fires once.
[nudge] Use instead: `coordinator/docs/wiki/harness-directive-conflicts.md`
"""


def _session_key(payload: dict) -> tuple[str, bool]:
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        safe = re.sub(r"[^A-Za-z0-9_-]", "", sid.strip())
        if safe:
            return safe, True
    return f"pid-{os.getpid()}", False


def _repo_root(payload: dict) -> str | None:
    cwd = payload.get("cwd") or os.getcwd()
    if not isinstance(cwd, str):
        return None
    probe = os.path.abspath(cwd)
    while True:
        if os.path.exists(os.path.join(probe, ".git")):
            return probe
        parent = os.path.dirname(probe)
        if parent == probe:
            return None
        probe = parent


def _sentinel_path(payload: dict) -> str | None:
    probe = _repo_root(payload)
    if probe is None:
        return None

    git_dir = _resolve_git_dir(os.path.join(probe, ".git"))
    if not git_dir:
        return None
    session_key, _has_true_sid = _session_key(payload)
    return os.path.join(
        git_dir, "coordinator-sessions", session_key,
        "harness-directive-nudge.fired",
    )


def _resolve_git_dir(dot_git: str) -> str | None:
    if os.path.isdir(dot_git):
        return dot_git
    try:
        with open(dot_git, "r", encoding="utf-8", errors="replace") as fh:
            pointer = fh.read().strip()
    except OSError:
        return None
    if not pointer.startswith("gitdir:"):
        return None
    target = pointer[len("gitdir:"):].strip()
    if not target:
        return None
    if not os.path.isabs(target):
        target = os.path.join(os.path.dirname(dot_git), target)
    return os.path.normpath(target)


def _claim_fire(sentinel: str | None) -> bool:
    if not sentinel:
        return True
    try:
        os.makedirs(os.path.dirname(sentinel), exist_ok=True)
        with open(sentinel, "x", encoding="utf-8", newline="\n") as fh:
            fh.write("1")
        return True
    except FileExistsError:
        return False
    except OSError:
        return True


def last_assistant_text(transcript_path: str) -> str:
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            if size > 512_000:
                fh.seek(size - 512_000)
                fh.readline()
            raw = fh.read()
        text = raw.decode("utf-8", errors="replace")
        lines = text.splitlines(keepends=True)
    except OSError:
        return ""

    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict) or entry.get("type") != "assistant":
            continue
        msg = entry.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            joined = "\n".join(p for p in parts if p)
            if joined.strip():
                return joined
            continue
    return ""


def _session_has_dispatched(payload: dict) -> bool:
    repo_root = _repo_root(payload)
    if repo_root is None:
        return False
    try:
        common_dir = git_common_dir(Path(repo_root))
    except Exception:
        return False
    session_key, _has_true_sid = _session_key(payload)
    dispatched = common_dir / "coordinator-sessions" / session_key / "dispatched-agents.txt"
    try:
        return dispatched.is_file() and dispatched.stat().st_size > 0
    except OSError:
        return False


def _final_message_text(payload: dict) -> str:
    """Return the text of the turn's final assistant message, or "".

    Prefers the harness-supplied ``last_assistant_message``, which the Stop
    payload schema documents as existing precisely so a hook need not read and
    parse the transcript (verified against the Claude Code binary, v2.1.220).
    That field is declared OPTIONAL, so the transcript scan below remains the
    compatibility fallback for harness versions that omit it — it is the
    fallback, never the primary path.
    """
    supplied = payload.get("last_assistant_message")
    spoken = supplied if isinstance(supplied, str) and supplied.strip() else ""

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return spoken
    if not spoken:
        spoken = last_assistant_text(transcript_path)

    asked = ask_user_question_text(transcript_path)
    if not asked:
        return spoken
    return f"{spoken}\n{asked}" if spoken else asked


def ask_user_question_text(transcript_path: str) -> str:
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            if size > 512_000:
                fh.seek(size - 512_000)
                fh.readline()
            raw = fh.read()
        lines = raw.decode("utf-8", errors="replace").splitlines()
    except OSError:
        return ""

    collected: list[str] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue
        if entry.get("type") == "user":
            break
        if entry.get("type") != "assistant":
            continue
        msg = entry.get("message")
        content = msg.get("content") if isinstance(msg, dict) else None
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            if block.get("name") != "AskUserQuestion":
                continue
            payload = block.get("input")
            if not isinstance(payload, dict):
                continue
            for question in payload.get("questions") or []:
                if not isinstance(question, dict):
                    continue
                for key in ("question", "header"):
                    val = question.get(key)
                    if isinstance(val, str) and val.strip():
                        collected.append(val)
                for option in question.get("options") or []:
                    if not isinstance(option, dict):
                        continue
                    for key in ("label", "description"):
                        val = option.get(key)
                        if isinstance(val, str) and val.strip():
                            collected.append(val)
    return "\n".join(collected)


def message_trips_tell(text: str) -> bool:
    if not text:
        return False
    if _META_DISCUSSION.search(text):
        return False
    return bool(
        _TELL_CITES_DIRECTIVE.search(text)
        or _TELL_ASKS_PERMISSION.search(text)
        or _tell_misattributes_to_pm(text)
        or _tell_asks_commit_permission(text)
    )


def op(payload: dict) -> dict | None:
    if not isinstance(payload, dict):
        return None
    if payload.get("stop_hook_active"):
        return None
    if payload.get("agent_id"):
        return None

    if os.environ.get("COORDINATOR_HARNESS_DIRECTIVE_NUDGE_OFF") == "1":
        return None

    sentinel = _sentinel_path(payload)
    if sentinel and os.path.exists(sentinel):
        return None

    if not message_trips_tell(_final_message_text(payload)):
        return None

    if not _claim_fire(sentinel):
        return None

    _, has_true_sid = _session_key(payload)
    message = _NUDGE_MESSAGE
    if not has_true_sid:
        message += (
            "\n[nudge] (sentinel is invocation-scoped: no session_id was"
            " present, so this nudge may repeat.)\n"
        )
    envelope = context_only("Stop", message)
    return {"message": envelope["hookSpecificOutput"]["additionalContext"]}


if __name__ == "__main__":  # pragma: no cover - manual probe path
    result = op(json.load(sys.stdin))
    if result:
        print(result)
