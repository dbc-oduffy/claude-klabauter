"""
coordinator_core.ops.plan_match — JSON-RPC "plan.match_candidates" operation.

Purpose: Read-only resolver that ranks the calling repo's ``docs/plans/*.md``
plan documents by fuzzy similarity of a caller-supplied ``text`` against each
plan's title.  Returns a ranked ``candidates`` list of ``{plan_id, title, score}``
dicts — the score is a blended float (0.0–1.0) combining 70% ``difflib.SequenceMatcher``
character-level ratio and 30% keyword token overlap, rounded to 4 decimals.  Powers the
offer-shaped "which plan did you mean?" picker at fork-authoring time.

COMPUTE_ONLY — this op reads ``docs/plans/*.md`` frontmatter and returns a computed
ranked list; it NEVER writes any file, issues any git command, or mutates any coordinator
substrate.  The COMPUTE_ONLY invariant (DR-208) is enforced by classification in
``coordinator_core/authz/classification.py``.

Self-registration: importing this module calls
``register_op("plan.match_candidates", _handler)`` as a side-effect.
Add this module to ``coordinator_core/ops/__init__.py`` to trigger registration
at start_server() time.

Read-path: frontmatter is extracted and parsed via a minimal scalar-only approach
(the ``_parse_frontmatter`` flat helper is NOT used; we do our own split on ``---``
and extract title/plan_id with ``yaml.safe_load`` to handle quoted values).
Ranking delegates to ``coordinator_core.ops.match_core.rank_candidates``
(stdlib ``difflib.SequenceMatcher`` only — no third-party fuzzy library added).

Worktree resolution mirrors ``goals_match.py`` and ``initiatives_serve.py``:
  - When ``repo_root`` is provided (router-supplied git common dir), the worktree
    root is derived via ``main_worktree_root(repo_root)``.
  - If ``repo_root`` is absent the op returns ``{"candidates": []}`` with a logged
    warning rather than raising — empty is safe for a picker nudge.

Spec backlink: pln-makima-fork-provenance-creatio-01c09f § C2
Sibling: coordinator_core/ops/handoff_match.py, coordinator_core/ops/goals_match.py
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


def _collect_plans(plans_dir: Path) -> List[dict]:
    """Enumerate ``docs/plans/*.md`` as ``[{id, title, text}]`` items for ranking.

    For each well-formed plan document the ``id`` field is the ``plan_id`` frontmatter
    value when present, otherwise the filename stem.  The ``text`` field (haystack) is
    the plan's ``title`` lowercased — sufficient for a "which plan did you mean?" picker.

    Files with YAML parse errors, non-dict frontmatter, or missing ``title`` fields are
    quarantined (skipped with a warning).  Files without ``plan_id`` frontmatter use the
    filename stem as the id (graceful fallback — many plans predate the ``plan_id`` field).

    Returns ``[]`` when ``plans_dir`` is absent (graceful-absent, mirrors
    ``goals_match.py._collect_goals`` / ``initiatives_serve.py._collect_initiatives``).

    Negative-spec:
    - Does NOT mutate any file or coordinator substrate.
    - Does NOT raise on missing/unreadable/malformed files — quarantines them.
    - Only reads ``docs/plans/*.md``; no cross-repo lookup.
    - The returned ``id`` key is the generic enumerator key; ``_handler`` remaps it to
      the ``plan_id`` wire key so the op's output shape is well-typed.
    """
    items: List[dict] = []

    if not plans_dir.is_dir():
        return items

    for fpath in sorted(plans_dir.glob("*.md")):
        fname = fpath.name
        try:
            raw = fpath.read_text(encoding="utf-8").replace("\r\n", "\n")
            # Extract only the frontmatter block (between first and second "---" line).
            if raw.startswith("---\n"):
                parts = raw.split("---\n", 2)
                fm_text = parts[1]
            else:
                # No frontmatter block — not a plan; generated indexes (e.g. INDEX.md)
                # land in docs/plans/ as bare markdown and must not be YAML-parsed whole.
                _LOG.warning(
                    "plan.match_candidates: skipping %s — no YAML frontmatter block", fname
                )
                continue
            fm = yaml.safe_load(fm_text)
        except Exception as exc:  # noqa: BLE001 — parity with goals_match quarantine pattern
            _LOG.warning("plan.match_candidates: skipping %s — parse error: %s", fname, exc)
            continue

        if not isinstance(fm, dict):
            _LOG.warning(
                "plan.match_candidates: skipping %s — YAML did not produce a dict", fname
            )
            continue

        title_val = fm.get("title")
        if not isinstance(title_val, str) or not title_val:
            _LOG.warning(
                "plan.match_candidates: skipping %s — missing required field: title", fname
            )
            continue

        # plan_id frontmatter is preferred; filename stem is the graceful fallback.
        plan_id_val = fm.get("plan_id")
        if not isinstance(plan_id_val, str) or not plan_id_val:
            plan_id_val = fpath.stem

        # Haystack: title is sufficient for a "which plan did you mean?" picker.
        haystack = title_val.lower()

        items.append({"id": plan_id_val, "title": title_val, "text": haystack})

    return items


@register_op("plan.match_candidates")
def _handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC "plan.match_candidates" handler.

    Returns plans from ``docs/plans/*.md`` ranked by fuzzy similarity of
    ``params["text"]`` against each plan's title.

    Params:
        text (str): The string to match — e.g. a plan name fragment.
                    If absent or empty, returns ``{"candidates": []}``.
        repo (str): Accepted and logged; path resolution uses ``repo_root``
                    (the router-supplied git common dir) NOT ``repo``.
                    Negative-spec: makima ops resolve paths from the router-supplied
                    repo_root — there is no cross-repo path registry in this op.

    Returns:
        {
            "candidates": [
                {"plan_id": str, "title": str, "score": float},
                ...
            ]
        }

    Candidates are sorted by score DESCENDING, tie-broken by plan_id ASCENDING.
    Score is a blended float (0.7 * SequenceMatcher ratio + 0.3 * keyword overlap,
    rounded to 4 decimals, range 0.0–1.0).

    Worktree resolution (mirrors goals_match.py / initiatives_serve.py):
    - repo_root (router-supplied git common dir) → main_worktree_root(repo_root)
    - None → return empty candidates with logged warning (empty is safe for a picker)
    """
    text = params.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return {"candidates": []}

    if repo_root is not None:
        worktree_root = main_worktree_root(repo_root)  # router common_dir → worktree root
    else:
        _LOG.warning(
            "plan.match_candidates: no repo_root resolved — "
            "repo_root arg absent; returning empty candidates"
        )
        return {"candidates": []}

    plans_dir = worktree_root / "docs" / "plans"
    # _collect_plans returns [{id, title, text}] using the generic enumerator protocol.
    # rank_candidates returns [{id, title, score}]; remap id→plan_id to produce the
    # wire key (plan.match_candidates emits "plan_id", not "id").
    raw = rank_candidates(text, _collect_plans(plans_dir))
    candidates = [
        {"plan_id": entry["id"], "title": entry["title"], "score": entry["score"]}
        for entry in raw
    ]

    return {"candidates": candidates}
