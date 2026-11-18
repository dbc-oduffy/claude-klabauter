"""coordinator_core.ops.pre_review_dir -- where a slice's write-once pre-review snapshot lives.

Every reader and writer of ``<share>/<...>.pre-review/`` derives the directory here, so the
writer (``review.prep``), ``review.reverify`` and the landing's comment-only-drift proof agree.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

# Trap: Windows ships with LongPathsEnabled=0, so a path over 259 chars raises FileNotFoundError.
# The deepest fixed-length snapshot path is ``<dir>/<sha256 hex>``; a ``warp-<sizing slug>`` run id
# inside ``slice_prefix`` pushed it to 267, and a refire reuses the id, so a refire never cleared it.
# Capping the dir name keeps the blob path at share + ~140 chars wherever the repo lives.
# Invariant: the cap sits above every ``manifest-ask-<ts>-<hex>-<sha12>-prep`` prefix (53 chars),
# so short-id snapshots already on disk keep their plain name.
MAX_PREFIX_LEN = 64
_HASHED_LEN = 16
_SUFFIX = ".pre-review"


def pre_review_dir(share: Path, slice_prefix: str) -> Path:
    """``<share>/<slice_prefix>.pre-review``, or ``<share>/<sha256(slice_prefix)[:16]>.pre-review``
    when ``slice_prefix`` is longer than ``MAX_PREFIX_LEN``."""
    if len(slice_prefix) <= MAX_PREFIX_LEN:
        return share / f"{slice_prefix}{_SUFFIX}"
    digest = hashlib.sha256(slice_prefix.encode("utf-8")).hexdigest()[:_HASHED_LEN]
    return share / f"{digest}{_SUFFIX}"
