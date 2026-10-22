"""
coordinator_core.hooks.guard_turn_intent -- Stop-hook engine op, the EM
turn-intent step.

Purpose: engine port of the turn-intent step coordinator-content-repo folded into
`coordinator/hooks/scripts/guard-manufactured-blocker.py`
(`_turn_intent_step`, `_heuristic_turn_intent`, and `main`'s use of them). An
EM under Cockpit ends every turn with `<turn-intent>{"needs_pm":bool,"ask":"..."}</turn-intent>`;
this op records that tail to `<git-common-dir>/coordinator-sessions/<session>/turn-intent.json`
and blocks a missing tail at most `BLOCK_CAP` times (own durable counter, never
`stop_hook_active`), only in a session holding the EM role marker AND running
under Cockpit. After the cap the record is labelled `source: heuristic`
(label only, never a block). Helpers: `hooks.support.turn_intent`,
`hooks.support.cockpit_session`.

Own op, NOT composed into `hooks.stop_dispatch`'s fan-in: BLOCKING-class, the
same reason `guard_manufactured_blocker` is excluded. `stop_hook_active` does
not exit this step -- the second block lands on a re-entered stop.

Op contract: `params` carries the Stop payload (`session_id`, `cwd`,
`transcript_path`, `last_assistant_message`, `agent_id`, ...), wrapped as
`params["payload"]` or flat; `_envelope.payload_of` reads both. Returns
`deny("Stop", BLOCK_REASON)` when the tail is missing and the block arms,
else the once-per-session go-ask `post_advisory` (Group EM live, go-ask in the
last two sentences; a block wins), else `no_advisory()`. Fail-open on every error.

Intent text: `last_assistant_message` when non-empty, else the transcript's
final assistant text.

Heuristic parity: the needs_pm label uses DoE's `_fires` predicate
(negation within three words, status-line "waiting on you", quote, reported
speech, third-party clause), reproduced here rather than the sibling
guard's clause-split `_pattern_matches`, because the engine sibling's
predicate has diverged from the DoE source.

DoE source: coordinator/hooks/scripts/guard-manufactured-blocker.py
"""

from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from coordinator_core.hooks._envelope import deny, no_advisory, payload_of, post_advisory
from coordinator_core.hooks.guard_manufactured_blocker import (
    _HANDOFF_PATTERNS,
    _OWNERSHIP_CUE_PATTERNS,
    _POSSESSIVE_PATTERNS,
    _QUOTE_CHARS,
    _final_assistant_text,
    _has_fired_this_session,
    _mark_fired_this_session,
    _repo_root,
    _reported_speech,
    _split_sentences,
    _third_party_clause,
)
from coordinator_core.hooks.support import turn_intent as _turn_intent
from coordinator_core.hooks.support.cockpit_session import under_cockpit
from coordinator_core.hooks.support.git_common_dir import resolve_git_common_dir
from coordinator_core.hooks.support.session_hub import ensure_session_dir, session_id_is_real
from coordinator_core.ipc import register_op
from coordinator_core.machine_profile import apply_guard_level

_PATTERN_GROUPS = (_HANDOFF_PATTERNS, _POSSESSIVE_PATTERNS)

_NEGATION_BEFORE = re.compile(
    r"\b(?:nothing|no|not|never|none|\w+n't)\b(?:\s+[\w']+){0,3}\s*$", re.IGNORECASE
)
_PROGRESSIVE_WAIT_RE = re.compile(r"\bwaiting\s+on\s+you\b", re.IGNORECASE)
_HANDOFF_REQUEST_RE = re.compile(
    r"\?|\b(?:decide|decision|approve|approval|authori[sz]\w*|confirm|go-ahead|sign-off|your\s+(?:call|input|go)"
    r"|please|need\s+you|can\s+you|could\s+you|let\s+me\s+know|tell\s+me|should\s+I|want\s+me)\b",
    re.IGNORECASE,
)
_SENTENCE_BREAK_RE = re.compile(r"[.!?\n]")

