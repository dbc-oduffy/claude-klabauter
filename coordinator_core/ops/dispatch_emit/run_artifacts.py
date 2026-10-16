"""Run artifacts: the zero-spawn, per-class set of every path an ask run authored.

The run's own record graph names what it wrote: ``state/warp/<run_id>/run.json`` names the sizing,
the sizing names its ``baton:`` and ``plan:``, the plan names its evidence and seam sidecars,
``parent.json`` names sibling slice sizings, and dirty record dirs binding the same sizing are
superseded predecessors. The landing sites (terminal commit, halt commit, reaper) stage only the
candidates this module returns, never a directory or the dirty tree.

No subprocess and no git here: every function reads files or filters caller-supplied status sets.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable, Mapping, Optional, Sequence

ARTIFACT_CLASSES = (
    "record-dir",
    "sizing",
    "baton",
    "handoff",
    "plan",
    "evidence-sidecar",
    "seam-sidecar",
    "delivery-note",
    "slice-sizing",
    "superseded-record",
    "docs-stage",
)
DOC_PATHS_PARAM = "doc_paths"

_RECORD_ROOT = "state/warp"
_RUN_FILE = "run.json"
_PARENT_FILE = "parent.json"
_STAMP_TAIL = re.compile(r"\d{8}T\d{6}-[0-9a-f]{6}$")
_SCALAR = r"^{key}:[ \t]*(?P<v>[^\r\n#]+?)[ \t]*(?:#.*)?$"


@dataclass(frozen=True)
class Landable:
    """``present`` files and ``deleted`` (tracked-at-HEAD, gone) paths to stage; ``withheld`` maps
    path to ``peer-held`` or ``not-claimed``; ``contest_unknown`` is set when peer contest could
    not be established (speculative: no caller reads it yet)."""

    present: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    withheld: Mapping[str, str] = field(default_factory=dict)
    contest_unknown: bool = False


@dataclass(frozen=True)
class RunArtifacts:
    """Every path one run authored: ``by_class`` maps class to sorted repo-relative posix file
    paths, ``record_dirs`` its own record dir plus superseded ones."""

    run_id: str
    by_class: Mapping[str, tuple[str, ...]]
    record_dirs: tuple[str, ...] = ()
    root: Optional[Path] = None

    def paths(self) -> tuple[str, ...]:
        """Every candidate path, de-duplicated and sorted."""
        return tuple(sorted({p for ps in self.by_class.values() for p in ps}))

    def class_of(self, path: str) -> Optional[str]:
        """The first class (in ``ARTIFACT_CLASSES`` order) holding ``path``, else ``None``."""
        norm = path.replace("\\", "/")
        for cls in ARTIFACT_CLASSES:
            if norm in self.by_class.get(cls, ()):
                return cls
        return None

    def with_superseded(self, run_ids: Sequence[str], record_root: str) -> "RunArtifacts":
        """A copy that also carries the top-level files of each superseded run's record dir."""
        by_class = {k: list(v) for k, v in self.by_class.items()}
        dirs = list(self.record_dirs)
        for rid in run_ids:
            if not _safe_run_id(rid):
                continue
            rel_dir = f"{record_root.rstrip('/')}/{rid}"
            if rel_dir in dirs:
                continue
            dirs.append(rel_dir)
            if self.root is not None:
                by_class.setdefault("superseded-record", []).extend(_top_level_files(self.root, rel_dir))
        return RunArtifacts(
            run_id=self.run_id,
            by_class={k: tuple(sorted(set(v))) for k, v in by_class.items()},
            record_dirs=tuple(dirs),
            root=self.root,
        )


def _safe_run_id(run_id: object) -> bool:
    return isinstance(run_id, str) and bool(run_id) and "/" not in run_id and "\\" not in run_id and ".." not in run_id


