"""Review floor: a run whose declared paths include code needs at least one reviewer verdict.

Predicates over repo-relative forward-slash paths and an `inline_review` params dict; no git. The
only disk touch is `verdict_count` statting each verdict's sidecar. The base classifier lives in
`coordinator_core.coverage` (the terminal-review hook shares it); `is_code_path` here adds a
floor-only carve-in for executable config the doc-suffix rule mislabels.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Mapping, Optional, Sequence

from coordinator_core import coverage

HALT_REVIEW_FLOOR = "review_floor"
MANIFEST_KEY = "review_code_paths"

_REASON_PATHS_SHOWN = 5
_HOOKS_CONFIG_NAMES = ("hooks.yaml", "hooks.yml")

__all__ = [
    "HALT_REVIEW_FLOOR", "MANIFEST_KEY",
    "code_paths", "floor_code_paths", "floor_reason", "floor_trailer", "is_code_path", "receipt",
    "request_code_paths", "review_owed_paths", "reviewable_paths", "slice_gap_paths", "slice_gap_reason",
    "verdict_count",
]


def is_code_path(path: str) -> bool:
    """`coverage._is_code_path` plus the floor carve-in: CI workflow yaml, `hooks.yaml|yml` and
    `commands/`/`skills/` markdown outside `docs/` are code (`hooks.yaml|yml` is code even under
    `docs/`). Bookkeeping and planning prefixes stay non-code."""
    if coverage._is_code_path(path):
        return True
    if coverage._is_bookkeeping_path(path) or path.startswith(coverage._GATE_LOCAL_NONCODE_PREFIXES):
        return False
    if path.startswith(".github/workflows/") and path.endswith((".yml", ".yaml")):
        return True
    parts = path.split("/")
    if parts[-1] in _HOOKS_CONFIG_NAMES:
        return True
    if path.startswith("docs/"):
        return False
    return path.endswith(".md") and ("commands" in parts[:-1] or "skills" in parts[:-1])


def code_paths(paths: Iterable[str]) -> list[str]:
    """The code subset of `paths`, input order, deduped; a trailing "/" marks a directory prefix, is
    classified with its slash (so `state/` stays bookkeeping) and stripped from the result."""
    out: list[str] = []
    for raw in paths:
        if not isinstance(raw, str):
            continue
        path = raw.rstrip("/")
        if path and path not in out and is_code_path(raw if raw.endswith("/") else path):
            out.append(path)
    return out


def reviewable_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """The paths a reviewer can read a product diff for: bookkeeping prefixes
    (`review_stamp.PRODUCT_FILE_EXCLUDED_PREFIXES`) and `scratch/` freeze no reviewable diff. The
    stage route and the commit floor both classify through this, so they cannot disagree."""
    from coordinator_core.ops.review_stamp import PRODUCT_FILE_EXCLUDED_PREFIXES

    skip = (*PRODUCT_FILE_EXCLUDED_PREFIXES, "scratch/")
    return tuple(p for p in paths if not p.replace("\\", "/").removeprefix("./").startswith(skip))


def review_owed_paths(paths: Iterable[str]) -> list[str]:
    """`code_paths` over the `reviewable_paths` of `paths`: the one review-owed set."""
    return code_paths(reviewable_paths(p for p in paths if isinstance(p, str)))


def request_code_paths(request: object) -> list[str]:
    """The review-owed subset of every chunk's declared `paths` and `prefixes` on a `CommitRequest`."""
    return review_owed_paths(p for c in request.chunks for p in (*c.paths, *c.prefixes) if p)  # type: ignore[attr-defined]


def floor_code_paths(request: object, inline_review: object, prep_from_disk: bool) -> list[str]:
    """The declared code subset, or [] for a verification-only run: `prep_from_disk` (the lander
    matched a stored prep reply) and that prep's `product_files` is the int 0. A relayed prep alone
    never exempts."""
    if prep_from_disk and isinstance(inline_review, Mapping):
        prep = inline_review.get("prep")
        if isinstance(prep, Mapping):
            n = prep.get("product_files")
            if n == 0 and isinstance(n, int) and not isinstance(n, bool):
                return []
    return request_code_paths(request)


def _sidecar_exists(sidecar: object, root: Path) -> bool:
    """True for a non-empty string naming an existing file; existence is the whole bar (no content,
    location or agent-type check), so a caller-supplied path to any real file satisfies it."""
    if not isinstance(sidecar, str) or not sidecar.strip():
        return False
    try:
        return (Path(root) / sidecar.strip()).is_file()
    except OSError:
        return False


def verdict_count(inline_review: object, root: Path) -> int:
    """Wave reviews with a non-empty string `verdict` AND a `sidecar_path` resolving (relative to
    `root`, or absolute) to an existing file; a bare relayed verdict is not a code reviewer."""
    if not isinstance(inline_review, Mapping):
        return 0
    reviews = inline_review.get("wave_reviews")
    if not isinstance(reviews, Sequence) or isinstance(reviews, (str, bytes)):
        return 0
    return sum(
        1 for r in reviews
        if isinstance(r, Mapping) and isinstance(r.get("verdict"), str) and r["verdict"].strip()
        and _sidecar_exists(r.get("sidecar_path"), root)
    )


def receipt(n: int) -> str:
    """The ledger receipt text: why the ledger is green."""
    return f"review: {n} verdicts"


def floor_trailer(reason: str) -> str:
    """The commit trailer recording a floor or slice-gap advisory; the landing is never refused for it."""
    return f"Review-Floor-Note: {' '.join(reason.split())}"


def floor_reason(code: Sequence[str], n: int) -> Optional[str]:
    """None when there is no code path or at least one verdict, else the floor's refusal sentence."""
    if not code or n > 0:
        return None
    shown = ", ".join(code[:_REASON_PATHS_SHOWN])
    return f"review: 0 verdicts on a code-editing run ({shown})"


def slice_gap_paths(landing_paths: Iterable[str], review_slice: Iterable[str]) -> list[str]:
    """Review-owed paths the run will land (own hunks in co-held files included) that its review
    slice omits, in landing order. A path is covered only by an exact slice entry."""
    covered = {p.replace("\\", "/").removeprefix("./") for p in review_slice if isinstance(p, str)}
    return [p for p in review_owed_paths(landing_paths) if p not in covered]


def slice_gap_reason(gap: Sequence[str]) -> Optional[str]:
    """None when the slice covers every landing path, else the refusal sentence naming the gap."""
    if not gap:
        return None
    return f"review: slice omits {len(gap)} landing path(s) ({', '.join(gap[:_REASON_PATHS_SHOWN])})"
