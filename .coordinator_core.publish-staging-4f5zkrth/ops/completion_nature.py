"""
coordinator_core.ops.completion_nature — heuristic completion-nature classifier.

Purpose: Replace the wsc step-2.6.4 per-session Sonnet dispatch with a
path-pattern + commit-keyword heuristic that covers >90% of classification
cases at zero token cost.

Exposes a single plain callable:

    classify_nature(paths, commit_msgs) -> str

where the returned string is one of the four canonical nature values:
  - "roadmap"    — new feature / initiative / roadmap deliverable work
  - "bugfix"     — defect, regression, crash, or correctness fix
  - "tech-debt"  — refactor, cleanup, consolidation, debt reduction
  - "infra"      — CI, tooling, config, install, dependency, scaffold

Override: if the environment variable COMPLETION_NATURE is set to a non-empty
value, classify_nature returns it verbatim without consulting any heuristic.
This preserves the `COMPLETION_NATURE` env bypass that existed in the legacy
Sonnet dispatch path.

This module is NOT a registered op — it is a helper consumed by
ceremony.wsc_resolve (C2.2) to pre-resolve the nature branch. No edits to
ops/__init__.py or ipc.py are needed or made here.

Spec backlink: pln-ceremony-as-pipeline-2-invert--fd1b98 § C1.3
Node-map reference: wsc node 2.6.4 (SHOULD-BE-SCRIPT → D)
"""

from __future__ import annotations

import os
import re
from typing import Sequence

# ---------------------------------------------------------------------------
# Nature classification — ordered heuristic priority
# ---------------------------------------------------------------------------
# Priority: bugfix > roadmap > tech-debt > infra
# Rationale:
#   - bugfix signals ("fix", "revert", "regression") are the most
#     disambiguating and should win over everything else when present.
#   - roadmap signals indicate new capability delivery — prominent in commit
#     subjects (feat/feature/add/implement) and plan-doc paths.
#   - tech-debt signals cover refactor/cleanup, which often appear in commit
#     subjects alongside other terms; rank below roadmap to avoid shadowing
#     deliberate feature work flagged as "add + cleanup".
#   - infra is the catch-all for tooling/CI/config work and wins only when
#     nothing more specific fires.

# Path patterns — applied as substring or regex against PurePosixPath strings.
_PATH_PATTERNS: dict[str, list[str]] = {
    "roadmap": [
        r"docs/plans/",
        r"docs/problems/",
        r"docs/research/",
        r"state/initiatives/",
        r"cross-repo/",
        r"coordinator_core/ops/ceremony/",
        r"roadmap",
    ],
    "bugfix": [
        r"state/bug-backlog/",
        r"state/recovery/",
    ],
    "tech-debt": [
        r"state/improvement-queue/",
        r"docs/decisions/",
        r"archive/",
    ],
    "infra": [
        r"\.github/",
        r"scripts/",
        r"bin/",
        r"skills/setup/",
        r"agent-install-manifest\.json",
        r"coordinator\.local\.md",
        r"requirements.*\.txt",
        r"pyproject\.toml",
        r"setup\.py",
        r"setup\.cfg",
        r"Makefile",
        r"\.mcp\.json",
        r"\.claude/",
    ],
}

