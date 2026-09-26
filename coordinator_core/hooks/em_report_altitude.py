"""
coordinator_core.hooks.em_report_altitude — Stop-hook advisory op.

Purpose: post-hoc measurement of the EM's own outgoing PM-facing message,
surfaced back to the EM as a non-blocking advisory. The ≤200-word EM->PM
budget and the "lead with the outcome, citations belong in the commit" rule
(global CLAUDE.md § Communication Style) are stated doctrine and were never
measured — nothing ever reported a violation, so the EM never learned it blew
the budget. This op closes that loop.

Empirical basis (measured, do not re-derive): in a real 100-message EM->PM
session, median reply was 28 words, 11 replies exceeded 200 words (max 401),
and 16 replies carried file:line / absolute-path / verification-narration
detail that changed no PM decision.

Detectors — exactly two, both evidence-pinned:

    D1 — budget overrun. Word-count the message, excluding fenced code blocks
    (```...```) and markdown table rows — that exclusion implements the
    doctrine's own carve-out ("a long reply needs a named reason ... or the
    content is a document"), so a reply that is mostly an artifact does not
    read as a verbosity violation. Fires above 200 words.

    D2 — wrong-altitude citation density. Counts `file.ext:NNN`-shaped
    citations and absolute filesystem paths (POSIX `/Users/...` and Windows
    `X:\\...`). Fires at >= 2 combined, INDEPENDENTLY of length — in the
    measured session, real altitude violations occurred in messages of 10, 16
    and 29 words, so a length prefilter would miss most of them.

Explicitly NOT detected (negative-spec, with reason):
    - Hedging. Measured ZERO in the corpus; a phrase-list detector would be
      precise but has no observed positives to justify shipping it.
    - Break-class escalation. Measured ZERO; the two naive
      break-word x ask-word co-occurrences in the corpus were both FALSE
      POSITIVES — one said "nothing is broken", the other narrated an
      already-resolved past mistake alongside a legitimate direction-class
      ask. A working detector needs negation scoping, tense discrimination,
      and break/ask polarity — a classifier, not a regex. Do not ship a regex
      for it.

NON-BLOCKING BY CONSTRUCTION: the DoE-side consumer composes this op's
`message` into an advisory `additionalContext` envelope and always exits 0.
This op must NEVER be wired to the exit-2 Stop-blocking channel. Blocking the
Stop gives the EM another turn to write MORE text, which makes a verbosity
defect worse, not better — the value here is next-turn calibration, not
interception.

Bark-once, not recurring (PM ruling supersedes an earlier taper design):
this op fires AT MOST ONCE per session, full stop — not the first-two-then-
compressed taper an earlier revision of this module built. The doctrine this
op backstops ("lead with the decision", the ≤200-word budget, citations
belong in the commit) now lives as always-loaded prose the EM reads every
session; this hook is suspenders for that belt, not a supervisor running
alongside it. Firing on every qualifying reply — even a tapering one — reads
as nagging once the doctrine is already stated in full, which is the exact
failure mode the PM ruling closes. A future reader must NOT "improve" this
back into a per-reply or escalating form; that reopens the nagging problem
this rewrite exists to close.

Scope of "once": per-session, GLOBAL across both detectors — a session gets
at most one comms nudge, ever, not one per detector. If the first qualifying
reply trips both D1 and D2, that single message carries both measurements;
it does not spend the session's one shot on only one of them. (A
per-detector scope would allow two nudges in one session, which is most of
the way back to the taper this ruling retires.)

Fire-once state reuses the former per-session tally file as a plain
has-fired sentinel — not a second mechanism. The write is best-effort: a
failed sentinel write must never change what `op()` returns for the call
that just fired. The honest failure mode is that a write failure means the
sentinel never gets recorded, so a later call in the same session may fire
again — for a backstop, firing an extra time is the safe direction (over-
warning), not silently going permanently mute.

Same Stop seam as `nudge_harness_directive_dispatch.py`, and this module
follows that sibling's shape deliberately: `last_assistant_text()` and
`_final_message_text()` are imported from it, not re-derived, and this module
carries no `@register_op`-decorated async handler for the same reason that
one doesn't — Stop events are not routed through the IPC daemon path, so
there is nothing for a daemon-side handler to register against. Transport
here is the DoE-resident stdin/stderr shim calling `op(payload)` directly.
The dispatch name `hooks.em_report_altitude` names this module/function pair
for that shim, not an IPC registration.

Environment variables:
    COORDINATOR_EM_REPORT_ALTITUDE_OFF=1
        Suppresses the op entirely — `op()` returns None unconditionally.
    COORDINATOR_EM_REPORT_ALTITUDE_TALLY_DIR=<dir>
        Overrides where the per-session has-fired sentinel is written (see
        `_tally_path`), falling back to the git-common-dir
        `coordinator-sessions/<session>/` location when unset. Exists so a
        demo run (replaying an archived transcript through this op) or a test
        run does not write sentinel state into a live repo's session
        bookkeeping, and so successive demo/test runs do not inherit each
        other's fired-state. The env var name kept its original "TALLY"
        spelling on purpose — same mechanism, repurposed, not replaced.

Spec backlink: coordinator/docs/wiki/em-pm-communication-style.md (DoE-claude)
"""

