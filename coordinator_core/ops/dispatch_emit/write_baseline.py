"""Write baseline: the bytes at each declared write path before any executor ran.

Invariant: the baseline is taken at stage, before any executor. A path whose bytes still equal
it was not written by this run, whatever HEAD says. Spawn-free; stdlib, ``kept_work``, ``git_state.head_blobs``
(in-process HEAD read) and run records under ``RECORD_DIR_ROOT``.
"""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional

from coordinator_core.ops.dispatch_emit.kept_work import blob_sha, checkin_form, fingerprint, kept_fingerprints

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


def without_carried(
    root: Path, run_dir: Path, baseline: Optional[Mapping[str, Optional[str]]]
) -> Optional[dict[str, Optional[str]]]:
    """``baseline`` minus each path the run's own earlier pass or ``resumed_from`` predecessor wrote
    (``run-outcome.json`` slice of the run's record, each predecessor's record, ``predecessor_wrote``,
    and a path still equal to a blob the record's own earlier ``kept.json`` fingerprinted).
    A refire stages its baseline over those carried bytes, so a row delivered earlier reads
    ``unwritten`` there; dropping the key makes the path unknown, which ``unwritten`` never counts.
    Trap: the run's own ``halted.json`` withheld map is read nowhere here; the halt being recorded
    stamps it first, and crediting it would credit every withheld path. None and empty baselines
    pass through unchanged."""
    if not baseline:
        return baseline if baseline is None else dict(baseline)
    from coordinator_core.ops._path_guard import safe_id

    carried: set = set(predecessor_wrote(root, run_dir, baseline))
    carried.update(_outcome_slice(run_dir))
    if safe_id(run_dir.name):
        from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
        from coordinator_core.ops.dispatch_emit.landing_integrity import _resumed_chain

        for prev in _resumed_chain(root, run_dir.name):
            try:
                carried.update(_recorded_paths(record_dir(root, prev)))
            except ValueError:
                continue
    for rel, blobs in kept_fingerprints(run_dir).items():
        if fingerprint(root, rel) in blobs:
            carried.add(rel)
    credited = _clean_paths(carried)
    return {p: v for p, v in baseline.items() if p not in credited}


