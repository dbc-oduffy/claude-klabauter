"""
coordinator_core.commit_ledger.classify -- per-path review weight.

Purpose: `commit_ledger` (the join C5 writes at commit time and the oracles
downstream read) needs a review WEIGHT per changed path, not the bucket
label `review_brightline_gate.classify_surface` already returns. This
module adds that weighting layer on top of the existing, shared bucket
classifier rather than reimplementing a second classifier (dispatch brief,
C4).

`classify_surface` and `_is_noise_path` are IMPORTED from
`coordinator_core.ops.review_brightline_gate` -- both are declared shared
primitives there (module comment at review_brightline_gate.py:301-308,
2026-08-04 review finding, three existing consumers) and this module adds a
fourth rather than forking them.

`_PLANNING_LOC_WEIGHT` (review_brightline_gate.py) is deliberately NOT
imported or reused here -- its own comment declares it local to
brightline's mandate with an anti-scope forbidding export. `_ELEVATED_SURFACE_WEIGHT`
below is a NEW constant, scoped to this module's own weighting mandate.

Weight resolution for a path:

  1. `_is_noise_path(path)` -- noise contributes nothing: weight 0.0.
  2. Otherwise, bucket the path via `classify_surface` and look up that
     bucket's baseline weight in the per-repo `commit_ledger_baseline_weight`
     mapping (`coordinator.local.md` frontmatter, read via
     `resolve_validation_cmd.cs_read_local_md_mapping` -- the repo's
     established nested-mapping reader; reused rather than adding a second
     parser on this hot path, per the dispatch brief). An absent repo
     config, an absent bucket key, or a value that fails float-coercion all
     fall back to `_DEFAULT_BASELINE_WEIGHT` -- never zero (AC5): an
     unlisted path is a redundant review, never a blind spot.
  3. If `path` matches any glob in the per-repo `commit_ledger_elevated_surfaces`
     list (`coordinator.local.md`, read via
     `ops.doc_content_verify._read_local_md_list` -- the repo's established
     flat-list reader), the baseline weight is scaled by
     `_ELEVATED_SURFACE_WEIGHT`. This list enumerates the WEIGHTY paths
     only (three-to-five stable globs per repo, per the dispatch brief) --
     it is not, and must never become, an exhaustive path registry.
"""

from __future__ import annotations

import fnmatch
from typing import Union

from coordinator_core.ops.doc_content_verify import _read_local_md_list
from coordinator_core.ops.review_brightline_gate import (
    _is_noise_path,
    classify_surface,
)
from coordinator_core.resolve_validation_cmd import cs_read_local_md_mapping

#: Per-repo config key (coordinator.local.md, nested mapping) --
#: classify_surface bucket name -> baseline weight for that bucket.
_BASELINE_WEIGHT_KEY = "commit_ledger_baseline_weight"

#: Per-repo config key (coordinator.local.md, flat list) -- glob patterns
#: for paths whose review weight is elevated above their bucket baseline.
_ELEVATED_SURFACES_KEY = "commit_ledger_elevated_surfaces"

#: Fallback baseline weight for a bucket with no configured value, or for a
#: repo with no `commit_ledger_baseline_weight` mapping at all. Never 0.0
#: (AC5) -- an unlisted/unconfigured path still carries review weight.
_DEFAULT_BASELINE_WEIGHT = 1.0

#: Multiplier applied to the baseline weight when a path matches one of the
#: repo's `commit_ledger_elevated_surfaces` globs. Scoped to THIS module's
#: weighting mandate -- not exported, not a rename of brightline's
#: anti-scoped `_PLANNING_LOC_WEIGHT`.
_ELEVATED_SURFACE_WEIGHT = 2.0


def _coerce_weight(value: Union[str, int, float, None]) -> float:
    """Best-effort float coercion for a baseline-weight mapping value.

    Never raises: an absent, non-numeric, or otherwise malformed value
    falls back to `_DEFAULT_BASELINE_WEIGHT` rather than propagating an
    exception onto the commit-time write path.
    """
    if value is None:
        return _DEFAULT_BASELINE_WEIGHT
    try:
        return float(value)
    except (TypeError, ValueError):
        return _DEFAULT_BASELINE_WEIGHT


def weight_for_path(repo_root: str, path: str) -> float:
    """Return `path`'s review weight for `repo_root`'s commit ledger.

    Never raises and never returns a negative weight; a noise path (per
    `_is_noise_path`) always returns 0.0. Every other path returns a
    strictly positive weight (AC5) -- baseline, optionally scaled by
    `_ELEVATED_SURFACE_WEIGHT` when `path` matches a configured elevated
    glob.
    """
    if _is_noise_path(path):
        return 0.0

    bucket = classify_surface(path)
    baseline_map = cs_read_local_md_mapping(repo_root, _BASELINE_WEIGHT_KEY)
    baseline = _coerce_weight(baseline_map.get(bucket))

    elevated_globs = _read_local_md_list(repo_root, _ELEVATED_SURFACES_KEY)
    if any(fnmatch.fnmatch(path, glob) for glob in elevated_globs):
        return baseline * _ELEVATED_SURFACE_WEIGHT

    return baseline