_GO_ASK_PATTERNS = [
    re.compile(
        r"\b(?:waiting|awaiting|wait)\s+(?:on|for)\s+your\s+"
        r"(?:go|go-ahead|say-so|word|nod|ok|okay|green\s*light)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bon\s+your\s+(?:go|go-ahead)\b", re.IGNORECASE),
    re.compile(r"\bsay\s+(?:go|the\s+word)\b", re.IGNORECASE),
    re.compile(r"\bgive\s+(?:me\s+)?the\s+(?:go|go-ahead|green\s*light)\b", re.IGNORECASE),
    re.compile(r"\bready\s+when\s+you\s+are\b", re.IGNORECASE),
    re.compile(
        r"\bshould\s+I\s+(?:go\s+ahead|proceed|fix|ship|file|push|start|continue|do)\b[^.?!\n]*\?",
        re.IGNORECASE,
    ),
    re.compile(r"\b(?:want|would\s+you\s+like)\s+me\s+to\b[^.?!\n]*\?", re.IGNORECASE),
]
_GO_ASK_TAIL_SENTENCES = 2

# The go-ask advisory (DoE `_emit_go_ask_advisory`): advisory only, never a block. Gated on the
# Group EM heartbeat so a session with no live holder may still end on a question.
_GROUP_EM_WATCH_REL = ("state", "group-em-watch.json")
_GROUP_EM_LIVE_SECONDS = 15 * 60
_GO_ASK_FIRED_MARKER = "guard-go-ask-fired.json"
_GO_ASK_ADVISORY_TEXT = (
    "Turn ends asking for a go while a Group EM is live. "
    "Edit the tail to state what you did or will do, or route the question to the Group EM."
)


def _inside_quote(prefix: str) -> bool:
    line = prefix[prefix.rfind("\n") + 1:]
    return sum(1 for ch in line if ch in _QUOTE_CHARS) % 2 == 1


def _status_line_wait(text: str, start: int, end: int) -> bool:
    """True when the progressive 'waiting on you' sits in a sentence that neither asks a
    question nor requests a decision: a status line describing a gate, not a handoff."""
    left = max((text.rfind(ch, 0, start) for ch in ".!?\n"), default=-1) + 1
    tail = _SENTENCE_BREAK_RE.search(text, end)
    right = tail.end() if tail else len(text)
    if text[end:end + 1] == ":":
        return False
    return not _HANDOFF_REQUEST_RE.search(text[left:right])


def _fires(pattern: "re.Pattern[str]", text: str) -> bool:
    for m in pattern.finditer(text):
        prefix = text[:m.start()]
        if _NEGATION_BEFORE.search(prefix[-60:]):
            continue
        if _PROGRESSIVE_WAIT_RE.fullmatch(m.group(0)) and _status_line_wait(text, m.start(), m.end()):
            continue
        if _inside_quote(prefix) or _reported_speech(prefix) or _third_party_clause(prefix):
            continue
        return True
    return False


def _matches_manufactured_blocker(text: str) -> bool:
    return any(_fires(p, text) for patterns in _PATTERN_GROUPS for p in patterns)


def _candidate_ownership_sentence(text: str) -> "Optional[str]":
    groups = (_HANDOFF_PATTERNS, _POSSESSIVE_PATTERNS, _OWNERSHIP_CUE_PATTERNS)
    for sentence in _split_sentences(text):
        for patterns in groups:
            for pattern in patterns:
                if _fires(pattern, sentence):
                    return sentence
    return None


def _heuristic_turn_intent(text: str) -> "tuple[bool, str]":
    """Label-only classifier for a turn that declared no intent: needs_pm iff
    a shipped handoff/go-ask/ownership pattern fires; ask is that sentence."""
    candidate = _candidate_ownership_sentence(text)
    if candidate is not None:
        return True, candidate[:_turn_intent.ASK_MAX]
    for sentence in _split_sentences(text)[-_GO_ASK_TAIL_SENTENCES:]:
        if any(_fires(p, sentence) for p in _GO_ASK_PATTERNS):
            return True, sentence[:_turn_intent.ASK_MAX]
    return (True, "") if _matches_manufactured_blocker(text) else (False, "")


