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
DR-123-the-per-turn-dispatch-restatement-is-ret.md`, example-doctrine-repo), the work of
countering the harness line is now split three ways: the binding statement
lives at SessionStart (`coordinator/snippets/agent-role-em.md` § How You
Dispatch); the mid-conversation salience carrier is the once-per-session
UserPromptSubmit line (`_DISPATCH_DEFAULT_LINE` in `runtime-tripwire-em-
check.py`, coordinator/example-doctrine-repo-side, not this module); and this op is the third,
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
      register against. Transport here is the example-doctrine-repo-resident stdin/stderr shim
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

# ---------------------------------------------------------------------------
# Tells — deliberately narrow. A false negative costs one un-nudged turn; a
# false positive costs the PM a blocked end-of-turn, so the bar is precision.
# ---------------------------------------------------------------------------

# Tell A — the EM citing the harness directive in its own prose. "AgentTool" is
# not the name of any tool the EM can actually call (the tool is `Agent`), so
# its appearance in an EM message is near-diagnostic of this failure. The
# second alternative catches a paraphrase that drops the token.
#
# Negative-spec: a third alternative (bare "not to use workflows or
# deep-research", with no "unless...requested" co-occurrence) was deliberately
# dropped (Review: code-reviewer — F4) — it fired on a benign, correct
# statement about tool choice with no framing of declining to dispatch. The
# two alternatives kept here are far more diagnostic on their own.
_TELL_CITES_DIRECTIVE = re.compile(
    r"\bAgentTool\b"
    r"|unless the user (?:requested|asks for) it",
    re.IGNORECASE,
)

# Tell B — the EM asking the PM for permission to dispatch. Dispatch sequencing
# is EM remit (First Officer Doctrine § Engineering Remit), so this question is
# itself the error, independent of how the EM arrived at it.
_TELL_ASKS_PERMISSION = re.compile(
    r"(?:want me|shall i|should i|would you like me|do you want me|ok(?:ay)? (?:for me )?)"
    r"[^.?!\n]{0,60}?"
    r"\b(?:dispatch|delegate|fan[- ]out|spawn|subagent|sub-agent)\b",
    re.IGNORECASE,
)

# Tell C — the EM ASCRIBING a dispatch restriction to the PM as the PM's own
# standing instruction. Distinct from Tell A (citing the harness line in the
# EM's own voice) and Tell B (asking the PM's permission): this is the EM
# misattributing an unattributed harness line's authorship, e.g. "holding
# that dispatch on your standing don't-call-the-Agent-tool instruction" or
# reporting it as "your standing instruction". Missed by both prior tells on
# 2026-08-02 (spec backlink below).
#
# Three components, all required within the SAME sentence to hold precision:
#   1. a possessive-attribution phrase ("your ... instruction/rule/...", or
#      "as you('ve) instructed") naming the PM as the source of a rule;
#   2. a dispatch-shaped noun/verb in that same sentence (dispatch, delegate,
#      fan-out, spawn, subagent, Agent-tool);
#   3. a restriction cue (don't, not, held/holding, declined, avoided, ...) —
#      this is what discriminates a RESTRICTION ("your rule against
#      dispatching") from a legitimate report of something the PM actually
#      asked for ("as you instructed, I dispatched two reviewers" has no
#      restriction cue and must not trip).
# Requiring all three in one sentence is deliberately narrower than a
# whole-message co-occurrence check: "your standing instruction to keep PRs
# under 300 lines" (a real, unrelated PM instruction) must not trip merely
# because some other sentence in the same turn happens to mention dispatch.
_POSSESSIVE_PM_ATTRIBUTION = re.compile(
    r"\byour\s+(?:standing\s+)?[^.?!\n]{0,60}?\b(?:instruction|rule|directive|order|policy)\b"
    r"|\bas you(?:'ve| have)?\s+instructed\b"
    r"|\byou(?:'ve| have)\s+instructed\b",
    re.IGNORECASE,
)

_TELL_C_DISPATCH_TERM = re.compile(
    r"dispatch\w*|delegat\w*|fan[- ]?out\w*|spawn\w*|sub-?agent\w*|agent[- ]?tool\w*",
    re.IGNORECASE,
)

_TELL_C_RESTRICTION_CUE = re.compile(
    r"\bdon'?t\b|\bdo not\b|\bdidn'?t\b|\bdid not\b"
    r"|\bdeclin\w*|\bheld\b|\bholding\b|\bavoid\w*|\brefrain\w*|\bwithhold\w*"
    r"|\bnot to\b|\bno\b|\bcannot\b|\bcan'?t\b|\bwon'?t\b|\bskip\w*",
    re.IGNORECASE,
)


def _tell_misattributes_to_pm(text: str) -> bool:
    """Return True iff `text` ascribes a dispatch restriction to the PM as the
    PM's own standing instruction, within any single sentence.

    Sentence-scoped (split on `.`/`?`/`!`/newline) rather than whole-message,
    per the false-positive note above the pattern definitions.
    """
    for sentence in re.split(r"[.?!\n]+", text):
        if not sentence.strip():
            continue
        if not _POSSESSIVE_PM_ATTRIBUTION.search(sentence):
            continue
        if _TELL_C_DISPATCH_TERM.search(sentence) and _TELL_C_RESTRICTION_CUE.search(sentence):
            return True
    return False


# Tell D — the EM asking the PM for permission to commit or stage work it
# already owns. Modelled directly on Tell B (_TELL_ASKS_PERMISSION above): same
# permission-phrase alternation, same "verb within N chars" shape, swapped to
# commit/stage terms. Added after a live 2026-08-02 session where the EM
# completed a full dispatch wave and then asked the PM "stage and commit?" —
# the identical permission reflex Tell B catches, aimed at the commit step
# instead of the dispatch step, and no tell fired. The EM's own doctrine
# (Commit Gate: only the EXECUTOR never commits) makes the commit step
# unconditionally the EM's own to take, so asking permission for it is the
# same error as Tell B, independent of how the EM arrived at it.
#
# Two closely-adjacent asks LOOK similar but must NOT trip, and are handled by
# dedicated negatives rather than folded into the permission pattern itself:
#   - merging to main is a genuine PM gate requiring a literal keyword
#     ("merge"), which this pattern's commit/stage vocabulary never matches on
#     its own;
#   - pushing to a shared remote / opening a PR is ask-before-external-action,
#     also a correct ask, and likewise never matches on "push"/"PR"/"remote"
#     alone.
# `_COMMIT_GATE_OR_OUTWARD_CUE` is a defensive backstop: a sentence naming
# BOTH a commit/stage verb AND one of merge/push/PR/remote/upstream is treated
# as the (correct) gated-or-outward ask and suppressed, even though the
# vocabulary mismatch alone already excludes most such cases.
#
# A third negative, `_COMMIT_SCOPING_CUE`, catches the scoping question —
# "which files should I include", "should I leave X out", "out of scope" —
# which asks about the CONTENTS of a commit the EM is already going to make,
# not for permission to make it.
_TELL_ASKS_COMMIT_PERMISSION = re.compile(
    r"(?:want me|shall i|should i|would you like me|do you want me|ok(?:ay)? (?:for me )?)"
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


# Suppression — the EM explaining this very mechanism (e.g. authoring or
# reviewing this hook, or answering a PM question about the harness line)
# should not trip Tell A. Without this, any session that discusses the doctrine
# nudges itself.
#
# Negative-spec: bare `DR-082`/`DR-108` (and the broad `harness[- ]directive`
# phrase) were deliberately dropped from this suppressor (Review: code-reviewer
# — F2) — DR-108 is the exact ratified "Dispatch Is Encouraged" citation, so a
# message citing it while ALSO asking permission ("Per DR-108 dispatch is
# encouraged, but... should I dispatch anyway?") is the single highest-value
# case this op exists to catch, and the broad suppressor was silently
# swallowing it. Only tokens a normal dispatch-reasoning sentence would never
# contain survive here.
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
    """Return (filesystem-safe session discriminator, whether a true session_id was present).

    The second element lets callers warn when the sentinel is only
    invocation-scoped (Review: code-reviewer — F7): a PID-keyed fallback means
    each fresh hook process gets its own sentinel, silently degrading
    fire-once to fire-every-time.
    """
    sid = payload.get("session_id")
    if isinstance(sid, str) and sid.strip():
        safe = re.sub(r"[^A-Za-z0-9_-]", "", sid.strip())
        if safe:
            return safe, True
    return f"pid-{os.getpid()}", False


def _repo_root(payload: dict) -> str | None:
    """Return the repo root directory (the one holding a `.git` entry), or None.

    Walks upward from ``payload["cwd"]`` (or the process cwd) looking for a
    `.git` entry. Existence, not directory-ness — worktrees/submodules use a
    `.git` FILE (a `gitdir:` pointer), matching the house convention in
    nudge_em_code_dispatch.py's `_is_outside_git_work_tree`
    (Review: code-reviewer — F5). Returns None when no repo root is resolvable,
    e.g. a cwd outside any git working tree.
    """
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
    """Return the once-per-session sentinel path, or None when no repo root is resolvable.

    Sentinel lives alongside the other per-session hook state under
    ``.git/coordinator-sessions/<session>/``. When there is no ``.git`` to hang it
    on, the op degrades to no-sentinel: it may then fire more than once, which is
    strictly better than not firing at all.
    """
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
    """Return the real git directory for a `.git` path, or None if unusable.

    A plain checkout's `.git` is the git directory itself. A worktree's or
    submodule's `.git` is a FILE holding a ``gitdir: <path>`` pointer, and
    nothing can be created *under* a file — resolving the root without also
    resolving the pointer leaves the sentinel write failing, which silently
    degrades fire-once to fire-every-time. Found by the F5 worktree test after
    the root-resolution half of F5 landed.
    """
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
    """Atomically claim the once-per-session fire slot; return True iff this call won it.

    Collapses the former check-then-write into one exclusive-creation step
    (Review: code-reviewer — F6): a plain exists-check followed by a separate
    write is a TOCTOU window where two racing Stop hooks for the same session
    can both observe "not yet fired" and both fire. ``open(..., "x")`` makes
    the claim atomic at the filesystem level.
    """
    if not sentinel:
        return True
    try:
        os.makedirs(os.path.dirname(sentinel), exist_ok=True)
        with open(sentinel, "x", encoding="utf-8") as fh:
            fh.write("1")
        return True
    except FileExistsError:
        return False
    except OSError:
        # A sentinel we cannot write means at worst a repeat nudge next turn.
        # Never let it turn into a raised exception on the Stop path.
        return True


def last_assistant_text(transcript_path: str) -> str:
    """Return the text of the final assistant message in the transcript, or "".

    Reads only the tail of the file: the last assistant turn is at the end, and a
    long session's transcript is megabytes. Returns "" on any I/O or parse
    failure — fail-silent is the correct posture for an advisory op.
    """
    try:
        size = os.path.getsize(transcript_path)
        with open(transcript_path, "rb") as fh:
            # Review: code-reviewer — F8: seek on raw bytes, not a text-mode
            # stream — text-mode seek() is only documented as safe for
            # offsets from tell() or 0, not an arbitrary computed byte offset.
            # Binary seek + discard-the-partial-line + decode-the-tail is
            # unambiguously well-defined regardless of UTF-8 boundary landing.
            if size > 512_000:
                fh.seek(size - 512_000)
                fh.readline()  # discard the partial line the seek landed inside
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
        # Review: code-reviewer — F1: a truthy non-dict `message` (older schema,
        # compaction-summary entry, hand-edited jsonl) must not raise on `.get`.
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
            # An assistant entry carrying only tool_use blocks is not the final
            # spoken turn; keep walking back to the last one that spoke.
            continue
    return ""


def _session_has_dispatched(payload: dict) -> bool:
    """Return True iff this session has direct on-disk evidence of an Agent dispatch.

    Evidence is track_dispatched_agents.py's own write target: a non-empty
    ``<git-common-dir>/coordinator-sessions/<session>/dispatched-agents.txt``.
    A non-empty file falsifies this op's premise (that the EM has not dispatched).

    Resolved via ``git_common_dir`` — deliberately NOT this module's
    worktree-local ``_resolve_git_dir`` walk. The writer keys its path off the
    git COMMON directory so a worktree session's bookkeeping lands under the
    MAIN worktree's `.git`, not the (linked) worktree-local one; in a worktree
    session those two directories differ, and reading the worktree-local one
    would silently look in a directory the writer never wrote to.

    Fail-open by design: any ambiguity — no resolvable repo root,
    ``git_common_dir`` raising, an unreadable/unstat-able file — returns
    False, i.e. the nudge still fires. Suppressing a genuine nudge on an
    unreadable file is the worse failure.
    """
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
    if isinstance(supplied, str) and supplied.strip():
        return supplied

    transcript_path = payload.get("transcript_path")
    if not isinstance(transcript_path, str) or not transcript_path:
        return ""
    return last_assistant_text(transcript_path)


def message_trips_tell(text: str) -> bool:
    """Return True iff `text` shows one of the four observed failure tells."""
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
    """Stop advisory: nudge an EM that declined to dispatch on a misread harness line.

    Returns ``{"message": <str>}`` when the nudge should fire, ``None`` otherwise.
    The caller owns transport (the example-doctrine-repo shim writes the message to stderr and
    exits 2, the documented Stop-hook block channel) — this op decides only
    *whether* to speak, per the DR-047 transport-seam split.

    Never raises on well-formed input.
    """
    if not isinstance(payload, dict):
        # Review: code-reviewer — F9: an un-reviewable caller passing a
        # non-dict must not turn "never raises" into an AttributeError storm.
        return None
    if payload.get("stop_hook_active"):
        return None  # this Stop came from a hook block — re-firing would loop
    if payload.get("agent_id"):
        return None  # a dispatched subagent holds no dispatch authority

    if os.environ.get("COORDINATOR_HARNESS_DIRECTIVE_NUDGE_OFF") == "1":
        return None

    sentinel = _sentinel_path(payload)
    if sentinel and os.path.exists(sentinel):
        return None  # fast-path skip; the real (atomic) gate is _claim_fire below

    if not message_trips_tell(_final_message_text(payload)):
        return None

    if _session_has_dispatched(payload):
        return None  # evidence of dispatch this session falsifies the tell

    if not _claim_fire(sentinel):
        return None  # lost the race to another concurrent Stop for this session

    _, has_true_sid = _session_key(payload)
    message = _NUDGE_MESSAGE
    if not has_true_sid:
        # Review: code-reviewer — F7: no session_id means the sentinel is
        # PID-scoped, so fire-once silently degrades to fire-every-time.
        # Surface that, matching the nudge_em_code_dispatch.py precedent.
        message += (
            "\n[nudge] (sentinel is invocation-scoped: no session_id was"
            " present, so this nudge may repeat.)\n"
        )
    # Routed through the shared chokepoint (coordinator_core._hook_envelope) so
    # this emitter's bytes are captured by capture_session() alongside every
    # other prose-carrying builder call, per AC12. This module's transport is
    # NOT the harness's hookSpecificOutput JSON protocol (see the example-doctrine-repo-resident
    # stdin/stderr shim note in the module banner above) — only ``message`` is
    # ever read by the caller — so the envelope is built for measurement and
    # then unwrapped back to the same ``{"message": <str>}`` shape this op has
    # always returned. Byte-for-byte: context_only() wraps ``message`` without
    # altering it, so the unwrapped string is identical to the pre-routing one.
    envelope = context_only("Stop", message)
    return {"message": envelope["hookSpecificOutput"]["additionalContext"]}


if __name__ == "__main__":  # pragma: no cover - manual probe path
    result = op(json.load(sys.stdin))
    if result:
        print(result)
