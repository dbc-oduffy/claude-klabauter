"""
coordinator_core.session.guard_unlock_sentinel — single-source resolver for
the in-session hard-deny guard unlock sentinel, keyed by BOTH session id and
guard module name.

WHY THIS EXISTS

Every hard-deny guard override today is a `COORDINATOR_OVERRIDE_*`
environment variable that must be set before session launch. The moment that
teaches an operator the override exists — the deny message itself — is
inside a session that can no longer set it: a Bash export does not reach the
harness's own PreToolUse hook evaluation, and there is no way to relaunch
mid-task without losing the reason the override is needed. This module is
the reachable remedy: an operator, from a terminal outside the denied
session, drops a one-shot sentinel file naming the exact `(session_id,
guard_name)` pair to clear, and the engine seam that produced the deny
consumes it on the next attempt.

This is deliberately narrower than `dispatch_nudge_sentinel` and
`autonomous_sentinel`, its two siblings in this package: those are keyed on
session id alone because they gate one binary behavior per session. A guard
unlock must never generalize into "all guards off for this session" — see
the plan's Anti-scope — so the filename is keyed on the (session_id,
guard_name) pair, not the session id alone, and a single grant clears
exactly one guard once.

Resolution follows the same convention as both siblings: the platform temp
directory via `tempfile.gettempdir()` (honours TMPDIR/TEMP/TMP per-platform),
never a hardcoded POSIX `/tmp`. Windows is first-class here — DR-record
`docs/decisions/DR-222-health-sentinel-durability-parity-settings-home-dual-read.md`
is the closest prior ruling on sentinel-home conventions and does not apply
here: this sentinel has exactly one writer (the operator, by hand) and
exactly one home, so a dual-home read would be unjustified complexity, not
parity.

Spec backlink: docs/plans/2026-08-03-in-session-operator-unlock-for-the-hard-.md § C1.

Negative-spec:
    - Do NOT hardcode ``/tmp`` or reach for ``tempfile.gettempdir()``
      directly at a new call site for this sentinel — import and call
      ``sentinel_path()`` so there is exactly one place this convention can
      drift again.
    - Do NOT add a second candidate location or an OR-list of candidate
      homes for this sentinel — it has exactly one home.
    - Do NOT let ``consume()`` raise. A crash here would fail a guard OPEN
      — the one direction a hard-deny guard must never fail in — so every
      failure mode (vanished file, permissions error, unresolvable temp
      dir) is caught and normalized to ``False``.
    - Do NOT key the sentinel on session id alone. One unlock must clear
      exactly one guard in exactly one session (AC2) — never a blanket
      "all guards off" switch (see the plan's Anti-scope).
    - Do NOT join the sanitized ``(session_id, guard_name)`` components on
      ``_`` or any run of it (``__`` included). A literal ``_`` survives
      ``_sanitize_component`` unmodified, so an underscore-based separator
      is not a true delimiter and two distinct pairs can render to the same
      filename. ``sentinel_path`` joins on ``.`` instead, a character
      ``_sanitize_component`` always strips from both raw components, which
      makes the join collision-free by construction. Do not use a hash
      either — this path is typed by hand by a human operator, and
      readability is load-bearing for that.
    - Do NOT write this sentinel from agent-reachable code. It exists to be
      typed by a human operator; this module only resolves and consumes the
      path, it never creates one.
"""

from __future__ import annotations

import functools
import re
import tempfile
from pathlib import Path
from typing import Any, Dict

from coordinator_core.doe_root_pointer import read_doe_root_pointer

_SENTINEL_PREFIX = "coordinator-guard-unlock-"

#: Portable, never-resolved pointer for the coordinator-only install (no example-doctrine-repo
#: checkout on this machine): the settings root is a real, portable path on
#: every coordinator machine (unlike an in-process-resolved home directory --
#: see this module's own Negative-spec and ``_helpers.OVERRIDE_KEYS_DOC_
#: DISPLAY``'s 2026-08-05 history for why an interpolated absolute path is
#: the wrong shape here). Left as ``~/...`` literally -- never expanded via
#: ``Path.home()`` or ``os.path.expanduser`` -- because expansion would
#: reintroduce exactly the machine-specific leak this form exists to avoid.
_SETTINGS_ROOT_WIKI_POINTER = "~/.coordinator-claude-settings/coordinator-claude/docs/wiki/"

