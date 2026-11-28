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

Spec backlink: pln-claude-klabauter-fork-provenance-creatio-01c09f § C2
Sibling: coordinator_core/ops/handoff_match.py, coordinator_core/ops/goals_match.py
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

import yaml

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.match_core import QuarantineLog, rank_candidates

_LOG = logging.getLogger(__name__)


_HEAD_CHUNK = 16384
_FM_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

#: invariant: path -> ((mtime_ns, size), ("item", id, title) | ("skip", reason, detail)). A warm engine
#: re-reads only the plans that changed; a cold scan of ~2.7k plans took 15s in pure-Python YAML.
_ENTRY_CACHE: dict = {}


def _frontmatter_text(fpath: Path) -> Optional[str]:
    """The text between the first two `---\n` delimiters (to EOF when unclosed), or None when the
    file does not open with one. Reads the head only: plan bodies are never needed."""
    head = ""
    with fpath.open("r", encoding="utf-8", newline="") as fh:
        while True:
            chunk = fh.read(_HEAD_CHUNK)
            head += chunk
            norm = head.replace("\r\n", "\n")
            if len(norm) >= 4 and not norm.startswith("---\n"):
                return None
            end = norm.find("---\n", 4)
            if end != -1:
                return norm[4:end]
            if not chunk:
                return norm[4:] if norm.startswith("---\n") else None


def _plan_entry(fpath: Path) -> tuple:
    try:
        fm_text = _frontmatter_text(fpath)
        if fm_text is None:
            return ("skip", "no YAML frontmatter block", None)
        fm = yaml.load(fm_text, Loader=_FM_LOADER)
    except Exception as exc:  # noqa: BLE001 — parity with goals_match quarantine pattern
        return ("skip", "parse error", str(exc))
    if not isinstance(fm, dict):
        return ("skip", "YAML did not produce a dict", None)
    title_val = fm.get("title")
    if not isinstance(title_val, str) or not title_val:
        return ("skip", "missing required field: title", None)
    plan_id_val = fm.get("plan_id")
    if not isinstance(plan_id_val, str) or not plan_id_val:
        plan_id_val = fpath.stem
    return ("item", plan_id_val, title_val)


def _collect_plans(plans_dir: Path) -> List[dict]:
    items: List[dict] = []

    if not plans_dir.is_dir():
        return items

    quarantine = QuarantineLog("plan.match_candidates", _LOG)

    for fpath in sorted(plans_dir.glob("*.md")):
        fname = fpath.name
        quarantine.scanned()
        try:
            st = fpath.stat()
        except OSError as exc:
            quarantine.skip(fname, "parse error", str(exc))
            continue
        stamp = (st.st_mtime_ns, st.st_size)
        cached = _ENTRY_CACHE.get(str(fpath))
        if cached is not None and cached[0] == stamp:
            entry = cached[1]
        else:
            entry = _plan_entry(fpath)
            _ENTRY_CACHE[str(fpath)] = (stamp, entry)
        if entry[0] == "skip":
            quarantine.skip(fname, entry[1], entry[2])
            continue
        items.append({"id": entry[1], "title": entry[2], "text": entry[2].lower()})

    quarantine.summarize()
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
                    Negative-spec: claude-klabauter ops resolve paths from the router-supplied
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
        worktree_root = main_worktree_root(repo_root)
    else:
        _LOG.warning(
            "plan.match_candidates: no repo_root resolved — "
            "repo_root arg absent; returning empty candidates"
        )
        return {"candidates": []}

    plans_dir = worktree_root / "docs" / "plans"
    raw = rank_candidates(text, _collect_plans(plans_dir))
    candidates = [
        {"plan_id": entry["id"], "title": entry["title"], "score": entry["score"]}
        for entry in raw
    ]

    return {"candidates": candidates}
