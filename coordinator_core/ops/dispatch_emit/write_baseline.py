"""Write baseline: the bytes at each declared write path before any executor ran.

Invariant: the baseline is taken at stage, before any executor. A path whose bytes still equal
it was not written by this run, whatever HEAD says. Spawn-free; stdlib, ``kept_work``, ``git_state.head_blobs``
(in-process HEAD read) and run records under ``RECORD_DIR_ROOT``.
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
    path is never dirt; ``unwritten`` still counts it, for the no-change refusal. A path whose bytes
    equal HEAD's blob has no diff to land (another run committed it), so it is never dirt either;
    HEAD is read in-process (``head_blobs``), no git spawn."""
    own = set(own_written)
    cand = [p for p in unwritten(root, baseline, paths) if p not in own and fingerprint(root, p) is not None]
    if not cand:
        return cand
    from coordinator_core.git.git_state import head_blobs

    head = head_blobs(root, cand)
    return [p for p in cand if p not in head or head[p][1] != fingerprint(root, p)]


#: Record-dir file mapping each path a halt withheld to the blob id its bytes had at the halt.
WITHHELD_PRINTS_FILE = "withheld-prints.json"


def _norm_rel(p: object) -> Optional[str]:
    if not isinstance(p, str) or not p:
        return None
    cleaned = _clean_paths([p])
    return next(iter(cleaned), None)


def _read_prints(run_record: Path) -> dict[str, str]:
    try:
        data = json.loads((run_record / WITHHELD_PRINTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, str] = {}
    for k, v in data.items():
        key = _norm_rel(k)
        if key is not None and isinstance(v, str) and v:
            out[key] = v
    return out


def predecessor_withheld(root: Path, sizing_rel: Optional[str], run_dir: Path) -> frozenset[str]:
    """Keys of ``halted.json`` ``run_artifacts.withheld`` of every other run whose halt marker is
    bound to ``sizing_rel``: the work an earlier run of the same sizing left on disk. Spawn-free;
    a dir without a readable matching marker contributes nothing. ``run_dir`` itself is skipped.
    A path is credited only while its current blob equals the one the halt recorded in
    ``WITHHELD_PRINTS_FILE``; a missing record, a missing print or a mismatch credits nothing
    (a foreign edit after the halt stays dirt)."""
    if not sizing_rel:
        return frozenset()
    from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT
    from coordinator_core.ops.dispatch_emit.halted_withheld_index import withheld_by_path

    norm = PurePosixPath(sizing_rel.replace("\\", "/")).as_posix()
    out: set[str] = set()
    run_ids = sorted(set(withheld_by_path(Path(root)).values()) - {run_dir.name})
    for run_id in run_ids:
        try:
            data = json.loads((Path(root) / RECORD_DIR_ROOT / run_id / "halted.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        bound = data.get("sizing_rel") if isinstance(data, dict) else None
        if not isinstance(bound, str) or PurePosixPath(bound.replace("\\", "/")).as_posix() != norm:
            continue
        arts = data.get("run_artifacts")
        withheld = arts.get("withheld") if isinstance(arts, dict) else None
        if isinstance(withheld, dict):
            prints = _read_prints(Path(root) / RECORD_DIR_ROOT / run_id)
            out.update(p for p in _clean_paths(withheld) if prints.get(p) is not None and prints[p] == fingerprint(root, p))
    return frozenset(out)


def _clean_paths(paths: Iterable) -> set[str]:
    out = set()
    for p in paths:
        if not isinstance(p, str) or not p:
            continue
        pure = PurePosixPath(p.replace("\\", "/"))
        if not pure.is_absolute() and ".." not in pure.parts and not (len(p) > 1 and p[1] == ":") and pure.as_posix() != ".":
            out.add(pure.as_posix())
    return out


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
