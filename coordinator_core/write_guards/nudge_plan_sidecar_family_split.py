"""coordinator_core.write_guards.nudge_plan_sidecar_family_split — advisory guard.

Spec: docs/plans/2026-08-03-plan-sidecar-write-seam-guard.md

Motivation: ``state/plan-sidecars/`` and ``state/subagent-share/`` are a
deliberate two-family split with OPPOSITE reap lifecycles (spec's Problem
section, citing ``state-placement-law.md`` rows 38/39). Row 39 reserves
``state/plan-sidecars/<stem>.<lens>.md`` for exactly the five plan-derivable
lens emitters named in ``provision_report._PLAN_DERIVABLE_LENS`` and marks
that class UNREAPED BY DESIGN — a second run of the same lens against the
same plan must find the first run's verdict at the same plan-derived path.
Row 38 puts every OTHER provisioned subagent sidecar, persona findings
included, at the session-keyed ``state/subagent-share/<session>/<name>.md``,
reaped under a liveness-and/or-age floor. Seven files in ``state/plan-
sidecars/`` today carry a persona name (``staff-eng-review``, ``memo-
residue``, ``cross-repo-amendment``, ...) instead of one of the five row-39
lenses — the engine never produced them (no persona type has ever been a
``_PLAN_DERIVABLE_LENS`` key), so a dispatching EM named that path in a
brief. This guard closes the write seam that let that happen, rather than
relying on brief prose to be remembered (this repo's discharge test).

Modeled on ``nudge_tasks_state_folder_split.py``'s shape: wrong-folder
detection, offering a redirect at write time, off the target path alone.

DETECTION: the target path's directory is ``state/plan-sidecars/`` (anchored
to a path-segment boundary) and its filename is one of three shapes: the
row-39 form ``<stem>.<lens>.md``, the archival rename-on-existing form
``<stem>.<lens>.<ISO8601>.md`` (e.g. ``...prior-art-check.2026-07-27T19-46-
54Z.md``), or the doubled-extension archival form ``<stem>.<lens>.md.
<ISO8601>.md`` (e.g. ``...prior-art-check.md.2026-08-03T14-57-57Z.md`` —
produced by a rename-on-existing tool that appended timestamp+``.md`` to an
already-``.md``-suffixed name rather than replacing the suffix). Both
archival shapes are real forms already on disk, verified via ``ls state/
plan-sidecars/`` (the doubled-extension shape was initially missed by that
verification pass and fixed after a code-review finding). ``<lens>`` is
found by scanning dot-segments right-to-left: drop a trailing ISO8601-shaped
segment, then drop any further trailing bare ``md`` extension segments, and
take the first remaining segment as the lens — rather than counting a fixed
position from the end, which mis-reads the doubled-extension shape's stray
``md`` segment as the lens. The result is compared against
``provision_report._PLAN_DERIVABLE_LENS.values()`` — imported, never
re-listed as a literal (AC5 of the spec): a drift between two copies of that
list is precisely the defect class this guard exists to prevent, reintroduced
one level up.

Negative-spec:
  - Does NOT deny/block anything — CLASS is "advisory"; the envelope carries
    only ``additionalContext``, never ``permissionDecision``. Leads with the
    redirect offer, per design-as-offers (global CLAUDE.md § Implementation
    Standards), same as the tasks/state sibling this is modeled on.
  - Does NOT fire on any of the five row-39 lenses (``prior-art-check``,
    ``plan-coverage-check``, ``docs-check``, ``external-pattern``) — AC3.
  - Does NOT fire on the archival ``<stem>.<lens>.<ISO8601>.md`` rename form,
    nor its doubled-extension variant ``<stem>.<lens>.md.<ISO8601>.md``, when
    ``<lens>`` is one of the five — AC4.
  - Does NOT relocate or touch any of the 7 existing misplaced files (spec
    Anti-scope) — this module only evaluates a PreToolUse payload; it has no
    write path of its own.
  - Does NOT re-list the five lens names as a literal (AC5) — imports
    ``provision_report._PLAN_DERIVABLE_LENS`` instead.
  - Does NOT hard-block (spec Anti-scope) — a guard that asks a model to
    refuse work it is eager to do is advisory in practice; the standing
    carve-out for hard blocks is irreversible harm, which this is not.
  - Never raises: any unexpected input shape returns ``None`` (ALLOW/no-op).

Spec backlink: pln-plan-sidecar-write-seam-guard-4e1906 § AC1-AC6
"""

from __future__ import annotations

import re
from typing import Any, Dict, Optional, Tuple

