"""Write baseline: the bytes at each declared write path before any executor ran.

Invariant: the baseline is taken at stage, before any executor. A path whose bytes still equal
it was not written by this run, whatever HEAD says. Spawn-free; stdlib, ``kept_work`` and the
run's own record files only.
"""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional

from coordinator_core.ops.dispatch_emit.kept_work import blob_sha, fingerprint

NO_CHANGE_REFUSAL = "no-change"

#: Commit refusals that reproduce under an unchanged refire.
NON_REFIRE_REFUSALS = ("unreviewed", NO_CHANGE_REFUSAL)

#: Reply keys of the no-change refusal.
MEANT_TO_LAND_KEY = "meant_to_land"
DIRTY_BEFORE_RUN_KEY = "dirty_before_run"


def common_dir_for(root: Path) -> Optional[Path]:
    """The repo's object-store dir, or None when ``root`` holds no git dir. Spawn-free."""
    from coordinator_core.git.git_dir import resolve_git_common_dir

    try:
        common = resolve_git_common_dir(root)
    except (OSError, ValueError):
        return None
    return common if common.is_dir() else None


def persist_raw(common_dir: Optional[Path], data: bytes) -> None:
    """Write ``data`` as a loose blob so a later check can read the bytes behind a fingerprint.
    An OSError leaves the blob unpersisted; the fingerprint stays valid."""
    if common_dir is None:
        return
    from coordinator_core.git import git_objects

    try:
        git_objects.write_object(common_dir, b"blob", data)
    except OSError:
        pass


def capture(root: Path, paths: Iterable[str]) -> dict[str, Optional[str]]:
    """Map each repo-relative path to its raw-bytes fingerprint (None means absent), persisting
    each present file's raw blob in the object store."""
    common = common_dir_for(root)
    out: dict[str, Optional[str]] = {}
    for p in paths:
        try:
            data = (Path(root) / p).read_bytes()
        except OSError:
            out[p] = None
            continue
        out[p] = blob_sha(data)
        persist_raw(common, data)
    return out


def unwritten(root: Path, baseline: Mapping[str, Optional[str]], paths: Iterable[str]) -> list[str]:
    """Paths (declared order, deduped) whose current fingerprint equals the baseline's.
    A path missing from ``baseline`` is never unwritten: unknown is not unchanged."""
    out: list[str] = []
    seen: set[str] = set()
    for p in paths:
        if p in seen:
            continue
        seen.add(p)
        if p in baseline and fingerprint(root, p) == baseline[p]:
            out.append(p)
    return out


def pre_existing_dirt(
    root: Path,
    baseline: Mapping[str, Optional[str]],
    paths: Iterable[str],
    own_written: Iterable[str] = (),
) -> list[str]:
    """Claimed paths that are foreign dirt: present in the tree, unchanged since ``baseline``, and
    not in ``own_written`` (paths the run's earlier passes or checkpoint commits wrote). An absent
    path is never dirt; ``unwritten`` still counts it, for the no-change refusal."""
    own = set(own_written)
    return [p for p in unwritten(root, baseline, paths) if p not in own and fingerprint(root, p) is not None]


def prior_pass_written(root: Path, run_dir: Path) -> frozenset[str]:
    """Repo-relative paths the run's earlier passes recorded as written, read from its own record
    files: the review slice and test-runner edits in ``run-outcome.json`` and the keys of
    ``halted.json`` ``run_artifacts.withheld``. Declared footprints are not evidence of a write.
    Unreadable or absent records yield nothing.

    Trap: ``slice_files`` is the review's worktree diff against the run base, so it also lists
    pre-existing dirt the review froze; crediting it as written masks that dirt, and no code
    separates the two. Only foreign dirt outside ``slice_files`` is still refused
    (``test_foreign_dirt_beside_a_prior_pass_path_still_refuses``)."""
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir

    homes = [run_dir]
    try:
        if safe_id(run_dir.name):
            homes.insert(0, record_dir(root, run_dir.name))
    except ValueError:
        pass
    found: list = []
    for home in homes:
        for name in ("run-outcome.json", "halted.json"):
            try:
                data = json.loads((home / name).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict):
                continue
            review = data.get("inline_review")
            review = review if isinstance(review, dict) else {}
            prep = review.get("prep")
            tests = review.get("tests")
            arts = data.get("run_artifacts")
            withheld = arts.get("withheld") if isinstance(arts, dict) else None
            for listed in (
                prep.get("slice_files") if isinstance(prep, dict) else None,
                tests.get("edited") if isinstance(tests, dict) else None,
                tests.get("created") if isinstance(tests, dict) else None,
                list(withheld) if isinstance(withheld, dict) else None,
            ):
                if isinstance(listed, list):
                    found.extend(p for p in listed if isinstance(p, str) and p)
    out = set()
    for p in found:
        p = p.replace("\\", "/")
        parts = PurePosixPath(p).parts
        if not PurePosixPath(p).is_absolute() and ".." not in parts and not (len(p) > 1 and p[1] == ":"):
            out.add(p)
    return frozenset(out)