#: Repo-qualified, relative pointer for a example-doctrine-repo user -- same citation
#: convention CLAUDE.md already uses for cross-repo references ("example-doctrine-repo
#: coordinator/docs/wiki/..."), and the same shape as ``_helpers.
#: OVERRIDE_KEYS_DOC_DISPLAY``. Preferred over the settings-root form when a
#: example-doctrine-repo checkout is present, per the PM's dual-install ruling: the example-doctrine-repo source
#: tree is the fresher, editable copy for a example-doctrine-repo user who has both.
#:
#: Repointed 2026-08-11 to the dedicated page: example-doctrine-repo landed
#: ``coordinator/docs/wiki/guard-unlock-channel.md`` (commit fe0919f3b) in
#: their tree. Safe to name the page (not just the directory) here because
#: this branch only resolves when a example-doctrine-repo checkout is present on this machine,
#: and on any such machine the file lands on disk the moment that commit is
#: pulled -- there is no install/seed step in between for this branch.
_DOE_SOURCE_WIKI_POINTER = "example-doctrine-repo coordinator/docs/wiki/guard-unlock-channel.md"

#: Deliberately LEFT pointing at the directory, not the page -- do not
#: repoint this one to ``guard-unlock-channel.md``. Unlike the example-doctrine-repo-source
#: branch above, this pointer is read on a coordinator-only machine with no
#: example-doctrine-repo checkout, where the file only ever arrives via example-doctrine-repo's seed-set install
#: (``~/.coordinator-claude-settings/coordinator-claude/docs/wiki/``). The
#: page exists in example-doctrine-repo's tree as of 2026-08-11 but is NOT yet in example-doctrine-repo's seed
#: allowlist (that allowlist doubles as their public OSS publish allowlist,
#: so admission is now a PM decision on example-doctrine-repo's side, outcome unknown) -- so
#: naming the page here would hand a 404 to precisely the operator with the
#: fewest other ways to find the answer. REPOINT HERE once example-doctrine-repo admits the
#: page to their seed set (see the cross-repo memo dispatched alongside this
#: change); until then this constant names the wiki DIRECTORY, not a page.
#: This comment is the single marked place to update when it does; do not
#: scatter a second reference to this pointer string elsewhere.

_UNSAFE_CHARS = re.compile(r"[^a-zA-Z0-9_-]")


def _sanitize_component(value: str) -> str:
    """Slugify a path-filename component; never lets a separator through.

    Guard module names and session ids are ``[a-z0-9_]`` by construction,
    but this does not assume it: anything outside the safe set (path
    separators, ``..``, whitespace, null bytes, and ``.``) is replaced with
    ``_`` rather than passed through. Note this means a literal ``_`` DOES
    survive sanitization unmodified — so ``_`` (and any run of it, e.g.
    ``__``) is never collision-free as a join separator between two
    sanitized components; see ``sentinel_path``, which joins on ``.``
    instead, a character this function always strips.
    """
    cleaned = _UNSAFE_CHARS.sub("_", value)
    return cleaned or "_"


def sentinel_path(session_id: str, guard_name: str) -> Path:
    """Return the guard-unlock sentinel path for ``(session_id, guard_name)``.

    Resolves the platform temp directory via ``tempfile.gettempdir()``
    rather than a hardcoded POSIX ``/tmp`` — the single point of truth for
    both the operator who writes this sentinel by hand and the engine seam
    that consumes it. Keyed on both components so one unlock clears exactly
    one guard in exactly one session (AC2).

    Joined on ``.`` rather than ``_``/``__``: ``_sanitize_component``'s
    ``[^a-zA-Z0-9_-]`` class maps every ``.`` inside either raw component to
    ``_``, so a literal ``.`` can only ever appear in the string at the one
    position this function itself inserts it. That makes the two-way split
    between session and guard unambiguous by construction, regardless of
    what characters either raw component contains — unlike the prior ``__``
    join, where a literal ``_`` surviving sanitization meant two distinct
    ``(session_id, guard_name)`` pairs could render to the identical
    filename (see the module's Negative-spec block).

    Deliberately not a hash of either component: an operator types this
    path by hand, off the deny message, from a terminal outside the denied
    session — a hashed component would destroy the one property (glanceable
    readability) that makes the whole mechanism usable. Do not "fix" this
    back to a hash.
    """
    session_part = _sanitize_component(session_id)
    guard_part = _sanitize_component(guard_name)
    return Path(tempfile.gettempdir()) / f"{_SENTINEL_PREFIX}{session_part}.{guard_part}"


