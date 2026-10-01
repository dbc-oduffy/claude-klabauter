"""Authoring-time leak gate: a composer that runs zero-spawn detectors over a commit's paths.

`leak_gate(worktree_root, gate_paths)` has the `(worktree_root, gate_paths) -> GateOutcome` shape the
`commit_v2` gate loop takes. Detectors are modules (or objects) exposing:

  * `candidates(gate_paths) -> Iterable[str]` -- every HEAD path the detector may read;
  * `detect(root, gate_paths, head) -> list[LeakFinding]`;
  * optionally `NAME` / `name` -- the label used in diagnostics.

`HeadView` reads the HEAD tree spine once over the union of all candidates, so N detectors cost one
`read_tree_spine`. Every key is a repo-relative forward-slash path. Nothing here spawns a process.

Invariant: a detector that fails to import or raises refuses the commit, naming the detector; a gate
that cannot evaluate its predicate must not pass. There is no override.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Set, Tuple, Union

from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.git.git_objects import read_object
from coordinator_core.git.git_state import read_tree_spine
from coordinator_core.ops.ceremony.commit_gates import GateOutcome

#: Default detector module names under this package, in run order.
WIRED_DETECTORS: Tuple[str, ...] = ("import_closure", "payload_locality", "foreign_identity")

#: Detectors that read the publisher's private scrub tables; see `_resolve_default_detectors`.
PUBLISHER_ONLY_DETECTORS: frozenset = frozenset({"payload_locality", "foreign_identity"})

#: Findings rendered per refusal before the "(+N more)" line.
MAX_RENDERED_FINDINGS = 5


@dataclass(frozen=True)
class LeakFinding:
    """One refusal-worthy fact: `path` (repo-relative, forward slash), 1-based `line`, `kind`, `detail`."""

    path: str
    line: int
    kind: str
    detail: str

    def render(self) -> str:
        return f"{self.path}:{self.line}: {self.kind} -- {self.detail}"


def normalize_path(path: str) -> str:
    """Repo-relative forward-slash form: backslashes become `/`, leading `./` is dropped."""
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p


def added_lines(old: Optional[str], new: str) -> List[Tuple[int, str]]:
    """`(1-based line no, text)` of the lines in `new` whose multiset count exceeds `old`'s.

    A moved line is not added; a duplicated line is (its later occurrence). `old=None` means every
    line is added.
    """
    remaining: Counter = Counter(old.splitlines()) if old else Counter()
    added: List[Tuple[int, str]] = []
    for lineno, text in enumerate(new.splitlines(), start=1):
        if remaining[text] > 0:
            remaining[text] -= 1
        else:
            added.append((lineno, text))
    return added


def _git_blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


class HeadView:
    """Read-only, zero-spawn view of HEAD over a fixed candidate path set.

    One `read_tree_spine` at construction (skipped when `candidates` is empty). An unresolvable HEAD
    leaves the view empty: every path reads as absent at HEAD. Blob reads are lazy and cached.
    """

    def __init__(self, worktree_root: Union[str, Path], candidates: Iterable[str]) -> None:
        self.root = Path(worktree_root)
        self._paths = sorted({normalize_path(p) for p in candidates})
        self._spine: Dict[str, Dict[str, Tuple[int, str]]] = {}
        self._common_dir: Optional[Path] = None
        self._blobs: Dict[str, Optional[bytes]] = {}
        if self._paths:
            spine = read_tree_spine(self.root, self._paths)
            self._spine = spine or {}

    @property
    def candidate_paths(self) -> Tuple[str, ...]:
        return tuple(self._paths)

    def entry(self, path: str) -> Optional[Tuple[int, str]]:
        """`(mode, sha)` of `path` at HEAD, or `None` when absent (or not among the candidates)."""
        head_dir, _, name = normalize_path(path).rpartition("/")
        return self._spine.get(head_dir, {}).get(name)

    def dir_entries(self, directory: str) -> Dict[str, Tuple[int, str]]:
        """Entries of a tree directory that lies on a candidate's spine (`""` is the root)."""
        return dict(self._spine.get(normalize_path(directory).strip("/"), {}))

    def in_head(self, path: str) -> bool:
        return self.entry(path) is not None

    def head_bytes(self, path: str) -> Optional[bytes]:
        """HEAD blob bytes of `path`, or `None` when absent or unreadable."""
        key = normalize_path(path)
        if key in self._blobs:
            return self._blobs[key]
        data: Optional[bytes] = None
        entry = self.entry(key)
        if entry is not None:
            if self._common_dir is None:
                self._common_dir = resolve_git_common_dir(self.root)
            result = read_object(self._common_dir, entry[1])
            if result is not None and result[0] == "blob":
                data = result[1]
        self._blobs[key] = data
        return data

    def head_text(self, path: str) -> Optional[str]:
        """Decoded HEAD text of `path`, or `None` when absent, unreadable or not UTF-8."""
        data = self.head_bytes(path)
        if data is None:
            return None
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError:
            return None

    def worktree_bytes(self, path: str) -> Optional[bytes]:
        try:
            return (self.root / normalize_path(path)).read_bytes()
        except OSError:
            return None

    def worktree_differs(self, path: str) -> bool:
        """True iff the worktree bytes of `path` are not the HEAD blob.

        Hashes the worktree bytes as a git blob and compares with the spine entry. Present on one
        side only differs; absent on both does not.
        """
        entry = self.entry(path)
        data = self.worktree_bytes(path)
        if entry is None:
            return data is not None
        if data is None:
            return True
        return _git_blob_sha(data) != entry[1]


