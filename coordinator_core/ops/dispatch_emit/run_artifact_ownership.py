"""Which paths a warp run owns as run-artifacts, and which run owns a slice sizing.

Pure in-process file reads, zero spawns; every path is repo-relative and forward-slash.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Optional

import yaml

from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.session.record_homes import home_dir

ASK_PARENT_KEY = "ask_parent"


def _warp_home() -> str:
    return str(PurePosixPath(home_dir("", "warp").replace(os.sep, "/")))


@dataclass(frozen=True)
class OwnArtifacts:
    """A run's own run-artifact paths; the empty value owns nothing."""

    sizings: frozenset[str] = frozenset()
    record_prefix: str = ""
    delivery_note: str = ""

    def covers(self, rel: str) -> bool:
        """True for the run's sizings, its delivery note, or anything under its record dir."""
        if not rel:
            return False
        if rel in self.sizings or (self.delivery_note and rel == self.delivery_note):
            return True
        return bool(self.record_prefix) and rel.startswith(self.record_prefix)


def _own_slice_sizing(root: Path, run_id: str) -> str:
    for rel in (contract.run_parent_copy_rel(run_id), f"scratch/warp/{run_id}/parent.json"):
        try:
            data = json.loads((root / rel).read_text(encoding="utf-8"))
            record = contract.ParentRecord.from_json(data)
        except (OSError, ValueError, KeyError, TypeError):
            continue
        for row in record.runs:
            if row.run_id == run_id:
                return row.slice_sizing.replace("\\", "/")
    return ""


def own_artifact_paths(root: Path, run_id: str) -> OwnArtifacts:
    """The sizing(s), record prefix and delivery note *run_id* owns; empty for an unsafe id."""
    if not isinstance(run_id, str) or not safe_id(run_id):
        return OwnArtifacts()
    root = Path(root)
    sizings = {
        rel
        for rel in (warp_run_deliverable.record_sizing_rel(root, run_id), _own_slice_sizing(root, run_id))
        if rel
    }
    return OwnArtifacts(
        sizings=frozenset(sizings),
        record_prefix=f"{_warp_home()}/{run_id}/",
        delivery_note=f"docs/delivery/{run_id}.md",
    )


def sizing_owner(root: Path, sizing_rel: str) -> Optional[str]:
    """The run id the sizing's ``em_analysis.ask_parent`` marker names, None when unmarked."""
    try:
        data: Any = yaml.safe_load((Path(root) / sizing_rel).read_text(encoding="utf-8"))
    except (OSError, ValueError, yaml.YAMLError):
        return None
    analysis = data.get("em_analysis") if isinstance(data, dict) else None
    if not isinstance(analysis, dict):
        return None
    parsed = contract.parse_ask_parent(analysis.get(ASK_PARENT_KEY))
    return parsed[1] if parsed else None


def foreign_sizing(root: Path, run_id: str, sizing_rel: str) -> Optional[str]:
    """The owning run id when the sizing is marked for a run other than *run_id*, else None."""
    owner = sizing_owner(root, sizing_rel)
    return owner if owner and owner != run_id else None
