"""Fold bar: reads the recorded full-suite verdict for a sha and says whether a fold or publish may proceed.

Pure file read, zero spawns. The suite itself is never run here; a verdict file is written by the
background suite job under a tier-u grant.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from coordinator_core.ops.dispatch_emit.run_truth_contract import (
    SUITE_TIER_FULL,
    SUITE_VERDICT_PATH_TEMPLATE,
    SuiteVerdict,
)


@dataclass(frozen=True)
class FoldBarResult:
    """`ok` is true only for a recorded, full-tier, zero-failure, fully bisected verdict."""

    ok: bool
    sha: str
    reason: Optional[str] = None
    nodeids: tuple = ()
    verdict: Optional[SuiteVerdict] = None


def _refuse(sha: str, reason: str, nodeids: tuple = (), verdict: Optional[SuiteVerdict] = None) -> FoldBarResult:
    return FoldBarResult(False, sha, reason, nodeids, verdict)


def verdict(sha: str, repo_root: Path) -> FoldBarResult:
    """Refuse when the verdict is absent, unreadable, not tier full, has failed > 0, or a red lacks first_red_commit."""
    if not sha:
        return _refuse(sha, "no sha given")
    path = Path(repo_root) / SUITE_VERDICT_PATH_TEMPLATE.format(sha=sha)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return _refuse(sha, f"no recorded full-suite verdict at {path.as_posix()}")
    except (OSError, ValueError) as exc:
        return _refuse(sha, f"unreadable suite verdict {path.as_posix()}: {exc}")
    if not isinstance(raw, dict):
        return _refuse(sha, f"suite verdict {path.as_posix()} is not an object")

    reds = tuple(r for r in (raw.get("reds") or ()) if isinstance(r, dict))
    try:
        sv = SuiteVerdict(
            sha=str(raw.get("sha") or sha),
            failed=int(raw.get("failed") or 0),
            passed=int(raw.get("passed") or 0),
            skipped=int(raw.get("skipped") or 0),
            grant_ref=raw.get("grant_ref"),
            reds=reds,
            tier=str(raw.get("tier") or ""),
        )
    except (TypeError, ValueError) as exc:
        return _refuse(sha, f"malformed suite verdict counts: {exc}")

    if sv.sha != sha:
        return _refuse(sha, f"verdict records sha {sv.sha}, not {sha}", verdict=sv)
    if sv.tier != SUITE_TIER_FULL:
        return _refuse(sha, f"verdict tier is {sv.tier!r}, not {SUITE_TIER_FULL!r}", verdict=sv)
    ids = tuple(str(r.get("nodeid", "")) for r in reds)
    unbisected = tuple(str(r.get("nodeid", "")) for r in reds if not r.get("first_red_commit"))
    if unbisected:
        return _refuse(sha, "red without first_red_commit: " + ", ".join(unbisected), unbisected, sv)
    if sv.failed > 0 or reds:
        return _refuse(sha, f"{sv.failed} failed: " + ", ".join(ids), ids, sv)
    if "passed" in raw and sv.passed <= 0:
        return _refuse(sha, "verdict records no passing test", verdict=sv)
    if not sv.grant_ref:
        return _refuse(sha, "verdict carries no grant_ref", verdict=sv)
    return FoldBarResult(True, sha, None, (), sv)
