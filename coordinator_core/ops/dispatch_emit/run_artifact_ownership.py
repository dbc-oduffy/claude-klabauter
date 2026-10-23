"""Which paths a warp run owns as run-artifacts.

Pure in-process file reads, zero spawns; every path is repo-relative and forward-slash.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.session.record_homes import home_dir


def _warp_home() -> str:
    return str(PurePosixPath(home_dir("", "warp").replace(os.sep, "/")))


@dataclass(frozen=True)
class OwnArtifacts:
    """A run's own run-artifact paths; the empty value owns nothing."""

    sizings: frozenset[str] = frozenset()
    record_prefix: str = ""
    delivery_note: str = ""
    # Speculative: no production consumer yet; set so a caller can tell a corrupt parent copy from a missing one.
    corrupt_parents: tuple[str, ...] = ()

    def covers(self, rel: str) -> bool:
        """True for the run's sizings, its delivery note, or anything under its record dir."""
        if not rel:
            return False
        if rel in self.sizings or (self.delivery_note and rel == self.delivery_note):
            return True
        return bool(self.record_prefix) and rel.startswith(self.record_prefix)

    def drop_expectations(self, expected: dict[str, str], keep: frozenset[str] | set[str] = frozenset()) -> list[str]:
        """Delete this run's own paths from *expected* (in place) except *keep*; return the sorted reread list."""
        reread = sorted(p for p in expected if p not in keep and self.covers(p))
        for p in reread:
            del expected[p]
        return reread


def _own_slice_sizing(root: Path, run_id: str) -> tuple[str, tuple[str, ...]]:
    """(sizing rel, parent copies that exist but do not parse); a missing copy is not corrupt."""
    corrupt: list[str] = []
    legacy = contract.parent_record_rel(run_id)
    for rel in (contract.run_parent_copy_rel(run_id), legacy):
        try:
            text = (root / rel).read_text(encoding="utf-8")
        except OSError:
            continue
        try:
            record = contract.ParentRecord.from_json(json.loads(text))
        except (ValueError, KeyError, TypeError):
            corrupt.append(rel)
            continue
        for row in record.runs:
            if row.run_id == run_id:
                return row.slice_sizing.replace("\\", "/"), tuple(corrupt)
    return "", tuple(corrupt)


def own_artifact_paths(root: Path, run_id: str) -> OwnArtifacts:
    """The sizing(s), record prefix and delivery note *run_id* owns; empty for an unsafe id."""
    if not isinstance(run_id, str) or not safe_id(run_id):
        return OwnArtifacts()
    root = Path(root)
    slice_rel, corrupt = _own_slice_sizing(root, run_id)
    sizings = {rel for rel in (warp_run_deliverable.record_sizing_rel(root, run_id), slice_rel) if rel}
    return OwnArtifacts(
        sizings=frozenset(sizings),
        corrupt_parents=corrupt,
        record_prefix=f"{_warp_home()}/{run_id}/",
        delivery_note=f"docs/delivery/{run_id}.md",
    )
