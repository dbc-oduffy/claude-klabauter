"""
coordinator_core.ops.handoff_match — JSON-RPC "handoff.match_candidates" operation.

Purpose: Read-only resolver that ranks the calling repo's ``state/handoffs/*.md``
handoff artifacts by fuzzy similarity of a caller-supplied ``text`` against each
handoff's title.  Returns a ranked ``candidates`` list of ``{handoff_id, title, score}``
dicts — the score is a blended float (0.0–1.0) combining 70% ``difflib.SequenceMatcher``
character-level ratio and 30% keyword token overlap, rounded to 4 decimals.  Powers the
offer-shaped "which handoff did you mean?" picker at fork-authoring time.

COMPUTE_ONLY — this op reads ``state/handoffs/*.md`` frontmatter and returns a computed
ranked list; it NEVER writes any file, issues any git command, or mutates any coordinator
substrate.  The COMPUTE_ONLY invariant (DR-208) is enforced by classification in
``coordinator_core/authz/classification.py``.

Self-registration: importing this module calls
``register_op("handoff.match_candidates", _handler)`` as a side-effect.
Add this module to ``coordinator_core/ops/__init__.py`` to trigger registration
at start_server() time.

Read-path: frontmatter is extracted and parsed via ``yaml.safe_load`` on the frontmatter
block (between first and second "---" line) to handle quoted title values correctly.
Ranking delegates to ``coordinator_core.ops.match_core.rank_candidates``
(stdlib ``difflib.SequenceMatcher`` only — no third-party fuzzy library added).

Worktree resolution mirrors ``goals_match.py`` and ``plan_match.py``:
  - When ``repo_root`` is provided (router-supplied git common dir), the worktree
    root is derived via ``main_worktree_root(repo_root)``.
  - If ``repo_root`` is absent the op returns ``{"candidates": []}`` with a logged
    warning rather than raising — empty is safe for a picker nudge.

Spec backlink: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C2
Sibling: coordinator_core/ops/plan_match.py, coordinator_core/ops/goals_match.py
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


def _collect_handoffs(handoffs_dir: Path) -> List[dict]:
    """Enumerate ``state/handoffs/*.md`` as ``[{id, title, text}]`` items for ranking.

    For each well-formed handoff the ``id`` field is the filename stem (handoff files are
    named by timestamp+slug convention, e.g. ``2026-07-02_230112_roadmap-pcore-12.md``).
    The ``text`` field (haystack) is the handoff's ``title`` frontmatter value, lowercased —
    sufficient for a "which handoff did you mean?" picker.

    Files with YAML parse errors, non-dict frontmatter, or missing ``title`` fields are
    quarantined (skipped with a warning).

    Returns ``[]`` when ``handoffs_dir`` is absent (graceful-absent, mirrors
    ``goals_match.py._collect_goals`` / ``plan_match.py._collect_plans``).

    Negative-spec:
    - Does NOT mutate any file or coordinator substrate.
    - Does NOT raise on missing/unreadable/malformed files — quarantines them.
    - Only reads ``state/handoffs/*.md``; does NOT scan archive/handoffs/ (live only).
    - The returned ``id`` key is the generic enumerator key; ``_handler`` remaps it to
      the ``handoff_id`` wire key so the op's output shape is well-typed.
    """
    items: List[dict] = []

    if not handoffs_dir.is_dir():
        return items

    for fpath in sorted(handoffs_dir.glob("*.md")):
        fname = fpath.name
        try:
            raw = fpath.read_text(encoding="utf-8").replace("\r\n", "\n")
            # Extract only the frontmatter block (between first and second "---" line).
            if raw.startswith("---\n"):
                parts = raw.split("---\n", 2)
                fm_text = parts[1]
            else:
                # No frontmatter block — skip; handoffs without frontmatter are malformed.
                _LOG.warning(
                    "handoff.match_candidates: skipping %s — no YAML frontmatter block", fname
                )
                continue
            fm = yaml.safe_load(fm_text)
        except Exception as exc:  # noqa: BLE001 — parity with goals_match quarantine pattern
            _LOG.warning("handoff.match_candidates: skipping %s — parse error: %s", fname, exc)
            continue

        if not isinstance(fm, dict):
            _LOG.warning(
                "handoff.match_candidates: skipping %s — YAML did not produce a dict", fname
            )
            continue

        title_val = fm.get("title")
        if not isinstance(title_val, str) or not title_val:
            _LOG.warning(
                "handoff.match_candidates: skipping %s — missing required field: title", fname
            )
            continue

        # Handoff id is the filename stem — the timestamp+slug convention makes this unique.
        handoff_id_val = fpath.stem

        # Haystack: title is sufficient for a "which handoff did you mean?" picker.
        haystack = title_val.lower()

        items.append({"id": handoff_id_val, "title": title_val, "text": haystack})

    return items


@register_op("handoff.match_candidates")
def _handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC "handoff.match_candidates" handler.

    Returns handoffs from ``state/handoffs/*.md`` ranked by fuzzy similarity of
    ``params["text"]`` against each handoff's title.

    Params:
        text (str): The string to match — e.g. a handoff name fragment.
                    If absent or empty, returns ``{"candidates": []}``.
        repo (str): Accepted and logged; path resolution uses ``repo_root``
                    (the router-supplied git common dir) NOT ``repo``.
                    Negative-spec: claude-klabauter ops resolve paths from the router-supplied
                    repo_root — there is no cross-repo path registry in this op.

    Returns:
        {
            "candidates": [
                {"handoff_id": str, "title": str, "score": float},
                ...
            ]
        }

    Candidates are sorted by score DESCENDING, tie-broken by handoff_id ASCENDING.
    Score is a blended float (0.7 * SequenceMatcher ratio + 0.3 * keyword overlap,
    rounded to 4 decimals, range 0.0–1.0).

    Worktree resolution (mirrors goals_match.py / plan_match.py):
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
            "handoff.match_candidates: no repo_root resolved — "
            "repo_root arg absent; returning empty candidates"
        )
        return {"candidates": []}

    handoffs_dir = worktree_root / "state" / "handoffs"
    # _collect_handoffs returns [{id, title, text}] using the generic enumerator protocol.
    # rank_candidates returns [{id, title, score}]; remap id→handoff_id to produce the
    # wire key (handoff.match_candidates emits "handoff_id", not "id").
    raw = rank_candidates(text, _collect_handoffs(handoffs_dir))
    candidates = [
        {"handoff_id": entry["id"], "title": entry["title"], "score": entry["score"]}
        for entry in raw
    ]

    return {"candidates": candidates}
