"""coordinator_core.ops.doctype_hosts — the checked-in `(type, ceremony)` table (C0).

Purpose: the SINGLE source of the B2 in-scope set — which `coordinator-doc-new
--type <T>` value is mandated by which ceremony, and whether that ceremony has
a `coordinator_core` module at HEAD to emit it through. This module holds DATA
ONLY: no directive is constructed here, no CLI is invoked, no file is written.
The hosts read this table to know what they must emit; C8's coverage pin reads
it to know what to check. Neither re-enumerates the list — a list that appears
twice diverges once (docs/plans/2026-09-11-document-scaffolding-is-emitted-
not-remembered.md § Which shape is canonical).

Row key: `(type, ceremony)`, not `type` alone — a type may be mandated at more
than one ceremony (e.g. `roadmap-baton` both at roadmap creation and at baton
continuation), and the emitted/excluded state is a property of the PAIR, never
the type in isolation.

Row shape (`DoctypeHostRow`):
  - `type`: the manifest `--type` value (see docs/research/2026-09-11-doc-
    scaffold-emitter-homes.md for the source-of-truth manifest read).
  - `ceremony`: the mandating ceremony's name, or `None` for a row with no
    placeable ceremony (host-less).
  - `module`: the `coordinator_core` package (dotted, repo-relative) that owns
    or will own the emission, or `None` for an excluded row.
  - `state`: `"emitted"` (this ceremony's module names `coordinator-doc-new`
    in `directives[].cli` for this type today, OR this plan's B-wave chunks
    (C3-C6) land it there) or `"excluded"` (no emission — see `reason`).
  - `reason`: for an `excluded` row only, one of the closed set
    `EXCLUSION_REASONS`. `None` for an `emitted` row.
  - `citation`: for a `prohibited-by-doctrine` row only — the DoE-claude path
    and ref the prohibition was read at. `None` otherwise.

Negative-spec: this module does not construct a `coordinator-doc-new`
directive, does not import any host package, does not shell out, and does not
register an IPC op. It is data, imported by the hosts and by C8's pin, never
the other way around.

Spec backlink: docs/plans/2026-09-11-document-scaffolding-is-emitted-not-
remembered.md, chunk C0.
Derivation and evidence: docs/research/2026-09-11-doc-scaffold-emitter-
homes.md — this module holds the table, that doc holds the reasoning; a
prose copy of the list belongs in neither the wiki nor a second file.
"""

from __future__ import annotations

from typing import NamedTuple, Optional

EXCLUSION_REASONS = frozenset({"no-ceremony-module", "prohibited-by-doctrine"})


class DoctypeHostRow(NamedTuple):
    type: str
    ceremony: Optional[str]
    module: Optional[str]
    state: str
    reason: Optional[str] = None
    citation: Optional[str] = None


DOCTYPE_HOSTS: tuple[DoctypeHostRow, ...] = (
    DoctypeHostRow(
        type="handoff",
        ceremony="baton-continuation",
        module="coordinator_core.baton_assemble",
        state="emitted",
    ),
    DoctypeHostRow(
        type="roadmap-baton",
        ceremony="baton-continuation",
        module="coordinator_core.baton_assemble",
        state="emitted",
    ),
    DoctypeHostRow(
        type="roadmap-baton",
        ceremony="roadmap-planning",
        module="coordinator_core.roadmap_planning_assemble",
        state="emitted",
    ),
    DoctypeHostRow(
        type="roadmap-seed",
        ceremony="roadmap-planning",
        module="coordinator_core.roadmap_planning_assemble",
        state="emitted",
    ),
    DoctypeHostRow(
        type="plan",
        ceremony="plan-assemble",
        module="coordinator_core.plan_assemble",
        state="emitted",
    ),
    DoctypeHostRow(
        type="sizing-object",
        ceremony="sizing-assemble",
        module="coordinator_core.sizing_assemble",
        state="emitted",
    ),
    DoctypeHostRow(
        type="review-findings",
        ceremony="review-assemble",
        module="coordinator_core.review_assemble",
        state="emitted",
    ),
    DoctypeHostRow(
        type="goal",
        ceremony="goals",
        module="coordinator_core.goals",
        state="emitted",
    ),
    DoctypeHostRow(
        type="goal-seed",
        ceremony="goals",
        module="coordinator_core.goals",
        state="emitted",
    ),
    DoctypeHostRow(
        type="health-status",
        ceremony="plugin-health",
        module="coordinator_core.plugin_health",
        state="emitted",
    ),
    DoctypeHostRow(
        type="run-report",
        ceremony="execute-plan-assemble",
        module="coordinator_core.execute_plan_assemble",
        state="emitted",
    ),
    DoctypeHostRow(
        type="decision",
        ceremony="backlog-grind-assemble",
        module="coordinator_core.backlog_grind_assemble",
        state="emitted",
    ),
    DoctypeHostRow(
        type="review-findings",
        ceremony="persona-review-dispatch",
        module=None,
        state="excluded",
        reason="prohibited-by-doctrine",
        citation=(
            "DoE-claude coordinator/docs/wiki/review-integration-doctrine.md:"
            "163-170 @ 4b4cb1f0a95b7afb9e30410d1cbd64bc3969522c — a dispatched"
            " persona writes into its pre-provisioned sidecar; the doctrine"
            " states 'No sentinel-append self-scaffold' once a path has"
            " arrived pre-provisioned, i.e. this ceremony must not scaffold"
            " review-findings itself."
        ),
    ),
    DoctypeHostRow(
        type="plan-coverage-check",
        ceremony="plan-preflight-review",
        module=None,
        state="excluded",
        reason="prohibited-by-doctrine",
        citation=(
            "DoE-claude coordinator/agents/plan-coverage-checker.md:36 @"
            " 587401187b228ba4baea830382565f798c53c48f — 'Sidecar path is"
            " provisioned (report_sidecar:) ... Never find/compute a"
            " fallback', and line 229: 'pre-provisioned, no scaffold step'."
        ),
    ),
    DoctypeHostRow(
        type="problem-set",
        ceremony=None,
        module=None,
        state="excluded",
        reason="no-ceremony-module",
    ),
    DoctypeHostRow(
        type="audit-record",
        ceremony=None,
        module=None,
        state="excluded",
        reason="no-ceremony-module",
    ),
    DoctypeHostRow(
        type="strategic-self-description",
        ceremony=None,
        module=None,
        state="excluded",
        reason="no-ceremony-module",
    ),
)


def emitted_rows() -> tuple[DoctypeHostRow, ...]:
    return tuple(row for row in DOCTYPE_HOSTS if row.state == "emitted")


def excluded_rows() -> tuple[DoctypeHostRow, ...]:
    return tuple(row for row in DOCTYPE_HOSTS if row.state == "excluded")


def rows_for_module(module: str) -> tuple[DoctypeHostRow, ...]:
    return tuple(row for row in DOCTYPE_HOSTS if row.module == module)
