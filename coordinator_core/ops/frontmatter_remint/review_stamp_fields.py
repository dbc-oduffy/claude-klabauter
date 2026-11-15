"""Resolver family for `review_stamp` and `review_stamp.register`: delegates to review_stamp's own writers.

The handler calls `write` once per failing family, `review_stamp` before its
register. `write` is stateless: a plan with no `review_stamp` is minted (the
register leg then follows on the handler's next call); a plan with one gets its
register written.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

import yaml

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ops import review_stamp
from coordinator_core.ops.frontmatter_remint._contract import Refused, Resolved, Result


def resolve(path: Path, fm: Mapping[str, Any], repo_root: Path) -> Result:
    """Always `Resolved({})`: both fields are written by `write`, never copied by the handler."""
    return Resolved({})


def _has_stamp(path: Path) -> bool:
    split = split_frontmatter(path.read_text(encoding="utf-8"))
    data = yaml.safe_load(split.fm_text) if split is not None else None
    return isinstance(data, dict) and isinstance(data.get("review_stamp"), dict)


def _register_refusal(text: str) -> Refused:
    if "has no review_stamp" in text:
        return Refused("register-no-stamp", text)
    if "no resolvable register_claims" in text:
        return Refused("register-absent", text)
    return Refused("register-unsettled", text)


def write(path: Path, repo_root: Path) -> Result:
    """Mint the stamp when absent (`repair=True`), else write its register; refuse with a REASONS member."""
    if not _has_stamp(path):
        try:
            review_stamp.mint(path, repo_root, build_test_path=None, repair=True)
        except review_stamp.MintRefusal as exc:
            return Refused("review-run-absent", str(exc))
        return Resolved({})
    try:
        review_stamp.write_register(path, repo_root)
    except review_stamp.MintRefusal as exc:
        return _register_refusal(str(exc))
    return Resolved({})
