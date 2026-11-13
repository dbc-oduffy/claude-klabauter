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


def _home_rel(kind: str) -> PurePosixPath:
    return PurePosixPath(home_dir("", kind).replace(os.sep, "/"))


def _warp_home() -> str:
    return str(_home_rel("warp"))


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


_SIZING_OBJECT = re.compile(r"^sizing_object:[ \t]*[\"']?([^\"'\r\n#]+?)[\"']?[ \t]*(?:#.*)?$", re.MULTILINE)
_STAMP = re.compile(r"\d{8}T\d{6}")
_FRONTMATTER_READ_CAP = 4096


def _plan_names_sizing(root: Path, plan_rel: str, sizing_rel: str) -> bool:
    """The plan's frontmatter `sizing_object:` names a sizing sharing `sizing_rel`'s stem."""
    try:
        with (root / plan_rel).open(encoding="utf-8") as fh:
            head = fh.read(_FRONTMATTER_READ_CAP)
    except OSError:
        return False
    match = _SIZING_OBJECT.search(head)
    return bool(match) and PurePosixPath(match.group(1).strip().replace("\\", "/")).stem == PurePosixPath(sizing_rel).stem


def _plan_docs(root: Path, sizings: set[str], chained: bool = False) -> frozenset[str]:
    """Each sizing's `plan:` edge when it is a `.md` sharing the sizing's stem: the engine-minted plan doc.

    A plan the sizing merely points at keeps its own stem and stays foreign, except under `chained`
    (a same-sizing predecessor run exists): a `docs/plans` plan whose own `sizing_object:` names the sizing
    is the plan that predecessor minted for it.
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
        if pp.is_absolute() or ".." in pp.parts or pp.suffix != ".md":
            continue
        if pp.stem != PurePosixPath(rel).stem and not (
            chained and pp.parts[:2] == ("docs", "plans") and _plan_names_sizing(root, str(pp), rel)
        ):
            continue
        out.add(str(pp))
    return frozenset(out)


def _handoff_docs(sizings: set[str]) -> frozenset[str]:
    """`state/handoffs/<same tail>.md` for each `state/sizings/<tail>.yaml`: the baton the sizing's run minted."""
    out: set[str] = set()
    sizing_home, handoff_home = _home_rel("sizings"), _home_rel("handoffs")
    for rel in sizings:
        pp = PurePosixPath(rel)
        if pp.parts[:len(sizing_home.parts)] == sizing_home.parts and pp.suffix in (".yaml", ".yml"):
            out.add(str((handoff_home / PurePosixPath(*pp.parts[len(sizing_home.parts):])).with_suffix(".md")))
    return frozenset(out)


def _stamp(run_id: str) -> str:
    found = _STAMP.findall(run_id)
    return found[-1] if found else ""


def _same_sizing_predecessors(root: Path, run_id: str, sizings: set[str]) -> tuple[str, ...]:
    """Earlier runs bound to the same sizing (parked, halted or re-minted ahead of `run_id`), nearest chain first.

    Candidates are the `resumed_from` chain plus record dirs named for the sizing's stem (`warp-<stem>-...`)
    or its trailing hex (`ask-<stamp>-<hex>`) whose stamp precedes `run_id`'s; each is confirmed by its
    `run.json` `sizing_rel` stem. One directory listing and one small read per candidate; no spawn.
    """
    stems = {PurePosixPath(s).stem for s in sizings if s}
    if not stems:
        return ()
    from coordinator_core.ops.dispatch_emit.landing_integrity import _resumed_chain

    out: dict[str, None] = dict.fromkeys(_resumed_chain(root, run_id))
    mine = _stamp(run_id)
    try:
        names = [p.name for p in record_dir(root, run_id).parent.iterdir()]
    except OSError:
        names = []
    hexes = {s.rsplit("-", 1)[1] for s in stems if re.fullmatch(r"[0-9a-f]{6}", s.rsplit("-", 1)[-1])}
    for name in sorted(names):
        if name == run_id or name in out or not safe_id(name):
            continue
        stamp = _stamp(name)
        if not (mine and stamp and stamp < mine):
            continue
        if any(name.startswith(f"warp-{stem}-") for stem in stems) or (
            name.startswith("ask-") and name.rsplit("-", 1)[-1] in hexes
        ):
            out[name] = None
    kept: list[str] = []
    for name in out:
        theirs = warp_run_deliverable.record_sizing_rel(root, name)
        if theirs and PurePosixPath(theirs).stem in stems:
            kept.append(name)
    return tuple(kept)


def own_artifact_paths(root: Path, run_id: str) -> OwnArtifacts:
    """The sizing(s), plan evidence, record prefix and delivery note *run_id* owns; empty for an unsafe id."""
    if not isinstance(run_id, str) or not safe_id(run_id):
        return OwnArtifacts()
    root = Path(root)
    slice_rel, corrupt = _own_slice_sizing(root, run_id)
    sizings = {rel for rel in (warp_run_deliverable.record_sizing_rel(root, run_id), slice_rel) if rel}
    predecessors = _same_sizing_predecessors(root, run_id, sizings)
    plans = set(_plan_docs(root, sizings, chained=bool(predecessors)))
    if predecessors:
        plans |= _handoff_docs(sizings)
    return OwnArtifacts(
        sizings=frozenset(sizings),
        plan_evidence=_plan_evidence(root, run_id, sizings),
        plan_docs=frozenset(plans),
        corrupt_parents=corrupt,
        record_prefix=f"{_warp_home()}/{run_id}/",
        delivery_note=delivery_note_locate(root, run_id),
    )
