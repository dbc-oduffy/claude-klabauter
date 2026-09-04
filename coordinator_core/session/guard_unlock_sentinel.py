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

Spec backlink: pln-in-session-operator-unlock-for-aa6cf9 § C1.

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
    - This module itself still NEVER creates a sentinel — it only resolves
      and consumes the path (that part of the old rule stands unchanged).
      What is retired, by PM ruling 2026-08-13
      (docs/plans/2026-08-13-em-exercisable-in-band-grant-route.md), is the
      former absolute claim that NO agent-reachable code may ever write this
      sentinel: ``coordinator_core.session.em_guard_grant`` is now a
      sanctioned EM-side writer, but only for the two guard names in its own
      ``_GRANTABLE_GUARDS`` allowlist (``bump-foreign-repo-write``,
      ``bump-outside-repo-write``) — not a general carve-out for this
      module or for agent-reachable code at large. That write path is
      gated upstream by two guards, not by anything in this module:
      ``bash_guards/block_subagent_guard_grant.py`` blocks the Bash
      acquisition channel, and ``write_guards/block_subagent_guard_grant_write.py``
      blocks the Write-tool channel against BOTH grant artifacts — the
      durable record AND this sentinel path itself, the latter being the
      load-bearing leg, since this sentinel is what actually clears a
      guard. Both restrict the write to the EM actor, never a dispatched
      subagent. For
      every guard name outside that two-item tier, and for every actor other
      than the gated EM-side writer, hand-typing by a human operator remains
      the only route to this sentinel.
    - Do NOT re-inline the sentinel's filename shape, its drop location, or
      the per-firing ``session_id`` into ``annotate_deny``'s rendered text,
      and do NOT render a pointer into the override-key/unlock doc surface
      (``message_register._rules`` B8 leg (d) grades that a gate-referent).
      This was tried once (2026-08-12) and reverted (2026-08-13, C3, item 7
      in ``annotate_deny``'s docstring) — the recipe stays out.
      AMENDED 2026-09-03 (item 11): ``guard_name`` is NO LONGER in this
      prohibition and is now rendered. It was removed by item 7 as one of
      *two* data points that, next to the filename shape, let a reader
      assemble the path by hand; the other two stay out, so a bare name
      assembles nothing. It is restored because it is the argument
      ``em_guard_grant``'s CLI takes — withholding it made the sanctioned
      in-band route unusable from the deny that triggers it. Keep the
      distinction the rest of this bullet rests on: IDENTITY (what fired)
      renders; the RECIPE (how to clear it) does not.
    - Do NOT let ``annotate_deny`` gate its render on identity resolution.
      AC-3 (2026-08-13, C3, item 8) once inverted the fail direction here so
      that only a positively-resolved EM audience emitted; item 10 then
      deleted the resolution entirely once the render was empty. As of item
      11 the rendered fact is the guard NAME, which is audience-independent
      by construction — a subagent may read which guard blocked it (that is
      what makes its "report to the dispatching EM" route accurate) and
      gains no affordance from knowing. Re-adding an audience branch here
      would resurrect a resolver on every deny path to withhold a label that
      is safe for both audiences. The audience-SPLIT that still matters is
      the in-band grant ROUTE, and it lives in
      ``bash_guards._write_bump_message.render_em_message`` — EM-class only,
      never rendered by this module.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

_SENTINEL_PREFIX = "coordinator-guard-unlock-"

#: REMOVED 2026-09-03: ``_SETTINGS_ROOT_WIKI_POINTER``, the settings-root
#: wiki-directory pointer this module used to render. It went unread when
#: item 9 stopped rendering any doc pointer here, and it can never come
#: back: ``message_register._rules`` B8 leg (d) grades a pointer into the
#: override-key/unlock doc surface a gate-referent, which is what item 9
#: measured. Item 11's render carries the guard NAME instead, and no path.
#:
#: DR-290 form 2 (the literal, never-expanded settings-root pointer) is NOT
#: lost with it -- ``bash_guards._override_doc.OVERRIDE_KEYS_DOC_DISPLAY``
#: is that exact string plus the page filename, is live, and is the pointer
#: readers actually receive. Cite THAT constant as the canonical form; the
#: three comments here and in ``_override_doc`` that used to cite this one
#: as the shape that "already ships" were describing an exemplar that had
#: stopped shipping.

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


def annotate_deny(
    out: Dict[str, Any],
    session_id: str,
    guard_name: str,
    doc_display: str,
    *,
    agent_id: str = "",
    git_root: Optional[str] = None,
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
    2. `cross-repo/inbox/2026-08-11-doe-claude-em-guard-unlock-banner-still-
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

    SUPERSEDED BY ITEM 9 BELOW: the appended block used to open with a
    sentence stating the human-only framing FIRST, ahead of the wiki
    pointer and doc_display reference, de-imperativized (no bare verb, no
    success-sequencing register, no resolved sentinel path). Item 9
    (2026-08-13, C4d) removes that framing sentence entirely — the block is
    now the two doc/wiki pointers alone, nothing else — kept here as the
    historical record of what item 9 replaces.

    The exact `sentinel_path(session_id, guard_name)` literal — the resolved
    absolute path, including the platform temp directory — is deliberately
    NOT rendered here: a ready-to-paste path is still the recipe item 3
    above removed. As of item 7 below, NEITHER the sentinel's filename shape
    NOR the per-firing `session_id`/`guard_name` values are rendered either
    — only the wiki/doc pointers are, so an operator takes the extra hop
    item 3 established rather than being handed the two data points a
    filename-shape line would let them assemble into the same recipe by
    hand.

    `doc_display` is passed in rather than resolved here: both callers
    already import `bash_guards._helpers._resolve_override_keys_doc_display`,
    and resolving it here would add a `session -> bash_guards` import edge
    for one string. It is named as a supporting reference alongside the
    settings-root wiki pointer — as of item 7 below, the two pointers ARE
    the payload again; item 6's inlined filename shape and drop-location
    description is reverted.

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

       FAIL DIRECTION (2026-08-11 as originally written; SUPERSEDED by item 8
       below — kept verbatim here as the historical record item 8's rebuttal
       responds to): a positively-RESOLVED subagent identity (non-empty
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
       different risks, deliberately not the same polarity. See item 8: this
       fail-open-on-unresolved direction is exactly what AC-3 now reverses.
    6. (2026-08-12, this dispatch) Branch collapse and instruction inlining
       — REVERTED by item 7 below, kept here as the historical record: the
       DoE-source-tree pointer branch is gone — the dedicated wiki page
       (`guard-unlock-channel.md`) has not landed in DoE's seed set, so the
       settings-root pointer is now unconditional, and `_unlock_wiki_pointer`
       (with its process-lifetime cache) is gone with it — nothing is left
       to resolve between. The pointer alone was insufficient even before
       this collapse: the settings-root wiki DIRECTORY exists but the page
       does not, so a live pointer to that directory still did not answer a
       blocked operator without a second hop. The line below inlines the
       sentinel's filename shape and drop location directly, descriptively,
       so a human at a terminal can construct the path without opening
       anything; the wiki/doc references remain as supporting citations,
       not the payload. Every register property from items 2–5 (descriptive
       not imperative, no create-then-retry sequencing, no "not this agent"
       disclaimer, human-only framing first, `agent_id` suppression and its
       fail direction, never-raises) is unchanged.
    7. (2026-08-13, C3, tasks/guard-messages-keys/C3.md Task 1) Revert of
       item 6: item 6 re-inlined exactly the recipe item 3 removed —
       the sentinel FILENAME SHAPE, the DROP LOCATION description, and
       BOTH per-firing identifiers (`session_id`, `guard_name`) as live
       parameters in the rendered text. Nothing failed when that regression
       landed (`TestAnnotateDenyInlinesTheUnlockInstruction` in
       `session/tests/test_guard_unlock_sentinel.py` was authored alongside
       item 6 to assert the regressed text as correct, rather than a
       pre-existing negative-spec test asserting those values were ABSENT —
       there was no ratchet in the other direction for a reader to trip).
       Item 3's own reasoning (still true, quoted there): the recipe comes
       out entirely so "an operator reading this message takes one extra
       hop (the wiki/doc) to construct the path themselves; that hop is the
       point, not an oversight." This item takes that back out: the block
       is the settings-root wiki pointer plus `doc_display`, nothing else,
       matching item 3's original design. The settings-root pointer stays
       unconditional (item 6's branch collapse is NOT reverted — only the
       filename-shape/drop-location/identifier inlining is).
    8. (2026-08-13, C3, tasks/guard-messages-keys/C3.md Task 2, AC-3) Fail
       direction inverted: item 5's FAIL DIRECTION above reasoned, quoting
       `state/audits/2026-08-11-guard-text-injection-mechanism-proof.md`
       § "The fix, and its measurement" — "Fail direction: only a
       *resolved* subagent suppresses. Absent `agent_id` means the main/EM
       session where a human is watching — emit. Malformed/unresolvable —
       emit, because `resolve_subagent_identity` fails closed to `""` and
       losing the operator's affordance is the worse error." That reasoning
       predates two things it did not have when it was written: the PM's
       2026-08-13 audience ruling, and a positive-EM predicate
       (`identity.resolves_em_audience`). It was choosing between two bad
       options with no third leg — emit-on-uncertainty (leaks to a
       structurally-incapable channel; items 2/4 above record the concrete
       cost, four dispatched agents and two reviewers classifying the block
       as prompt injection) or suppress-on-uncertainty (loses a real EM's
       affordance). This plan adds the third leg: a positive-EM signal
       (`resolves_em_audience`, DECISIONS.md D1) distinguishes "observed a
       real envelope with no agent identity" (EM — emit) from "could not
       observe" (degrade to terse), so "unresolved" no longer has to default
       to either bad option. Both live branches item 5 named as
       fail-open-to-emit now fail-closed-to-terse instead:
         - `except Exception: pass` (any exception during identity
           resolution) -> now returns `out` unchanged (degrade), never
           falls through to the render block.
         - the implicit else (absent/empty/malformed `agent_id`,
           unresolvable envelope) -> routed through `resolves_em_audience`,
           which is False for all of these (DECISIONS.md D1 contract) ->
           degrade.
       `resolve_subagent_identity` stays as the FAST positive-subagent leg
       (unchanged from item 5: a genuine positive resolution still
       suppresses immediately, no need to consult the new predicate) — the
       EM-audience DECISION, not the subagent fast path, is what now routes
       through `resolves_em_audience(payload, git_root)` instead of
       defaulting to emit.

    9. (2026-08-13, C4d, docs/plans/2026-08-13-guard-messages-stop-handing-
       agents-the-keys.md AC-2) The appended block comes out entirely --
       `annotate_deny` now always returns `out` unchanged. Items 3/4/7
       above progressively stripped the RECIPE (sentinel path, filename
       shape, drop location, identifiers) while keeping the FACT that an
       unlock exists, framed as human-only and its use a doctrine
       violation; this dispatch tried narrowing the block to a bare
       doc/wiki-pointer sentence next ("See <wiki-pointer> and <doc_
       display> for guard-override conventions.") and measured it against
       `message_register._rules.run_rule("B8", ...)` -- B8's own leg (d)
       ("a doc/wiki pointer to the override-key registry or unlock wiki")
       fired on that sentence too: B8 treats ANY pointer into the
       override-key/unlock doc surface as a gate-referent, not only a
       resolved path or an explanatory sentence around it. There is no
       narrower rendered form left between "the old disclosure paragraph"
       and "nothing" that B8 grades clean, so this item lands on "nothing":
       the function's return value is now `out`, unmodified, on every call.
       The `agent_id`/`resolves_em_audience`/`session_id` resolution logic
       from items 5/8 is kept (harmless, and cheap insurance if a future
       chunk reintroduces a narrower render gated on the same audience
       decision) but its result no longer branches into any render step.

    10. (2026-08-13, staff-eng review, B8 leg (c)/AC-6) Item 9 above kept
        ~20 lines of identity resolution live-but-unbranching as "cheap
        insurance if a future chunk reintroduces a narrower render." Two
        costs review-integrator agreed outweigh that insurance: (1) it ran
        a backpointer-capable resolver on every deny path for zero effect;
        (2) it made AC-6 ("annotate_deny is reachable by the register
        lint") vacuous — the lint reached a render that was unconditionally
        empty, the exact "NO GATE GOES VACUOUS" trap C7 names. The body is
        now the honest DECIDABLE BRANCH: `return out`, unmodified, always.
        The ~6 lines of resolution logic are trivially reconstructible from
        `resolves_em_audience`/`resolve_subagent_identity` if a future
        render appears; AC-6 is withdrawn in the plan text (see this
        plan's AC table) rather than kept pointed at a seam that no longer
        renders.

    11. (2026-09-03) The block renders again, reduced to ONE fact items 3-10
        never actually weighed: the NAME of the guard that fired.

        Item 9 concluded "there is no narrower rendered form left between
        the old disclosure paragraph and nothing" after B8 leg (d) fired on
        a bare doc/wiki-pointer sentence. That conclusion generalized from a
        single candidate. B8 leg (d) is about POINTERS INTO THE OVERRIDE-KEY/
        UNLOCK DOC SURFACE -- it fires on the doc reference, not on prose per
        se. Measured 2026-09-03 against `run_rule("B8", ...)`: the wiki-
        pointer sentence flags (gate-referent), while ``Guard: `<name>`.``
        returns clean. A narrower clean form existed the whole time; items
        9/10 landed on "nothing" without testing one.

        WHAT THIS DOES NOT REINTRODUCE, and why the earlier items stand:
        no sentinel path, no filename shape, no drop location, no
        `session_id`, no doc/wiki pointer, no imperative verb, no
        create-then-retry sequencing, no statement that an unlock exists.
        Every removal items 3/4/7/9 made is still in force. A guard name is
        IDENTITY, not an affordance -- it names the thing that fired, not a
        button; nothing can be written with it; and it is the same label the
        crash path (`bash_guards.dispatch._crash_deny`) has always rendered,
        so it is not new disclosure on this surface at all.

        WHY IT IS LOAD-BEARING RATHER THAN NICE-TO-HAVE. `session.
        em_guard_grant` is the EM-exercisable in-band route for exactly the
        `_GRANTABLE_GUARDS` pair, and its CLI takes the guard name as its
        first argument. With the name rendered nowhere, that route was
        unusable in principle: an EM could know the route exists and still
        not be able to fill in its argument from the deny it just received.
        The affordance ratcheted to zero across items 3->10 -- each step
        defensible, the sum leaving a hard deny that named neither what
        blocked it nor any next step -- and the only surface still naming
        the guard was the operator-shaped sentinel channel, inverting
        guard-proportionality: the human-only door was findable and the
        agent-takeable one was not. Driving incident: example-cockpit-repo-em,
        2026-09-03, PM-commissioned write to a new directory, denied with no
        machine-takeable next step ("my word should be enough" -- PM).

        Subagent channel: unchanged in substance. A subagent is told to
        report to its dispatching EM (`_write_bump_message.
        render_subagent_message`); the guard's name is what makes that
        report accurate, and it names no mechanism the subagent could use.
        The in-band grant ROUTE stays EM-class-only and is rendered by
        `render_em_message`, not here.

    Never raises. A malformed envelope is returned unchanged: this function
    only ever runs on a deny that has already been decided, and an
    augmentation that crashed would turn that settled deny into an engine
    crash.
    """
    # Item 11 (2026-09-03): render the firing guard's NAME, nothing else.
    # Not a reversal of items 3/4/7/9 -- those removed the bypass RECIPE and
    # the pointer at the unlock doc surface, and both stay removed. A guard
    # name is IDENTITY, not an affordance: nothing can be written with it,
    # no button is named by it, and B8 grades it clean (measured, see the
    # docstring's item 11) precisely because it is not a gate-referent.
    del doc_display, agent_id, git_root, session_id
    if not guard_name:
        return out
    try:
        hso = out.get("hookSpecificOutput")
        if not isinstance(hso, dict):
            return out
        reason = hso.get("permissionDecisionReason")
        if not isinstance(reason, str) or not reason:
            return out
        hso["permissionDecisionReason"] = "%s\n\nGuard: `%s`." % (
            reason.rstrip(),
            guard_name,
        )
    except Exception:
        return out
    return out
