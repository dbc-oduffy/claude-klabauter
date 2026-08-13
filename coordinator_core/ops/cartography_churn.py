"""
coordinator_core.ops.cartography_churn — JSON-RPC "cartography.churn" operation.

Purpose: Thin RPC wrapper over coordinator_core.cartography.churn's pure
emergent-set computation. Shells out to git (read-only queries) to derive the
three input path-lists the pure primitive needs (churned_all / catalogued /
head_present), then delegates the actual set-arithmetic + mitigations to
coordinator_core.cartography.churn.compute_emergent_set. Promotes coordinator-claude survey
chunk-K (architecture-survey.md:104-120) into a tested, reusable op.

Self-registration: importing this module calls register_op("cartography.churn",
...) as a side-effect — same pattern as ops/ping.py. This op is LIVE on the
dispatch path: coordinator_core.ops.__init__ imports this module, and the other
three shared seams (authz/classification.py, ipc.py _OP_KEY_SCOPE,
coordinator_core/benchmarks/budget-manifest.json) all carry a "cartography.churn"
entry.

Fleet-generic, COMPUTE_ONLY, scope-"none" op (coordinator-core-op-target-
resolution-model: explicit target_root param, any repo, NOT the caller's own
dispatching tree) — mirrors ops/ping.py's registration shape and
ops/roadmap_dag.py's {nodes, edges}-style plain-dict emission convention.

Read-only git precedent: all subprocess calls here are read-only git queries
(`git log --name-only`, `git ls-files`) — same COMPUTE_ONLY classification
precedent as ops/coverage_gate.py's underlying coordinator_core.coverage
module (DR-208's own table: "all subprocess calls are read-only git queries").

Wire params:
    target_root    (str, required) — root of the repo tree to compute churn
                                      against. Path-guarded via
                                      cartography._guard.path_guard so a
                                      caller-supplied traversal cannot escape
                                      the given root.
    since           (str, required) — git log --since=<since> date/ref
                                       expression (the oldest catalogued
                                       system's Last-mapped date, per chunk-K).
    system_dirs     (list[str], required) — catalogued system directories to
                                       scope the "catalogued" diff to (the
                                       chunk-K `<system-dirs>` list).
    excluded_dirs   (list[str], optional) — source-dir prefilter list
                                       (mitigation (c)); caller-suppliable.
                                       NO IMPLICIT DEFAULT is applied when
                                       omitted — an empty prefilter is used
                                       instead of
                                       cartography.churn.DEFAULT_EXCLUDED_DIRS
                                       ("docs/", "tasks/", "archive/").
                                       Chunk C9/2026-08-06: the tree-specific
                                       default had outlived the tree it was
                                       cut for (A5/2026-08-06 finding: on
                                       coordinator-claude's tree the emergent set at that
                                       default was 82% noise from state/,
                                       cross-repo/, and dist/ — none of which
                                       the default names), and both sides
                                       converged on caller-supplies-the-set
                                       rather than either side re-guessing a
                                       shared default. Every caller — this
                                       repo's survey included — must now pass
                                       an explicit excluded_dirs reflecting
                                       its own tree (e.g. ["docs/", "tasks/",
                                       "archive/", "state/", "cross-repo/",
                                       "dist/"]); a caller that omits it gets
                                       NO prefiltering, not the old default.

Reply fields (result object in JSON-RPC response):
    emergent               (list[str]) — final emergent (changed-but-
                                          uncatalogued) file set.
    excluded_by_prefilter  (list[str]) — diagnostic: paths dropped by the
                                          source-dir prefilter.
    deleted_at_head        (list[str]) — diagnostic: paths dropped because
                                          they no longer exist at HEAD.
    churn_ratio            (float)     — bounded to [0.0, 1.0] BY
                                          CONSTRUCTION: numerator and
                                          denominator are drawn from the SAME
                                          population (files tracked under
                                          system_dirs at HEAD), never from
                                          the raw `--name-only` diff, which
                                          also carries renamed-from and
                                          deleted paths no longer at HEAD.
                                          Concretely: numerator is the
                                          catalogued churn-diff list
                                          INTERSECTED with catalogued_at_head
                                          (files tracked under system_dirs at
                                          HEAD, via `git ls-files`);
                                          denominator is catalogued_at_head's
                                          count. A path that was renamed or
                                          deleted within the window cannot
                                          inflate the numerator past the
                                          denominator's population, so the
                                          ratio can never exceed 1.0. 0.0 when
                                          catalogued_count is 0 (never raises
                                          ZeroDivisionError). This is the
                                          number the chunk-K threshold
                                          ("churn > 50% of catalogued") is
                                          applied to by the caller — the op
                                          returns the ratio and its
                                          denominator, never the verdict
                                          (see Negative-spec). Numerator
                                          POPULATION (system_dirs-scoped vs
                                          tree-wide "total churned files" per
                                          architecture-survey.md:120) is a
                                          separate, cross-repo question —
                                          NOT addressed by this bound-fix; see
                                          the cross-repo memo on chunk-K's
                                          numerator population.
    catalogued_count        (int)      — the denominator churn_ratio was
                                          computed from: count of files
                                          tracked at HEAD (`git ls-files`)
                                          under system_dirs.
    uncatalogued            (list[str]) — NEW (chunk C3, additive). Sorted
                                          paths, at file-level granularity,
                                          that the RECORDED mapping rule
                                          (docs/architecture/file-index.md
                                          state, frozen at `last_mapped`)
                                          maps to no system at all. Answers
                                          "outside any recorded system".
                                          Derived from the RECORDED atlas,
                                          NEVER from a live re-evaluation of
                                          the mapping rule. `[]` when the
                                          atlas is unreadable (see
                                          `atlas_unreadable` below).
    drifted_systems         (list[dict]) — NEW (chunk C3, additive). One
                                          dict per catalogued system whose
                                          LIVE tracked-file membership
                                          (under the RECORDED mapping rule)
                                          diverges from its RECORDED
                                          `files:` fingerprint — per-system
                                          count granularity, not file-level.
                                          Each dict: {"system": str,
                                          "recorded_files": int,
                                          "live_files": int, "delta": int}.
                                          Answers "inside a recorded system
                                          but not in its recorded
                                          membership" — a question
                                          `uncatalogued` cannot answer
                                          (that field only sees files
                                          outside every recorded system).
                                          A system present in the recorded
                                          atlas with zero live members
                                          still reports here (vanished
                                          system = drift). `[]` when the
                                          atlas is unreadable.
    last_mapped              (str|None) — NEW (chunk C3, additive). The
                                          recorded stamp `uncatalogued` and
                                          `drifted_systems` are frozen
                                          against, so a consumer can see the
                                          reference point the atlas claims
                                          to be current as of — never the
                                          op's own invocation time. `None`
                                          when the atlas is unreadable.
    catalogued_source_count  (int)     — NEW (chunk C3, additive). The
                                          stated denominator for
                                          `uncatalogued`/`drifted_systems`
                                          (RecordedExpansion.
                                          considered_count) — distinct from
                                          `catalogued_count` (the
                                          churn_ratio denominator, unchanged
                                          meaning), so a consumer can tell
                                          "3 of 1225" from "3 of 12" against
                                          the RIGHT population for each
                                          pair of fields. `0` when the atlas
                                          is unreadable.
    atlas_unreadable          (dict, optional) — NEW (chunk C3, additive).
                                          Present ONLY when the recorded
                                          atlas (docs/architecture/
                                          file-index.md + system pages)
                                          could not be loaded/parsed:
                                          {"reason": <str|None>}. An
                                          explicit discriminated failure —
                                          `uncatalogued`/`drifted_systems`
                                          are `[]` and `last_mapped`/
                                          `catalogued_source_count` are
                                          `None`/`0` in this case, never a
                                          silent empty result mistaken for
                                          "fully catalogued". Absent
                                          entirely on a successful atlas
                                          load.

The four NEW fields above are ADDITIVE ONLY. `emergent`,
`excluded_by_prefilter`, `deleted_at_head`, `churn_ratio`, and
`catalogued_count` keep their pre-existing semantics unchanged (AC7) — see
Negative-spec.

Spec backlink: pln-claude-klabauter-cartography-substrate-a-26eb2e
§ chunk C3 (cartography.churn); extended additively by
docs/plans/2026-08-06-churn-emergent-detection-file-granularity.md § chunk C3.

Consumption status: CONSUMED — one of only two of nine cartography op names
with a real call site (refresh mode, coordinator-claude's survey.workflow.js chunk-K gate)
in the survey's Workflow script today
(docs/plans/2026-08-06-claude-klabauter-ize-the-survey-census.md § "The survey calls
two of nine cartography op names").

Negative-spec:
  - Does NOT apply the chunk-K threshold decision (emergent non-empty OR
    churn_ratio > 0.5) — that policy belongs to the Phase-0.5 consume-gate
    (coordinator-claude-owned, Part B, not executed here); this op only returns the
    computed sets plus the ratio and its denominator, never a boolean,
    threshold, verdict, or "action recommended" field (A5/2026-08-06: the
    decision-application boundary — op returns the number, caller keeps the
    judgment — is load-bearing and written into coordinator-claude's contract for this op
    family).
  - Does NOT write to any fleet store or state/ path — pure compute-and-
    return, no disk artifact (unlike ops/coverage_gate.py's cockpit-facing
    artifact write).
  - Does NOT change `emergent`'s semantics (churned paths outside
    system_dirs, via the diff-window mechanism) — `uncatalogued` is a
    SEPARATE, RECORDED-atlas-derived field, not a replacement or
    redefinition. Existing consumers gating on `emergent`'s length (e.g.
    coordinator-claude's survey.workflow.js chunk-K gate) see no behaviour change.
  - Does NOT re-derive or touch `churn_ratio`'s numerator/denominator —
    the new `catalogued_source_count` is a DIFFERENT denominator
    (RecordedExpansion.considered_count), never conflated with
    `catalogued_count`.
  - Does NOT touch disk itself for the new fields — delegates the sole
    disk-touching call to
    coordinator_core.cartography.atlas_record.load_recorded_atlas, and
    reuses the existing tree-wide `git ls-files` result already computed
    for `head_present` rather than issuing a second git call.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Optional

from coordinator_core.cartography._guard import path_guard
from coordinator_core.cartography.atlas_record import (
    ATLAS_UNREADABLE,
    expand_recorded_mapping,
    load_recorded_atlas,
)
from coordinator_core.cartography.churn import (
    compare_against_recorded_atlas,
    compute_emergent_set,
)
from coordinator_core.ipc import register_op
from coordinator_core.win_portability import no_console_creationflags

_GIT_TIMEOUT_SECS = 30


def _git_name_only(root: Path, since: str, system_dirs: Optional[list[str]] = None) -> list[str]:
    """Return sorted-unique file paths from `git log --since=<since> --name-only`.

    Mirrors architecture-survey.md:104-120's `churned-all.txt` /
    `catalogued.txt` derivation: if *system_dirs* is given, the diff is
    scoped to those directories via `-- <dirs...>` (the "catalogued" list);
    otherwise it is tree-wide (the "churned-all" list). Read-only git query.

    Returns an empty list on any subprocess failure (fail-closed: an empty
    catalogued/churned list never fabricates emergent entries it shouldn't —
    worst case an all-tree diff is misreported as fully emergent, which is
    the same fail-open-to-agentic-review posture the bash chunk-K has).
    """
    cmd = ["git", "log", f"--since={since}", "--name-only", "--pretty=format:"]
    if system_dirs:
        cmd.append("--")
        cmd.extend(system_dirs)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT_SECS,
            cwd=root,
            **no_console_creationflags(),
        )
    except Exception:  # noqa: BLE001
        return []
    if proc.returncode != 0:
        return []
    lines = {line.strip() for line in proc.stdout.splitlines() if line.strip()}
    return sorted(lines)


def _git_ls_files(root: Path, system_dirs: Optional[list[str]] = None) -> list[str]:
    """Return the list of file paths tracked at HEAD (`git ls-files`).

    With *system_dirs* omitted: the head-present universe used for the
    deleted-at-HEAD cross-check (mitigation (b)) — tree-wide.

    With *system_dirs* given: scoped via `-- <dirs...>`, used as the
    `catalogued_count` denominator for `churn_ratio` (A5) — the count of
    files actually tracked under the catalogued system directories at HEAD,
    as distinct from `catalogued` (the churned-and-catalogued diff list from
    `_git_name_only`).

    Read-only git query. Returns an empty list on any subprocess failure
    (fail-closed).
    """
    cmd = ["git", "ls-files"]
    if system_dirs:
        cmd.append("--")
        cmd.extend(system_dirs)
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            encoding="utf-8",
            timeout=_GIT_TIMEOUT_SECS,
            cwd=root,
            **no_console_creationflags(),
        )
    except Exception:  # noqa: BLE001
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


@register_op("cartography.churn")
async def _cartography_churn(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'cartography.churn' handler.

    Derives the three chunk-K input path-lists via read-only git queries
    against the caller-supplied (path-guarded) target_root, then delegates
    the emergent-set computation to
    coordinator_core.cartography.churn.compute_emergent_set.

    Params: see module docstring "Wire params".

    Returns:
        {"emergent": [...], "excluded_by_prefilter": [...], "deleted_at_head": [...]}
        or {"error": <str>} if target_root is missing/invalid.
    """
    target_root_raw = params.get("target_root")
    if not target_root_raw:
        return {"error": "cartography.churn requires a non-empty target_root param"}

    try:
        root = path_guard(target_root_raw, ".")
    except Exception as exc:  # noqa: BLE001
        return {"error": f"invalid target_root: {exc}"}

    since = str(params.get("since") or "")
    if not since:
        return {"error": "cartography.churn requires a non-empty since param"}

    system_dirs_raw = params.get("system_dirs") or []
    if isinstance(system_dirs_raw, str):
        system_dirs = [system_dirs_raw] if system_dirs_raw else []
    elif isinstance(system_dirs_raw, list):
        system_dirs = [str(d) for d in system_dirs_raw if d]
    else:
        system_dirs = []
    if not system_dirs:
        return {"error": "cartography.churn requires a non-empty system_dirs param"}

    excluded_dirs_raw = params.get("excluded_dirs")
    if isinstance(excluded_dirs_raw, list):
        excluded_dirs: tuple[str, ...] = tuple(str(d) for d in excluded_dirs_raw)
    else:
        # No implicit default (chunk C9): a caller that omits excluded_dirs
        # gets no prefiltering, never the retired tree-specific default.
        excluded_dirs = ()

    churned_all = await asyncio.to_thread(_git_name_only, root, since)
    catalogued = await asyncio.to_thread(_git_name_only, root, since, system_dirs)
    head_present = await asyncio.to_thread(_git_ls_files, root)
    catalogued_at_head = await asyncio.to_thread(_git_ls_files, root, system_dirs)

    result = compute_emergent_set(
        churned_all=churned_all,
        catalogued=catalogued,
        head_present=head_present,
        excluded_dirs=excluded_dirs,
    )

    catalogued_count = len(catalogued_at_head)
    catalogued_at_head_set = set(catalogued_at_head)
    catalogued_churn_at_head = [p for p in catalogued if p in catalogued_at_head_set]
    churn_ratio = (
        (len(catalogued_churn_at_head) / catalogued_count) if catalogued_count else 0.0
    )

    reply = {
        "emergent": result.emergent,
        "excluded_by_prefilter": result.excluded_by_prefilter,
        "deleted_at_head": result.deleted_at_head,
        "churn_ratio": churn_ratio,
        "catalogued_count": catalogued_count,
    }

    # Additive: RECORDED-atlas comparison (chunk C3, see module docstring).
    from coordinator_core.cartography.atlas_record import (
        ATLAS_UNREADABLE,
        expand_recorded_mapping,
        load_recorded_atlas,
    )

    atlas = load_recorded_atlas(root)
    if atlas.error == ATLAS_UNREADABLE:
        reply["atlas_unreadable"] = {"reason": atlas.error_detail}
        reply["uncatalogued"] = []
        reply["drifted_systems"] = []
        reply["last_mapped"] = None
        reply["catalogued_source_count"] = 0
    else:
        # Reuse the already-computed tree-wide `git ls-files` result
        # (head_present) rather than issuing a second git call.
        expansion = expand_recorded_mapping(head_present, atlas)
        comparison = compare_against_recorded_atlas(expansion, atlas)
        reply["uncatalogued"] = comparison.uncatalogued
        reply["drifted_systems"] = [
            {
                "system": d.system,
                "recorded_files": d.recorded_files,
                "live_files": d.live_files,
                "delta": d.delta,
            }
            for d in comparison.drifted_systems
        ]
        reply["last_mapped"] = comparison.last_mapped
        reply["catalogued_source_count"] = comparison.denominator

    return reply