from __future__ import annotations

import json
import os
import re
import sys

from coordinator_core.hooks._envelope import context_only
from coordinator_core.hooks.nudge_harness_directive_dispatch import (
    _final_message_text,
    _repo_root,
    _resolve_git_dir,
    _session_key,
)


_WORD_BUDGET = 200

_FENCED_CODE_RE = re.compile(r"```.*?```", re.DOTALL)

_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP_RE = re.compile(r"^(?=.*[-|])[\s|:-]+$")


def _is_table_row(line: str) -> bool:
    return bool(_TABLE_ROW_RE.match(line)) or bool(_TABLE_SEP_RE.match(line))


def _strip_excluded(text: str) -> str:
    text = _FENCED_CODE_RE.sub(" ", text)
    kept_lines = [line for line in text.splitlines() if not _is_table_row(line)]
    return "\n".join(kept_lines)


def _word_count(text: str) -> int:
    return len(_strip_excluded(text).split())


# file:line citation (see _FILE_LINE_RE below) — kept as a named constant,
_SOURCE_FILE_EXTENSIONS = (
    "py", "js", "ts", "tsx", "jsx", "sh", "md", "json", "yaml", "yml",
    "toml", "cpp", "h", "hpp", "rs", "go", "sql",
)
_SOURCE_EXT_ALT = "|".join(re.escape(ext) for ext in _SOURCE_FILE_EXTENSIONS)

_FILE_LINE_RE = re.compile(
    r"\b(?:[\w.-]+/)+[\w.-]+\.\w+:\d+\b"
    r"|\b[\w-]+\.(?:" + _SOURCE_EXT_ALT + r"):\d+\b"
)

_POSIX_ABS_PATH_ROOTS = ("Users", "home", "opt", "var", "tmp", "usr", "etc")
_POSIX_ABS_PATH_RE = re.compile(
    r"/(?:" + "|".join(_POSIX_ABS_PATH_ROOTS) + r")/\S+"
)

_WIN_ABS_PATH_RE = re.compile(r"(?<![:\w])[A-Za-z]:\\\S+")


def _citation_count(text: str) -> tuple[int, int]:
    file_line = len(_FILE_LINE_RE.findall(text))
    abs_path = len(_POSIX_ABS_PATH_RE.findall(text)) + len(_WIN_ABS_PATH_RE.findall(text))
    return file_line, abs_path


_META_DISCUSSION = re.compile(
    r"em_report_altitude|hooks\.em_report_altitude",
    re.IGNORECASE,
)


_TALLY_FILENAME = "em-report-altitude-tally.json"

GENERATES: list = []


