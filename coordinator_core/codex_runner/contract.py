"""codex_runner.contract -- request/result types and mode signatures every codex_runner module shares.

review.run, execute.run and agy.review_run (implemented by sibling modules) match ModeRun: they are
called once per run in the detached worker, call on_spawn(codex_pid) exactly once
right after Popen, and return a RunResult whose status is one of STATUS.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from coordinator_core._settings_home import settings_home

MODES = ("review", "execute", "agy-review")
ARTIFACT_MODES = ("review", "agy-review")

STATUS_OK = "ok"
STATUS_LIMIT_EXHAUSTED = "limit_exhausted"
STATUS_CONFINEMENT_BREACH = "confinement_breach"
STATUS_SCOPE_BREACH = "scope_breach"
STATUS_FAILED = "failed"
STATUS = frozenset({
    STATUS_OK,
    STATUS_LIMIT_EXHAUSTED,
    STATUS_CONFINEMENT_BREACH,
    STATUS_SCOPE_BREACH,
    STATUS_FAILED,
})

FALLBACK_REVIEWER = "claude-reviewer"

RUNS_DIRNAME = "codex-runs"

OnSpawn = Callable[[int], None]


@dataclass(frozen=True)
class RunRequest:
    """One run. review and agy-review need `artifact`; execute needs `plan` and `chunk`."""

    mode: str
    repo_root: str
    artifact: Optional[str] = None
    plan: Optional[str] = None
    chunk: Optional[str] = None
    parent_session_id: Optional[str] = None
    allow_api_key: bool = False

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}, got {self.mode!r}")
        if not self.repo_root:
            raise ValueError("repo_root is required")
        if self.mode in ARTIFACT_MODES:
            if not self.artifact:
                raise ValueError(f"{self.mode} mode requires artifact")
            if self.plan or self.chunk:
                raise ValueError(f"{self.mode} mode takes artifact only, not plan/chunk")
        else:
            if not (self.plan and self.chunk):
                raise ValueError("execute mode requires plan and chunk")
            if self.artifact:
                raise ValueError("execute mode takes plan+chunk only, not artifact")

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "repo_root": self.repo_root,
            "artifact": self.artifact,
            "plan": self.plan,
            "chunk": self.chunk,
            "parent_session_id": self.parent_session_id,
            "allow_api_key": self.allow_api_key,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunRequest":
        return cls(
            mode=d.get("mode", ""),
            repo_root=d.get("repo_root", ""),
            artifact=d.get("artifact"),
            plan=d.get("plan"),
            chunk=d.get("chunk"),
            parent_session_id=d.get("parent_session_id"),
            allow_api_key=d.get("allow_api_key") is True,
        )


@dataclass(frozen=True)
class RunResult:
    """Outcome of one run. `fallback` is FALLBACK_REVIEWER on a review-arm limit_exhausted, else None (execute has no fallback)."""

    run_id: str
    status: str
    findings: list[dict[str, str]] = field(default_factory=list)
    verdict: Optional[str] = None
    fallback: Optional[str] = None
    confinement: Optional[dict[str, Any]] = None
    scope: Optional[dict[str, Any]] = None
    codex_pid: Optional[int] = None
    usage: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    def __post_init__(self) -> None:
        if self.status not in STATUS:
            raise ValueError(f"status must be one of {sorted(STATUS)}, got {self.status!r}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "findings": list(self.findings),
            "verdict": self.verdict,
            "fallback": self.fallback,
            "confinement": self.confinement,
            "scope": self.scope,
            "codex_pid": self.codex_pid,
            "usage": self.usage,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunResult":
        return cls(
            run_id=d["run_id"],
            status=d["status"],
            findings=list(d.get("findings") or []),
            verdict=d.get("verdict"),
            fallback=d.get("fallback"),
            confinement=d.get("confinement"),
            scope=d.get("scope"),
            codex_pid=d.get("codex_pid"),
            usage=d.get("usage"),
            error=d.get("error"),
        )


class ModeRun(Protocol):
    """Signature of review.run, execute.run and agy.review_run: (req, run_dir, on_spawn) -> RunResult."""

    def __call__(self, req: RunRequest, run_dir: Path, on_spawn: OnSpawn) -> RunResult: ...


def run_dir(run_id: str) -> Path:
    """Run directory `<settings_home>/codex-runs/<run_id>`; rejects ids that could escape it."""
    if not run_id or run_id in (".", "..") or any(c in run_id for c in "/\\\0"):
        raise ValueError(f"invalid run_id: {run_id!r}")
    return settings_home() / RUNS_DIRNAME / run_id


def findings_schema_path() -> Path:
    """Path of the strict review-findings JSON schema passed to codex --output-schema."""
    return Path(__file__).with_name("review_findings.schema.json")