class Detector(Protocol):
    """A leak detector; see the module docstring for the attribute contract."""

    def candidates(self, gate_paths: Sequence[str]) -> Iterable[str]: ...

    def detect(
        self, worktree_root: Path, gate_paths: Sequence[str], head: HeadView
    ) -> List[LeakFinding]: ...


def _detector_name(detector: Any) -> str:
    for attr in ("NAME", "name"):
        value = getattr(detector, attr, None)
        if isinstance(value, str) and value:
            return value
    mod = getattr(detector, "__name__", None)
    if isinstance(mod, str):
        return mod.rsplit(".", 1)[-1]
    return type(detector).__name__


def _resolve_default_detectors(names: Sequence[str]) -> Tuple[List[Any], List[str]]:
    """Import each named detector module; return `(detectors, refusal diagnostics)`.

    Trap: `PUBLISHER_ONLY_DETECTORS` guard text against the publish scrub and read its private
    tables, so they never ship. A distribution without the publisher (`percolate`) has no scrub for
    them to guard and does not wire them; with the publisher present, their absence still refuses.
    """
    detectors: List[Any] = []
    failures: List[str] = []
    publisher_present = importlib.util.find_spec("coordinator_core.percolate") is not None
    for name in names:
        if name in PUBLISHER_ONLY_DETECTORS and not publisher_present:
            continue
        try:
            detectors.append(importlib.import_module(f"{__name__}.{name}"))
        except ImportError as exc:
            failures.append(f"leak_gate: detector {name} failed to import ({exc})")
    return detectors, failures


def _refusal(diagnostics: List[str]) -> GateOutcome:
    return GateOutcome(passed=False, skipped=False, diagnostics=diagnostics)


def leak_gate(
    worktree_root: Union[str, Path],
    gate_paths: Sequence[str],
    detectors: Optional[Sequence[Any]] = None,
) -> GateOutcome:
    """Refuse a commit whose `gate_paths` trip any detector.

    `detectors=None` resolves `WIRED_DETECTORS` by fixed module name under this package; an
    ImportError there refuses. An empty `gate_paths` or an empty detector list passes (skipped).
    A detector that raises refuses, naming it. Diagnostics are `leak_gate: <path>:<line>: <kind> --
    <detail>`, capped at `MAX_RENDERED_FINDINGS`, then one alternative line.
    """
    root = Path(worktree_root)
    paths = [normalize_path(p) for p in gate_paths]
    if not paths:
        return GateOutcome(passed=True, skipped=True, diagnostics=[])

    if detectors is None:
        active, import_failures = _resolve_default_detectors(WIRED_DETECTORS)
        if import_failures:
            return _refusal(import_failures)
    else:
        active = list(detectors)
    if not active:
        return GateOutcome(passed=True, skipped=True, diagnostics=[])

    union: Set[str] = set()
    for det in active:
        try:
            union.update(normalize_path(p) for p in det.candidates(paths))
        except Exception as exc:
            return _refusal([f"leak_gate: detector {_detector_name(det)} raised {type(exc).__name__}: {exc}"])

    try:
        head = HeadView(root, union)
    except Exception as exc:
        return _refusal([f"leak_gate: HEAD read raised {type(exc).__name__}: {exc}"])

    findings: List[LeakFinding] = []
    for det in active:
        try:
            findings.extend(det.detect(root, paths, head))
        except Exception as exc:
            return _refusal([f"leak_gate: detector {_detector_name(det)} raised {type(exc).__name__}: {exc}"])

    if not findings:
        return GateOutcome(passed=True, skipped=False, diagnostics=[])

    lines = [f"leak_gate: {f.render()}" for f in findings[:MAX_RENDERED_FINDINGS]]
    extra = len(findings) - MAX_RENDERED_FINDINGS
    if extra > 0:
        lines.append(f"leak_gate: (+{extra} more)")
    lines.append("Fix the listed items, or add the missing definer to this commit.")
    return _refusal(lines)
