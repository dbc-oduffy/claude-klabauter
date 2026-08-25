"""Section porter — roadmap DAG nodes + edges.

Envelope keys: ``roadmap_dag_nodes`` (nodes) and ``roadmap_dag_edges`` (edges).

Emits RoadmapDagNode and RoadmapDagEdge records per roadmap by calling
``ctx.assembler_dag(roadmap_id)`` for each roadmap with a non-null ``roadmap_id``.
Enumerates the same roadmap set that ``sections/roadmaps.py`` uses — the
``roadmap`` record type resolved against the makima working tree
(``ctx.subprocess_root`` when set, else ``ctx.central_state_root.parent``,
mirroring the roadmaps porter's cwd/``--root`` resolution).

Node-subprocess retirement: this section originally shelled out to
``node COORDINATOR_ROOT/bin/query-records.js --type roadmap``. It now calls
``coordinator_core.ops.ceremony.records_query.query_records`` in-process —
no ``node`` binary, no subprocess spawn (mirrors the repoint precedent in
``sections/cross_repo_memos.py``). The seam's ``_load_record`` already applies
``_TYPE_TO_GLOB``'s ``roadmap`` wildcard-dir glob (``state/roadmap/*/OVERVIEW.md``,
ported from ``query-records.js``'s ``_buildTypeToGlob``, bin/query-records.js:211-272,
walked via the ``_walk_glob_segments`` scandir-alphasort port of
``walkSegments``/``filePatternToRegex``, bin/query-records.js:762-820/822-831) and
``_normalize_roadmap_status`` (port of ``normalizeRoadmapStatus``,
bin/query-records.js:1052-1080) before this section ever sees a record — same
ordering the oracle applies, now inherited from the seam rather than re-run here.
Return shape is unchanged (``{"path", "frontmatter"}`` per record), so
``normalize_frontmatter`` below needs no adjustment for the new transport.

Records are tagged ``kind='node'|'edge'`` as an internal routing token for the
envelope's explicit ``place`` fn (registered in ``envelope._wire_sections``).
The tag is stripped before any record lands in the envelope — it is NOT a
contract field on RoadmapDagNode or RoadmapDagEdge.

Malformed rows (F7 — Patrik review):
  ``assemble_roadmap_dag`` self-quarantines field-level problems internally and
  returns clean nodes/edges.  Malformed rows in this section's output are produced
  ONLY when the assembler raises an exception for a specific roadmap_id.  On the
  happy path the malformed buckets are present-but-empty.  The wrapping try/except
  per roadmap_id makes this explicit.

Provenance (F9 — as-implemented; see plan § C1 F9 for deviation rationale):
  nodes → source_kind='local_fs', path='', derivation='parsed'.
    The assembler does not expose individual stub handoff paths in its return value;
    using path='' gracefully omits content_hash stamping via the _stamp_content_hash
    skip-on-empty path.  derivation='parsed' is correct because each node originates
    from a single parsed stub handoff file.
    RAG INVALIDATION TRADEOFF: because path='' is used (rather than the per-node stub
    handoff path), _stamp_content_hash is skipped and no content_hash is attached.  This
    means rag cannot content-hash-invalidate its node projections on stub file changes —
    a changed stub produces different node output but rag sees no content_hash delta and
    re-ingests fully.  Correctness is unaffected; invalidation granularity is reduced.
    Extending assemble_roadmap_dag's return shape to expose per-stub paths is out of scope
    for this landing; tracked in the D3 DoE-confirm memo.
  edges → source_kind='local_fs', path='', derivation='computed'.
    Edges are derived from the blocks arrays across multiple stub files — no single
    source file, so path='' and derivation='computed' per the contract rule
    (content_hash omitted for computed rows).

Node identity: (repo, roadmap_id, stub_id).
Edge identity: (repo, roadmap_id, from, to).
coordinator_root_path is additive on both — not part of logical identity (F6).

Spec backlink: pln-emit-first-class-roadmap-dag-i-137a28 § C1
Parity oracle: none — new record types; Python-authoritative per D3.

Negative-spec:
  - Does NOT call assemble_roadmap_dag directly — always goes via ctx.assembler_dag()
    (D2 instance-level memo; avoids duplicate computation with sections/roadmaps.py).
  - Does NOT touch sections/roadmaps.py (peer chunk C2 owns that file).
  - Does NOT emit phantom nodes for dangling edges — the assembler drops dangling edges
    before returning; this section passes through the assembler's clean output.
  - Does NOT duplicate a malformed row into both buckets (F7).
"""

