"""Pins the pre-review snapshot's path budget: a blob path stays within Windows MAX_PATH
(LongPathsEnabled=0, the fresh-box default) for a worst-case run id, short names are unchanged,
and the landing's comment-only-drift proof finds the dir review.prep wrote."""

from __future__ import annotations

import json
import os
from pathlib import Path

from coordinator_core.git.content_hash import eol_folded_sha256
from coordinator_core.ops.dispatch_emit.comment_only_drift import reviewed_preimages
from coordinator_core.ops.pre_review_dir import pre_review_dir

_SESSION = "d751b916-7fb3-479b-9d54-6c1d6892f3a0"
# The id that broke review.prep on a fresh Windows box (267-char blob path), and a far longer slug.
_LONG_RUN_IDS = [
    "warp-2026-11-17-12d24d-3-statusline-mods-and-native-form-20261117T161339-91c5df",
    "warp-2026-11-17-08a195-2-" + "x" * 160 + "-20261117T160207-1be3a6",
]
_SHORT_PREFIX = "manifest-ask-20261117T155009-f478b0-0123456789ab-prep"


def _prefix(run_id: str) -> str:
    return f"manifest-{run_id}-0123456789ab-prep"


def test_worst_case_run_id_blob_path_fits_max_path():
    # Length arithmetic only: a 48-char repo root, deeper than the fleet's D:\<repo> clones.
    root = Path(os.path.abspath(os.sep)) / ("r" * 45)
    share = root / ".coordinator-local" / "subagent-share" / _SESSION
    for run_id in _LONG_RUN_IDS:
        blob = pre_review_dir(share, _prefix(run_id)) / ("f" * 64)
        assert len(str(blob)) <= 259, str(blob)


def test_short_id_prefix_keeps_plain_name(tmp_path):
    assert pre_review_dir(tmp_path, _SHORT_PREFIX) == tmp_path / f"{_SHORT_PREFIX}.pre-review"


def test_hashed_name_is_stable_and_distinct(tmp_path):
    a, b = (pre_review_dir(tmp_path, _prefix(r)) for r in _LONG_RUN_IDS)
    assert a != b
    assert a == pre_review_dir(tmp_path, _prefix(_LONG_RUN_IDS[0]))
    assert a.name.endswith(".pre-review") and len(a.name) == 27


def test_comment_only_drift_reads_the_hashed_dir(tmp_path):
    share = tmp_path / "share"
    prefix = _prefix(_LONG_RUN_IDS[0])
    snap = pre_review_dir(share, prefix)
    snap.mkdir(parents=True)
    body = b"x = 1\n"
    digest = eol_folded_sha256(body)
    (snap / digest).write_bytes(body)
    (snap / "manifest.json").write_text(json.dumps({"files": {"a.py": digest}}), encoding="utf-8")
    sidecar_rel = f"share/{prefix}.review-prep.md"
    assert reviewed_preimages(tmp_path, sidecar_rel, {"a.py": digest}) == {"a.py": body}