def _go_ask_tail(text: str) -> bool:
    tail = _split_sentences(text)[-_GO_ASK_TAIL_SENTENCES:]
    return any(_fires(p, s) for s in tail for p in _GO_ASK_PATTERNS)


def _group_em_live(payload: dict) -> bool:
    """True iff the watch heartbeat names a holder and ticked within `_GROUP_EM_LIVE_SECONDS`.
    Any read or parse failure is "not live"."""
    try:
        repo_root = _repo_root(payload)
        if repo_root is None:
            return False
        with open(os.path.join(repo_root, *_GROUP_EM_WATCH_REL), encoding="utf-8") as fh:
            watch = json.load(fh)
        if not isinstance(watch, dict) or not str(watch.get("holder_session_id") or "").strip():
            return False
        ticked = datetime.fromisoformat(str(watch["last_tick_at"]).replace("Z", "+00:00"))
        if ticked.tzinfo is None:
            ticked = ticked.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - ticked).total_seconds() <= _GROUP_EM_LIVE_SECONDS
    except Exception:
        return False


def _go_ask_advisory(payload: dict, text: str) -> dict:
    """Once per session; the marker is written only when the advisory is returned."""
    if not _go_ask_tail(text) or not _group_em_live(payload):
        return no_advisory()
    if _has_fired_this_session(payload, _GO_ASK_FIRED_MARKER):
        return no_advisory()
    _mark_fired_this_session(payload, _GO_ASK_FIRED_MARKER)
    return post_advisory(_GO_ASK_ADVISORY_TEXT)


def _git_root_walk(start: str) -> "Optional[str]":
    try:
        origin = Path(start).resolve()
        for candidate in (origin, *origin.parents):
            if (candidate / ".git").exists():
                return str(candidate)
    except Exception:
        pass
    return None


def _turn_intent_step(payload: dict, text: str) -> bool:
    """Record this stop's turn intent; True iff the stop must be blocked for a
    missing tail. Blocks only with a role marker and a durable counter."""
    session_id = payload.get("session_id")
    cwd = payload.get("cwd")
    if not text.strip() or not isinstance(cwd, str) or not cwd:
        return False
    if not session_id_is_real(session_id):
        return False
    git_root = _git_root_walk(cwd)
    git_dir = resolve_git_common_dir(git_root) if git_root else ""
    if not git_dir:
        return False
    directory = _turn_intent.session_dir(git_dir, session_id)
    if not ensure_session_dir(directory, session_id):
        return False
    decision = _turn_intent.decide(
        text,
        _turn_intent.read_blocks(directory),
        # A session started under Cockpit and resumed in a terminal keeps its marker, so the marker alone is not the gate.
        _turn_intent.role_marker_present(directory) and under_cockpit(),
        _heuristic_turn_intent,
        _turn_intent.announces_unexecuted_work(text),
    )
    if decision.block:
        count = _turn_intent.bump_blocks(directory)
        return _turn_intent.read_blocks(directory) == count
    if decision.record is not None:
        needs_pm, ask, source = decision.record
        _turn_intent.write_record(directory, session_id, needs_pm, ask, source, time.time())
    return False


@register_op("hooks.guard_turn_intent")
def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)
    try:
        if payload.get("agent_id"):
            return no_advisory()
        last_message = payload.get("last_assistant_message")
        if isinstance(last_message, str) and last_message.strip():
            text = last_message
        else:
            transcript_path = payload.get("transcript_path")
            has_transcript = isinstance(transcript_path, str) and bool(transcript_path)
            text = _final_assistant_text(transcript_path) if has_transcript else ""
        if _turn_intent_step(payload, text):
            reason = (
                _turn_intent.ANNOUNCE_BLOCK_REASON
                if _turn_intent.announces_unexecuted_work(text)
                else _turn_intent.BLOCK_REASON
            )
            return apply_guard_level("guard-turn-intent", deny("Stop", reason)) or no_advisory()
        return _go_ask_advisory(payload, text)
    except Exception:
        pass
    return no_advisory()
