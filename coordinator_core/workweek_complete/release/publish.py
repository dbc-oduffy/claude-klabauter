"""publish-evidence rule and the params for the publish op step.

Starts no `gh` child and cuts no tag; `release_tagging.py` owns both.
"""

from __future__ import annotations

import hashlib
from typing import Dict, Tuple

from coordinator_core.git.run import run_git
from coordinator_core.workweek_complete.release.contract import RuleResult

RULE = "publish-evidence"


def evaluate(
    repo_root: str, *, merge_sha: str, tag: str, notes_path: str, notes_sha256: str
) -> RuleResult:
    """Pass when the merge is on origin/main, the tag is absent or already on it, and the notes are unchanged."""
    at = ["-C", str(repo_root)]
    ancestor = run_git([*at, "merge-base", "--is-ancestor", merge_sha, "origin/main"])
    peeled = run_git([*at, "rev-parse", "--verify", "-q", f"refs/tags/{tag}^{{commit}}"])
    tag_existed = peeled.returncode == 0
    tag_sha = peeled.stdout.strip() if tag_existed else ""
    # binary: the hash is over the blob's own bytes, which text mode would newline-translate.
    shown = run_git([*at, "show", f"{merge_sha}:{notes_path}"], binary=True)
    notes_hash = hashlib.sha256(shown.stdout_bytes).hexdigest() if shown.returncode == 0 else ""
    evidence: Dict[str, object] = {
        "merge_sha": merge_sha,
        "tag": tag,
        "tag_existed": tag_existed,
        "notes_sha256": notes_hash,
    }
    if ancestor.returncode != 0:
        reason = "merge-not-on-main"
    elif tag_existed and tag_sha != merge_sha:
        reason = "tag-on-other-commit"
    elif shown.returncode != 0 or notes_hash != notes_sha256:
        reason = "notes-changed"
    else:
        return RuleResult(RULE, True, evidence=evidence)
    return RuleResult(RULE, False, reason, evidence)


def publish_params(
    release_mode: str, *, repo_root: str, merge_sha: str, tag: str, notes_text: str
) -> Tuple[str, dict]:
    """Op name and params for the publish step; `tag_prefix` carries the fully resolved tag."""
    params = {"repo_root": str(repo_root), "merge_sha": merge_sha, "tag_prefix": tag}
    if release_mode == "gh-release":
        return "release.cut_tag_and_publish", {**params, "release_notes": notes_text}
    if release_mode == "git-tag":
        return "release.cut_tag", params
    raise ValueError(f"no publish step for release mode {release_mode!r}")
