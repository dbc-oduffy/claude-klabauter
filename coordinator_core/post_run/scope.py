"""coordinator_core.post_run.scope -- the data contract between a pipeline's tail and the post-run steps.

Frozen dataclasses only, no I/O: a step reads and parses each touched page at most once
(mirroring `learn_lessons_pipeline.mechanical_close`), and never discovers its own scope.
`touched` and `evidence` are supplied by the caller from files the run itself wrote -- never
by a directory scan or a git spawn.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal, Sequence

Pipeline = Literal["distill", "lessons"]

PIPELINES: tuple[str, ...] = ("distill", "lessons")


@dataclass(frozen=True)
class PostRunScope:
    """What one run wrote.

    `repo_root` is the originating worktree, never the git common dir. `wiki_roots` are
    absolute directories. `touched` is the run's written path set (repo-relative posix);
    `evidence` is the run's evidence-file path list, committed with it but never transformed.
    """

    pipeline: str
    repo_root: Path
    wiki_roots: tuple[Path, ...]
    touched: tuple[str, ...]
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.pipeline not in PIPELINES:
            raise ValueError(f"pipeline must be one of {PIPELINES}, got {self.pipeline!r}")
        object.__setattr__(self, "repo_root", Path(self.repo_root))
        object.__setattr__(self, "wiki_roots", tuple(Path(p) for p in self.wiki_roots))
        object.__setattr__(self, "touched", tuple(self.touched))
        object.__setattr__(self, "evidence", tuple(self.evidence))

    @classmethod
    def for_run(
        cls,
        pipeline: str,
        repo_root: Path | str,
        wiki_paths: Sequence[str],
        touched: Sequence[str],
        evidence: Sequence[str] = (),
    ) -> "PostRunScope":
        """Build a scope, joining each pipeline wiki path (repo-relative) onto `repo_root`; pure path arithmetic."""
        root = Path(repo_root)
        return cls(
            pipeline=pipeline,
            repo_root=root,
            wiki_roots=tuple(root / p for p in wiki_paths),
            touched=tuple(touched),
            evidence=tuple(evidence),
        )

    @property
    def commit_paths(self) -> tuple[str, ...]:
        """Touched then evidence paths, de-duplicated in first-seen order: the explicit commit pathspec."""
        return tuple(dict.fromkeys((*self.touched, *self.evidence)))


@dataclass(frozen=True)
class StepResult:
    """One step's outcome: `changed` are the repo-relative paths it rewrote; `residual` the
    findings it could not resolve (reported, never deleted)."""

    name: str
    changed: tuple[str, ...] = ()
    residual: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "changed", tuple(self.changed))
        object.__setattr__(self, "residual", tuple(self.residual))


@dataclass(frozen=True)
class PostRunReport:
    """The ordered step results of one run's tail."""

    pipeline: str
    steps: tuple[StepResult, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "steps", tuple(self.steps))

    @property
    def changed(self) -> tuple[str, ...]:
        """Every path any step changed, de-duplicated in first-seen order."""
        return tuple(dict.fromkeys(p for s in self.steps for p in s.changed))

    @property
    def residual(self) -> tuple[str, ...]:
        """Every unresolved finding, prefixed with its step name."""
        return tuple(f"{s.name}: {r}" for s in self.steps for r in s.residual)

    @property
    def clean(self) -> bool:
        """True when no step left a residual."""
        return not self.residual


# A step is a pure function of the scope; ordering and halting belong to the caller.
Step = Callable[[PostRunScope], StepResult]