from __future__ import annotations

from coordinator_core.ops.ceremony.records_query import query_records as _records_query
from coordinator_core.ops.emit.context import EmitContext

from ._shared import normalize_frontmatter


def _query_roadmap_records(ctx: EmitContext) -> list[dict]:
    """Enumerate ``roadmap`` records via the in-process records-query seam.

    Mirrors roadmaps.py._query_roadmap_records exactly: resolves the same worktree
    root ``ctx.subprocess_root`` when set (frozen-fixture doctrine — mirrors the
    ``--root`` pattern in sections/handoffs.py, lessons.py, etc.), else
    ``ctx.central_state_root.parent`` (the makima working tree the roadmap stub
    handoffs live under, matching the retired subprocess's ``cwd``/git-toplevel
    resolution). Any failure degrades to an empty list, never aborts emit — same
    fail-open contract the node-subprocess call carried.
    """
    worktree_root = (
        ctx.subprocess_root if ctx.subprocess_root is not None else ctx.central_state_root.parent
    )
    try:
        data = _records_query("roadmap", worktree_root)
    except (OSError, ValueError, SystemExit):
        return []
    return data if isinstance(data, list) else []


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build (records, malformed) for roadmap DAG nodes and edges.

    Enumerates roadmaps via query-records.js, skips any without a non-null roadmap_id,
    then calls ctx.assembler_dag(roadmap_id) for each.  Records are tagged
    kind='node'|'edge' for envelope routing; malformed rows are tagged kind='node'
    (assembler failures produce a single node-bucket entry per failed roadmap_id).

    Returns:
        records  — list of node+edge dicts, each with kind='node'|'edge' tag.
        malformed — list of quarantine dicts, each with kind='node' tag (only
                    populated when ctx.assembler_dag raises for a roadmap_id).
    """
    raw = _query_roadmap_records(ctx)

    records: list[dict] = []
    malformed: list[dict] = []

    for rec in raw:
        if not isinstance(rec, dict):
            continue
        fm = normalize_frontmatter(rec)
        roadmap_id = fm.get("roadmap_id")
        if not roadmap_id:
            # Roadmaps without roadmap_id are not DAG candidates; skip silently.
            continue

        roadmap_id = str(roadmap_id)

        try:
            dag = ctx.assembler_dag(roadmap_id)
        except Exception as exc:
            # Assembler failure for this roadmap_id — quarantine as a node-bucket malformed row.
            # Do NOT produce an edge-bucket row (F7: no duplication into both buckets).
            malformed.append({
                "kind": "node",
                "roadmap_id": roadmap_id,
                "reason": f"assembler failure: {exc}",
            })
            continue

        for node in dag.get("nodes") or []:
            records.append({
                "kind": "node",
                "repo": ctx.repo_name,
                "coordinator_root_path": ".",
                "roadmap_id": node.get("roadmap_id") or roadmap_id,
                "stub_id": node.get("stub_id"),
                "status": node.get("status"),
                "sprint": node.get("sprint"),
                "wave": node.get("wave"),
                "shipped_sha": node.get("shipped_sha"),
                # Provenance (F9): local_fs + parsed; path='' because the assembler does
                # not expose individual stub handoff paths — content_hash omitted gracefully.
                "provenance": ctx.provenance("local_fs", path="", derivation="parsed"),
            })

        for edge in dag.get("edges") or []:
            records.append({
                "kind": "edge",
                "repo": ctx.repo_name,
                "coordinator_root_path": ".",
                "roadmap_id": edge.get("roadmap_id") or roadmap_id,
                "from": edge.get("from"),
                "to": edge.get("to"),
                "type": "blocks",
                # Provenance (F9): computed — no single source file for an edge.
                "provenance": ctx.provenance("local_fs", path="", derivation="computed"),
            })

    return records, malformed
