"""coordinator_core.write_guards.nudge_tasks_state_folder_split — advisory guard.

Spec: coordinator-claude docs/plans/2026-07-27-claude-md-altitude-triage.md § C14
Discharges (partly — see module tail): the "state/ vs tasks/" placement rule
in coordinator-claude's always-on global-doctrine/CLAUDE.md, and the C3 ledger's own
Row 7 ("central-state-in-claude-klabauter routing sentence" — ACCEPT-UNENFORCED,
naming this guard as what was owed).

Motivation: coordinator-claude's ``state/`` holds always-on session substrate
(orientation_cache, lessons, handoffs, trackers, queues, ledgers, memos,
review-trail, week-changelog, audits, recovery, scratch, the *-backlog and
improvement-queue dirs); ``tasks/`` is UUID flight-recorder dirs, dated
reports, and loose scratch, swept AGGRESSIVELY by ``/distill`` and
``/update-docs``. Writing a load-bearing surface under ``tasks/`` puts it in
the path of that aggressive sweep — the sweep has no way to know the write
was load-bearing and removes it, a silent-data-loss failure mode. This guard
adds tool-boundary friction (an OFFER, not a block) the moment such a write
is attempted, rather than leaving the mistake to be discovered only after
the next sweep has already run.

This is a NEW guard, not a port of a coordinator-claude reference ``.sh`` hook — grepped
absent from ``write_guards/*.py`` before authoring (the C3 ledger's own Row 7
independently records the same absence). It follows the module-shape
convention set by ``nudge_baton_body_bar.py`` (advisory CLASS, path-gate-
then-content-free-check shape, docstring/negative-spec layout) and
``nudge_improvement_queue_write.py`` (constants naming, fail-open contract).
Unlike both of those, this guard needs no whole-file/whole-body content
reconstruction — the violation is fully determined by the TARGET PATH alone,
so ``Edit``/``MultiEdit``/``Write`` are all evaluated identically off
``tool_input['file_path']`` (``NotebookEdit``'s ``notebook_path`` is also
accepted, for parity with the sibling CLAUDE.md-class guard's path
extraction, though a load-bearing surface under ``tasks/`` is not expected
to be a notebook in practice).

DETECTION: the target path's first segment after ``tasks/`` (anchored to a
path-segment boundary, not a bare substring — mirrors
``nudge_baton_body_bar._matches_baton_path``'s anchoring discipline so a
coincidental substring like ``vendor/mytasks/lessons.md`` does not
false-positive) is tested against the literal STEMS+GLOBS array below, taken
verbatim from ``docs/wiki/coordinator-tripwires.md`` § tasks-state-folder-
split's "Always-on `state/` substrate (enumerated allowlist)" line:
orientation_cache, lessons, handoffs/, trackers, queues, ledgers, memos/,
review-trail/, week-changelog/, audits/, recovery/, scratch/*,
debt-backlog/, bug-backlog/, improvement-queue/.

Three of those fifteen tokens (trackers, queues, ledgers) are category
words, not literal on-disk basenames — the real files are named things like
``handoff-tracker.md``, ``improvement-queue/``, ``health-ledger.md``. Those
three match on a case-insensitive SINGULAR substring within the first path
segment's basename (``tracker`` / ``queue`` / ``ledger``) rather than an
exact-segment match, so ``tasks/handoff-tracker.md`` and
``tasks/health-ledger.md`` are caught the same way a literal
``tasks/lessons.md`` is. The other twelve tokens match the first path
segment exactly (directory-form tokens ending in ``/`` in the wiki source
match that segment as a directory; bare tokens match the segment's file
stem, i.e. with any single trailing extension removed).

EXEMPTION: ``tasks/<sid>/`` — the wiki's own allowlist carries
``tasks/<sid>/`` (per-session completeness-checklist mirror, added
2026-07-06, spec: ``docs/plans/2026-07-06-ceremony-as-pipeline-2-doe-land-
d-slice.md`` § C1.2) as a legitimate, intentionally-under-``tasks/``
surface. A first path segment that is SESSION-ID-shaped (hex digits and
hyphens only, >= 8 characters — the shape every session id in this repo
takes, e.g. ``state/subagent-share/<uuid>/``) is exempted from the surface
check entirely, matching the wiki's own carve-out rather than re-litigating
it here.

Negative-spec:
  - Does NOT deny/block anything — CLASS is "advisory"; the envelope carries
    only ``additionalContext``, never ``permissionDecision``. Per
    design-as-offers (global CLAUDE.md § Implementation Standards): the
    offer leads with the redirect (``did you mean state/<surface>?``), not
    a bare refusal.
  - Does NOT inspect Write/Edit/MultiEdit CONTENT — the violation is fully
    determined by the target path; content-shape guards
    (``nudge_baton_body_bar``, ``nudge_windows_subprocess_popup``) are a
    different concern and this module does not reconstruct whole-file state
    the way they do.
  - Does NOT fire on a bare substring coincidence — the match is anchored to
    the first path segment after ``tasks/``, mirroring
    ``nudge_baton_body_bar._matches_baton_path``'s anchoring discipline.
  - Does NOT fire on the ``tasks/<sid>/`` per-session mirror carve-out — see
    EXEMPTION above.
  - Does NOT widen the checked surface list by hand — the STEMS+GLOBS array
    below is transcribed verbatim from the wiki's own allowlist line; a
    drift between the two is a doctrine-sync defect, not something this
    guard's code should silently correct by adding entries of its own.
  - Never raises: any unexpected input shape returns ``None`` (ALLOW/no-op).

Discharge status (for the serial C14z prose-cut chunk that consumes this):
PARTLY discharges the placement-rule prose. This module structurally
enforces the STATE placement direction (writing a load-bearing surface
under ``tasks/`` now gets redirected toward ``state/<surface>`` at the tool
boundary, before any sweep can silently remove it) — the mechanize half of
DEC-6's discharge hierarchy. It does NOT discharge the surrounding
EXPLANATORY clause (why ``state/`` vs ``tasks/`` exist as a distinction at
all — the always-on-substrate-vs-UUID-flight-recorder framing, and the
"sweep has no way to know it was load-bearing" rationale) — that
explanatory framing has no artifact to read it off of once the tripwire
prose is gone; a future agent hitting this guard's advisory text sees
WHAT/WHERE to redirect to, but the WHY (the aggressive-sweep hazard model)
lives only in this module's own docstring and the wiki, neither of which is
always-on. C14z should keep that one explanatory clause in the always-on
file (or fold it into the advisory text itself) rather than treating this
guard as a full discharge of the whole paragraph.

Spec backlink: docs/wiki/coordinator-tripwires.md § tasks-state-folder-split
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 140  # deny-offer: runs after the structural block_* guards (≤90)

#: STEMS+GLOBS — transcribed verbatim (order and wording) from
#: docs/wiki/coordinator-tripwires.md § tasks-state-folder-split's
#: "Always-on `state/` substrate (enumerated allowlist)" line. Each entry is
#: (label-as-written-in-the-wiki, is_directory_form, is_category_word).
#: is_directory_form mirrors the wiki's own trailing "/" spelling for
#: directory surfaces; is_category_word marks the three tokens (trackers,
#: queues, ledgers) that are generic category words rather than literal
#: on-disk basenames — see module docstring "DETECTION" for the distinct
#: matching rule each class gets.
_SURFACE_TOKENS: Tuple[Tuple[str, bool, bool], ...] = (
    ("orientation_cache", False, False),
    ("lessons", False, False),
    ("handoffs", True, False),
    ("trackers", False, True),
    ("queues", False, True),
    ("ledgers", False, True),
    ("memos", True, False),
    ("review-trail", True, False),
    ("week-changelog", True, False),
    ("audits", True, False),
    ("recovery", True, False),
    ("scratch", True, False),
    ("debt-backlog", True, False),
    ("bug-backlog", True, False),
    ("improvement-queue", True, False),
)

#: Category-word -> singular substring to search for within the first path
#: segment's basename (case-insensitive). "trackers"/"queues"/"ledgers" in
#: the wiki are plural category words; the real on-disk basenames
#: (handoff-tracker.md, improvement-queue/, health-ledger.md) carry the
#: singular form.
_CATEGORY_SINGULAR = {
    "trackers": "tracker",
    "queues": "queue",
    "ledgers": "ledger",
}

#: ``tasks/<sid>/`` per-session completeness-checklist mirror exemption —
#: session ids in this repo are hex-and-hyphen, >= 8 characters (see module
#: docstring EXEMPTION).
_SESSION_ID_RE = re.compile(r"^[0-9a-f-]{8,}$", re.IGNORECASE)

#: ``tasks/`` path-segment anchor — mirrors nudge_baton_body_bar's
#: anchoring discipline (a leading path prefix, or a bare repo-relative
#: path) so a substring coincidence like ``vendor/mytasks/lessons.md`` does
#: not false-positive.
_TASKS_PREFIX_RE = re.compile(r"(?:^|/)tasks/(.+)$")


def _extract_file_path(payload: Dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _first_segment_and_stem(remainder: str) -> Tuple[str, str]:
    """Split the post-``tasks/`` remainder into (first_segment, file_stem).

    ``file_stem`` strips a single trailing extension from the first segment
    when it is the LAST segment (a bare file directly under ``tasks/``,
    e.g. ``lessons.md``); for a multi-segment remainder (e.g.
    ``handoffs/2026-07-27_foo.md``) ``file_stem`` equals the first segment
    unchanged, since the directory name itself carries no extension to
    strip.
    """
    parts = remainder.split("/", 1)
    first_segment = parts[0]
    if len(parts) == 1:
        stem = re.sub(r"\.[^.]+$", "", first_segment)
    else:
        stem = first_segment
    return first_segment, stem


def _matches_surface(first_segment: str, stem: str) -> Optional[str]:
    """Return the matched wiki-label if ``first_segment``/``stem`` name a
    load-bearing surface per ``_SURFACE_TOKENS``, else None.

    Exact literal/directory-form matches are checked FIRST, category-word
    substring matches SECOND — an exact match (e.g. ``improvement-queue/``,
    itself a named token) must win over an incidental substring match
    (``improvement-queue`` also containing the "queue" category-word
    substring) rather than the two racing on token declaration order.
    """
    stem_lower = stem.lower()
    segment_lower = first_segment.lower()
    for label, is_directory_form, is_category_word in _SURFACE_TOKENS:
        if is_category_word:
            continue
        if is_directory_form:
            if segment_lower == label:
                return label
        else:
            if stem_lower == label:
                return label
    for label, is_directory_form, is_category_word in _SURFACE_TOKENS:
        if not is_category_word:
            continue
        singular = _CATEGORY_SINGULAR[label]
        if singular in stem_lower:
            return label
    return None


_REASON_TEMPLATE = """tasks/{file_path_norm} looks load-bearing ({surface}), not scratch -- tasks/ is swept aggressively and this could get silently deleted. Did you mean `state/{surface}`? Offer only, not a block."""


def check(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        tool_name = payload.get("tool_name") or ""
        if tool_name not in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            return None

        file_path = _extract_file_path(payload)
        if not file_path:
            return None
        file_path_norm = file_path.replace("\\", "/")
        while "//" in file_path_norm:
            file_path_norm = file_path_norm.replace("//", "/")

        match = _TASKS_PREFIX_RE.search(file_path_norm)
        if not match:
            return None
        remainder = match.group(1)
        if not remainder:
            return None

        first_segment, stem = _first_segment_and_stem(remainder)
        if not first_segment:
            return None

        # tasks/<sid>/ per-session mirror exemption (see module docstring).
        if _SESSION_ID_RE.match(first_segment):
            return None

        surface = _matches_surface(first_segment, stem)
        if surface is None:
            return None

        reason = _REASON_TEMPLATE.format(file_path_norm=remainder, surface=surface)
        return {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "additionalContext": reason,
            }
        }
    except Exception:
        # Fail-OPEN on any unexpected error -- this guard offers only on a
        # positive surface match, never on an error.
        return None