def _tally_path(payload: dict) -> str | None:
    """Return the per-session has-fired sentinel path, or None when no home
    for it is resolvable (mirrors
    `nudge_harness_directive_dispatch._sentinel_path`, with this op's own
    filename).

    Honours `COORDINATOR_EM_REPORT_ALTITUDE_TALLY_DIR` as an override for the
    directory the sentinel lives under, falling back to the git-common-dir
    `coordinator-sessions/<session>/` location when unset. This exists so a
    demo run (replaying an archived transcript's ~100 messages through this
    op) does not write fired-state into a live repo's session bookkeeping,
    and so successive demo/test runs do not inherit each other's fired
    state — each gets its own directory instead of sharing the real one.
    """
    override_dir = os.environ.get("COORDINATOR_EM_REPORT_ALTITUDE_TALLY_DIR")
    if override_dir:
        session_key, _has_true_sid = _session_key(payload)
        return os.path.join(override_dir, session_key, _TALLY_FILENAME)

    probe = _repo_root(payload)
    if probe is None:
        return None
    git_dir = _resolve_git_dir(os.path.join(probe, ".git"))
    if not git_dir:
        return None
    session_key, _has_true_sid = _session_key(payload)
    return os.path.join(git_dir, "coordinator-sessions", session_key, _TALLY_FILENAME)


def _has_already_fired(payload: dict) -> bool:
    path = _tally_path(payload)
    if not path:
        return False
    try:
        return os.path.isfile(path)
    except OSError:
        return False


def _mark_fired(payload: dict) -> None:
    path = _tally_path(payload)
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            fh.write("1")
    except OSError:
        pass


def _d1_block(word_count: int) -> str:
    return (
        f"[comms] {word_count} words this reply — tends to land better leading with "
        "the decision and saving evidence for if it's asked for (code blocks and "
        "table rows already don't count, so a genuine document isn't what this is "
        "aimed at). If this one earned the length, let it stand — just a nudge, not "
        "a rule."
    )


def _d2_block(citation_count: int) -> str:
    return (
        f"[comms] {citation_count} file:line/absolute-path citations this reply — "
        "they rarely change what the PM decides, so they usually read better living "
        "in the commit than the reply. Ignore this if the citation was the point."
    )


def _compose_message(word_count: int, file_line: int, abs_path: int) -> str:
    lines = []
    if word_count > _WORD_BUDGET:
        lines.append(_d1_block(word_count))
    if (file_line + abs_path) >= 2:
        lines.append(_d2_block(file_line + abs_path))
    return "\n".join(lines)


def op(payload: dict) -> dict | None:
    """Stop advisory: measure the EM's own final message against the EM->PM
    budget and citation-altitude doctrine.

    Returns ``{"message": <str>}`` when D1 or D2 trips AND this session has
    not already fired, ``None`` otherwise. NON-BLOCKING BY CONSTRUCTION — see
    module docstring; the caller must never route this return value to the
    exit-2 Stop-blocking channel.

    Bark-once, not recurring: at most one firing per session, global across
    both detectors — see module docstring "Bark-once, not recurring".

    Never raises on well-formed OR malformed input (fail-open).
    """
    try:
        if not isinstance(payload, dict):
            return None
        if payload.get("stop_hook_active"):
            return None
        if payload.get("agent_id"):
            return None
        if os.environ.get("COORDINATOR_EM_REPORT_ALTITUDE_OFF") == "1":
            return None

        text = _final_message_text(payload)
        if not text:
            return None
        if _META_DISCUSSION.search(text):
            return None

        word_count = _word_count(text)
        file_line, abs_path = _citation_count(text)

        d1 = word_count > _WORD_BUDGET
        d2 = (file_line + abs_path) >= 2
        if not (d1 or d2):
            return None

        if _has_already_fired(payload):
            return None

        message = _compose_message(word_count, file_line, abs_path)
        _mark_fired(payload)
        envelope = context_only("Stop", message)
        return {"message": envelope["hookSpecificOutput"]["additionalContext"]}
    except Exception:
        return None


if __name__ == "__main__":  # pragma: no cover - manual probe path
    result = op(json.load(sys.stdin))
    if result:
        sys.stdout.write(result["message"])
