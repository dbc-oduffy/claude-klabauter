"""Artifact subject-matter classifier: given the path of a coordinator
working-data artifact, classifies its SUBJECT MATTER as one of engine,
doctrine, or cross-cutting. The discriminator is what the artifact is
ABOUT, not where it physically lives on disk. Subject-matter is the
routing key used by coordinator_state_root to place doctrine artifacts in
the coordinator-claude plane and engine artifacts in the claude-klabauter plane.

Spec backlinks:
    docs/plans/2026-07-04-doe-authoring-repo-build-subject-matter-.md § W2.2
    docs/wiki/state-placement-law.md § Plan Homes
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md
    (residual clean-slate wave; this module is NOT wired to the classifier's
    consumers yet)

Classification contract:

    engine        — artifact is ABOUT engine internals: coordinator_core/**,
                    pcore roadmap, claude-klabauter's own install-chain node (NOT the
                    coordinator install skill), MCP/resident-service
                    research, memos addressed TO claude-klabauter.

    doctrine      — artifact is ABOUT coordinator doctrine: skills, hooks,
                    agents, ceremonies, coordinator plugin source
                    (plugins/coordinator/**), meta-repo
                    doctrine surfaces (CLAUDE.md, CLAUDE.local.md,
                    docs/decisions/, docs/wiki/), coordinator install skill /
                    commands/install.md. Default class for everything else
                    coordinator.

    cross-cutting — artifact genuinely spans BOTH planes; requires an
                    explicit human routing decision. Never silently
                    auto-routed.

Install-chain disambiguation (required by spec):
    claude-klabauter's own install script                       -> engine
    coordinator install skill / commands/install.md    -> doctrine
    Discriminator: whose install it is (subject), not the shared word
    "install".

Negative-spec: this module does NOT attempt to detect all possible
cross-cutting artifacts — that is undecidable from path alone. It catches
the explicitly-named patterns from the plan (DR-207-shaped, fleet-spine
emitter-binding). Truly undecidable paths that match none of the patterns
fall through to the engine/doctrine heuristics.
"""
from __future__ import annotations

import re


class Subject:
    ENGINE = "engine"
    DOCTRINE = "doctrine"
    CROSS_CUTTING = "cross-cutting"


class CrossCuttingArtifact(Exception):
    """Raised by classify_or_raise when the artifact is cross-cutting.

    Carries the remediation message a caller would otherwise have to
    reconstruct itself.
    """

    def __init__(self, path: str, message: str):
        super().__init__(message)
        self.path = path
        self.message = message


_CROSS_CUTTING_PATTERNS = (
    # Review: code-reviewer -- bash oracle's `case` alternation matches only
    # the two literal casings `DR-207`/`dr-207` (no case-insensitive flag set);
    # re.IGNORECASE was an undocumented broadening past the faithful-repro
    # contract this migration wave holds itself to. Tightened to exact parity.
    re.compile(r"DR-207|dr-207"),  # DR-207-shaped: spans both planes
    re.compile(r"fleet-spine.*emitter"),  # fleet-spine emitter-binding
    # standalone *emitter-binding* covers any artifact whose name signals
    # a spine/emit straddle that doesn't carry the fleet-spine prefix (e.g. a future
    # cockpit-emitter-binding plan). The fleet-spine pattern above catches
    # the primary known instance; this is the catch-all for the class.
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
    """The stderr diagnostic for a cross-cutting artifact."""
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
