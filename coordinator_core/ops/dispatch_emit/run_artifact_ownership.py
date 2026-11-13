"""Which paths a warp run owns as run-artifacts.

Pure in-process file reads, zero spawns; every path is repo-relative and forward-slash.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from coordinator_core.ops import warp_run_deliverable
from coordinator_core.ops._path_guard import safe_id
from coordinator_core.ops.dispatch_emit import ask_fanout_contract as contract
from coordinator_core.ops.dispatch_emit.ask_contract import delivery_note_locate, record_dir
from coordinator_core.session.output_tree_layout import locate
from coordinator_core.session.record_homes import home_dir


def _warp_home() -> str:
    return str(PurePosixPath(home_dir("", "warp").replace(os.sep, "/")))


_SHARD_DIR = re.compile(r"^\d{4}-\d{2}$")


@dataclass(frozen=True)
class OwnArtifacts:
    """A run's own run-artifact paths; the empty value owns nothing."""

    sizings: frozenset[str] = frozenset()
    record_prefix: str = ""
    delivery_note: str = ""
    # Speculative: no production consumer yet; set so a caller can tell a corrupt parent copy from a missing one.
    corrupt_parents: tuple[str, ...] = ()
    # `<plan stem>.evidence.yaml` beside each plan the run names; the criterion-evidence stage writes it.
    plan_evidence: frozenset[str] = frozenset()
    # The plan doc the run's sizing was minted from: its `plan:` edge names a `.md` whose stem is the sizing's stem.
    plan_docs: frozenset[str] = frozenset()

    def _is_note(self, rel: str) -> bool:
        """The run's note at its located path, or at the other of its flat and `<YYYY-MM>` shard homes."""
        if rel == self.delivery_note:
            return True
        parts = rel.split("/")
        mine = self.delivery_note.split("/")
        return (
            parts[:2] == mine[:2] == ["docs", "delivery"] and parts[-1] == mine[-1]
            and (len(parts) == 3 or (len(parts) == 4 and _SHARD_DIR.match(parts[2]) is not None))
        )

    def covers(self, rel: str) -> bool:
        """True for the run's sizings, plan evidence, minted plan doc, delivery note, or anything under its record dir."""
        if not rel:
            return False
        if rel in self.sizings or rel in self.plan_evidence or rel in self.plan_docs:
            return True
        if self.delivery_note and self._is_note(rel):
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


_PLAN_EDGE = re.compile(r"^plan:[ \t]*[\"']?([^\"'\r\n#]+?)[\"']?[ \t]*(?:#.*)?$", re.MULTILINE)


def _plan_evidence(root: Path, run_id: str, sizings: set[str]) -> frozenset[str]:
    """Evidence paths beside the run's plan: halted.json's `plan` and each sizing's top-level `plan:` edge, each as named and at its located path."""
    plans: set[str] = set()
    try:
        halted = json.loads((record_dir(root, run_id) / "halted.json").read_text(encoding="utf-8"))
        if isinstance(halted, dict) and isinstance(halted.get("plan"), str):
            plans.add(halted["plan"])
    except (OSError, ValueError):
        pass
    for rel in sizings:
        try:
            match = _PLAN_EDGE.search((root / rel).read_text(encoding="utf-8"))
        except OSError:
            continue
        if match:
            plans.add(match.group(1))
    out: set[str] = set()
    for plan in plans:
        pp = PurePosixPath(plan.strip().replace("\\", "/"))
        if pp.is_absolute() or ".." in pp.parts or not pp.name:
            continue
        out.add(str(pp.with_name(pp.stem + ".evidence.yaml")))
        located = PurePosixPath(locate(str(root), str(pp)))
        out.add(str(located.with_name(located.stem + ".evidence.yaml")))
    return frozenset(out)


def _plan_docs(root: Path, sizings: set[str]) -> frozenset[str]:
    """Each sizing's `plan:` edge when it is a `.md` sharing the sizing's stem: the engine-minted plan doc.

    A plan the sizing merely points at keeps its own stem and stays foreign.
    """
    out: set[str] = set()
    for rel in sizings:
        try:
            match = _PLAN_EDGE.search((root / rel).read_text(encoding="utf-8"))
        except OSError:
            continue
        if not match:
            continue
        pp = PurePosixPath(match.group(1).strip().replace("\\", "/"))
        if pp.is_absolute() or ".." in pp.parts or pp.suffix != ".md" or pp.stem != PurePosixPath(rel).stem:
            continue
        out.add(str(pp))
    return frozenset(out)


def own_artifact_paths(root: Path, run_id: str) -> OwnArtifacts:
    """The sizing(s), plan evidence, record prefix and delivery note *run_id* owns; empty for an unsafe id."""
    if not isinstance(run_id, str) or not safe_id(run_id):
        return OwnArtifacts()
    root = Path(root)
    slice_rel, corrupt = _own_slice_sizing(root, run_id)
    sizings = {rel for rel in (warp_run_deliverable.record_sizing_rel(root, run_id), slice_rel) if rel}
    return OwnArtifacts(
        sizings=frozenset(sizings),
        plan_evidence=_plan_evidence(root, run_id, sizings),
        plan_docs=_plan_docs(root, sizings),
        corrupt_parents=corrupt,
        record_prefix=f"{_warp_home()}/{run_id}/",
        delivery_note=delivery_note_locate(root, run_id),
    )
