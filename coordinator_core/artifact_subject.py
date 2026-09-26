from __future__ import annotations

import re


class Subject:
    ENGINE = "engine"
    DOCTRINE = "doctrine"
    CROSS_CUTTING = "cross-cutting"


class CrossCuttingArtifact(Exception):

    def __init__(self, path: str, message: str):
        super().__init__(message)
        self.path = path
        self.message = message


_CROSS_CUTTING_PATTERNS = (
    # re.IGNORECASE was an undocumented broadening past the faithful-repro
    re.compile(r"DR-207|dr-207"),
    re.compile(r"fleet-spine.*emitter"),
    re.compile(r"emitter-binding"),
)

_ENGINE_PATTERNS = (
    re.compile(r"coordinator_core"),
    re.compile(r"pcore"),
    re.compile(r"claude-klabauter.*install|claude-klabauter-install"),
    re.compile(r"resident-service"),
    re.compile(r"docs/research.*mcp[-_]server"),
    re.compile(r"docs/plans.*mcp[-_]server"),
    re.compile(r"to-claude-klabauter|to-claude-klabauter"),
)

_DOCTRINE_PREEMPT_PATTERNS = (
    re.compile(r"commands/install"),
    re.compile(r"skills/repo-setup"),
    re.compile(r"plugins/coordinator-claude.*mcp[-_]server"),
    re.compile(r"docs/wiki.*mcp[-_]server"),
    re.compile(r"commands.*mcp[-_]server"),
)


def _is_cross_cutting(path: str) -> bool:
    return any(p.search(path) for p in _CROSS_CUTTING_PATTERNS)


def _is_doctrine_preempt(path: str) -> bool:
    return any(p.search(path) for p in _DOCTRINE_PREEMPT_PATTERNS)


def _is_engine(path: str) -> bool:
    return any(p.search(path) for p in _ENGINE_PATTERNS)


def classify(path: str) -> str:
    """Classify ``path``'s subject matter. Returns Subject.ENGINE,
    Subject.DOCTRINE, or Subject.CROSS_CUTTING — never raises.

    Mirrors coordinator_artifact_subject() phase ordering:
    Phase 1 cross-cutting (must run first, fail-loud-on-ambiguity contract)
    -> Phase 2 install-chain/doctrine pre-emption -> Phase 3 engine
    -> Phase 4 doctrine (default).
    """
    if not path:
        raise ValueError(
            "coordinator_artifact_subject: usage: classify(<artifact-path>)"
        )

    if _is_cross_cutting(path):
        return Subject.CROSS_CUTTING

    if _is_doctrine_preempt(path):
        return Subject.DOCTRINE

    if _is_engine(path):
        return Subject.ENGINE

    return Subject.DOCTRINE


def remediation_message(path: str) -> str:
    return (
        f"coordinator_artifact_subject: cross-cutting artifact detected — '{path}'\n"
        "  This artifact spans both doctrine and engine planes. It cannot be\n"
        "  auto-routed and requires an explicit human routing decision.\n"
        "  Action: identify which plane OWNS the changed contract or doctrine\n"
        "  surface and route the artifact manually to that plane.\n"
        "  Reference: docs/wiki/state-placement-law.md § Plan Homes\n"
        "             (cross-cutting plans paragraph)\n"
        "  Spec: docs/plans/2026-07-04-doe-authoring-repo-build-subject-matter-.md § W2.2"
    )


def classify_or_raise(path: str) -> str:
    """Like classify(), but raises CrossCuttingArtifact instead of
    returning Subject.CROSS_CUTTING — convenient for callers that want a
    fail-loud shape as a Python exception instead of a string comparison."""
    subject = classify(path)
    if subject == Subject.CROSS_CUTTING:
        raise CrossCuttingArtifact(path, remediation_message(path))
    return subject