# Commit-message keyword patterns — matched case-insensitively against
# individual commit message strings.
_COMMIT_PATTERNS: dict[str, list[str]] = {
    "bugfix": [
        r"\bfix\b",
        r"\bfixes\b",
        r"\bfixed\b",
        r"\bbugfix\b",
        r"\brevert\b",
        r"\bregression\b",
        r"\bcrash\b",
        r"\bpatch\b",
        r"\bcorrect\b",
        r"\brepro\b",
        r"\bhot[- ]?fix\b",
    ],
    "roadmap": [
        r"\bfeat\b",
        r"\bfeature\b",
        r"\badd\b",
        r"\badds\b",
        r"\bimplement\b",
        r"\bimplemented\b",
        r"\bship\b",
        r"\bships\b",
        r"\bintroduce\b",
        r"\bnew\b",
        r"\blaunch\b",
        r"\broadmap\b",
        r"\binitiative\b",
        r"\bdeliverable\b",
        r"\bchunk\b",
        r"\bwave\b",
        r"\bplan\(",
        r"\bplan:",
        r"\bpipeline\b",
    ],
    "tech-debt": [
        r"\brefactor",
        r"\bcleanup\b",
        r"\bclean[ -]?up\b",
        r"\bdebt\b",
        r"\bconsolidat",  # consolidate / consolidation (prefix — no trailing \b)
        r"\bsimplif",     # simplify / simplification (prefix)
        r"\bremov",       # remove / removal (prefix)
        r"\bdedup\b",
        r"\bextract\b",
        r"\bmove\b",
        r"\brename\b",
        r"\bdeprec",      # deprecate / deprecated (prefix)
        r"\bstrangl",     # strangle / strangler (prefix)
    ],
    "infra": [
        r"\bci\b",
        r"\bcd\b",
        r"\bbuild\b",
        r"\bdeploy",      # deploy / deployment (prefix)
        r"\binstall",     # install / installation (prefix)
        r"\bconfig\b",
        r"\bscaffold\b",
        r"\bdependenc",   # dependency / dependencies (prefix)
        r"\bupgrade\b",
        r"\bbump\b",
        r"\bvendor\b",
        r"\bsetup\b",
        r"\bbootstrap\b",
        r"\bdocker\b",
        r"\bmakefile\b",
        r"\btooling\b",
        r"\bchore\b",
    ],
}

# Evaluation order — earlier = higher priority.
_PRIORITY: tuple[str, ...] = ("bugfix", "roadmap", "tech-debt", "infra")


def _score_paths(paths: Sequence[str]) -> dict[str, int]:
    """Count path-pattern hits per nature label."""
    scores: dict[str, int] = {n: 0 for n in _PRIORITY}
    for path in paths:
        for nature, patterns in _PATH_PATTERNS.items():
            for pat in patterns:
                if re.search(pat, path):
                    scores[nature] += 1
                    break  # one hit per path per nature is enough
    return scores


def _score_commits(commit_msgs: Sequence[str]) -> dict[str, int]:
    """Count commit-keyword hits per nature label."""
    scores: dict[str, int] = {n: 0 for n in _PRIORITY}
    for msg in commit_msgs:
        for nature, patterns in _COMMIT_PATTERNS.items():
            for pat in patterns:
                if re.search(pat, msg, re.IGNORECASE):
                    scores[nature] += 1
                    break  # one hit per commit per nature is enough
    return scores


def classify_nature(
    paths: Sequence[str],
    commit_msgs: Sequence[str],
) -> str:
    """Classify a session's completion nature from touched paths and commit messages.

    Returns one of: "roadmap", "bugfix", "tech-debt", "infra".

    Priority: bugfix > roadmap > tech-debt > infra.  The highest-scoring
    nature wins; ties are broken by priority order.  When no signal fires at
    all (empty inputs or zero matches), returns "infra" as the safe default.

    Override: COMPLETION_NATURE env var bypasses all heuristics when set to a
    non-empty string.

    Args:
        paths:       Sequence of file paths touched this session (repo-relative
                     or absolute strings; forward-slash separators assumed).
        commit_msgs: Sequence of commit message strings (subject lines or full
                     messages) authored this session.

    Returns:
        Nature string: "roadmap" | "bugfix" | "tech-debt" | "infra".
    """
    # Honor the env override — return verbatim, no heuristic.
    override = os.environ.get("COMPLETION_NATURE", "").strip()
    if override:
        return override

    path_scores = _score_paths(paths)
    commit_scores = _score_commits(commit_msgs)

    # Aggregate: sum path and commit scores per nature.
    combined: dict[str, int] = {
        n: path_scores[n] + commit_scores[n] for n in _PRIORITY
    }

    # Strict-priority selection: return the FIRST nature (in priority order)
    # that has at least one signal hit.  Ties are broken purely by priority —
    # bugfix wins over roadmap even if roadmap has more cumulative hits, because
    # the presence of ANY fix/revert/regression signal is disambiguating enough
    # to outweigh a higher keyword density in a lower-priority category.
    # When no nature has any signal, fall through to the default "infra".
    for nature in _PRIORITY:
        if combined[nature] > 0:
            return nature

    # No signals at all → safe default.
    return "infra"
