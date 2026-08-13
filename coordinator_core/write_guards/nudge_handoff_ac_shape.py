"""coordinator_core.write_guards.nudge_handoff_ac_shape — advisory guard.

Closes the gap named by
docs/plans/2026-08-13-handoff-ac-shape-template-rule-and-write-time-offer.md:
`workstream_complete`'s consumed-handoff completeness gate (leg A,
`coordinator_core/workstream_complete/directives_session_hygiene.py`'s
`parse_consumed_handoff_acceptance_criteria`, wired at
`coordinator_core/workstream_complete/__init__.py` near line 1948) counts
`- [ ]`/`- [x]` checkboxes under a handoff's `## Acceptance criteria`
heading and reports `verdict: indeterminate` when it finds the heading with
zero of them — but the checkbox convention is stated only in
`example-doctrine-repo coordinator/skills/plan/SKILL.md`, never in the spinoff or
handoff skills, and `coordinator/templates/handoffs/` holds no body
template at all. An author following the skills they were actually given
can write prose bullets instead, satisfying every instruction they read
while silently degrading the gate to "could not look". Neither end is
defective — the gate is right to refuse to guess, and it reports honestly.
The missing thing is the write-time offer that names the shape the gate
can actually parse.

CLASS is "advisory", not "hard-deny": a handoff legitimately may carry no
`## Acceptance criteria` section at all (that must not fire this guard),
and a block here would refuse valid work and teach the next author to
route around the guard — the project CLAUDE.md north star's
advisory-in-practice shape. This guard only offers the checkbox form; it
never blocks the write.

SINGLE SOURCE OF TRUTH for what counts as the `## Acceptance criteria`
heading and what counts as a checkbox under it: this guard imports
`coordinator_core.workstream_complete.directives_session_hygiene
.parse_consumed_handoff_acceptance_criteria` directly rather than
reimplementing a second heading dialect — that function is documented
(module docstring) as a pure, `__init__`-independent text parser over
already-read body text, and is the exact function leg A calls. A
hand-rolled second detector that ever disagreed with the gate would
reproduce the defect this guard exists to prevent.

Path-candidate extraction is modelled on
`block_memo_status_hand_edit.py` (top-level `file_path`, or every
`edits[].file_path` for `MultiEdit`; the same backslash/slash-run
normalization and `..`-traversal rejection) — CLASS differs (advisory
here, hard-deny there), so this module does NOT copy that guard's deny
envelope or its live-claim gating, but it DOES copy the git-root
containment check the plan's C1 task body explicitly named as a thing
to copy (only CLASS was authorized to diverge). Unlike
`block_memo_status_hand_edit.py`'s hand-rolled casefold-and-prefix
compare, this guard reuses the shared
`coordinator_core.ops._path_guard.contained_path` helper directly, per
`write_guards/INTERFACE.md` rule 8 ("reuse shared helpers ... don't
reinvent"). `_HANDOFF_RE` matches on a *substring*, not a rooted path —
an absolute candidate such as `/tmp/anywhere/state/handoffs/x.md`
matches the regex but is NOT under the resolved git root, so the
containment check (not the regex) is what rejects it.

Scope: `state/handoffs/*.md` only — one path segment between `handoffs/`
and the `.md` filename. A spinoff is NOT a separate path shape: spinoffs
observed on disk (e.g.
`state/handoffs/2026-08-13-sedge-partial-ac-closure.md`) live in
`state/handoffs/` with `kind: spinoff` in frontmatter, same directory as
every other handoff — there is no `state/spinoffs/` directory on this
tree, so one matcher covers both and no second path shape is needed.

Fire condition: `kind` is parsed from the post-write body's frontmatter
(`coordinator_core.frontmatter.schema_validate.parse_frontmatter`, the same
helper leg A uses) FIRST, mirroring leg A's own branch order in
`workstream_complete/__init__.py`'s consumed-handoff evaluator — for
`kind: session-handoff` leg A never calls
`parse_consumed_handoff_acceptance_criteria` at all (it joins on
`deliverable_id` and reads the resolved plan's `status:` instead), so this
guard is silent for that kind unconditionally. For every other kind, the
post-write body is evaluated with `parse_consumed_handoff_acceptance_criteria`,
which must return a non-`None` result (the `## Acceptance criteria` heading
is present) whose `total == 0` (zero checkboxes appear under it before the
next same-or-higher-level heading) — a `None` return (no heading at all) or
a `total > 0` result (real checkboxes already present) is silent, matching
leg A's own `indeterminate`-only-on-`total == 0` branch exactly. Finally,
when a pre-image is available, its AC state is parsed with the same
function and compared: if the pre-image already resolved to `total == 0`
(the section was already prose/empty before this edit), the guard is
silent — it fires only on the edit that actually changes the AC section,
not on every later unrelated edit to a handoff left in that state.

Message register (docs/wiki/guard-messaging.md § Register): leads with the
checkbox form to use, states the one fact once, no self-legitimacy
(B1), no repeated claim (B2), no reassurance wrapper (B3), no apology
(B4), stays well under the 220-byte prose cap (B5), and never names an
override env var (B6) — there is no override here; the guard is advisory
and never blocks, so there is nothing to bypass.

Negative-spec:
  - Does NOT fire on a handoff with no `## Acceptance criteria` heading at
    all — that shape is legitimate (module docstring's own CLASS
    rationale) and `parse_consumed_handoff_acceptance_criteria` returns
    `None` for it, which this guard treats as silent.
  - Does NOT fire when checkboxes are already present under the heading,
    however many are open or done — `total > 0` is silent regardless of
    the `open`/`done` split; this guard nudges toward the checkbox FORM,
    it is not a completeness gate (that is leg A's job, unchanged).
  - Does NOT fire on any path outside `state/handoffs/*.md` — a plan file,
    a spinoff-shaped path elsewhere, or any other artifact is out of
    scope per the plan's Anti-scope ("Do not widen to plan files").
  - Does NOT block the write under any circumstance — CLASS is
    "advisory"; `check()` always returns either `None` or an
    `additionalContext` envelope, never a `permissionDecision`.
  - Does NOT fail closed on any error — a resolve/read failure degrades to
    ALLOW/silent (fewer candidates matched), matching the sibling guards'
    fail-open discipline.
  - Does NOT fire on `kind: session-handoff` — leg A never parses that
    kind's AC section (see Fire condition above); the advisory's own
    "the gate ... cannot read the section otherwise" claim would be false
    for that kind.
  - Does NOT re-fire on an edit that leaves an already-zero AC section
    unchanged — a handoff sitting on disk with a prose (or absent-box)
    section does not get re-nagged on every later unrelated edit; only the
    edit that changes the section's state can fire.

Spec backlink: docs/plans/2026-08-13-handoff-ac-shape-template-rule-and-write-time-offer.md (chunk C1)
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.ops._path_guard import contained_path
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
# Advisory band; next free slot after nudge_outbox_draft_frontmatter_shape
# (210) — see docs/wiki/write-guard-priority-bands.md for the band
# convention. No lower-numbered advisory guard matches the
# `state/handoffs/*.md` surface with an overlapping fire condition (this
# guard's own scoped read confirms none of the existing advisories key off
# that path shape for the AC-heading/checkbox signal), so no same-surface
# collision applies.
PRIORITY = 220

#: '..' as a full path component.
_TRAVERSAL_RE = re.compile(r"(^|/)\.\.(/|$)")

#: state/handoffs/<name>.md — flat directory, one path segment. Spinoffs
#: live in this same directory (kind: spinoff in frontmatter); no second
#: path shape exists on this tree (see module docstring's Scope section).
_HANDOFF_RE = re.compile(r"(^|/)state/handoffs/[^/]+\.md$", re.IGNORECASE)

#: Matches nudge_baton_body_bar's own cap — this guard's PreToolUse read of
#: the pre-image is otherwise uncapped (Review: coordinatorstaff-eng-0839d50e
#: Finding 2); a handoff this large is out of scope for a checkbox-shape
#: advisory regardless, so exceeding it degrades to silent, not an error.
_MAX_WHOLE_FILE_BYTES = 256 * 1024


def _collapse_slashes(value: str) -> str:
    """Backslash -> slash, collapse slash runs."""
    normalized = value.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _extract_candidates(payload: Dict[str, Any]) -> List[str]:
    """Top-level ``file_path`` (Write/Edit), or every ``edits[].file_path``
    (MultiEdit) when there is no top-level ``file_path``."""
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return []
    fp = tool_input.get("file_path")
    if fp:
        return [fp]
    out: List[str] = []
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for edit in edits:
            if isinstance(edit, dict):
                efp = edit.get("file_path")
                if efp:
                    out.append(efp)
    return out


def _normalize_and_gate(cand: str) -> Optional[str]:
    """Normalize + traversal-reject + path-shape-gate a single candidate.
    Returns the normalized string, or ``None`` if this candidate is out of
    scope. Git-root containment is a separate check applied by the caller
    (`check()`) via `contained_path`, once a candidate's resolved path is
    known — `_HANDOFF_RE` matches on a substring, not a rooted path, so it
    alone is not equivalent scoping (see module docstring)."""
    cn = _collapse_slashes(cand)

    if _TRAVERSAL_RE.search(cn):
        return None

    if not _HANDOFF_RE.search(cn):
        return None

    return cn


def _resulting_body(tool_name: str, tool_input: Dict[str, Any], pre_image: Optional[str]) -> Optional[str]:
    """The full post-write body text this tool call would produce, or
    ``None`` if it cannot be determined (fails open to silent).

    ``Write`` supplies a full ``content`` replacement outright. ``Edit``
    applies its single old/new fragment to ``pre_image``. ``MultiEdit``
    applies every fragment targeting this candidate, in order, to
    ``pre_image``. A fragment that does not match the current text (stale
    ``old_string``) degrades to ``None`` — this guard never guesses at a
    body it cannot construct. ``replace_all`` (Edit/MultiEdit fragments) is
    honored the same way `nudge_baton_body_bar`, `nudge_prose_queue_append`,
    and `guard_memory_store_cap` already do: truthy -> replace every
    occurrence, falsy/absent -> replace only the first.

    This is a near-duplicate of `nudge_baton_body_bar._reconstruct_whole_file`
    / `_apply_one_edit` (Review: coordinatorstaff-eng-0839d50e Finding 2)
    left un-extracted for now — those helpers are module-private (leading
    underscore) and this module's PreToolUse hot-path budget did not
    justify an unreviewed cross-module import of private names in this
    pass; a shared `write_guards/_post_write_body.py` extraction is the
    right eventual fix and is recorded for follow-up alongside the
    containment-gap entry the executor already filed. In the meantime this
    function also adopts that sibling's read-side size cap
    (`_MAX_WHOLE_FILE_BYTES`, applied in `check()`'s pre-image read) rather
    than leaving the PreToolUse read uncapped.
    """
    if tool_name == "Write":
        content = tool_input.get("content")
        return content if isinstance(content, str) else None

    if pre_image is None:
        return None

    if tool_name == "Edit":
        old_s = tool_input.get("old_string")
        new_s = tool_input.get("new_string")
        if not isinstance(old_s, str) or not isinstance(new_s, str):
            return None
        if old_s not in pre_image:
            return None
        if tool_input.get("replace_all"):
            return pre_image.replace(old_s, new_s)
        return pre_image.replace(old_s, new_s, 1)

    if tool_name == "MultiEdit":
        edits = tool_input.get("edits")
        if not isinstance(edits, list):
            return None
        body = pre_image
        for edit in edits:
            if not isinstance(edit, dict):
                continue
            old_s = edit.get("old_string")
            new_s = edit.get("new_string")
            if not isinstance(old_s, str) or not isinstance(new_s, str):
                continue
            if old_s not in body:
                continue
            if edit.get("replace_all"):
                body = body.replace(old_s, new_s)
            else:
                body = body.replace(old_s, new_s, 1)
        return body

    return None


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit"):
            return None

        tool_input = payload.get("tool_input") or {}
        if not isinstance(tool_input, dict):
            return None

        candidates = _extract_candidates(payload)
        if not candidates:
            return None

        cwd = payload.get("cwd") or None
        base_dir = Path(cwd) if cwd else Path.cwd()
        git_root = resolve_repo_root(cwd)
        allowed_roots = [Path(git_root)] if git_root else []

        matched: Optional[Path] = None
        for cand in candidates:
            cn = _normalize_and_gate(cand)
            if cn is None:
                continue
            candidate_path = Path(cn) if Path(cn).is_absolute() else (base_dir / cn)
            if allowed_roots:
                contained = contained_path(candidate_path, allowed_roots)
                if contained is None:
                    continue
                candidate_path = contained
            matched = candidate_path
            break

        if matched is None:
            return None

        try:
            if matched.stat().st_size > _MAX_WHOLE_FILE_BYTES:
                pre_image = None
            else:
                pre_image = matched.read_text(encoding="utf-8", errors="replace")
        except OSError:
            pre_image = None

        body = _resulting_body(tool_name, tool_input, pre_image)
        if body is None:
            return None

        # Deferred import: paid only after a candidate has already matched
        # the path-shape regex (and, when a git root resolved, passed
        # containment) — mirrors `nudge_outbox_draft_frontmatter_shape.py`'s
        # deferred-import discipline. `directives_session_hygiene` pulls in
        # `coordinator_core.frontmatter.schema_validate` transitively (yaml,
        # subprocess, git_scope, ...), and `engine.py`'s `_discover_guards()`
        # imports every guard module fresh on every PreToolUse call with no
        # memoization — an eager top-level import here would tax every
        # unrelated Write/Edit/MultiEdit in the repo, not just candidates
        # actually in scope.
        from coordinator_core.frontmatter.schema_validate import parse_frontmatter
        from coordinator_core.workstream_complete.directives_session_hygiene import (
            parse_consumed_handoff_acceptance_criteria,
        )

        # Review: coordinatorstaff-eng-0839d50e Finding 0 — leg A
        # (`workstream_complete/__init__.py`'s consumed-handoff evaluator)
        # branches on `kind` BEFORE it ever calls
        # `parse_consumed_handoff_acceptance_criteria`: for
        # `kind: session-handoff` it joins on `deliverable_id` and reads the
        # resolved plan's `status:` instead, never counting checkboxes. This
        # guard must mirror that same branch order — offering the checkbox
        # form for a kind leg A never reads it from would make the advisory
        # itself false.
        frontmatter = parse_frontmatter(body).get("frontmatter")
        kind = frontmatter.get("kind") if isinstance(frontmatter, dict) else None
        if kind == "session-handoff":
            return None

        parsed = parse_consumed_handoff_acceptance_criteria(body)
        if parsed is None:
            return None
        if parsed.get("total", 0) != 0:
            return None

        # Review: coordinatorstaff-eng-0839d50e Finding 3 — fire only when
        # this edit actually changed the AC section, not on every unrelated
        # write to a handoff whose AC section was already prose-shaped.
        # Compare the pre-image's own AC state (same parser) to the
        # post-image's: if the pre-image already resolved to the same
        # zero-checkbox state, this edit did not touch the section, so stay
        # silent rather than re-nagging on every future edit to that file.
        if pre_image is not None:
            pre_parsed = parse_consumed_handoff_acceptance_criteria(pre_image)
            if pre_parsed is not None and pre_parsed.get("total", 0) == 0:
                return None

        advisory = (
            "Use `- [ ]`/`- [x]` checkboxes under `## Acceptance criteria`, "
            "not prose bullets — the consumed-handoff completeness gate "
            "only counts checkboxes and cannot read the section otherwise."
        )
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": advisory,
            }
        }
    except Exception:
        return None