def _outcome_slice(home: Path) -> list:
    """Review slice and test-runner edits listed in ``home/run-outcome.json`` (not its withheld map)."""
    try:
        data = json.loads((home / "run-outcome.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    review = data.get("inline_review") if isinstance(data, dict) else None
    review = review if isinstance(review, dict) else {}
    prep, tests = review.get("prep"), review.get("tests")
    out: list = []
    for listed in (
        prep.get("slice_files") if isinstance(prep, dict) else None,
        tests.get("edited") if isinstance(tests, dict) else None,
        tests.get("created") if isinstance(tests, dict) else None,
    ):
        if isinstance(listed, list):
            out.extend(p for p in listed if isinstance(p, str) and p)
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
    equal HEAD's blob, raw or in checkin form (a CRLF worktree file under ``core.autocrlf``), has no
    diff to land (another run committed it), so it is never dirt either; HEAD is read in-process
    (``head_blobs``), no git spawn."""
    own = set(own_written)
    cand = [p for p in unwritten(root, baseline, paths) if p not in own and fingerprint(root, p) is not None]
    if not cand:
        return cand
    from coordinator_core.git.git_state import head_blobs

    head = head_blobs(root, cand)
    return [p for p in cand if p not in head or not _equals_head(root, p, head[p][1])]


def _equals_head(root: Path, rel: str, head_sha: str) -> bool:
    """True when ``rel``'s raw bytes, or their checkin form, hash to ``head_sha``."""
    try:
        data = (Path(root) / rel).read_bytes()
    except OSError:
        return False
    if blob_sha(data) == head_sha:
        return True
    form = checkin_form(root, rel, data)
    return form is not None and blob_sha(form) == head_sha


#: Record-dir file mapping each path a halt withheld to the blob id its bytes had at the halt.
WITHHELD_PRINTS_FILE = "withheld-prints.json"


def predecessor_withheld(root: Path, sizing_rel: Optional[str], run_dir: Path) -> frozenset[str]:
    """The paths an earlier run of ``sizing_rel`` left withheld on disk, answered from the per-run
    index alone (no ``halted.json`` is opened). ``run_dir`` itself is skipped.
    Every indexed same-sizing run is walked, not only the last holder of each path, so a later
    run that changed a path an earlier run withheld still chains. A path is credited only while its
    current blob equals the print the newest same-sizing run that withheld it recorded (newest by
    index ``at_ns``, run id as tiebreak); a missing print credits nothing, and a mismatch credits only
    while the live blob still holds every line the run added over HEAD (``_carries_print``), so a
    peer's hunk beside it keeps the credit and a foreign rewrite stays dirt."""
    if not sizing_rel:
        return frozenset()
    from coordinator_core.ops.dispatch_emit.halted_withheld_index import entries

    norm = PurePosixPath(sizing_rel.replace("\\", "/")).as_posix()
    newest: dict[str, tuple[tuple[int, str], Optional[str]]] = {}
    for run_id, entry in entries(Path(root)).items():
        if run_id == run_dir.name or not entry.paths or entry.sizing_rel is None:
            continue
        if PurePosixPath(entry.sizing_rel.replace("\\", "/")).as_posix() != norm:
            continue
        stamp = (entry.at_ns, run_id)
        for p in _clean_paths(entry.paths):
            if p not in newest or stamp > newest[p][0]:
                newest[p] = (stamp, entry.prints.get(p))
    return frozenset(
        p for p, (_, print_) in newest.items() if print_ is not None and _carries_print(root, p, print_, fingerprint(root, p))
    )


#: Largest recorded print blob read back to test for its hunks in a changed live file.
_PRINT_READ_CAP = 1 << 20


def _carries_print(root: Path, rel: str, print_: str, live: Optional[str]) -> bool:
    """True when ``rel``'s live blob is the halt's ``print_``, or a later edit still holding every line
    the halted run added over HEAD (a peer's hunk landed beside the withheld one). The print blob must be
    persisted in the object store and add at least one non-blank line: an unreadable print, a pure deletion
    or a foreign rewrite that dropped the run's lines credits nothing. In-process; reads at most the print,
    HEAD and live bytes of a mismatching path."""
    if live is None:
        return False
    if print_ == live:
        return True
    from collections import Counter

    from coordinator_core.git import git_objects
    from coordinator_core.git.git_state import head_blobs

    common = common_dir_for(root)
    if common is None:
        return False
    try:
        held = git_objects.read_object(common, print_)
        head = head_blobs(Path(root), [rel]).get(rel)
        base = git_objects.read_object(common, head[1]) if head else None
        data = (Path(root) / rel).read_bytes()
    except (OSError, ValueError):
        return False
    if held is None or held[0] != "blob" or len(held[1]) > _PRINT_READ_CAP or len(data) > _PRINT_READ_CAP:
        return False
    if head and base is None:
        return False

    def _lines(raw: bytes) -> Counter:
        return Counter(s.strip() for s in raw.splitlines() if s.strip())

    added = _lines(held[1]) - (_lines(base[1]) if base else Counter())
    return bool(added) and not (added - _lines(data))


def predecessor_wrote(root: Path, run_dir: Path, baseline: Mapping[str, Optional[str]]) -> frozenset[str]:
    """Paths a ``resumed_from`` predecessor of ``run_dir`` wrote, read from its stage manifest alone:
    a path the predecessor baselined whose fingerprint at this run's stage (``baseline``) differs
    from the predecessor's own. Covers a predecessor that halted at commit with no withheld record
    and an empty review slice. A path the predecessor never baselined, or left as it found it, is
    not credited. Trap: a foreign edit between the predecessor's stage and this run's stage on a
    path the predecessor baselined is indistinguishable from the predecessor's write."""
    from coordinator_core.ops._path_guard import safe_id
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.landing_integrity import _resumed_chain

    if not baseline or not safe_id(run_dir.name):
        return frozenset()
    out: set[str] = set()
    for prev in _resumed_chain(root, run_dir.name):
        try:
            data = json.loads((record_dir(root, prev) / "manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        before = data.get("write_baseline") if isinstance(data, dict) else None
        if not isinstance(before, dict):
            continue
        for p in _clean_paths(before):
            if p in baseline and p in before and baseline[p] != before[p]:
                out.add(p)
    return frozenset(out)


def exclude_halted_withheld(
    root: Path, captured: Mapping[str, Optional[str]], sizing_rel: Optional[str], run_name: str
) -> dict[str, Optional[str]]:
    """``captured`` (live-tree fingerprints taken at stage) minus any halted run's withheld hunk.

    A path whose live blob equals a withheld print of another halted run is that run's work, never
    this run's base. Same sizing: the baseline becomes HEAD's blob (None when HEAD lacks the path),
    so the hunk reads as this run's own write. Different sizing: the path is omitted, and an
    unbaselined path is neither unwritten nor dirt. A same-sizing print the live blob no longer equals
    still matches while the live blob holds every line the halted run added (``_carries_print``);
    otherwise the capture is left alone. Spawn-free."""
    from coordinator_core.ops.dispatch_emit.halted_withheld_index import entries

    norm = PurePosixPath(sizing_rel.replace("\\", "/")).as_posix() if sizing_rel else None
    same: set[str] = set()
    foreign: set[str] = set()
    for run_id, entry in entries(Path(root)).items():
        if run_id == run_name or not entry.paths:
            continue
        bound = entry.sizing_rel
        is_same = norm is not None and bound is not None and PurePosixPath(bound.replace("\\", "/")).as_posix() == norm
        for p in _clean_paths(entry.paths):
            if p not in captured or captured[p] is None:
                continue
            want = entry.prints.get(p)
            if want == captured[p] or (is_same and want is not None and _carries_print(root, p, want, captured[p])):
                (same if is_same else foreign).add(p)
    foreign -= same
    if not same and not foreign:
        return dict(captured)
    head: Mapping = {}
    if same:
        from coordinator_core.git.git_state import head_blobs

        head = head_blobs(Path(root), sorted(same))
    out: dict[str, Optional[str]] = {}
    for p, fp in captured.items():
        if p in foreign:
            continue
        out[p] = (head[p][1] if p in head else None) if p in same else fp
    return out


def _clean_paths(paths: Iterable) -> set[str]:
    out = set()
    for p in paths:
        if not isinstance(p, str) or not p:
            continue
        pure = PurePosixPath(p.replace("\\", "/"))
        if not pure.is_absolute() and ".." not in pure.parts and not (len(p) > 1 and p[1] == ":") and pure.as_posix() != ".":
            out.add(pure.as_posix())
    return out


def _resumed_withheld(root: Path, run_id: str) -> list:
    """Keys of ``halted.json`` ``run_artifacts.withheld`` for each ``resumed_from`` predecessor of
    ``run_id`` (the ledger chain, hop-capped). Only withheld keys are credited from predecessors."""
    from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
    from coordinator_core.ops.dispatch_emit.landing_integrity import _resumed_chain

    out: list = []
    for prev in _resumed_chain(root, run_id):
        try:
            data = json.loads((record_dir(root, prev) / "halted.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        arts = data.get("run_artifacts") if isinstance(data, dict) else None
        withheld = arts.get("withheld") if isinstance(arts, dict) else None
        if isinstance(withheld, dict):
            out.extend(p for p in withheld if isinstance(p, str) and p)
    return out


def _recorded_paths(home: Path) -> list:
    """Paths one record home lists as written: slice, test edits/creations and withheld keys."""
    found: list = []
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
    return found


_PORCELAIN_PREFIX = re.compile(r"^(?:[MADRCU?!]{1,2})\s+")


def prior_attempt_reported(
    root: Path, plan_rel: Optional[str], rows: Iterable[tuple[str, Iterable[str]]]
) -> frozenset[str]:
    """Declared write paths an earlier attempt's executor reported editing: each ``(row id, writes)``
    row's ``<row id>.prior.md`` dispatch report (a refire sets the earlier report aside under that
    name) names the path on a line of its own. A refire stages its baseline over the earlier attempt's
    bytes, so such a path reads unchanged-since-stage; the report is the evidence it was the run's own
    edit. Only a row's own declared paths are credited, and a path the report never names stays
    foreign dirt. A fresh run has no ``.prior.md``, so nothing is credited. Spawn-free: bounded file reads."""
    if not plan_rel:
        return frozenset()
    from coordinator_core.ops.dispatch_emit.emit import _dispatch_report_path

    out: set[str] = set()
    for row_id, writes in rows:
        declared = {p for p in writes if isinstance(p, str)}
        try:
            live = _dispatch_report_path(plan_rel, str(row_id))
            text = (Path(root) / (live[: -len(".md")] + ".prior.md")).read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        named = {
            _PORCELAIN_PREFIX.sub("", line.strip().lstrip("-*").strip()).strip("`").replace("\\", "/")
            for line in text.splitlines()
        }
        out.update(declared & named)
    return frozenset(out)


def prior_pass_written(root: Path, run_dir: Path, include_predecessors: bool = True) -> frozenset[str]:
    """Repo-relative paths the run's earlier passes recorded as written, read from its own record
    files: the review slice and test-runner edits in ``run-outcome.json`` and the keys of
    ``halted.json`` ``run_artifacts.withheld``. With ``include_predecessors`` the same fields of
    each ``resumed_from`` predecessor's record are credited too; without it only predecessors'
    withheld keys are. Declared footprints are not evidence of a write.
    Unreadable or absent records yield nothing.

    Trap: ``slice_files`` is the review's worktree diff against the run base, so it also lists
    pre-existing dirt the review froze; crediting it as written masks that dirt, and no code
    separates the two. Only foreign dirt outside ``slice_files`` is still refused
    (``test_gate_still_refuses_foreign_dirt_beside_predecessor_write``)."""
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
        found.extend(_recorded_paths(home))
    if safe_id(run_dir.name):
        found.extend(_resumed_withheld(root, run_dir.name))
        if include_predecessors:
            from coordinator_core.ops.dispatch_emit.landing_integrity import _resumed_chain

            for prev in _resumed_chain(root, run_dir.name):
                try:
                    found.extend(_recorded_paths(record_dir(root, prev)))
                except ValueError:
                    continue
    out = set()
    for p in found:
        p = p.replace("\\", "/")
        parts = PurePosixPath(p).parts
        if not PurePosixPath(p).is_absolute() and ".." not in parts and not (len(p) > 1 and p[1] == ":"):
            out.add(p)
    return frozenset(out)