def consume(session_id: str, guard_name: str) -> bool:
    """One-shot consume the unlock for ``(session_id, guard_name)``.

    Returns True exactly once per grant: if the sentinel exists, it is
    unlinked and True is returned; a second call for the same pair (no new
    sentinel written in between) returns False, so a denied write is
    re-denied on retry (AC3).

    Race-safe by construction: ``Path.unlink()`` is the atomic OS operation
    that decides ownership when two callers race on the same path — the
    caller whose unlink succeeds observes True, the other observes
    ``FileNotFoundError`` and is normalized to False below.

    Never raises. Every failure mode — the file already gone, a permissions
    error, an unresolvable temp directory — is caught here and returned as
    False, because a crash in this function would fail its caller's guard
    OPEN, which is the one direction a hard-deny guard must not fail in.
    """
    try:
        path = sentinel_path(session_id, guard_name)
    except Exception:
        return False

    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return False
    except Exception:
        return False


@functools.lru_cache(maxsize=1)
def _unlock_wiki_pointer() -> "tuple[str, bool]":
    """Resolve which wiki pointer form ``annotate_deny`` names, paired with
    whether that pointer is still pending: the example-doctrine-repo source-tree form (page,
    not pending -- the page landed 2026-08-11) when a example-doctrine-repo checkout is
    present on this machine, the settings-root form (directory, still
    pending -- awaiting example-doctrine-repo seed-set admission) otherwise (PM's dual-install
    ruling, this dispatch).

    Returns ``(pointer, pending)`` rather than the pointer alone so the
    pending-ness travels with the branch that decided it, instead of
    ``annotate_deny`` re-deriving "is this pointer a page or a directory"
    from the string shape -- the resolver already knows which branch fired;
    string-sniffing the result back out in the caller would be a second,
    driftable place to encode the same fact this function just decided.

    Cheap and cached: this runs on the PreToolUse hot path on every guard
    firing (``annotate_deny``'s own contract), so the presence check is a
    single ``registry_get`` read plus one ``Path.is_dir()`` stat, memoized
    for the process lifetime via ``lru_cache`` -- the same pattern this
    codebase already uses for other hot-path, presence-stable resolutions
    (``machine_resolver._git_user_email_cached``). A example-doctrine-repo checkout does not
    appear or vanish mid-process, so process-lifetime caching cannot go
    stale within a single guard-firing session.

    Never raises, and degrades to the settings-root form (pending=True) on
    ANY doubt (unresolved registry key, missing directory, or any exception
    along the way) -- ``annotate_deny``'s never-raises contract is absolute,
    and a crash here would fail a guard OPEN.

    TEST SEAM: this function is process-lifetime memoized via ``lru_cache``,
    so whichever branch resolves first in a given pytest process is what
    every later call sees for the rest of that process -- a test asserting
    on either branch's text (or pending-ness) MUST call
    ``_unlock_wiki_pointer.cache_clear()`` both before exercising its own
    monkeypatched state and after (so it does not leak a resolved value into
    whichever test runs next), rather than relying on import-time or
    test-order luck.
    """
    try:
        doe_root = read_doe_root_pointer()
        if doe_root and (Path(doe_root) / "coordinator" / "docs" / "wiki").is_dir():
            return (_DOE_SOURCE_WIKI_POINTER, False)
    except Exception:
        pass
    return (_SETTINGS_ROOT_WIKI_POINTER, True)


