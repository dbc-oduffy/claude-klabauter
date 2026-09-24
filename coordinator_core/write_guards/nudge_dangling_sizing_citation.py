"""coordinator_core.write_guards.nudge_dangling_sizing_citation — advisory guard.

Purpose: `docs/plans/2026-08-06-plan-sizing-citation-gate.md` chunk C4. The
sibling read-side gate (`coordinator_core/ops/assert_plan_sizing_citation.py`,
chunk C3) and the write-time scaffolder gate
(`coordinator/bin/coordinator-doc-new --sizing-object`, chunk C2) both catch a
dangling `sizing_object:` citation at their own choke points — a fresh scaffold
and a corpus-wide assert run. Neither catches the third, most common path: an
EM or executor hand-editing an EXISTING `docs/plans/*.md` file's frontmatter
directly (Write/Edit/MultiEdit), typing or repointing a `sizing_object:` path
that does not resolve on disk, with no scaffolder and no assert run in
between. This guard closes that write-time gap for hand-authored edits, per
this repo's own north star discharge test (CLAUDE.md § North star): "for
every rule, what artifact discharges it? 'The operator remembers' is not an
answer."

CLASS is "advisory" (``nudge_*``, never ``block_*``), per the plan's own
task-row title and body: "offering the correct path rather than blocking."
The plan's Anti-scope forbids intent-matching and body-prose scanning but
says nothing against a hard-deny here, and the row title's own naming
convention (`nudge_dangling_sizing_citation`, not
`block_dangling_sizing_citation`) is itself the class decision — mirroring
this package's design-as-offers convention used by every other frontmatter
write-time advisory (`nudge_terminal_artifact_edit.py`,
`nudge_handoff_ac_shape.py`): a dangling citation is not evidence of harm
requiring a hard stop, and a hand-authored plan mid-draft may cite a sizing
object not yet written for entirely legitimate reasons (see FIRE CONDITION
below for the one case this guard still stays silent on: a fresh sizing
object still being drafted in the same batch).

SCOPE (frontmatter only, never body prose): mirrors AC6 / the plan's
Anti-scope directly — "Never scan body prose. A plan legitimately cites a
nonexistent sizing object when documenting its absence; a body-scanning
check makes that unwriteable." This guard therefore reads `sizing_object`
ONLY from the parsed frontmatter mapping (`split_frontmatter` +
`read_fm_field_unquoted`, the same primitives `assert_plan_sizing_citation.py`
uses and every other frontmatter-reading write guard in this package uses —
never a regex over the whole file text, which is what would also catch a
body-prose citation).

PATH SHAPE: `docs/plans/*.md` only, one path segment — matches the plan's own
`prime_exit_criterion` scope ("any docs/plans frontmatter sizing_object
citation") and AC5's "hand-authored plan write" wording. `docs/problems/` and
`docs/research/` are out of scope: `sizing_object` is a plan-frontmatter
field (`plan.schema.json`, chunk C1), not declared on those other doc kinds.

POST-WRITE BODY RECONSTRUCTION: modelled directly on
`nudge_handoff_ac_shape.py`'s `_resulting_body` — `Write` supplies the full
replacement `content` outright; `Edit`/`MultiEdit` apply their fragment(s) to
the pre-image read from disk. A fragment whose `old_string` does not match
the current on-disk text (stale) degrades to silent (`None`), matching that
sibling's documented behaviour: this guard never guesses at a body it cannot
construct.

FIRE CONDITION: the post-write frontmatter's `sizing_object` (read
unquoted, comment-stripped) is non-empty, matches the schema's own path
shape (`^state/sizings/.+\\.yaml$`, chunk C1's regex — a value NOT shaped
like a sizing path is a different defect, out of this guard's remit, and
stays silent rather than nagging on the wrong problem), and does not resolve
to an existing file under the resolved git root. A `sizing_object` that
already resolved before this edit and still resolves after is silent
(nothing changed); one that was already dangling before this specific edit
and is UNCHANGED by it also stays silent, mirroring `nudge_handoff_ac_shape
.py`'s "fire only on the edit that actually changes the state, not on every
later unrelated edit" discipline — re-nagging on every subsequent touch to
an already-known-dangling plan is not this guard's job (the corpus-wide
`assert_plan_sizing_citation.py` op is the standing enforcement surface for
that).

Negative-spec:
  - Does NOT block the write under any circumstance — CLASS is "advisory";
    `check()` returns only `None` or an `additionalContext` envelope, never
    a `permissionDecision`.
  - Does NOT scan plan body prose for a sizing-object-shaped path anywhere —
    the plan's Anti-scope / AC6 load-bearing negative. Only the parsed
    frontmatter mapping's `sizing_object` key is read.
  - Does NOT compare the citation against the plan's problem restatement or
    otherwise intent-match — the plan's Anti-scope forbids this; existence
    on disk is the only check.
  - Does NOT fire on a `sizing_object` value that is empty, absent, or does
    not match the `^state/sizings/.+\\.yaml$` shape — a malformed value is
    `plan.schema.json`'s own validation remit (chunk C1), not this guard's.
  - Does NOT fire when the cited path already resolved before this edit and
    the edit does not change `sizing_object` to a new, still-dangling value
    — see FIRE CONDITION.
  - Does NOT fire on `docs/problems/**` or `docs/research/**` — scope is
    `docs/plans/*.md` only, one path segment.
  - Never raises: any unexpected input shape returns `None` (fail-open),
    matching every other write_guards module's contract.

CONCURRENCY (plan's Anti-scope): a live peer session holds
`coordinator_core/write_guards/` at authoring time. This is a single new
module; the plan directs staging only this new file, its test, and one
registry row — never the directory.

Spec backlink: docs/plans/2026-08-06-plan-sizing-citation-gate.md § C4 (AC5).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from coordinator_core.frontmatter.primitives import (
    read_fm_field_unquoted,
    split_frontmatter,
)
from coordinator_core.write_guards._repo_root import resolve_repo_root

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit"]
# Advisory band; next free slot after nudge_unattributed_process_time_figure
# (223) — see docs/wiki/write-guard-priority-bands.md for the band
# convention. No lower-numbered advisory guard keys off `sizing_object`
# frontmatter or the `docs/plans/*.md` path shape for this fire condition,
# so no same-surface collision applies.
PRIORITY = 224

#: docs/plans/<name>.md — flat directory, one path segment, matching the
#: plan's own scope ("any docs/plans frontmatter sizing_object citation").
_PLAN_PATH_RE = re.compile(r"(^|/)docs/plans/[^/]+\.md$")

#: Matches plan.schema.json's own sizing_object regex (chunk C1) — a value
#: not shaped like this is a different, schema-owned defect, not this
#: guard's remit (see FIRE CONDITION in the module docstring).
_SIZING_PATH_SHAPE_RE = re.compile(r"^state/sizings/.+\.yaml$")

_MAX_WHOLE_FILE_BYTES = 256 * 1024


def _extract_candidates(payload: Dict[str, Any]) -> List[str]:
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


def _collapse_slashes(value: str) -> str:
    normalized = value.replace("\\", "/")
    while "//" in normalized:
        normalized = normalized.replace("//", "/")
    return normalized


def _resulting_body(tool_name: str, tool_input: Dict[str, Any], pre_image: Optional[str]) -> Optional[str]:
    """The full post-write body text this tool call would produce, or `None`
    if it cannot be determined (fails open to silent).

    Modelled directly on `nudge_handoff_ac_shape.py`'s `_resulting_body` —
    see that module's docstring for the full rationale on Write/Edit/
    MultiEdit reconstruction and `replace_all` handling.
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


