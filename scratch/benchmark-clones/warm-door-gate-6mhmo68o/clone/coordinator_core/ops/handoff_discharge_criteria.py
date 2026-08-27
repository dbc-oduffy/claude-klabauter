"""
coordinator_core.ops.handoff_discharge_criteria — JSON-RPC
"handoff.discharge_criteria" operation.

Purpose: a BOUNDED WRAPPER over `handoff.correct_body` (never re-implements
its write path — see that module for the ownership arms, archive-follow
resolution, terminal-state refusal, and stamped paper trail this op inherits
identically). Its whole reason for existing: the decomposition norm
(docs/plans/2026-08-06-executing-session-can-discharge-criteria.md D2
won't-do) means handoffs now carry MORE `## Acceptance criteria` checkboxes,
each more finely grained and phrased more similarly to its siblings — raising
both how often a caller needs to tick one AND how often two checkbox lines
collide on raw text, hitting `handoff.correct_body`'s exactly-once refusal.
This op resolves the target checkbox by CRITERION IDENTITY (an `AC-N`-shaped
token in the text immediately preceding it) or STRUCTURAL POSITION (its
ordinal among checkboxes in the `## Acceptance criteria` section) — NEVER by
matching the checkbox's literal line text, which is exactly the ambiguity
this verb exists to eliminate (see
`coordinator_core/ops/tests/test_handoff_correct_body.py`'s
`test_duplicate_checkbox_line_text_ties_to_correct_box_via_context`, the
evidence-gathering test this op's reason for existing was built against).

Authority: docs/decisions/DR-274-possession-replaces-authorship-as-handof.md
§ D3 — the DR that sanctions this op as a SECOND body-mutating verb (DR-247
D1 sanctions `handoff.correct_body` only; this DR extends that sanction to
this op explicitly, so it does not ship un-sanctioned). Spec:
docs/plans/2026-08-06-executing-session-can-discharge-criteria.md, chunk C7
(AC16-AC19).

Two operations:
    tick  — flip one unticked (`- [ ]`) checkbox, resolved by identity or
            position, to ticked (`- [x]`), preserving its trailing text
            verbatim. Refused if the resolved checkbox is already ticked.
    split — replace one unticked checkbox line with a met (ticked) line and
            an unmet (unticked) line — a one-to-two checkbox expansion, per
            the decomposition norm (a partially-met criterion decomposes
            into a met unit and a routed unmet remainder, never stays a
            single "partially met" box). Caller supplies `met_text` and
            `unmet_text` in place of the original line's trailing text.
            This IS a legal bounded `old_string` -> `new_string` replacement
            under DR-247 D2(v): D2(v) bars only a new HEADING line or a new
            frontmatter-delimiter (`---`) line, never a new list item
            (verified against disk, DR-247 D2(v) text). The size/growth caps
            (`_NET_GROWTH_CAP`, `_MAX_OLD_STRING_LEN`,
            `_MAX_OLD_STRING_BODY_RATIO` in `handoff_correct_body.py`) bound
            it independently and are enforced by `handoff.correct_body`
            itself — this op never re-derives them; it inherits them by
            delegating the actual write.

Mechanism: this module reads the target's CURRENT body (a read-only,
pre-lock resolution mirroring `handoff_correct_body._handler`'s own
live-then-archive resolution — see `_resolve_read_path` below, duplicated
deliberately for identity/position resolution ONLY, never for the write
itself) to locate the target checkbox and build a body-unique
`old_string` -> `new_string` pair, then delegates the entire write —
ownership gate, archive-follow, terminal-state refusal, all D2 bounds, the
stamped correction note — to `handoff_correct_body._handler`, imported and
called directly (the same "reach into a sibling op's designated reuse
point" convention `handoff_correct_body.py` itself uses for
`baton_assemble._resolve_current_session_id` and
`pickup_assemble._claim_already_self_held`). `handoff_correct_body._handler`
re-resolves and re-verifies the same path authoritatively under
`locked_rmw` — a race between this module's read and the eventual write is
caught there (`MutateAbort`), exactly as it would be for a direct
`handoff.correct_body` caller.

Self-registration: importing this module fires
``@register_op("handoff.discharge_criteria")`` as a side-effect. Added to
`coordinator_core/ops/__init__.py`'s eager import list to trigger
registration at `start_server()` time.

Registration completeness is computed, not remembered — see
`coordinator_core/authz/registration_quad.check_registration_quad()` for the
authoritative five-surface set (`_REGISTRY`, `OP_CLASSIFICATION`,
`_OP_KEY_SCOPE`, `OP_MODULE_MAP`, `_EAGER_OP_MODULES`) and
`test_registry_map_sync.py` / `test_registration_quad.py` for the guards
that enforce it. A registered op missing `OP_CLASSIFICATION` fail-closes to
DENY in `classify()`; missing `_OP_KEY_SCOPE` resolves `repo_root=None` in
dispatch — either makes the op reachable only via a direct
`_handler(params, repo_root=...)` call, never via `invoke`.

Exit-code contract: identical shape to `handoff_correct_body`'s (simple
dict envelope, not the fleet {mode,dry_run,candidates} shape):
    exit_code 0, applied True  — replacement applied, correction note stamped.
    exit_code 1, applied False — refused; a DISTINCT `error` string per
                                  precondition (this module's own resolution
                                  preconditions, or any of
                                  `handoff_correct_body`'s, forwarded as-is).

Negative-spec:
    - Does NOT re-implement `handoff_correct_body`'s write path, ownership
      gate, archive-follow resolution, terminal-state refusal, or stamped
      paper trail — all inherited by delegation (AC16).
    - Does NOT resolve a checkbox by matching its raw line text against the
      body — always by criterion identity or structural position (AC17).
    - Does NOT accept a whole-body or multi-checkbox payload — exactly one
      checkbox is resolved and mutated per call, mirroring
      `handoff_correct_body`'s own single-replacement bound.
    - Does NOT touch any file outside `state/handoffs/`/`archive/handoffs/`
      — inherited from `handoff_correct_body`'s own containment.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import handoff_archive_dest, main_worktree_root
from coordinator_core.ops.handoff_correct_body import _MAX_OLD_STRING_LEN
from coordinator_core.ops.handoff_correct_body import _handler as _correct_body_handler

# ---------------------------------------------------------------------------
# Body parsing — locate the "## Acceptance criteria" section and its
# checkboxes, resolving each by criterion identity or structural position
# (AC17). Never by raw line-text match.
# ---------------------------------------------------------------------------

_ANY_HEADING_LINE_RE = re.compile(r"^(#{1,6})[ \t]")
_ACC_CRITERIA_HEADING_RE = re.compile(
    r"^#{1,6}[ \t]+acceptance criteria\b", re.IGNORECASE
)
# A checkbox list item — captures indent, the marker character (' '/'x'/'X'),
# the trailing text, and the line's own ending (so reconstruction preserves
# the file's exact newline convention rather than assuming "\n").
_CHECKBOX_LINE_RE = re.compile(r"^([ \t]*)-[ \t]*\[([ xX])\][ \t]*(.*?)([\r\n]*)$")
# Criterion-identity token — "AC-1", "AC1", "AC-2b", case-insensitive. Used
# only to find the identity tag associated with a checkbox, never to match
# the checkbox line itself.
_CRITERION_ID_RE = re.compile(r"\bAC-?\d+[A-Za-z]?\b", re.IGNORECASE)

# Cap on how far back this module will expand context lines to build a
# body-unique old_string for a resolved checkbox (mirrors the C3 duplicate-
# checkbox-line test's own "just enough context" shape, generalized with a
# bound rather than unbounded expansion). A body this repetitive within this
# many lines is a distinct, reported refusal rather than silent overreach.
_MAX_CONTEXT_EXPANSION_LINES = 30


def _normalize_criterion_id(value: str) -> str:
    return value.strip().upper().replace("-", "")


def _parse_checkboxes(body: str) -> "list[dict]":
    """Return every checkbox item within the body's `## Acceptance criteria`
    section (or the first heading matching that text, case-insensitive, at
    any level 1-6), in document order. Each item carries its 1-indexed
    structural `position` and, when a nearby `AC-N`-shaped token is found in
    the contiguous non-blank text immediately above it, its `criterion_id`.
    Empty list when no such section exists.
    """
    lines = body.splitlines(keepends=True)

    section_start = None
    section_level = None
    for i, line in enumerate(lines):
        if _ACC_CRITERIA_HEADING_RE.match(line):
            section_start = i + 1
            section_level = len(line) - len(line.lstrip("#"))
            break
    if section_start is None:
        return []

    section_end = len(lines)
    for i in range(section_start, len(lines)):
        hm = _ANY_HEADING_LINE_RE.match(lines[i])
        if hm and len(hm.group(1)) <= section_level:
            section_end = i
            break

    # Line indices (within `lines`) of every checkbox in this section, in
    # document order — used below to bound the backward-context scan so it
    # can never cross into a previous list item's continuation lines.
    checkbox_line_idxs = [
        i for i in range(section_start, section_end)
        if _CHECKBOX_LINE_RE.match(lines[i])
    ]

    # First pass: for every checkbox, find the extent of its OWN text —
    # its own line plus any wrapped continuation lines (contiguous
    # non-blank lines below it, up to the next checkbox or section end).
    # `own_text_end[pos]` is the line index one PAST the last line that
    # belongs to that item's own text — the true boundary a previous
    # item's continuation occupies, which is NOT simply its checkbox line
    # (a wrapped continuation can span several lines past it).
    own_text_end: "list[int]" = []
    own_texts: "list[str]" = []
    for pos, i in enumerate(checkbox_line_idxs):
        cm = _CHECKBOX_LINE_RE.match(lines[i])
        limit = (
            checkbox_line_idxs[pos + 1]
            if pos + 1 < len(checkbox_line_idxs)
            else section_end
        )
        own_text_parts = [cm.group(3)]
        end = i + 1
        for k in range(i + 1, limit):
            stripped = lines[k].strip()
            if not stripped:
                break
            own_text_parts.append(stripped)
            end = k + 1
        own_text_end.append(end)
        own_texts.append(" ".join(own_text_parts))

    items: "list[dict]" = []
    ordinal = 0
    for pos, i in enumerate(checkbox_line_idxs):
        cm = _CHECKBOX_LINE_RE.match(lines[i])
        ordinal += 1
        criterion_id = None
        # F1 (chain-review Slice B): resolve identity from the checkbox
        # item's OWN text first (including wrapped continuation lines) —
        # real handoff bodies wrap acceptance criteria across multiple
        # lines, so the line immediately above a checkbox is usually the
        # PREVIOUS item's continuation, not this item's identity tag. Only
        # fall back to preceding context — bounded at the END of the
        # previous item's own text, never crossing into its continuation
        # lines — when this item's own text carries no AC token.
        idm = _CRITERION_ID_RE.search(own_texts[pos])
        if idm:
            criterion_id = idm.group(0)
        else:
            prev_boundary = own_text_end[pos - 1] - 1 if pos > 0 else section_start - 1
            j = i - 1
            while j > prev_boundary:
                stripped = lines[j].strip()
                if not stripped:
                    break
                idm = _CRITERION_ID_RE.search(stripped)
                if idm:
                    criterion_id = idm.group(0)
                    break
                j -= 1
        items.append({
            "line_idx": i,
            "position": ordinal,
            "criterion_id": criterion_id,
            "indent": cm.group(1),
            "marker": cm.group(2),
            "rest": cm.group(3),
            "ending": cm.group(4),
        })
    return items


def _build_unique_replacement(
    body: str, lines: "list[str]", line_idx: int, new_line: str
) -> "tuple[Optional[str], Optional[str]]":
    """Expand context backward from `lines[line_idx]` (the target checkbox
    line) until the resulting `old_string` occurs exactly once in `body`,
    then build the matching `new_string` (same context, target line
    replaced by `new_line`). Returns (old_string, new_string) or
    (None, error_message) if no context window within
    `_MAX_CONTEXT_EXPANSION_LINES` disambiguates it.
    """
    for back in range(0, _MAX_CONTEXT_EXPANSION_LINES + 1):
        start = line_idx - back
        if start < 0:
            break
        old_candidate = "".join(lines[start:line_idx + 1])
        # F5 (chain-review Slice B): bound the expansion by the same
        # character cap `handoff_correct_body` enforces on old_string, so a
        # too-large candidate is reported in this module's own terms rather
        # than surfacing as an opaque `correct_body` refusal naming a param
        # this caller never supplied.
        if len(old_candidate) > _MAX_OLD_STRING_LEN:
            return None, (
                "cannot construct a body-unique replacement target for the "
                "resolved checkbox: the smallest disambiguating context "
                f"exceeds the {_MAX_OLD_STRING_LEN}-character cap on "
                "replacement-target size — body structure is too repetitive "
                "for this op's disambiguation bound"
            )
        if body.count(old_candidate) == 1:
            prefix = "".join(lines[start:line_idx])
            return old_candidate, prefix + new_line
    return None, (
        "cannot construct a body-unique replacement target for the resolved "
        f"checkbox within {_MAX_CONTEXT_EXPANSION_LINES} lines of context — "
        "body structure is too repetitive for this op's disambiguation bound"
    )


def _resolve_read_path(
    handoff_path_raw: str, repo_root: Path
) -> "tuple[Optional[Path], Optional[str]]":
    """Read-only path resolution mirroring `handoff_correct_body._handler`'s
    own live-then-archive resolution (AC12), duplicated here SOLELY so this
    wrapper can read the target's current body to resolve a checkbox by
    identity/position before delegating the write. Never used for, and
    never a substitute for, `handoff_correct_body._handler`'s own
    authoritative re-resolution under lock.
    """
    worktree = main_worktree_root(repo_root)
    p = Path(handoff_path_raw)
    if not p.is_absolute():
        p = worktree / p
    allowed_roots = [worktree / "state" / "handoffs", worktree / "archive" / "handoffs"]
    try:
        p = contained_path(p, allowed_roots)
    except ValueError as exc:
        return None, f"handoff_path is malformed (cannot be resolved as a filesystem path): {exc}"
    if p is None:
        return None, (
            "handoff_path escapes state/handoffs/ and archive/handoffs/ — the "
            f"only two roots this op ever touches: {handoff_path_raw!r}"
        )
    if not p.is_file():
        archived_candidate = handoff_archive_dest(worktree, p)
        try:
            archived_candidate = contained_path(archived_candidate, allowed_roots)
        except ValueError:
            archived_candidate = None
        if archived_candidate is not None and archived_candidate.is_file():
            p = archived_candidate
        else:
            return None, (
                "handoff not found on disk (checked state/handoffs/ and "
                f"archive/handoffs/): {handoff_path_raw}"
            )
    return p, None


def _err(msg: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": msg}


@register_op("handoff.discharge_criteria")
async def _handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC "handoff.discharge_criteria" handler.

    Params:
        handoff_path   (str) — same as `handoff.correct_body`'s param.
        criterion_id   (str) — resolve the target checkbox by its `AC-N`-
                                shaped identity token. Mutually exclusive
                                with `position`; exactly one is required.
        position       (int) — resolve the target checkbox by its 1-indexed
                                ordinal within the `## Acceptance criteria`
                                section's checkbox list. Mutually exclusive
                                with `criterion_id`; exactly one is required.
        met_text       (str) — OPTIONAL. Supplied together with
                                `unmet_text` to SPLIT the resolved checkbox
                                into a met (ticked) line carrying this text
                                and an unmet (unticked) line carrying
                                `unmet_text`, instead of a plain tick.
        unmet_text     (str) — OPTIONAL. See `met_text`. When the criterion
                                being split has a resolvable criterion_id,
                                unmet_text MUST carry that same identity
                                token (F4) — refused otherwise, so the
                                still-unmet line remains addressable by
                                criterion_id after the split.
        override_reason (str) — OPTIONAL. Forwarded verbatim to
                                `handoff.correct_body` (see that op's own
                                param doc) — consulted only when the calling
                                session is neither the claim holder nor the
                                authoring session of the target.

    Returns: `handoff_correct_body._handler`'s own result dict, verbatim,
    plus (on success) `resolved_position` and `resolved_criterion_id`
    naming which checkbox was targeted, and `discharge_op` ("tick"/"split").
    Refusals from THIS module's own preconditions (target resolution)
    return the same `{exit_code: 1, applied: False, error: ...}` shape
    `handoff_correct_body` uses, so a caller need not distinguish the
    source.
    """
    handoff_path_raw: str = params.get("handoff_path") or ""
    if not handoff_path_raw:
        return _err("missing required param: handoff_path")

    criterion_id_raw = params.get("criterion_id")
    position_raw = params.get("position")
    if criterion_id_raw is not None and position_raw is not None:
        return _err(
            "criterion_id and position are mutually exclusive — supply exactly one"
        )
    if criterion_id_raw is None and position_raw is None:
        return _err(
            "missing target: supply exactly one of criterion_id or position"
        )
    if criterion_id_raw is not None:
        if not isinstance(criterion_id_raw, str) or not criterion_id_raw.strip():
            return _err("criterion_id must be a non-empty string")
    if position_raw is not None:
        if isinstance(position_raw, bool) or not isinstance(position_raw, int):
            return _err("position must be an integer")
        if position_raw < 1:
            return _err("position must be >= 1 (1-indexed)")

    met_text_raw = params.get("met_text")
    unmet_text_raw = params.get("unmet_text")
    is_split = met_text_raw is not None or unmet_text_raw is not None
    if is_split:
        if met_text_raw is None or unmet_text_raw is None:
            return _err(
                "split requires BOTH met_text and unmet_text — only one was supplied"
            )
        if not isinstance(met_text_raw, str) or not met_text_raw.strip():
            return _err("met_text must be a non-empty string")
        if not isinstance(unmet_text_raw, str) or not unmet_text_raw.strip():
            return _err("unmet_text must be a non-empty string")

    if repo_root is None:
        return _err(
            "handoff.discharge_criteria: repo_root is required "
            "(no founding root available — handler called without socket-authoritative common_dir)"
        )

    p, resolve_err = _resolve_read_path(handoff_path_raw, repo_root)
    if p is None:
        return _err(resolve_err)

    try:
        text = p.read_bytes().decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        # F6 (chain-review Slice B): UnicodeDecodeError is a ValueError, not
        # an OSError — uncaught it escaped the {exit_code: 1} envelope
        # contract, reintroducing the defect class handoff_correct_body
        # already fixed for embedded NULs.
        return _err(f"cannot read handoff file: {exc}")

    split = split_frontmatter(text)
    if split is None:
        return _err(f"no valid YAML frontmatter block in: {handoff_path_raw}")

    body = split.body_with_leading_newline
    items = _parse_checkboxes(body)
    if not items:
        return _err(
            f"no '## Acceptance criteria' section (or no checkboxes in it) found "
            f"in {handoff_path_raw}"
        )

    target: Optional[dict] = None
    if criterion_id_raw is not None:
        wanted = _normalize_criterion_id(criterion_id_raw)
        matches = [
            it for it in items
            if it["criterion_id"] is not None
            and _normalize_criterion_id(it["criterion_id"]) == wanted
        ]
        if not matches:
            return _err(
                f"no checkbox found for criterion_id {criterion_id_raw!r} in "
                f"{handoff_path_raw}'s '## Acceptance criteria' section"
            )
        if len(matches) > 1:
            return _err(
                f"criterion_id {criterion_id_raw!r} matches {len(matches)} "
                "checkboxes — ambiguous identity resolution, refusing"
            )
        target = matches[0]
    else:
        if position_raw > len(items):
            return _err(
                f"position {position_raw} is out of range — "
                f"{handoff_path_raw}'s '## Acceptance criteria' section has "
                f"{len(items)} checkbox(es)"
            )
        target = items[position_raw - 1]

    if target["marker"] in ("x", "X"):
        op_name = "split" if is_split else "tick"
        return _err(
            f"the resolved checkbox (position {target['position']}"
            + (f", criterion_id {target['criterion_id']!r}" if target["criterion_id"] else "")
            + f") is already ticked — cannot {op_name} an already-met criterion"
        )

    lines = body.splitlines(keepends=True)
    indent = target["indent"]
    ending = target["ending"]

    if is_split:
        # F4 (chain-review): after a split, the still-unmet line MUST remain
        # the one addressable by criterion_id — identity is owned by the
        # checkbox's OWN text (F1's governing principle), so the caller's
        # unmet_text is REQUIRED to carry it, rather than this op injecting
        # it (mutating human-authored text) or accepting a new out-of-band
        # identity parameter (reintroducing the shape F1 removed). Only
        # enforced when the criterion being split actually carries a
        # resolvable identity — a checkbox with none was never addressable
        # by criterion_id before the split either, so there is nothing to
        # preserve.
        if target["criterion_id"] is not None:
            wanted_id = _normalize_criterion_id(target["criterion_id"])
            found_ids = {
                _normalize_criterion_id(m)
                for m in _CRITERION_ID_RE.findall(unmet_text_raw)
            }
            if wanted_id not in found_ids:
                return _err(
                    "split requires unmet_text to carry this criterion's own "
                    f"identity token ({target['criterion_id']!r}) — after a "
                    "split, the still-unmet line is the ONE line that must "
                    f"remain addressable by criterion_id; include "
                    f"{target['criterion_id']!r} in unmet_text"
                )
        met_text = met_text_raw.strip()
        unmet_text = unmet_text_raw.strip()
        sep = ending if ending else "\n"
        new_line = f"{indent}- [x] {met_text}{sep}{indent}- [ ] {unmet_text}{ending}"
        discharge_op = "split"
    else:
        new_line = f"{indent}- [x] {target['rest']}{ending}"
        discharge_op = "tick"

    old_string, new_string_or_err = _build_unique_replacement(
        body, lines, target["line_idx"], new_line
    )
    if old_string is None:
        return _err(new_string_or_err)
    new_string = new_string_or_err

    correct_body_params = {
        "handoff_path": handoff_path_raw,
        "old_string": old_string,
        "new_string": new_string,
    }
    if "override_reason" in params:
        correct_body_params["override_reason"] = params.get("override_reason")

    result = await _correct_body_handler(correct_body_params, repo_root=repo_root)
    if result.get("exit_code") == 0:
        result = dict(result)
        result["resolved_position"] = target["position"]
        result["resolved_criterion_id"] = target["criterion_id"]
        result["discharge_op"] = discharge_op
    return result