def annotate_deny(
    out: Dict[str, Any],
    session_id: str,
    guard_name: str,
    doc_display: str,
    *,
    agent_id: str = "",
) -> Dict[str, Any]:
    """Append the in-session-unlock block to a firing hard-deny envelope,
    AFTER the guard's own reason.

    Lives here rather than in either engine because both engine seams
    (`write_guards.engine`, `bash_guards.dispatch`) emit the identical block
    and a second copy is exactly the drift this chunk's one-seam design
    exists to prevent — the same reasoning that put the intercept at the
    engines instead of in 47 guards applies to the text those engines print.

    ORDERING: reason first, unlock block second (2026-08-11 flip; was
    prepend, now append). Driving incidents, in order:

    1. `cross-repo/archive/2026-08-10-example-retrieval-repo-em-guard-unlock-banner-
       needs-separate-invocation-clause.md` — the leading disclaimer needed
       its own sentence, not a shared one with the bypass instruction. Fixed
       by giving the disclaimer its own leading clause (still true today).
    2. `cross-repo/inbox/2026-08-11-example-doctrine-repo-em-guard-unlock-banner-still-
       reads-as-agent-instruction.md` — leading was not sufficient. Four
       independently-dispatched agents still hit the (then-prepended) line
       and three classified it as prompt injection: an agent under a
       hard-deny reads top-to-bottom and acts on the first actionable thing
       it meets, and a fully-resolved path plus a bare imperative
       ("create ... FIRST") plus success-sequencing advice IS actionable,
       disclaimer or not. The deny and its reason must be the first thing
       met — an agent that complies disables a hard-deny guard, the one
       direction such a guard must never fail in. The unlock block is for a
       human already sitting at a terminal, who is not skimming top-to-
       bottom under a stop signal, so it trails instead.
    3. PM ruling, 2026-08-11 (same dispatch as item 2): appending was still
       not the whole fix — the appended block still handed the reader a
       working, fully-parameterized bypass RECIPE (the literal
       `sentinel_path(...)`, plus the ordering fact framed as "how to make a
       combined call succeed"). In the PM's words, that is "here, take a
       bite out of this lovely apple," not an informational pointer. The
       recipe comes out entirely: this function now names that the unlock
       exists and is an operator affordance, and routes to the wiki (via
       `_unlock_wiki_pointer()`) plus the existing `doc_display` reference —
       never the sentinel path itself, never an imperative verb, never
       create-then-retry sequencing. An operator reading this message takes
       one extra hop (the wiki/doc) to construct the path themselves; that
       hop is the point, not an oversight.
    4. Cross-repo memo (example-market-data-repo-em, plus two prior siblings,
       2026-08-11, same day as item 3, this dispatch): the block's OPENING
       clause — `"Bypass options for a human operator, not this agent:"` —
       was itself a tell, not a safeguard: a code-reviewer and a
       review-integrator, independently and with no shared context,
       classified it as prompt injection and declined to act, because that
       exact "not this agent" register is what an attacker writes to make an
       agent feel authorised while preserving deniability. Fixed by dropping
       the disclaimer phrasing entirely and opening the block with the plain
       fact it existed to carry — that this unlock is a human-only
       affordance — stated directly rather than through a disclaimer frame.
       Every other property (facts carried, ordering, de-imperativized body,
       no resolved path) is unchanged; only the opening register moved.

    Only `annotate_deny` flips position. `bash_guards._helpers.
    operator_override_note`'s own leading placement (in its callers'
    concatenation, not this function) is unchanged and correctly so: its
    payload names an affordance an in-session agent structurally cannot use
    — a pre-launch env var read once at hook-process spawn — so the payload
    corroborates the human-only framing rather than contradicting it,
    regardless of where it sits. That function's own docstring (NEGATIVE
    SPEC 4, 2026-08-11) carries the parallel fix for its payload: no
    assignment form, ever. This function's payload used to be the opposite:
    the sentinel it names IS creatable from inside the session, right now,
    by the reader, which made leading-but-imperative read as agent-directed
    sanction instead of an operator affordance.

    Within the appended block the opening sentence still states the
    human-only framing FIRST, ahead of the wiki pointer and the two
    identifiers — that ordering is unchanged, only the sentence's wording
    moved (item 4, 2026-08-11: no more disclaimer register). The block is
    also de-imperativized: no bare verb ("create ... FIRST"), no
    success-sequencing register ("chaining it onto the denied command
    re-denies"), and (item 3, 2026-08-11) no resolved sentinel path at all.
    It restates the operator-actionable FACT (this unlock exists, and is a
    human-only affordance) descriptively, and is made explicitly
    self-limiting — naming that this sentinel is created from a terminal
    OUTSIDE this session, and that an agent inside the session creating it
    is a doctrine violation (matching this module's own Negative-spec:
    "Do NOT write this sentinel from agent-reachable code").

    The exact `sentinel_path(session_id, guard_name)` literal is
    deliberately NOT rendered here as of item 3 above — an operator
    constructs it themselves, following the wiki/doc's documented shape.
    The bare `session_id`/`guard_name` VALUES are still rendered, though
    (2026-08-11, second PM pass, same dispatch): removing the assembled
    path must not also remove the two per-firing data points no static
    wiki page can ever supply on its own — the RECIPE (a ready-to-paste
    path, an imperative, create-then-retry sequencing) is what came out;
    the two bare identifiers are DATA a human still needs to follow the
    wiki's construction steps, and are named explicitly as such ("the
    construction steps there need these two values from this firing"),
    never assembled into a path or paired with an instruction to act on
    them.

    `doc_display` is passed in rather than resolved here: both callers
    already import `bash_guards._helpers._resolve_override_keys_doc_display`,
    and resolving it here would add a `session -> bash_guards` import edge
    for one string. It is named alongside `_unlock_wiki_pointer()`'s result
    only when that result's `pending` flag is True — an interim reference
    for the branch whose dedicated wiki page has not landed on this machine
    yet (settings-root form, pending on example-doctrine-repo's seed-set admission; see that
    function's own docstring and the REPOINT comment above its constants).
    On the non-pending (example-doctrine-repo-source) branch the dedicated page is the whole
    answer and `doc_display` is dropped from the line — it was never itself
    the unlock's home, only a stand-in for the page that branch no longer
    lacks.

    A missing/empty ``session_id`` skips the line rather than rendering a
    sentinel path keyed to an empty session — there would be nothing literal
    and actionable to print.

    5. (2026-08-11, this dispatch) `agent_id`-gated suppression: this block
       is a human-only affordance (its own opening sentence says so) — a
       dispatched subagent cannot exercise it, so on that channel its
       information value is zero while its cost is real (items 2/4 above:
       independently, four dispatched agents and two reviewers classified
       this exact block as prompt injection and either refused to act or
       flagged it, spending report space and eroding fleet trust on text
       that channel structurally cannot use). Rather than a fifth wording
       pass — wording was never the variable — this block is now emitted
       only when the deny is headed to a session a human is plausibly
       watching, and suppressed entirely for a resolved dispatched
       subagent.

       Discriminator: `coordinator_core.session.identity.
       resolve_subagent_identity(agent_id, session_id)`, imported locally in
       this function body (not at module level) — this module and
       `bash_guards` must not gain a new cross-package import edge, per this
       function's own `doc_display`-is-passed-in-not-resolved-here
       paragraph above; `identity` lives in this same `session` package, so
       calling it here adds no new edge, unlike resolving `doc_display`
       here would.

       FAIL DIRECTION (get this right; it is the safety-critical half of
       this change): a positively-RESOLVED subagent identity (non-empty
       return) is the ONLY condition that suppresses the block. Every other
       case emits it, unchanged from before this item:
         - `agent_id` absent/empty -> the EM/main session, where a human IS
           watching -> emit.
         - `agent_id` present but unrecognised/malformed ->
           `resolve_subagent_identity` already fails closed and returns
           `""` for exactly this case (see that function's own docstring,
           path (c)) -> treated identically to "absent" -> emit. This
           function does NOT special-case "malformed" as its own suppress
           condition; it only ever suppresses on a genuine positive
           resolution, so a resolver bug or an unexpected future harness
           shape degrades to emitting the block (a human still gets their
           session/guard values), never to silently dropping the one
           affordance an operator needs to recover from a hard-deny they
           did not expect. Losing the block for a human is the real cost
           here (the operator still needs the session/guard values from
           this exact firing); emitting it once too often for an edge-case
           subagent is merely the injection-classification cost this item
           exists to reduce, not eliminate at all costs.
       This mirrors `resolve_subagent_identity`'s own fail-closed contract
       (empty return = "not a resolved subagent") composed with THIS
       function's fail-open-on-affordance direction (empty/unresolved =
       "emit, don't suppress") — two different fail directions for two
       different risks, deliberately not the same polarity.

    Never raises. A malformed envelope is returned unchanged: this function
    only ever runs on a deny that has already been decided, and an
    augmentation that crashed would turn that settled deny into an engine
    crash.
    """
    if not session_id:
        return out
    try:
        from coordinator_core.session.identity import resolve_subagent_identity

        if resolve_subagent_identity(agent_id, session_id):
            return out
    except Exception:
        pass
    try:
        hso = out.get("hookSpecificOutput")
        if not isinstance(hso, dict):
            return out
        reason = hso.get("permissionDecisionReason")
        if not isinstance(reason, str):
            return out
        wiki_pointer, wiki_pending = _unlock_wiki_pointer()
        pending_clause = (
            " (wiki page for this channel still pending -- meanwhile also %s)"
            % doc_display
            if wiki_pending
            else ""
        )
        line = (
            "An in-session unlock exists for this guard, but it is a "
            "human-only affordance: it is granted by a human operator from "
            "a terminal outside this session, it cannot be granted by this "
            "agent, and creating it from inside the session is a doctrine "
            "violation, not a shortcut. How to construct and grant it is "
            "documented at %s%s; the construction steps there need these "
            "two values from this firing: session %s, guard %s."
            % (wiki_pointer, pending_clause, session_id, guard_name)
        )
        hso["permissionDecisionReason"] = "%s\n\n%s" % (reason, line)
    except Exception:
        return out
    return out