def _sizing_object(text: str) -> Optional[str]:
    """Read `sizing_object` from `text`'s parsed frontmatter mapping ONLY —
    never a regex over the whole file. Returns `None` when there is no
    parseable frontmatter or the key is absent/empty.
    """
    split = split_frontmatter(text)
    if split is None:
        return None
    value = read_fm_field_unquoted(split.fm_text, "sizing_object")
    if not value:
        return None
    return value.strip()


def _dangling(repo_root: Path, sizing_object: str) -> bool:
    """True iff `sizing_object` is schema-shaped but does not resolve on disk
    under `repo_root`."""
    if not _SIZING_PATH_SHAPE_RE.match(sizing_object):
        return False
    candidate = repo_root / sizing_object
    return not candidate.is_file()


#: The closing reassurance this used to carry ("nothing is blocked and the
#: write already happened") is gone per docs/wiki/guard-messaging.md
#: § Register, which names reassurance as one of the things a guard message
#: does not do. It also said nothing the envelope did not: this is an
#: advisory guard, so the `OFFER:` opener already carries the class. Its 47
#: bytes were what put the rendered message over the 220-byte prose cap on a
#: realistically-long sizing path.
_REASON_TEMPLATE = (
    "OFFER: sizing_object: {sizing_object} does not resolve under this repo. "
    "Route via coordinator:sizing to mint it, or correct the path if this was "
    "a typo."
)


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

        matched_raw: Optional[str] = None
        for cand in candidates:
            cn = _collapse_slashes(cand)
            if _PLAN_PATH_RE.search(cn):
                matched_raw = cn
                break
        if matched_raw is None:
            return None

        cwd = payload.get("cwd") or None
        base_dir = Path(cwd) if cwd else Path.cwd()
        candidate_path = Path(matched_raw) if Path(matched_raw).is_absolute() else (base_dir / matched_raw)

        try:
            if candidate_path.is_file() and candidate_path.stat().st_size > _MAX_WHOLE_FILE_BYTES:
                pre_image = None
            elif candidate_path.is_file():
                pre_image = candidate_path.read_text(encoding="utf-8", errors="replace")
            else:
                pre_image = None
        except OSError:
            pre_image = None

        body = _resulting_body(tool_name, tool_input, pre_image)
        if body is None:
            return None

        sizing_object = _sizing_object(body)
        if sizing_object is None:
            return None

        git_root = resolve_repo_root(cwd)
        repo_root = Path(git_root) if git_root else base_dir

        if not _dangling(repo_root, sizing_object):
            return None

        # Fire only on the edit that actually changes to a dangling state --
        # mirrors nudge_handoff_ac_shape.py's re-nag suppression. A pre-image
        # already citing this same dangling value is unchanged by this edit;
        # stay silent rather than nagging on every later unrelated touch.
        if pre_image is not None:
            pre_sizing = _sizing_object(pre_image)
            if pre_sizing == sizing_object:
                return None

        reason = _REASON_TEMPLATE.format(sizing_object=sizing_object)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        return None
