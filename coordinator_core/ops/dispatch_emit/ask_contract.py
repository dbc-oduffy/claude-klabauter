"""Names shared by the warp --ask seam: phases, op names, halt kinds, gate verdict, stage manifest.

Signatures implemented in their own modules:
  ask_gate.gate(repo_root: Path, sizing_rel: str, *, writes: Sequence[str] = ()) -> GateVerdict
  ask_stage.stage(repo_root: Path, *, run_id: str, plan_rel: str | None = None,
                  sizing_rel: str | None = None, writes: Sequence[str] = ()) -> StageManifest
  ask_compose.compose_ask_script(*, repo_root: str, prompt: str | None, sizing_rel: str | None,
                  run_id: str, session_id: str | None, baton: dict | None = None,
                  accept_pending: bool = False) -> str
                  (`baton` = {"path", "deliverable_id"}; the accept phase composes when
                  `accept_pending` or on a raw ask)
  ask_plan_blitz.wrap_stage(plan_blitz_text: str) -> tuple[str, list[str]]
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

ASK_PHASES = ("size", "gate", "accept", "plan", "stage", "execute", "review")
OP_ASK_GATE, OP_ASK_STAGE = "dispatch.ask_gate", "dispatch.ask_stage"
HALT_ROOM, HALT_TOUCHPOINT, HALT_REFUSAL = "room", "touchpoint", "refusal"
RUN_DIR_ROOT = "scratch/warp"
ASK_MANIFEST_MARKER = "// coordinator:ask-run-manifest v1 "


@dataclass(frozen=True)
class GateVerdict:
    """dispatch.ask_gate reply: an arm to run, or a halt dict
    {"kind": HALT_*, "reason": str, "touchpoint"?: str, "route"?: str}; baton is the
    minted {"id", "path"} at an M+ arm."""

    arm: str | None
    halt: dict | None
    baton: dict | None = None
    # Set only when the engine's size rule discharged a null `exit_criterion.accepted`: the
    # sizing object cannot carry this, so the verdict is the record that nobody was asked.
    acceptance: dict | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"arm": self.arm, "halt": self.halt, "baton": self.baton}
        if self.acceptance is not None:
            out["acceptance"] = self.acceptance
        return out


@dataclass(frozen=True)
class ManifestRow:
    id: str
    agent_type: str
    model: str
    brief_path: str
    writes: tuple[str, ...]
    wave: int
    #: The rows this one waits on (`DagNode.after`): never a whole earlier wave.
    deps: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "agent_type": self.agent_type,
            "model": self.model,
            "brief_path": self.brief_path,
            "writes": list(self.writes),
            "wave": self.wave,
            "deps": list(self.deps),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "ManifestRow":
        return cls(
            id=data["id"],
            agent_type=data["agent_type"],
            model=data["model"],
            brief_path=data["brief_path"],
            writes=tuple(data["writes"]),
            wave=data["wave"],
            deps=tuple(data.get("deps") or ()),
        )


@dataclass(frozen=True)
class GatedRow:
    """A row withheld from the run; ``reason`` is ``external_gate`` or
    ``transitive_gate_closure`` and ``gate`` describes the blocking gate."""

    id: str
    reason: str
    gate: str
    owner_repo: str = ""
    closure_key: Any = None

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id, "reason": self.reason, "gate": self.gate,
            "owner_repo": self.owner_repo, "closure_key": self.closure_key,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "GatedRow":
        return cls(
            id=data["id"], reason=data["reason"], gate=data["gate"],
            owner_repo=data.get("owner_repo") or "", closure_key=data.get("closure_key"),
        )


@dataclass(frozen=True)
class StageManifest:
    """dispatch.ask_stage reply, also written as manifest.json in the run dir.
    ``gated`` lists rows withheld, never staged; an absent key reads as ``()``."""

    run_dir: str
    rows: tuple[ManifestRow, ...]
    review_declared_paths: tuple[str, ...]
    marker_path: str
    plan_id: str | None = None
    gated: tuple[GatedRow, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "run_dir": self.run_dir,
            "rows": [r.to_json() for r in self.rows],
            "review_declared_paths": list(self.review_declared_paths),
            "marker_path": self.marker_path,
            "gated": [g.to_json() for g in self.gated],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "StageManifest":
        return cls(
            run_dir=data["run_dir"],
            rows=tuple(ManifestRow.from_json(r) for r in data["rows"]),
            review_declared_paths=tuple(data["review_declared_paths"]),
            marker_path=data["marker_path"],
            plan_id=data.get("plan_id"),
            gated=tuple(GatedRow.from_json(g) for g in data.get("gated") or ()),
        )