def _norm_rel(raw: object) -> Optional[str]:
    """A clean repo-relative posix path, else ``None`` (absolute, drive-lettered, or ``..``)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip().replace("\\", "/")
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        return None
    parts = PurePosixPath(text).parts
    if not parts or ".." in parts:
        return None
    return PurePosixPath(*parts).as_posix()


def _contained(root: Path, rel: str) -> bool:
    from coordinator_core.ops._path_guard import contained_path

    return contained_path(root / rel, [root]) is not None


def _read_text(root: Path, rel: Optional[str]) -> Optional[str]:
    if not rel:
        return None
    try:
        return (root / rel).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None


def _read_json(root: Path, rel: str) -> Optional[dict]:
    text = _read_text(root, rel)
    if text is None:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def _top_level_files(root: Path, dir_rel: str) -> list[str]:
    out: list[str] = []
    try:
        with os.scandir(root / dir_rel) as it:
            for entry in it:
                if entry.is_file():
                    out.append(f"{dir_rel}/{entry.name}")
    except OSError:
        return []
    return sorted(out)


def _glob_files(root: Path, dir_rel: str, pattern: str) -> list[str]:
    try:
        return sorted(f"{dir_rel}/{p.name}" for p in (root / dir_rel).glob(pattern) if p.is_file())
    except OSError:
        return []


def _scalar(text: Optional[str], key: str) -> Optional[str]:
    if not text:
        return None
    match = re.search(_SCALAR.format(key=re.escape(key)), text, re.MULTILINE)
    if not match:
        return None
    value = match.group("v").strip().strip("'\"")
    return value or None


def candidate_paths(
    worktree_root: Path,
    run_id: str,
    *,
    sizing_rel: Optional[str] = None,
    plan_rel: Optional[str] = None,
    doc_paths: Sequence[str] = (),
) -> RunArtifacts:
    """Read ``run_id``'s record graph into a per-class candidate set. Zero spawns; a missing or
    unreadable file contributes nothing and never raises."""
    root = Path(worktree_root)
    classes: dict[str, list[str]] = {}
    seen: set[str] = set()

    def add(cls: str, rel: Optional[str], *, must_exist: bool = True) -> None:
        norm = _norm_rel(rel)
        if norm is None or norm in seen or not _contained(root, norm):
            return
        if must_exist and not (root / norm).is_file():
            return
        seen.add(norm)
        classes.setdefault(cls, []).append(norm)

    if not _safe_run_id(run_id):
        return RunArtifacts(run_id=str(run_id), by_class={}, record_dirs=(), root=root)

    record_dir = f"{_RECORD_ROOT}/{run_id}"
    for rel in _top_level_files(root, record_dir):
        add("record-dir", rel)

    run_json = _read_json(root, f"{record_dir}/{_RUN_FILE}") or {}
    sizing = _norm_rel(sizing_rel) or _norm_rel(run_json.get("sizing_rel"))
    add("sizing", sizing)
    sizing_text = _read_text(root, sizing)
    add("baton", _scalar(sizing_text, "baton"))
    plan = _norm_rel(plan_rel) or _norm_rel(_scalar(sizing_text, "plan"))
    if plan:
        add("plan", plan)
        stem = PurePosixPath(plan)
        if stem.suffix:
            base = stem.with_suffix("").as_posix()
            add("evidence-sidecar", f"{base}.evidence.yaml")
            add("seam-sidecar", f"{base}.seam.yaml")
            add("handoff", f"state/handoffs/{stem.stem}.md")
    add("delivery-note", f"docs/delivery/{run_id}.md")

    if sizing:
        sizing_path = PurePosixPath(sizing)
        for rel in _glob_files(root, sizing_path.parent.as_posix(), f"{sizing_path.stem}-slice-*{sizing_path.suffix}"):
            add("slice-sizing", rel)

    parent = _read_json(root, f"{record_dir}/{_PARENT_FILE}") or {}
    runs = parent.get("runs")
    for item in runs if isinstance(runs, list) else ():
        if isinstance(item, dict) and item.get("slice_sizing"):
            add("slice-sizing", item.get("slice_sizing"))

    for raw in doc_paths or ():
        norm = _norm_rel(raw)
        if norm is None or norm.startswith("scratch/") or norm == "scratch":
            continue
        if norm == _RECORD_ROOT or norm.startswith(_RECORD_ROOT + "/"):
            continue
        add("docs-stage", norm)

    return RunArtifacts(
        run_id=run_id,
        by_class={k: tuple(sorted(v)) for k, v in classes.items()},
        record_dirs=(record_dir,),
        root=root,
    )


def superseded_runs(
    worktree_root: Path,
    run_id: str,
    sizing_rel: Optional[str],
    changed: Iterable[str],
    live_run_ids: Iterable[str] = (),
) -> tuple[str, ...]:
    """Dirty record dirs other than ``run_id`` that bind ``sizing_rel`` and carry an earlier
    stamp. Reads ``run.json`` only for dirs ``changed`` already shows."""
    sizing = _norm_rel(sizing_rel)
    mine = _STAMP_TAIL.search(run_id or "")
    if sizing is None or mine is None:
        return ()
    live = set(live_run_ids)
    prefix = _RECORD_ROOT + "/"
    names: set[str] = set()
    for raw in changed:
        rel = str(raw).replace("\\", "/")
        if not rel.startswith(prefix):
            continue
        name, _, rest = rel[len(prefix):].partition("/")
        if name and rest and "/" not in rest and name != run_id:
            names.add(name)
    root = Path(worktree_root)
    out: list[str] = []
    for name in sorted(names):
        if name in live:
            continue
        tail = _STAMP_TAIL.search(name)
        if tail is None or tail.group(0) >= mine.group(0):
            continue
        data = _read_json(root, f"{_RECORD_ROOT}/{name}/{_RUN_FILE}") or {}
        if _norm_rel(data.get("sizing_rel")) == sizing:
            out.append(name)
    return tuple(out)


def landable(
    worktree_root: Path,
    artifacts: RunArtifacts,
    changed: Iterable[str],
    ignored: Iterable[str],
    *,
    session_id: Optional[str],
) -> Landable:
    """Split the changed, un-ignored candidates into present files and tracked deletions, and
    withhold peer-held paths and docs-stage paths this session does not claim."""
    root = Path(worktree_root)
    changed_set = {str(p).replace("\\", "/") for p in changed}
    ignored_set = {str(p).replace("\\", "/") for p in ignored}
    cands = [p for p in artifacts.paths() if p in changed_set and p not in ignored_set]
    withheld: dict[str, str] = {}
    contest_unknown = False

    if session_id and cands:
        from coordinator_core.session import scope

        contested = scope.contested_by_live_peers(list(cands), session_id, cwd=str(root))
        if contested is None:
            contest_unknown = True
        else:
            for path in cands:
                if contested.get(path):
                    withheld[path] = "peer-held"

    docs = [p for p in cands if artifacts.class_of(p) == "docs-stage" and p not in withheld]
    if docs:
        claimed: set[str] = set()
        if session_id:
            from coordinator_core.session import scope

            claimed = {str(c).replace("\\", "/") for c in scope.own_path_claims_in_process(session_id, str(root))}
        for path in docs:
            if path not in claimed:
                withheld[path] = "not-claimed"

    kept = [p for p in cands if p not in withheld]
    present = [p for p in kept if (root / p).is_file()]
    gone = [p for p in kept if p not in present]
    deleted: list[str] = []
    if gone:
        from coordinator_core.git.commit import partition_declared_deletions

        split = partition_declared_deletions(root, gone)
        if split is not None:
            deleted = list(split[0])
    return Landable(
        present=tuple(present),
        deleted=tuple(deleted),
        withheld=withheld,
        contest_unknown=contest_unknown,
    )


def residue(
    artifacts: RunArtifacts,
    still_changed: Iterable[str],
    landed: Iterable[str],
    withheld: Mapping[str, str],
) -> dict[str, str]:
    """Candidates still changed after the caller's commits that were neither landed nor withheld,
    as ``path -> class``."""
    cands = set(artifacts.paths())
    done = {str(p).replace("\\", "/") for p in landed} | set(withheld)
    out: dict[str, str] = {}
    for raw in still_changed:
        path = str(raw).replace("\\", "/")
        if path in cands and path not in done:
            out[path] = artifacts.class_of(path) or ""
    return dict(sorted(out.items()))
