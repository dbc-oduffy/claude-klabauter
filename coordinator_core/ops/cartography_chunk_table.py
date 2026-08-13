"""
coordinator_core.ops.cartography_chunk_table — JSON-RPC "cartography.chunk_table"
operation.

Purpose: the DR-228 § D6 scratch-tier writer coordinator-claude's memo asks for
(cross-repo/inbox/2026-08-06-coordinator-claude-em-cartography-chunk-table-producer-
seam.md): computes ``coordinator_core.cartography.chunk_table.compute_chunk_table``
over a caller-supplied ``target_root`` + caller-supplied ``systems`` boundary map
and, on ``emit=true``, writes ONE ``schema_version``-pinned JSON artifact to
disk and stops. A path on disk is the fix for the memo's actual defect: their
Workflow script invokes cartography ops through an LLM agent (no subprocess
primitive of their own), and the harness silently offloaded a ~515KB reply as
a pointer object their code couldn't dereference. Writing the reduction to a
file crosses both the LLM-transport boundary and the process boundary in one
shot (no Node binding to coordinator_core exists) — their consumer side
already has ``fanout.poll_scratch_dir`` to read it back.

Fourth DR-228 § D6 scratch-tier writer (amended alongside ``distill.scope``,
``distill.curation_status``, ``memo.fate_partition`` — see
docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6
amendment). Recomputable-from-disk-truth (the D6 sufficiency statement's
governing property): a chunk table over an unmodified tree with the same
``systems``/``chunk_size`` params is byte-identical run over run, so a
last-writer-wins overwrite race is tolerable by construction, same as
``curation_status``.

Target-resolution model: mirrors the rest of the ``cartography.*`` family
(``cartography.tree`` et al, ``op_scopes.py``'s "cartography.* — fleet-generic,
COMPUTE_ONLY ops" comment) — an explicit caller-supplied ``target_root`` wire
param, ANY repo, not the caller's own dispatching tree; ``repo_root`` is
ignored entirely; scope "none". The one difference from its COMPUTE_ONLY
siblings: this op is MUTATING (DR-208 fail-closed default: any write
disqualifies COMPUTE_ONLY) because ``emit=true`` writes to disk — see
``coordinator_core/authz/classification.py``'s DR-208 five-question
affirmation block for this op, citing DR-228 § D6(i)-(v).

Write target: ``<target_root>/state/scratch/cartography-chunk-table/<run_id>/
chunk-table.json`` — write-confined (D6(i)) to this op's own named,
run-id-scoped subdirectory; never a sibling run-id's directory, never any
other ``state/`` path. ``run_id`` is a caller-supplied path segment (never
op-generated, mirroring ``distill.scope``/``memo.fate_partition``'s
determinism contract — AC1-shaped: this op never mints an id from wallclock),
validated via ``coordinator_core.ops._path_guard.safe_id`` before path
interpolation.

Composition, not reinvention: all business logic lives in
``coordinator_core.cartography.chunk_table`` (pure, no I/O); this module is
the thin RPC wrapper + the one disk write, mirroring
``coordinator_core.ops.distill_scope``'s handler/compute_scope split.

Wire params:
    target_root (str, required)  — containment root to reduce. Guarded via
                                    coordinator_core.cartography._guard.path_guard.
    systems     (dict[str, list[str]], optional) — caller-supplied
                                    {system_name: [path_prefix, ...]} boundary
                                    map (the load-bearing param the memo asks
                                    for). Defaults to {} — every source file
                                    lands in "unbucketed" rather than this op
                                    inventing a directory-name-derived
                                    boundary (that would reproduce the exact
                                    defect the memo describes).
    chunk_size  (int, optional, default 25) — fixed chunk size per bucket.
    run_id      (str, required)  — caller-supplied, safe_id-validated path
                                    segment; confines the emit write to this
                                    run's own subdirectory (D6(i)). Required
                                    unconditionally (even when emit is
                                    false/absent) so a bare compute-only call
                                    and an emitting call share one param
                                    contract — no separate un-runid'd shape.
    emit        (bool, optional, default false) — controls the disk write
                                    (DR-228 § D6 scratch-tier convention,
                                    mirrors distill_curation_status's --emit).
    oversized_threshold (int, optional) — positive int line-count threshold.
                                    Absent by default: no file is opened,
                                    schema_version is unchanged, emitted bytes
                                    are byte-identical to a pre-this-param run
                                    (AC1). When supplied, adds an `oversized`
                                    reply field and bumps schema_version to
                                    SCHEMA_VERSION_OVERSIZED (see that
                                    constant's docstring for why the bump is
                                    gated rather than unconditional). Caller-
                                    supplied deliberately — the consumer's
                                    Read-truncation budget (currently 800) is
                                    a property of the consumer, not this repo,
                                    so this op does not bake in a default.

Reply fields:
    schema_version, run_id, target_root, systems (echoed), chunk_size, counts
    — always present. ``buckets``/``unbucketed`` are present ONLY on a
    compute-only (``emit`` false) call; an emitting call returns
    ``chunk_table_path`` (rel-posix to target_root) INSTEAD of them.
    ``oversized`` is present ONLY when ``oversized_threshold`` was supplied,
    regardless of ``emit`` (unlike ``buckets``/``unbucketed``, it is never
    stripped from an emitting call's reply — it is already the small,
    threshold-filtered shape the emit-vs-compute asymmetry above exists to
    protect against, not the oversized body itself).

    That asymmetry is the point of the op, not an oversight: returning the
    full reduction alongside the file would hand an emitting caller the same
    oversized reply that broke coordinator-claude's pipeline (a ~515KB body their harness
    offloaded to a pointer object). An emitting caller has already said it
    wants the artifact on disk — the reply stays small enough to survive any
    transport, and the payload is read back from ``chunk_table_path``.
    A caller that genuinely wants the body inline asks for it by omitting
    ``emit``.

Negative-spec: performs no LLM call, no system-boundary inference, no commit,
no delete — see coordinator_core.cartography.chunk_table's own negative-spec
for the pure-computation boundary this handler wraps.

Spec backlink: cross-repo/inbox/2026-08-06-coordinator-claude-em-cartography-chunk-table-producer-seam.md
Governing DR: docs/decisions/DR-228-distill-disposal-substrate-writer-category.md § D6

Consumption status: CONSUMED — one of only two of nine cartography op names
with a real call site (``emit: true``) in the survey's Workflow script today
(docs/plans/2026-08-06-claude-klabauter-ize-the-survey-census.md § "The survey calls
two of nine cartography op names"); its consumer side is
``fanout.poll_scratch_dir`` (see "Purpose" above).
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional

from coordinator_core.cartography._guard import path_guard
from coordinator_core.cartography.chunk_table import compute_chunk_table
from coordinator_core.cartography.tree import _loc_for
from coordinator_core.ipc import register_op
from coordinator_core.ops._path_guard import safe_id

__all__ = [
    "SCHEMA_VERSION",
    "SCHEMA_VERSION_OVERSIZED",
    "build_chunk_table_artifact",
    "write_chunk_table",
]

#: Schema version for this op's emitted JSON artifact — always the first key
#: written on disk (mirrors coordinator_core.distill.manifest_schema's
#: convention, DR-228 § D6(v)). A standalone constant (not manifest_schema's
#: SCHEMA_VERSION) because this artifact is a genuinely different shape
#: (chunk-table, not scope/disposal/curation-status) with its own independent
#: version lineage. This is the version emitted when `oversized_threshold` is
#: absent — unchanged, so an absent-param call stays byte-identical to every
#: prior run (AC1).
SCHEMA_VERSION: int = 1

#: Schema version emitted ONLY when the caller supplies `oversized_threshold`
#: (the additive `oversized` field). Gated on the param rather than an
#: unconditional bump: coordinator-claude's forward-version fail-loud consumer declines any
#: run on an unrecognized schema_version, so bumping unconditionally would
#: trip their gate on the default (no-threshold) path for zero delivered
#: benefit — staff-eng review Finding 0, 2026-08-06.
#:
#: A member of `_KNOWN_SCHEMA_VERSIONS` below, so `check_schema_version`
#: accepts an artifact this module itself just emitted — an op whose own
#: reader rejects its own output is a latent break for the first real
#: caller, not a deferrable gap. Adding a future version means adding it to
#: `_KNOWN_SCHEMA_VERSIONS`, not just defining a new constant here.
SCHEMA_VERSION_OVERSIZED: int = 2

#: Every schema_version this module can consume without failing loud — the
#: set `check_schema_version` validates against, not a single ceiling. A
#: version outside this set is unknown-forward by definition, whether or not
#: it happens to be numerically less than some other known version.
_KNOWN_SCHEMA_VERSIONS: frozenset[int] = frozenset({SCHEMA_VERSION, SCHEMA_VERSION_OVERSIZED})


class ChunkTableSchemaError(ValueError):
    """Raised when a chunk-table JSON artifact carries an unknown FORWARD
    schema_version (newer than this module knows) — fail-loud consumption,
    per DR-228 § D6(v)."""


def check_schema_version(payload: dict[str, Any]) -> None:
    """Fail loud on an unknown forward schema_version; silent on a known
    version — DR-228 § D6(v)'s consumption contract.

    Checked against `_KNOWN_SCHEMA_VERSIONS` (currently `{SCHEMA_VERSION,
    SCHEMA_VERSION_OVERSIZED}`), not a single `version > ceiling` comparison
    — this module has more than one artifact shape it can validly emit."""
    version = payload.get("schema_version")
    if not isinstance(version, int) or version not in _KNOWN_SCHEMA_VERSIONS:
        raise ChunkTableSchemaError(
            f"cartography.chunk_table artifact has unknown forward schema_version "
            f"{version!r} (this module knows {sorted(_KNOWN_SCHEMA_VERSIONS)!r})"
        )


def build_chunk_table_artifact(
    target_root: str | Path,
    *,
    run_id: str,
    systems: dict[str, list[str]] | None,
    chunk_size: int,
    oversized_threshold: int | None = None,
) -> dict[str, Any]:
    """Compute the chunk-table result and shape it into the schema_version-pinned
    artifact dict (schema_version first, per D6(v)'s "always the first key
    written on disk" contract).

    `oversized_threshold` is absent by default: when absent, no file is
    opened, `schema_version` stays `SCHEMA_VERSION`, and the emitted dict is
    byte-identical (once JSON-encoded) to a pre-`oversized_threshold` run —
    AC1's byte-identity guarantee. When supplied, this reads every bucketed
    and unbucketed file's line count via `cartography.tree._loc_for` (already
    binary-safe: an undecodable file yields `None`, never a coerced `0`, and
    a `None` loc is NOT oversized — unknown is not "over") and emits
    `oversized: [<relpath>, ...]`, the paths at or above the threshold,
    sorted; `schema_version` bumps to `SCHEMA_VERSION_OVERSIZED` for this
    additive field, gated on the param rather than unconditional (see that
    constant's docstring).

    Deliberately does NOT add a per-file `loc` map to the artifact body, for
    two independent reasons: (1) the artifact already carries every path
    twice (`buckets[].files` and `buckets[].chunks`) — a third per-file
    listing widens the body for a value this op's one consumer does not read
    back; (2) the consumer's readback is an LLM call, not a filesystem read
    (cross-repo/inbox/2026-08-06-coordinator-claude-em-cartography-chunk-table-
    consumer-wired.md), so there is no reader positioned to exploit a
    ready-made loc index the way a script would. This scopes the anti-scope
    to the artifact's *per-file record* only — not to loc computation itself,
    which this function already pays for once `oversized_threshold` is
    supplied. A loc-weighted `chunk_list` (balancing sub-chunks by total loc
    instead of file count) is a named, undischarged follow-on, not built in
    this chunk (staff-eng review Finding 11, 2026-08-06).
    """
    result = compute_chunk_table(target_root, systems, chunk_size)
    artifact: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "target_root": str(target_root),
        "systems": dict(systems) if systems else {},
        "chunk_size": chunk_size,
        "buckets": result.buckets,
        "unbucketed": result.unbucketed,
        "counts": result.counts,
    }
    if oversized_threshold is not None:
        artifact["schema_version"] = SCHEMA_VERSION_OVERSIZED
        root = Path(target_root)
        all_paths = [p for bucket in result.buckets.values() for p in bucket["files"]]
        all_paths.extend(result.unbucketed)
        oversized: list[str] = []
        for relpath in all_paths:
            loc = _loc_for(root / relpath)
            if loc is not None and loc >= oversized_threshold:
                oversized.append(relpath)
        artifact["oversized"] = sorted(oversized)
    return artifact


def write_chunk_table(target_root: Path, run_id: str, artifact: dict[str, Any]) -> Path:
    """Write the chunk-table artifact to
    <target_root>/state/scratch/cartography-chunk-table/<run_id>/chunk-table.json,
    atomically (mkstemp + os.replace, DR-228 § D6(ii) create-or-full-rewrite
    only — never a partial in-place edit of an existing artifact).

    Write-confined (D6(i)): only this run-id's own subdirectory under
    state/scratch/cartography-chunk-table/ is touched.
    """
    run_dir = target_root / "state" / "scratch" / "cartography-chunk-table" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "chunk-table.json"

    body = json.dumps(artifact, indent=2).encode("utf-8")
    fd, tmp_path = tempfile.mkstemp(dir=str(run_dir), suffix=".tmp")
    try:
        try:
            os.write(fd, body)
        finally:
            os.close(fd)
        os.replace(tmp_path, str(target))
        tmp_path = None
    finally:
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
    return target


@register_op("cartography.chunk_table")
def _cartography_chunk_table(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "cartography.chunk_table" handler.

    See module docstring "Wire params" / "Reply fields". `repo_root` is
    ignored entirely (scope "none", same target-resolution model as the rest
    of the cartography.* family — target_root is the only root that matters).
    """
    target_root = params.get("target_root")
    if not target_root:
        raise ValueError("cartography.chunk_table requires param: target_root")

    run_id = params.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("cartography.chunk_table requires a non-empty string param: run_id")
    if not safe_id(run_id):
        raise ValueError(f"cartography.chunk_table: run_id is not a safe path segment: {run_id!r}")

    systems = params.get("systems") or {}
    if not isinstance(systems, dict):
        raise ValueError("cartography.chunk_table: systems must be a {system_name: [prefix, ...]} dict")

    chunk_size = params.get("chunk_size", 25)
    if not isinstance(chunk_size, int) or chunk_size < 1:
        raise ValueError("cartography.chunk_table: chunk_size must be a positive int")

    emit = bool(params.get("emit", False))

    oversized_threshold = params.get("oversized_threshold")
    if oversized_threshold is not None:
        if (
            not isinstance(oversized_threshold, int)
            or isinstance(oversized_threshold, bool)
            or oversized_threshold < 1
        ):
            raise ValueError(
                "cartography.chunk_table: oversized_threshold must be a positive int"
            )

    guarded_root = path_guard(target_root, ".")
    artifact = build_chunk_table_artifact(
        guarded_root,
        run_id=run_id,
        systems=systems,
        chunk_size=chunk_size,
        oversized_threshold=oversized_threshold,
    )

    if not emit:
        return dict(artifact)

    written_path = write_chunk_table(guarded_root, run_id, artifact)
    result = {k: v for k, v in artifact.items() if k not in ("buckets", "unbucketed")}
    result["chunk_table_path"] = written_path.resolve().relative_to(guarded_root).as_posix()
    return result