from coordinator_core.subagent_sandbox.provision_report import _PLAN_DERIVABLE_LENS

CLASS = "advisory"
MATCHERS = ["Write", "Edit", "MultiEdit", "NotebookEdit"]
PRIORITY = 141  # deny-offer: within-phase order only -- engine.py's two-phase
# design (hard-deny phase runs to completion before the advisory phase
# starts at all) is what actually protects ordering against hard-deny
# guards, regardless of numeric priority; this number only orders relative
# to other advisory guards.
#
# Was 140, which collided with nudge_tasks_state_folder_split.py's own
# PRIORITY = 140 -- engine.py sorts by PRIORITY and takes the first
# non-None on a tie, so resolution fell to undefined discovery order.
# Moved to 141 (docs/wiki/write-guard-priority-bands.md § "The 140/140
# collision (AC9)") -- one slot after its former tied peer, with no
# observable behavior change since the two guards' surfaces (plan-sidecar
# placement vs. tasks/-state placement) rarely co-match the same write.

#: Row-39-legal lens suffixes, imported (not re-listed) per AC5.
_ROW_39_LENSES = frozenset(_PLAN_DERIVABLE_LENS.values())

#: Anchors ``state/plan-sidecars/<filename>`` at a path-segment boundary,
#: mirroring ``nudge_tasks_state_folder_split._TASKS_PREFIX_RE``'s anchoring
#: discipline so a coincidental substring does not false-positive.
#: Both roots. The bucket relocated to `.coordinator-local/plan-sidecars/`;
#: an advisory pinned to the old root stops firing without failing, exactly
#: as the two hard-deny sidecar guards did. Kept as a local pattern rather
#: than a seam accessor because this is the only plan-sidecar path matcher
#: in the tree -- one call site does not make a convention, and
#: `machinery_paths.plan_sidecars_dir` already owns the directory itself.
_SIDECAR_DIR_RE = re.compile(
    r"(?:^|/)(?:state|\.coordinator-local)/plan-sidecars/([^/]+)$"
)

#: The archival rename-on-existing timestamp segment, e.g.
#: ``2026-07-27T19-46-54Z`` — verified against real files on disk (colons
#: replaced with dashes, trailing ``Z``).
_ISO8601_SEGMENT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}Z$")


def _extract_file_path(payload: Dict[str, Any]) -> str:
    tool_input = payload.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return ""
    return tool_input.get("file_path") or tool_input.get("notebook_path") or ""


def _extract_lens(filename: str) -> Optional[Tuple[str, str]]:
    """Return ``(stem, lens)`` for a row-39-shaped filename, else None.

    Accepts ``<stem>.<lens>.md``, the archival ``<stem>.<lens>.<ISO8601>.md``
    form, and the doubled-extension archival form
    ``<stem>.<lens>.md.<ISO8601>.md``. A filename with fewer than two dot-
    segments (no recognizable ``<lens>`` position at all) returns None —
    this guard only judges the lens value, not the general filename shape.

    Scans dot-segments right-to-left rather than counting a fixed position
    from the end: a trailing ISO8601-shaped segment is dropped first (the
    archival timestamp), then any further trailing bare ``md`` extension
    segments are dropped (the doubled-extension shape), and the first
    remaining segment is taken as the lens. Counting fixed positions
    mis-reads the doubled-extension shape's stray ``md`` segment as the
    lens (code-review Finding 1).
    """
    if not filename.endswith(".md"):
        return None
    stem_and_lens = filename[: -len(".md")]
    parts = stem_and_lens.split(".")
    if len(parts) < 2:
        return None
    idx = len(parts) - 1
    if _ISO8601_SEGMENT_RE.match(parts[idx]):
        idx -= 1
    while idx > 0 and parts[idx] == "md":
        idx -= 1
    if idx < 0:
        return None
    lens = parts[idx]
    stem = ".".join(parts[:idx])
    return stem, lens


_REASON_TEMPLATE = (
    "{lens} is not a plan-derivable lens -- state/plan-sidecars/ is "
    "unreaped-by-design for those only. Did you mean "
    "`state/subagent-share/<session>/<name>.md`? Offer only, not a block."
)


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

        match = _SIDECAR_DIR_RE.search(file_path_norm)
        if not match:
            return None
        filename = match.group(1)
        if not filename:
            return None

        parsed = _extract_lens(filename)
        if parsed is None:
            return None
        _stem, lens = parsed

        if lens in _ROW_39_LENSES:
            return None

        reason = _REASON_TEMPLATE.format(lens=lens)
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
