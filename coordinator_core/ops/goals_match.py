"""
coordinator_core.ops.goals_match — JSON-RPC "goal.match_candidates" operation.

Purpose: Read-only resolver that ranks the calling repo's ``state/goals/*.yaml``
authored goal artifacts by fuzzy similarity of a caller-supplied ``text`` against
each goal's objective + key-result text.  Returns a ranked ``candidates`` list of
``{goal_id, title, score}`` dicts — the score is a blended float (0.0–1.0)
combining 70% ``difflib.SequenceMatcher`` character-level ratio and 30% keyword
token overlap, rounded to 4 decimals.  The keyword-overlap component
(token-intersection / max(query-length, 1)) compensates for SequenceMatcher's
length-sensitivity against goals with many key results.  Powers the offer-shaped
"did you mean to tag goal X?" laddering nudge at initiative-authoring time.

COMPUTE_ONLY — this op reads ``state/goals/*.yaml`` and returns a computed ranked
list; it NEVER writes any file, issues any git command, or mutates any coordinator
substrate.  The COMPUTE_ONLY invariant (DR-208) is enforced by classification in
``coordinator_core/authz/classification.py``.

Self-registration: importing this module calls
``register_op("goal.match_candidates", _handler)`` as a side-effect.
Add this module to ``coordinator_core/ops/__init__.py`` to trigger registration
at start_server() time.

Read-path: ``yaml.safe_load`` is used directly (imported from the PyYAML standard
engine-op dependency).  The flat ``_simple_yaml_load`` (scalar-only) and
``_parse_frontmatter`` (scalar-arrays only) are NOT used here because the goal
artifact carries ``key_results`` as an array of nested mappings; both helpers
corrupt nested structures.  PyYAML is already an engine-op dependency
(handoff_transition.py, memo_transition.py, emit/recorder.py, fleet/prune_bugs.py).
The ranking step delegates to ``coordinator_core.ops.match_core.rank_candidates``
(stdlib ``difflib.SequenceMatcher`` only — no third-party fuzzy library added).

Worktree resolution mirrors ``handoff_children.py`` and ``initiatives_serve.py``:
  - When ``repo_root`` is provided (router-supplied git common dir), the worktree
    root is derived via ``main_worktree_root(repo_root)``.
  - If ``repo_root`` is absent the op returns ``{"candidates": []}`` with a logged
    warning rather than raising — empty is safe for a nudge.

Spec backlink: DoE-claude:pln-per-repo-okr-goal-setting-syst-80bced § C3
Refactor: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C1
Parity/sibling: coordinator_core/ops/initiatives_serve.py, coordinator_core/ops/match_core.py
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import yaml

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.match_core import rank_candidates

_LOG = logging.getLogger(__name__)


def _collect_goals(goals_dir: Path) -> List[dict]:
    """Enumerate ``state/goals/*.yaml`` as ``[{id, title, text}]`` items for ranking.

    For each well-formed active goal the ``text`` field (haystack) is the goal's
    ``title``, ``objective``, and every ``key_results[i].text`` joined with spaces,
    lowercased — identical order to the pre-refactor ``_collect_candidates`` haystack.

    Files with YAML parse errors, non-dict results, or missing required ``id``/``title``
    fields are quarantined (skipped with a warning).  Goals with an absent ``status``
    field are also skipped with a warning (operator signal: unset status is a data
    error, distinct from an intentionally achieved/abandoned goal).  Goals whose status
    is present but not ``"active"`` (achieved, abandoned) are silently skipped — the
    nudge must not suggest completed goals.

    Returns ``[]`` when ``goals_dir`` is absent (graceful-absent, mirrors
    ``initiatives_serve.py._collect_initiatives`` / ``handoff_children.py``).

    Negative-spec:
    - Does NOT mutate any file or coordinator substrate.
    - Does NOT raise on missing/unreadable/malformed files — quarantines them.
    - Does NOT use ``_simple_yaml_load`` or ``_parse_frontmatter`` — those helpers corrupt
      nested ``key_results`` array-of-mappings structures.
    - Only reads ``state/goals/*.yaml``; no cross-repo lookup.
    - The returned ``id`` key is the generic enumerator key; ``_handler`` remaps it to
      the ``goal_id`` wire key so the op's output shape is unchanged.
    """
    items: List[dict] = []

    if not goals_dir.is_dir():
        return items

    for fpath in sorted(goals_dir.glob("*.yaml")):
        fname = fpath.name
        try:
            # Review: code-reviewer — normalise CRLF at read time so Windows-authored
            # goal files take the "---\n" frontmatter-extraction path correctly.
            raw = fpath.read_text(encoding="utf-8").replace("\r\n", "\n")
            # Defensive frontmatter extraction: if the file starts with "---\n",
            # extract and parse only the frontmatter block (content between first and
            # second "---" line); otherwise parse the whole file.
            if raw.startswith("---\n"):
                parts = raw.split("---\n", 2)
                # parts = ["", frontmatter, optional-body]; parts[1] is always present.
                # Review: code-reviewer — len(parts) >= 2 was always-true inside this
                # branch; the else raw arm was dead code. Set fm_text directly.
                fm_text = parts[1]
            else:
                fm_text = raw
            fm = yaml.safe_load(fm_text)
        except Exception as exc:  # noqa: BLE001 — parity with emit porter quarantine
            _LOG.warning("goal.match_candidates: skipping %s — parse error: %s", fname, exc)
            continue

        if not isinstance(fm, dict):
            _LOG.warning(
                "goal.match_candidates: skipping %s — YAML did not produce a dict", fname
            )
            continue

        id_val = fm.get("id")
        title_val = fm.get("title")

        if not isinstance(id_val, str) or not id_val:
            _LOG.warning(
                "goal.match_candidates: skipping %s — missing required field: id", fname
            )
            continue
        if not isinstance(title_val, str) or not title_val:
            _LOG.warning(
                "goal.match_candidates: skipping %s — missing required field: title", fname
            )
            continue

        # Only offer active goals in the nudge; achieved/abandoned are silently excluded.
        # Review: code-reviewer — absent status (None) is a data error distinct from
        # an intentionally non-active goal; log a warning so the operator can diagnose
        # why no candidates are being suggested rather than silently returning [].
        status_val = fm.get("status")
        if status_val is None:
            _LOG.warning(
                "goal.match_candidates: skipping %s — missing required field: status", fname
            )
            continue
        if status_val != "active":
            continue

        # Build the haystack from title, objective, and all key_results[*].text.
        # Field order (title → objective → kr_texts) and lowercasing are preserved
        # byte-for-byte from the pre-refactor implementation — changing this order
        # would shift scores and break the regression gate in test_goals_match.py.
        objective = fm.get("objective") or ""
        if not isinstance(objective, str):
            objective = ""

        key_results = fm.get("key_results")
        if not isinstance(key_results, list):
            key_results = []

        kr_texts: List[str] = []
        for kr in key_results:
            if isinstance(kr, dict):
                kr_text = kr.get("text")
                if isinstance(kr_text, str) and kr_text:
                    kr_texts.append(kr_text)

        haystack_parts = [title_val, objective] + kr_texts
        haystack = " ".join(part for part in haystack_parts if part).lower()

        items.append({"id": id_val, "title": title_val, "text": haystack})

    return items


@register_op("goal.match_candidates")
def _handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC "goal.match_candidates" handler.

    Returns active goals from ``state/goals/*.yaml`` ranked by fuzzy similarity
    of ``params["text"]`` against each goal's objective + key-result text.

    Params:
        text (str): The string to match — e.g. an initiative label.
                    If absent or empty, returns ``{"candidates": []}``.
        repo (str): Accepted and logged; path resolution uses ``repo_root``
                    (the router-supplied git common dir) NOT ``repo``.
                    Negative-spec: claude-klabauter ops resolve paths from the router-supplied
                    repo_root — there is no cross-repo path registry in this op.

    Returns:
        {
            "candidates": [
                {"goal_id": str, "title": str, "score": float},
                ...
            ]
        }

    Candidates are sorted by score DESCENDING, tie-broken by goal_id ASCENDING.
    Score is a blended float (0.7 * SequenceMatcher ratio + 0.3 * keyword overlap,
    rounded to 4 decimals, range 0.0–1.0).  Only goals with ``status == "active"``
    are included; goals with absent ``status`` are warned and skipped.

    Worktree resolution (mirrors initiatives_serve.py / handoff_children.py):
    - repo_root (router-supplied git common dir) → main_worktree_root(repo_root)
    - None → return empty candidates with logged warning (empty is safe for a nudge)
    """
    text = params.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return {"candidates": []}

    if repo_root is not None:
        worktree_root = main_worktree_root(repo_root)  # router common_dir → worktree root
    else:
        _LOG.warning(
            "goal.match_candidates: no repo_root resolved — "
            "repo_root arg absent; returning empty candidates"
        )
        return {"candidates": []}

    goals_dir = worktree_root / "state" / "goals"
    # _collect_goals returns [{id, title, text}] using the generic enumerator protocol.
    # rank_candidates returns [{id, title, score}]; remap id→goal_id to preserve the
    # wire key (goal.match_candidates has always emitted "goal_id", not "id").
    raw = rank_candidates(text, _collect_goals(goals_dir))
    candidates = [
        {"goal_id": entry["id"], "title": entry["title"], "score": entry["score"]}
        for entry in raw
    ]

    return {"candidates": candidates}
