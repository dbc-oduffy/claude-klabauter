"""
coordinator_core.hooks.guard_manufactured_blocker — Stop-hook engine op,
the manufactured-blocker check (off-altitude items in end-of-turn PM
handoffs).

Purpose: warm command/native-door counterpart of DoE-claude's
`coordinator/hooks/scripts/guard-manufactured-blocker.py` (936 lines).
Verbatim port of that script's detection logic (C5 altitude test, C6
decidability check, C10 declarative-stall check, and the A13 exemptions) —
see the DoE source's own module docstring for the full worked rationale
behind each clause; kept in lockstep with that file's helper names so a
future diff against the source is mechanical.

NOT COMPOSED into `hooks.stop_dispatch`'s own fan-in (that module's own
docstring, leg 3, explicitly excludes this guard: BLOCKING-class per
`DR-warm-hook-miss-policy`, and `warm/hook_http.py::BLOCKING_EVENTS` does not
cover `Stop`, so an http-flipped registration would fail open against a dead
engine). This chunk's row is command/native-door only (per the W4-C1
verdict) — DoE registers `<settings-bin>/hook-run hooks.guard_manufactured_blocker`
as its own, separate `Stop` hooks.json entry (unchanged from today's
subprocess registration, just re-pointed at hook-run instead of `python3`
this script directly) rather than folding into the `stop_dispatch` fan-in;
that registration edit is DoE's, not this chunk's (per the plan's own exit
criterion).

Op contract: `params` reaches this op in either shape a `hooks.*` handler
receives — wrapped as `params["payload"]` by both engine doors, flat by the
cold chain; `_envelope.payload_of` reads both, supplying the Stop payload
dict (`session_id`, `transcript_path`, `cwd`, `stop_hook_active`,
`agent_id`, ...) — never `os.environ` or this process's own `cwd`. Returns
one `hookSpecificOutput`
envelope: `deny("Stop", text)` at a blocking posture (`default`/
`substrate-free`) when the C5 altitude check fires unexempted; `post_advisory(text)`
for every other text-carrying verdict (C5 at `precision`, the unconditional
C6/C10 advisories); `no_advisory()` (empty dict) when nothing fires —
matching `stop_dispatch._extract_advisory`'s own uniform reading of every
composed leg's return shape.

Graceful degradation: any failure to read the transcript, parse posture, or
resolve a repo root falls through toward silence (`no_advisory()`), matching
the source script's own fail-open contract (exit 0 on every failure path).

Spec backlink: docs/plans/2026-09-18-doe-holds-no-scripts.md § W4-C14
DoE source: coordinator/hooks/scripts/guard-manufactured-blocker.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Mapping, Optional

from coordinator_core import block_discharge as _block_discharge
from coordinator_core.hooks._envelope import deny, no_advisory, payload_of, post_advisory
from coordinator_core.hooks.support.posture import resolve_posture
from coordinator_core.hooks.support.touch_record import _touch_lines
from coordinator_core.ipc import register_op


_HANDOFF_PATTERNS = [
    re.compile(r"\bwait(?:s|ing)?\s+on\s+you\b", re.IGNORECASE),
    re.compile(r"\bblocks?\s+on\s+you\b", re.IGNORECASE),
    re.compile(r"\byour\s+call\b", re.IGNORECASE),
    re.compile(r"\bneeds?\s+your\s+(?:decision|input|sign-off)\b", re.IGNORECASE),
    re.compile(r"\bover\s+to\s+you\b", re.IGNORECASE),
    re.compile(r"\bnow\s+wait\s+on\s+you\b", re.IGNORECASE),
]

_POSSESSIVE_PATTERNS = [
    re.compile(r"\bgenuinely\s+yours\b", re.IGNORECASE),
    re.compile(r"\bgenuinely\s+unresolved\b", re.IGNORECASE),
    re.compile(r"\bstill\s+yours\b", re.IGNORECASE),
    re.compile(r"\bone\s+item\s+that'?s\s+actually\s+yours\b", re.IGNORECASE),
    re.compile(r"that'?s\s+a\s+PM\s+call,?\s*not\s+mine\b", re.IGNORECASE),
    re.compile(r"\bis\s+yours\s+to\s+(?:decide|disposition|call)\b", re.IGNORECASE),
]

_PATTERN_GROUPS = (_HANDOFF_PATTERNS, _POSSESSIVE_PATTERNS)

_OWNERSHIP_CUE_PATTERNS = [
    re.compile(r"\b(?:is|'s)\s+yours\b", re.IGNORECASE),
    re.compile(r"\byours\s+to\s+\w+", re.IGNORECASE),
]

_BARE_IDENTIFIER_PATTERNS = [
    re.compile(r"\bchunk\s+C\d+\b", re.IGNORECASE),
    re.compile(r"\bAC\s+A\d+\b", re.IGNORECASE),
    re.compile(r"[(/]\s*[AC]\d+\b"),
    re.compile(r"\bdocs/plans/\S+"),
    re.compile(r"\b[0-9a-f]{7,40}\b", re.IGNORECASE),
    re.compile(r"\b(?:dlv|pln)-\S+", re.IGNORECASE),
]

_CATEGORY_CALL_RE = re.compile(
    r"\ba\s+(?:scope|product|direction|prioritization)\s+call\b", re.IGNORECASE
)

_RECOMMENDATION_RE = re.compile(
    r"\bI\s+recommend\b"
    r"|\bI'?d\s+suggest\b"
    r"|\bI\s+would\s+suggest\b"
    r"|\bmy\s+read\s+is\b"
    r"|\bI\s+lean\b"
    r"|\bunless\s+you'?d\s+rather\b"
    r"|\bunless\s+you\s+would\s+rather\b",
    re.IGNORECASE,
)

_DECIDABILITY_CORRECTION_TEXT = (
    "[guard] This is a PM call, but not decidable as written -- presume zero "
    "retained PM context: no bare identifiers up front, state the choice in "
    "plain words, the options, and a recommendation with its cost (this "
    "repo's reporting doctrine). Restate it so the PM can answer cold.\n"
)

_EXTERNAL_ACTION_PENDING_RE = re.compile(
    r"ask-before-external-action"
    r"|external[- ]action\b.{0,40}\b(?:pending|awaiting|authoriz)"
    r"|(?:may\s+i|requesting\s+(?:your\s+)?(?:authorization|permission))\b.{0,60}"
    r"\b(?:push|force[- ]push|delete|deploy|merge|commit)\b"
    r"|before\s+i\s+(?:push|force[- ]push|delete|deploy|commit)\b.{0,40}"
    r"(?:\bok\b|\bokay\b|\bconfirm|authoriz|permission)",
    re.IGNORECASE,
)

_WRONG_SIGNATORY_PATTERNS = [
    re.compile(r"\bwrong\s+signat(?:ure|ory)\b", re.IGNORECASE),
    re.compile(r"\bratify(?:ing)?\s+my\s+own\b", re.IGNORECASE),
    re.compile(r"\bmy\s+own\s+authorship\b", re.IGNORECASE),
    re.compile(r"\bapprove\s+my\s+own\b", re.IGNORECASE),
    re.compile(r"\bsign(?:ing)?\s+off\s+on\s+my\s+own\b", re.IGNORECASE),
    re.compile(r"\btwo-decider\b", re.IGNORECASE),
    re.compile(r"\bnamed\s+deciders\b", re.IGNORECASE),
    re.compile(r"\bthe\s+record'?s\s+deciders\b", re.IGNORECASE),
    re.compile(r"\bboth\s+deciders\b", re.IGNORECASE),
]

_AUTHORSHIP_ADMISSION_RE = re.compile(
    r"\bI\s+(?:wrote|authored)\s+\S+\b"
    r"|\bI(?:'m|\s+am)\s+one\s+of\s+the\s+named\s+deciders\b",
    re.IGNORECASE,
)

_RATIFICATION_VOCAB_RE = re.compile(
    r"\bsign[- ]?off\b|\bratif\w*\b|\bapprove\b", re.IGNORECASE
)

_SIZING_PATH_RE = re.compile(r"^state/sizings/[^/]+\.ya?ml$")

_APPETITE_DIVERGENCE_DETENT = "appetite_exceeded"
_POST_SIZE_PROMPT_DETENT = "post_size_prompt_pending"

_TAIL_WINDOW_BYTES = 200_000

_CORRECTION_TEXT = (
    "[guard] This turn closed with a PM-handoff construct for an item the PM "
    "would not recognise by its own nouns. Apply the altitude test "
    "(the plan skill's B.0 un-gaming clause, on whichever route you loaded): "
    "a genuine PM call concerns scope, deliverable, or product direction, "
    "and states itself in the PM's vocabulary. If the PM would not recognise "
    "the nouns, "
    "it is an EM call -- decide it, do not hand it up. Re-close the turn "
    "having resolved it yourself, or state the genuine PM-altitude question "
    "plainly if one actually remains.\n"
)

_SIZING_TOPIC_PATTERNS = [
    re.compile(r"\bt-?shirt\b", re.IGNORECASE),
    re.compile(r"\bappetite\b", re.IGNORECASE),
    re.compile(r"\bxl[_ -]?exit\b", re.IGNORECASE),
    re.compile(r"\bpm-decision\b", re.IGNORECASE),
    re.compile(r"\b(?:XS|XL|XXL)\b"),
    re.compile(r"\b(?:cut|split)\s+it\b", re.IGNORECASE),
    re.compile(r"\bgo\s+with\s+that\b", re.IGNORECASE),
    re.compile(r"\bmulti-session\b", re.IGNORECASE),
    re.compile(r"\bsizing\s+lobby\b", re.IGNORECASE),
    re.compile(r"\bsized\s+it\b", re.IGNORECASE),
    re.compile(r"\bpost_size_prompt\b", re.IGNORECASE),
]

_DECLARATIVE_STALL_PATTERNS = [
    re.compile(r"^\s*(?:now\s+|so\s+)?proceeding\s+with\b", re.IGNORECASE),
    re.compile(r"^\s*(?:now\s+)?(?:starting|beginning)\s+(?:on|with|the)\b", re.IGNORECASE),
    re.compile(
        r"\bI'?ll\s+(?:now\s+)?(?:start|begin|get\s+on\s+with|move\s+on\s+to|"
        r"crack\s+on\s+with|kick\s+off|pick\s+up)\b",
        re.IGNORECASE,
    ),
    re.compile(r"\bnext\s+(?:up\b|I'?ll\b)", re.IGNORECASE),
    re.compile(r"\bI'?ll\s+stop\s+\w+ing\b", re.IGNORECASE),
]

_DECLARATIVE_STALL_CORRECTION_TEXT = (
    "[guard] This turn announces the next action instead of taking it. If "
    "available, take it and report what happened; if blocked, name the "
    "blocker and recommendation. See "
    "coordinator/snippets/em-operating-doctrine.md.\n"
)

_BULLET_CONTINUATION_LINE_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


def _tail_text(path: str, max_bytes: int = _TAIL_WINDOW_BYTES) -> str:
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > max_bytes:
                fh.seek(size - max_bytes)
                fh.readline()
            raw = fh.read()
        return raw.decode("utf-8", errors="replace")
    except OSError:
        return ""


def _final_assistant_text(transcript_path: str) -> str:
    text = _tail_text(transcript_path)
    if not text:
        return ""
    last_text = ""
    for line in text.splitlines():
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
        if not isinstance(content, list):
            continue
        parts = [
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        last_text = "\n".join(parts) if parts else ""
    return last_text


def _matches_manufactured_blocker(text: str) -> bool:
    for patterns in _PATTERN_GROUPS:
        for pattern in patterns:
            if pattern.search(text):
                return True
    return False


def _external_action_pending(text: str) -> bool:
    return bool(_EXTERNAL_ACTION_PENDING_RE.search(text))


def _repo_root(payload: Mapping) -> "Optional[str]":
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


def _resolve_git_dir(dot_git: str) -> "Optional[str]":
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


def _extract_scalar(lines: "list[str]", key: str) -> "Optional[str]":
    prefix = key + ":"
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith(prefix):
            value = stripped[len(prefix):].strip()
            if "#" in value:
                value = value.split("#", 1)[0].strip()
            value = value.strip("'\"")
            return value
    return None


def _extract_detents(lines: "list[str]") -> "list[str]":
    values: "list[str]" = []
    collecting = False
    for raw_line in lines:
        stripped = raw_line.strip()
        if not collecting:
            if not stripped.startswith("detents:"):
                continue
            rest = stripped[len("detents:"):].strip()
            if rest.startswith("[") and rest.endswith("]"):
                inner = rest[1:-1]
                return [item.strip().strip("'\"") for item in inner.split(",") if item.strip()]
            collecting = True
            continue
        if stripped.startswith("- "):
            values.append(stripped[2:].strip().strip("'\""))
        elif stripped == "":
            continue
        else:
            break
    return values


def _is_null_scalar(value: "Optional[str]") -> bool:
    return value is None or value in ("null", "~", "None", "")


def _sizing_topic_in_scope(scope: str) -> bool:
    return any(pattern.search(scope) for pattern in _SIZING_TOPIC_PATTERNS)


def _sizing_object_exempts(repo_root: str, rel_path: str) -> bool:
    try:
        with open(os.path.join(repo_root, rel_path), "r", encoding="utf-8") as fh:
            lines = fh.readlines()
    except OSError:
        return True

    route = _extract_scalar(lines, "route")
    fork = _extract_scalar(lines, "fork")
    xl_exit = _extract_scalar(lines, "xl_exit")
    detents = _extract_detents(lines)

    if any(line.strip().startswith("pm_resolution:") for line in lines):
        return False

    fork_open = (
        _APPETITE_DIVERGENCE_DETENT in detents or _POST_SIZE_PROMPT_DETENT in detents
    ) and _is_null_scalar(fork)
    xl_open = route == "pm-decision" and _is_null_scalar(xl_exit)
    return fork_open or xl_open


def _sizing_exemption_applies(payload: Mapping) -> bool:
    session_id = payload.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return False
    repo_root = _repo_root(payload)
    if repo_root is None:
        return False
    git_dir = _resolve_git_dir(os.path.join(repo_root, ".git"))
    if not git_dir:
        return False
    sizing_paths = [
        rel for rel in _touch_lines(git_dir, session_id) if _SIZING_PATH_RE.match(rel)
    ]

    return any(_sizing_object_exempts(repo_root, rel) for rel in sizing_paths)


def _wrong_signatory_pending(scope: str, full_text: str) -> bool:
    if any(pattern.search(scope) for pattern in _WRONG_SIGNATORY_PATTERNS):
        return True
    return bool(
        _AUTHORSHIP_ADMISSION_RE.search(full_text)
        and _RATIFICATION_VOCAB_RE.search(full_text)
    )


def _split_sentences(text: str) -> "list[str]":
    pieces = re.split(r"(?<=[.!?])\s+|\n+", text)
    return [p.strip() for p in pieces if p.strip()]


def _trigger_window_idxs(text: str) -> "tuple[list[str], set]":
    sentences = _split_sentences(text)
    if not sentences:
        return sentences, set()
    trigger_idxs = set()
    for i, sentence in enumerate(sentences):
        for patterns in _PATTERN_GROUPS:
            if any(pattern.search(sentence) for pattern in patterns):
                trigger_idxs.add(i)
                break
    if not trigger_idxs:
        return sentences, set()
    window_idxs = set()
    for i in trigger_idxs:
        window_idxs.update(range(max(0, i - 1), min(len(sentences), i + 2)))
    return sentences, window_idxs


def _final_sentence(text: str) -> str:
    sentences = _split_sentences(text)
    return sentences[-1] if sentences else ""


def _any_exemption_applies(payload: Mapping, text: str) -> bool:
    sentences, window_idxs = _trigger_window_idxs(text)
    scope = " ".join(sentences[i] for i in sorted(window_idxs)) if window_idxs else text

    final_idx = len(sentences) - 1 if sentences else -1
    final_sentence_eligible = bool(window_idxs) and final_idx == max(window_idxs) + 1
    sizing_in_scope = _sizing_topic_in_scope(scope) or (
        final_sentence_eligible and _sizing_topic_in_scope(_final_sentence(text))
    )
    return (
        _external_action_pending(scope)
        or (sizing_in_scope and _sizing_exemption_applies(payload))
        or _wrong_signatory_pending(scope, text)
    )


def _candidate_ownership_sentence(text: str) -> "Optional[str]":
    groups = (_HANDOFF_PATTERNS, _POSSESSIVE_PATTERNS, _OWNERSHIP_CUE_PATTERNS)
    for sentence in _split_sentences(text):
        for patterns in groups:
            for pattern in patterns:
                if pattern.search(sentence):
                    return sentence
    return None


def _has_bare_identifier(sentence: str) -> bool:
    return any(pattern.search(sentence) for pattern in _BARE_IDENTIFIER_PATTERNS)


def _has_category_call(sentence: str) -> bool:
    return bool(_CATEGORY_CALL_RE.search(sentence))


def _has_recommendation(full_text: str) -> bool:
    return bool(_RECOMMENDATION_RE.search(full_text))


def _fails_decidability(sentence: str, full_text: str) -> bool:
    return (
        _has_bare_identifier(sentence)
        or _has_category_call(sentence)
        or not _has_recommendation(full_text)
    )


def _final_stall_unit(text: str) -> str:
    paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
    if not paragraphs:
        return ""
    lines = [ln for ln in paragraphs[-1].splitlines() if ln.strip()]
    if len(lines) > 1 and _BULLET_CONTINUATION_LINE_RE.match(lines[-1]):
        idx = len(lines) - 1
        while idx > 0 and _BULLET_CONTINUATION_LINE_RE.match(lines[idx]):
            idx -= 1
        if not _BULLET_CONTINUATION_LINE_RE.match(lines[idx]):
            return lines[idx].strip()
    return _final_sentence(text)


def _is_declarative_stall(text: str) -> bool:
    unit = _final_stall_unit(text)
    if not unit:
        return False
    return any(pattern.search(unit) for pattern in _DECLARATIVE_STALL_PATTERNS)


def _emit_declarative_stall_verdict(text: str) -> dict:
    if _is_declarative_stall(text):
        return post_advisory(_DECLARATIVE_STALL_CORRECTION_TEXT)
    return no_advisory()


def _emit_decidability_verdict(candidate: str, text: str) -> dict:
    if _fails_decidability(candidate, text):
        return post_advisory(_DECIDABILITY_CORRECTION_TEXT)
    return no_advisory()


@register_op("hooks.guard_manufactured_blocker")
def _handler(params: dict, repo_root=None) -> dict:
    payload = payload_of(params)

    if payload.get("agent_id"):
        return no_advisory()
    if payload.get("stop_hook_active"):
        return no_advisory()

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return no_advisory()

    text = _final_assistant_text(transcript_path)
    if not text:
        return no_advisory()

    if not _matches_manufactured_blocker(text):
        candidate = _candidate_ownership_sentence(text)
        if candidate is None:
            return _emit_declarative_stall_verdict(text)
        return _emit_decidability_verdict(candidate, text)

    if _any_exemption_applies(payload, text):
        candidate = _candidate_ownership_sentence(text) or text
        return _emit_decidability_verdict(candidate, text)

    try:
        posture = resolve_posture(_repo_root(payload))
    except Exception:
        posture = "precision"

    if posture in ("default", "substrate-free"):
        session_id = payload.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            session_id = "unknown-session"
        repo_root_for_ledger = _repo_root(payload)
        nonce = None
        if repo_root_for_ledger is not None:
            try:
                nonce = _block_discharge.record_fire(
                    repo_root_for_ledger, session_id, "guard-manufactured-blocker", _CORRECTION_TEXT
                )
            except Exception:
                nonce = None
        if nonce is not None:
            discharge_note = (
                f"Recorded as {nonce}. When you have acted on this, run:\n"
                f'  "{sys.executable}" "coordinator/bin/block-discharge.py" record --nonce {nonce} '
                f'--action "<what you did>" --repo-root "{repo_root_for_ledger}"\n'
            )
        else:
            discharge_note = (
                f"Could not record this fire (write failed) at "
                f"state/block-discharge/{session_id}.jsonl.\n"
                "No nonce to discharge -- this failure is visible here, not "
                "laundered into a clean check.\n"
            )
        return deny("Stop", _CORRECTION_TEXT + discharge_note)

    return post_advisory(_CORRECTION_TEXT)
