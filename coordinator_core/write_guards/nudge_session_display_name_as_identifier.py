"""coordinator_core.write_guards.nudge_session_display_name_as_identifier —
advisory guard.

Catches a SESSION DISPLAY NAME (``claude-klabauter-49``, ``doe-claude-3a``,
``klabauter-7f`` — a repo-slug shortname plus a short, disambiguating
suffix) used INSIDE a written record's body in an ATTRIBUTION or ADDRESS
construction: the object of a crediting verb (``fixed by``, ``reported
by``, ``credited to``, ...) or the value of a crediting frontmatter field
(``author:``, ``found_by:``, ...). The live incident this guard exists
for: a bug-backlog row said "ESTABLISHED AND FIXED BY claude-klabauter-49";
two live sessions answered to that display name at the time, the wrong one
got both the credit and the author's confirmation message, and nothing at
write time prompted the author to record the one value that actually
disambiguates a session — its uuid.

NARROWED 2026-08-30 (coordinator corpus sweep, `state/subagent-share/
aac212bc.../coordinatorexecutor.af9616168754bf1b1.md`): the first cut of
this guard fired on ANY name-shaped token anywhere in the body — 441/3535
in-scope records, 12.5%, most of them a session name mentioned in
narrative ("doe-claude-3e hit real divergence...", "measured ... by
Claude-klabauter-c0 and claude-klabauter-em jointly") rather than doing
identifier work. A guard that nags on narrative gets disabled, per the
brief's own warning, and then catches nothing — so the predicate now
requires an ATTRIBUTION/ADDRESS CONSTRUCTION (`_ATTRIB_VERB_RE`/
`_ATTRIB_ADDRESS_RE`/`_ATTRIB_FIELD_RE` below), not bare co-occurrence.
Re-measured against the same corpus sweep after narrowing: ~3.4% (120/3533),
sampled and found to be entirely genuine attribution constructions, none
narrative -- but 77 of those 120 were a tool-stamped `author:` field, not a
human crediting claim (see `_ATTRIB_FIELDS`'s own comment for the
`_resolve_plan_author` finding). Dropping `author:` from the field roster
(second narrowing pass, same date) brought the same sweep to ~1.3%; see the
`_ATTRIB_VERB_PHRASES`/`_ATTRIB_FIELDS` rosters' own docstring notes for the
exact verb/field set chosen, deliberately non-exhaustive (missing an exotic
phrasing is preferred over firing on narrative, per the brief).

CLASS is "advisory", not "hard-deny" — deliberately, not as a lesser
version of a block. Three reasons, and the guard must never be strengthened
past them without a fresh ruling:

  1. Prose LEGITIMATELY mentions session display names — an incident report
     narrating what happened (this module's own docstring, this guard's own
     tests, a lesson describing a name collision) is not the defect this
     guard exists to catch, and a deny would make those unwritable.
  2. The corpus evidence is that this is COSMETIC, not load-bearing: no
     reader anywhere in `coordinator_core/` or `coordinator/bin/` resolves,
     matches, or compares a session DISPLAY NAME against anything. The one
     field with real machine consequences, `claimed_by`, is already
     sid-typed (a uuid, per `coordinator_core.session.core._UUID_RE`) and
     lives separately from the free body text this guard scans.
  3. The whole value of this guard is reaching the AUTHOR at write time,
     while they still know the uuid — no later backfill recovers a session
     identity once the display name that named it has rotated to a
     different live session. A block would only ever refuse the write; an
     advisory is the only shape that can deliver that reminder without
     also blocking legitimate prose.

Scope — fires only on record classes where a name-as-identifier is a real
defect (the plan brief's own list, adopted verbatim): `state/bug-backlog/`,
`state/debt-backlog/`, `state/lessons/`, `docs/plans/`, `state/sizings/`,
`state/handoffs/`, `docs/decisions/`. These are exactly the record classes
that carry attribution/crediting prose about a session's work product, as
opposed to a session's own scratch/transcript surface.

Explicitly OUT of scope, per the same brief: `state/subagent-share/`
(a dispatched worker's own sidecar — self-referential, not a third-party
attribution surface), `archive/**` (already-closed, immutable-in-spirit
history; nagging a mechanical archive-move write teaches nothing), and
`docs/research/` (working notes, not a record another session's tooling
or a future reader treats as an attribution ledger). Transcripts and
anything under `.git/` are excluded the same way, and are additionally
never `Write`/`Edit`/`MultiEdit` targets in the first place.

KNOWN DISPLAY-NAME SLUGS. Detection is deliberately anchored to a small,
named roster of live repo shortnames (`_KNOWN_SLUGS`) rather than a bare
"hyphenated-word-plus-suffix" shape — the latter would fire on ordinary
prose (a CLI flag, a package name, a doc anchor) far more often than on a
real session name. Widen `_KNOWN_SLUGS` freely as new repos join the
fleet; narrowing it silently turns this guard into a no-op over whatever
slug was dropped, same failure shape `nudge_windows_subprocess_popup.py`'s
own negative-spec warns against for its own pattern list.

SUFFIX SHAPE. A display name's disambiguating suffix is short (1-4
alnum characters) and, empirically, always carries at least one digit
(`claude-klabauter-49`, `doe-claude-3a`, `klabauter-7f`) — a plain
letters-only trailing segment (`claude-klabauter-em`, a role suffix, not a
disambiguator) reads as a different kind of token and is deliberately left
alone; strengthening a real name-collision incident's own recorded shape
into a bug against this guard's fire condition, rather than a target to
widen it onto, would need a fresh incident to justify.

ATTRIBUTION/ADDRESS CONSTRUCTION, not bare mention. A display-name token is
only a defect when it is doing the work of an identifier: the object of a
crediting or addressing verb (`_ATTRIB_VERB_PHRASES` — "fixed by",
"reported by", "found by", "filed by", "raised by", "owned by", "resolved
by", "closed by", "authored by", "written by", "claimed by", "established
by", "confirmed by", "credited to", "assigned to", "delivered to",
"addressed to"), the recipient of a "message ... to" addressing
construction (`_ATTRIB_ADDRESS_RE`), or the value of a crediting
frontmatter field (`_ATTRIB_FIELDS` — `found_by`, `reported_by`,
`filed_by`, `raised_by`, `credited_to`, `owned_by`, `assigned_to`,
`fixed_by`, `resolved_by`, `claimed_by`). This roster is deliberately
non-exhaustive — missing an exotic phrasing is the accepted tradeoff for
not firing on narrative; widen it only against a fresh measured false
negative, never against a hunch. `author` is deliberately NOT in this
roster — see `_ATTRIB_FIELDS`'s own comment for why: it is
tool-stamped, not human-written, and an advisory against a tool's own
output has no action behind it.

FENCED CODE / INLINE CODE ARE EXCLUDED FROM THE SCAN, quoted prose is NOT.
A name pasted inside a ``` fenced block``` or a `` `backtick span` `` reads
as terminal output or a literal log line an author is transcribing, not an
attribution claim — firing there is exactly the "nags on pasted terminal
output, gets turned off within a day" failure the brief warns about, so
those spans are stripped before the scan runs. A name inside plain quoted
prose (`"ESTABLISHED AND FIXED BY claude-klabauter-49"`, the live incident's
own shape) is NOT excluded — that is precisely the crediting/attribution
use this guard exists to catch; quoting the phrase does not change what
work the name is doing in the sentence.

Negative-spec:
  - Does NOT fire on a display-name token merely CO-OCCURRING in the body —
    only an attribution/address construction (verb, "message ... to", or
    frontmatter field — see ATTRIBUTION/ADDRESS CONSTRUCTION above) fires.
    A session named in narrative ("doe-claude-3e hit real divergence...")
    is silent; pinned by `TestNarrativeMentionIsSilent`.
  - Does NOT deny/block anything — CLASS is "advisory"; the envelope
    carries only `additionalContext`, never `permissionDecision`. There is
    no override to name (B6 is inapplicable: nothing here can be bypassed).
  - Does NOT fire on a bare uuid (`coordinator_core.session.core._UUID_RE`
    shape) anywhere in the body — a uuid IS the correct identifier this
    guard is steering authors toward; flagging it would contradict the
    guard's own remediation.
  - Does NOT fire on `state/subagent-share/`, `archive/**`,
    `docs/research/`, or any path outside the seven scoped prefixes above.
  - Does NOT fire on a display-name-shaped token found only inside a fenced
    code block or an inline-code span — see FENCED CODE section above.
  - Does NOT fire on `NotebookEdit` — only `Write`/`Edit`/`MultiEdit` carry
    the record-body prose this guard scans.
  - Does NOT cover a record written via a shell heredoc
    (`cat > file <<'EOF' ... EOF`) — that path bypasses
    `PreToolUse(Write|Edit|MultiEdit)` entirely, so this guard never sees
    it. This is a known, stated coverage hole, not something this guard
    attempts to close; a heredoc-shaped write needs a different guard with
    a different threat model (Bash-tool payload inspection), out of scope
    here per the brief that authorized this module.
  - Does NOT fail closed on any error — an unexpected payload shape,
    unreadable pre-image, or parse failure degrades to silence (ALLOW),
    matching every advisory sibling's fail-open discipline.

Spec backlink: dispatch brief "session citation stops depending on a name"
  (Deliverable-Id: dlv-session-citation-stops-depending-on-a-name-1c3053)
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional

from coordinator_core.write_guards._case_fold_path import casefold_path
from coordinator_core.write_guards.nudge_windows_subprocess_popup import (
    _MAX_WHOLE_FILE_BYTES,
    _extract_content,
    _extract_file_path,
)

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
# Advisory band; next free slot after nudge_handoff_ac_shape (220). See
# docs/wiki/write-guard-priority-bands.md for the band convention. No
# lower-numbered advisory guard scans record-body prose for a session
# display name, so no same-surface collision applies.
PRIORITY = 221

#: In-scope path segments (forward-slash form; a raw payload path is
#: normalized to forward slashes before this check runs). A record class
#: is in scope iff its normalized path CONTAINS one of these as a
#: substring segment — matches `nudge_private_git_fact_resolver.py`'s own
#: "hot path marker" substring-containment style rather than a rooted
#: prefix compare, since the payload path may be absolute, cwd-relative,
#: or already git-root-relative depending on caller.
_IN_SCOPE_MARKERS = (
    "state/bug-backlog/",
    "state/debt-backlog/",
    "state/lessons/",
    "docs/plans/",
    "state/sizings/",
    "state/handoffs/",
    "docs/decisions/",
)

#: Explicitly out-of-scope markers, checked BEFORE `_IN_SCOPE_MARKERS` —
#: named here (not merely absent from that tuple) so a reader sees the
#: exclusion is deliberate. None of these strings currently collide with
#: an `_IN_SCOPE_MARKERS` entry, but the explicit-first check keeps that
#: true even if a future in-scope marker were ever a substring of one of
#: these (e.g. a nested `archive/` mirror of a scoped directory).
_OUT_OF_SCOPE_MARKERS = (
    "state/subagent-share/",
    "archive/",
    "docs/research/",
)

#: Known live repo shortnames a session display name is built from. Widen
#: freely as new repos join the fleet; see module docstring "KNOWN
#: DISPLAY-NAME SLUGS" for why narrowing this is a silent regression, not
#: a tightening.
_KNOWN_SLUGS = (
    "claude-klabauter",
    "doe-claude",
    "klabauter",
    "example-retrieval-repo",
    "coordinator",
    "claude-central",
    "project-widgets",
    "example-store-repo",
)

#: A known slug immediately followed by `-<1-4 alnum chars containing at
#: least one digit>`, word-bounded on both ends. Built from `_KNOWN_SLUGS`
#: at import time so the two never drift apart. The suffix charclass is
#: deliberately narrow (1-4 chars) -- see module docstring "SUFFIX SHAPE".
#: Kept as a STRING (not compiled directly) so `_ATTRIB_VERB_RE`/
#: `_ATTRIB_ADDRESS_RE`/`_ATTRIB_FIELD_RE` below can embed the identical
#: pattern inside their own larger constructions without drifting from it.
_NAME_CORE = (
    r"(?:" + "|".join(re.escape(slug) for slug in _KNOWN_SLUGS) + r")"
    r"-(?=[0-9a-zA-Z]{1,4}\b)[0-9a-zA-Z]*[0-9][0-9a-zA-Z]*"
)

#: Full-uuid shape (mirrors `coordinator_core.session.core._UUID_RE`) --
#: never itself flagged; a uuid is the correct identifier this guard
#: steers authors toward.
_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)

#: Fenced code block (``` ... ```), non-greedy, DOTALL so it spans lines.
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)

#: Inline code span (`` `...` ``), single line only -- markdown inline code
#: never spans a newline.
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")

#: Crediting/attributing verb phrases -- the name is their grammatical
#: OBJECT ("FIXED BY claude-klabauter-49"). See module docstring
#: "ATTRIBUTION/ADDRESS CONSTRUCTION" for the rationale and the
#: deliberately-non-exhaustive stance.
_ATTRIB_VERB_PHRASES = (
    "fixed by",
    "reported by",
    "found by",
    "filed by",
    "raised by",
    "owned by",
    "resolved by",
    "closed by",
    "authored by",
    "written by",
    "claimed by",
    "established by",
    "confirmed by",
    "credited to",
    "assigned to",
    "delivered to",
    "addressed to",
)

#: Crediting frontmatter fields -- the name is their VALUE (`found_by:
#: claude-klabauter-49`). See module docstring "ATTRIBUTION/ADDRESS
#: CONSTRUCTION".
#:
#: DELIBERATELY EXCLUDES `author` (2026-08-30 coordinator ruling, second
#: pass). `author:` is not human-written on a plan/lesson: `coordinator/bin/
#: coordinator-doc-new.py :: _resolve_plan_author()` stamps it automatically
#: at SCAFFOLD time with the session's display name -- that mechanism, not
#: a human crediting someone, is what put `author: claude-klabauter-49` into
#: `docs/plans/2026-08-29-the-265ms-floor-is-a-global-stanza.md`. Nudging on
#: it fires on a line the author didn't write and can't act on at the write
#: this guard sees (the offending line was already on disk before they
#: touched the file) -- an advisory with no available action is exactly
#: what trains a reader to dismiss the whole guard. `author:` IS a real
#: instance of the underlying defect (a name there is just as unresolvable
#: later as anywhere else), but the fix belongs at the producer
#: (`_resolve_plan_author`), not at a lint pointed at the producer's
#: output -- do not re-add `author` here without first fixing the
#: scaffolder; that fix is out of scope for this guard.
_ATTRIB_FIELDS = (
    "found_by",
    "reported_by",
    "filed_by",
    "raised_by",
    "credited_to",
    "owned_by",
    "assigned_to",
    "fixed_by",
    "resolved_by",
    "claimed_by",
)

#: Verb phrase, case-insensitive (the incident's own shape is "ESTABLISHED
#: AND FIXED BY"), immediately or near-immediately (optional "the"/
#: "session") followed by the display-name shape -- the name stays
#: case-SENSITIVE (a display name is conventionally all-lowercase; loosely
#: matching would risk pulling in unrelated capitalized hyphenated prose).
#: Captured in group(1).
_ATTRIB_VERB_RE = re.compile(
    r"(?i:\b(?:" + "|".join(re.escape(p) for p in _ATTRIB_VERB_PHRASES) + r")\b)"
    r"\s+(?:the\s+)?(?:session\s+)?(\b" + _NAME_CORE + r"\b)"
)

#: "message ... to <name>" addressing construction -- the name is the
#: RECIPIENT, not the subject. Bounded gap (`{0,30}`, no newline) between
#: "message" and "to" so this does not reach across unrelated sentences.
_ATTRIB_ADDRESS_RE = re.compile(
    r"(?i:\bmessage(?:d|s)?\b)[^\n]{0,30}?(?i:\bto\b)"
    r"\s+(\b" + _NAME_CORE + r"\b)"
)

#: A crediting frontmatter field, at the START of a line (optionally
#: indented), followed by `:` and an optional quote, then the display
#: name. `re.MULTILINE` so `^` matches per-line inside a whole-file scan.
_ATTRIB_FIELD_RE = re.compile(
    r"(?im:^[ \t]*(?:" + "|".join(re.escape(f) for f in _ATTRIB_FIELDS) + r")\s*:\s*)"
    r"[\"']?(\b" + _NAME_CORE + r"\b)"
)

_ADVISORY_TEMPLATE = (
    "Session display name `{token}` used as an identifier — display names "
    "rotate across live sessions and do not disambiguate. Write the "
    "session uuid instead."
)


def _is_in_scope(file_path: str) -> bool:
    # Both sides casefolded: on Windows and APFS `State/Bug-Backlog/x.yaml` is
    # the SAME file as `state/bug-backlog/x.yaml`, and a raw comparison walks
    # this scope check with nothing but a shift key. Markers are declared
    # lowercase, so only the candidate needs folding -- `casefold_path` also
    # normalizes separators, which is why the manual `replace` is gone.
    normalized = casefold_path(file_path)
    if any(marker in normalized for marker in _OUT_OF_SCOPE_MARKERS):
        return False
    return any(marker in normalized for marker in _IN_SCOPE_MARKERS)


def _strip_code_spans(text: str) -> str:
    """Remove fenced code blocks then inline-code spans (order matters: a
    fence may itself contain backticks that would otherwise be
    misinterpreted as inline-code delimiters once the fence markers are
    gone). See module docstring "FENCED CODE / INLINE CODE ARE EXCLUDED".
    """
    without_fences = _FENCE_RE.sub(" ", text)
    return _INLINE_CODE_RE.sub(" ", without_fences)


def _find_display_name_token(text: str) -> Optional[str]:
    """Returns the first display-name token found doing ATTRIBUTION or
    ADDRESS work -- a bare mention is not enough (module docstring
    "NARROWED 2026-08-30"). Tries, in order: crediting verb phrase,
    "message ... to" addressing, crediting frontmatter field.
    """
    scanned = _strip_code_spans(text)
    for pattern in (_ATTRIB_VERB_RE, _ATTRIB_ADDRESS_RE, _ATTRIB_FIELD_RE):
        match = pattern.search(scanned)
        if match is None:
            continue
        token = match.group(1)
        if _UUID_RE.fullmatch(token):
            continue
        return token
    return None


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            return None

        raw_file_path = _extract_file_path(payload)
        if not raw_file_path:
            return None

        if not _is_in_scope(raw_file_path):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        content = _extract_content(tool_name, tool_input, raw_file_path)
        if not content:
            return None

        if len(content.encode("utf-8", errors="replace")) > _MAX_WHOLE_FILE_BYTES:
            return None

        token = _find_display_name_token(content)
        if token is None:
            return None

        reason = _ADVISORY_TEMPLATE.format(token=token)

        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error (module docstring negative-spec).
        return None
