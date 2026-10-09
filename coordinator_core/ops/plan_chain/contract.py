"""plan_chain contract: stages, halt table, manifest/state/result shapes, runner protocol.

Pure data. Nothing here touches disk or the process at import time. The stage names are the
vendored ``doe-wake-digest.schema.json`` ``$defs.chain_stage.enum``; a halt's ``halted_at`` is
always one of them.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol

STAGES: tuple[str, ...] = ("plan", "ready-gate", "execute", "review", "terminal-commit")

# HALTS value for a halt whose stage is whichever stage was running when it fired.
STAGE_RELATIVE = "stage-relative"

HALT_REASON_MAX = 300

HALTS: Mapping[str, str] = {
    "plan-no-digest": "plan",
    "ready-gate-not-ready": "ready-gate",
    "stamp-stale-substantive": "execute",
    "roadmap-gate-shut": "execute",
    "peer-claim": "execute",
    "spine-check-failed": "execute",
    "external-gate-uncleared": "execute",
    "falsifier-owed": "execute",
    "dirty-write-set": "execute",
    "cross-repo-write-approval": "execute",
    "executor-block": "execute",
    "review-fail": "review",
    "falsifier-not-met": "review",
    "usage-limit": STAGE_RELATIVE,
    "fire-cap-reached": STAGE_RELATIVE,
    "child-no-digest": STAGE_RELATIVE,
    "terminal-commit-refused": "terminal-commit",
}

CHAIN_BIN = "plan-chain-run"
CHAIN_FLAG = "--chain"


@dataclass(frozen=True)
class Halt:
    """A chain stop: ``halted_at`` is a member of STAGES, ``reason`` is capped at 300 chars."""

    halted_at: str
    reason: str

    def __post_init__(self) -> None:
        if self.halted_at not in STAGES:
            raise ValueError(f"halted_at {self.halted_at!r} is not a chain stage")
        if len(self.reason) > HALT_REASON_MAX:
            object.__setattr__(self, "reason", self.reason[:HALT_REASON_MAX])


def halt(halt_id: str, reason: str, *, running_stage: str | None = None) -> Halt:
    """Build the Halt for ``halt_id``; stage-relative ids take ``running_stage``."""
    stage = HALTS[halt_id]
    if stage == STAGE_RELATIVE:
        if running_stage is None:
            raise ValueError(f"halt {halt_id!r} is stage-relative and needs running_stage")
        stage = running_stage
    return Halt(stage, reason)


@dataclass(frozen=True)
class ChainManifest:
    """What ``emit-wave-fire --chain`` hands the driver; paths are repo-relative."""

    sizing_object: str
    baton: str
    deliverable_id: str | None
    interaction_mode: str
    repo_root: str
    trail_dir: str
    wave_args: dict[str, Any]
    script_source: str
    # The route and size the chain was fired under; None on a manifest written before they were recorded.
    accepted_route: str | None = None
    accepted_tshirt: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "ChainManifest":
        return cls(**json.loads(text))


class ChainManifestCollision(Exception):
    """A manifest for a different chain already sits at the target path."""


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "x"


def chain_key(baton_id: str, deliverable_id: str | None = None) -> str:
    """The identity a chain runs for: its baton, narrowed by deliverable when one is named."""
    return _slug(baton_id) + (f".{_slug(deliverable_id)}" if deliverable_id else "")


def manifest_path(trail_dir: str | Path, wave_number: int, key: str) -> Path:
    """``<trail>/chain-<n>-<key>.json``; ``key`` is :func:`chain_key`, so one trail holds many chains."""
    return Path(trail_dir) / f"chain-{wave_number}-{key}.json"


def write_manifest(path: Path, manifest: ChainManifest) -> None:
    """Write ``manifest`` at ``path``; the same chain rewrites idempotently, another chain is refused.

    Chain identity is (sizing_object, baton, deliverable_id); a manifest at ``path`` that cannot be
    read is treated as foreign rather than clobbered.
    """
    if path.exists():
        try:
            prior = ChainManifest.from_json(path.read_text(encoding="utf-8"))
            same = (prior.sizing_object, prior.baton, prior.deliverable_id) == (
                manifest.sizing_object, manifest.baton, manifest.deliverable_id)
            prior_desc = f"sizing {prior.sizing_object} baton {prior.baton}"
        except (ValueError, TypeError, OSError):
            same, prior_desc = False, "an unreadable manifest"
        if not same:
            raise ChainManifestCollision(
                f"{path} already holds a different chain ({prior_desc}); "
                f"refusing to overwrite it with sizing {manifest.sizing_object} baton {manifest.baton}"
            )
    path.write_text(manifest.to_json(), encoding="utf-8", newline="\n")


@dataclass
class ChainState:
    """The driver's running record; the final digest is assembled from it."""

    stages_run: list[str] = field(default_factory=list)
    halt: Halt | None = None
    commit: dict[str, str] | None = None
    plan_path: str | None = None
    # `dispatch.terminal_commit` params for a run that halted after its execute child returned.
    resume: dict[str, Any] | None = None


@dataclass(frozen=True)
class WorkflowResult:
    """One headless child's outcome: the Workflow's returned digest, if it parsed."""

    digest: dict[str, Any] | None
    raw_result: str
    child_session_id: str
    task_output_path: str = ""


class WorkflowRunner(Protocol):
    def __call__(self, script_path: Path, *, session_id: str) -> WorkflowResult: ...
