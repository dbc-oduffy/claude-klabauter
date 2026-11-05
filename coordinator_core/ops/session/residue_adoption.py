"""Pure decisions for adopting a sole live EM's unclaimed dirt.

No git, no I/O. `session.safe_commit_offer` feeds in the unclaimed dirty
paths, the live-session set and the tracked paths under the candidate
directories; this module answers three questions: is the caller the repo's
only live session, which path class does each unclaimed path belong to, and
what commit groups does an adoptable class yield.
"""

from __future__ import annotations

from collections import OrderedDict
from typing import Dict, Iterable, List, Optional, Set, TypedDict

# A class key is a directory at least this many segments deep: `state/warp`
# qualifies, `state` and the repo root do not, so one tracked top-level file
# can never make everything adoptable.
MIN_CLASS_DEPTH = 2

ROOT_CLASS = "(repo root)"


class AdoptionClassification(TypedDict):
    adoptable: "OrderedDict[str, List[str]]"
    gitignore_candidates: List[str]


class AdoptionGroup(TypedDict):
    paths: List[str]
    message: str


def is_sole_live_em(session_id: str, live: Optional[Iterable[str]]) -> bool:
    """True only when the live set is exactly ``{session_id}``.

    ``None`` or an empty set is undetermined (a liveness error reads as an
    empty set), never "sole": adopting on it would commit a live peer's work.
    """
    if not live or not session_id:
        return False
    return set(live) == {session_id}


def candidate_ancestor_dirs(path: str) -> List[str]:
    """Ancestor directories of `path` that may be a class key, nearest first.

    Only directories at least ``MIN_CLASS_DEPTH`` segments deep are returned.
    """
    segments = path.split("/")[:-1]
    return [
        "/".join(segments[:depth])
        for depth in range(len(segments), MIN_CLASS_DEPTH - 1, -1)
    ]


def _candidate_class(path: str) -> str:
    """Label for a path with no tracked ancestor class: its top-two dirs."""
    segments = path.split("/")[:-1][:MIN_CLASS_DEPTH]
    return "/".join(segments) if segments else ROOT_CLASS


def _tracked_dir_prefixes(tracked_paths: Iterable[str]) -> Set[str]:
    prefixes: Set[str] = set()
    for tracked in tracked_paths:
        for ancestor in candidate_ancestor_dirs(tracked):
            prefixes.add(ancestor)
    return prefixes


def classify_unclaimed(
    unclaimed: Iterable[str], tracked_paths: Iterable[str]
) -> AdoptionClassification:
    """Split unclaimed dirty paths into adoptable classes and ignore candidates.

    A path's class is its nearest ancestor directory (at least
    ``MIN_CLASS_DEPTH`` segments deep) whose subtree holds a tracked file. The
    parent directory alone is not the key: a new run directory has no tracked
    file of its own, but its tracked-class ancestor does. A path with no such
    ancestor lands in ``gitignore_candidates`` (labelled by its top-two
    directories, deduplicated, in first-seen order).
    """
    tracked_dirs = _tracked_dir_prefixes(tracked_paths)
    adoptable: "OrderedDict[str, List[str]]" = OrderedDict()
    candidates: List[str] = []
    seen: Set[str] = set()
    for path in unclaimed:
        if path in seen:
            continue
        seen.add(path)
        for ancestor in candidate_ancestor_dirs(path):
            if ancestor in tracked_dirs:
                adoptable.setdefault(ancestor, []).append(path)
                break
        else:
            label = _candidate_class(path)
            if label not in candidates:
                candidates.append(label)
    return {"adoptable": adoptable, "gitignore_candidates": candidates}


def adoption_groups(
    adoptable: Dict[str, List[str]], session_id: str = ""
) -> List[AdoptionGroup]:
    """One ``{paths, message}`` commit group per adoptable class.

    The message asserts how the paths were bucketed, never why they were
    committed, matching `_default_groups`.
    """
    groups: List[AdoptionGroup] = []
    for key, paths in adoptable.items():
        if not paths:
            continue
        tag = " (session %s, %s)" % (session_id[:6], key) if session_id else " (%s)" % key
        subject = "auto-commit: %d file(s)%s" % (len(paths), tag)
        body = (
            "Mechanically grouped by tracked path class %r; this commit asserts "
            "nothing about why it happened, only how the paths below were "
            "bucketed.\n\n%s"
        ) % (key, "\n".join("  - %s" % p for p in paths))
        groups.append({"paths": list(paths), "message": "%s\n\n%s" % (subject, body)})
    return groups
