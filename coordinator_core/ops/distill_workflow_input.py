"""
coordinator_core.ops.distill_workflow_input — JSON-RPC "distill.workflow_input"
operation.

Purpose: own the ONE translation between distill.scope's producer shape (the
scope-manifest — run_id / flat-list batches / wiki_slugs-as-list, C9's schema)
and the distill-harvest Workflow script's consumer shape (runId /
[{batchId, files, description, formatHints}] / wikiSlugs-as-object-map, plus a
repoRoot the producer never emits). Before this op, an EM hand-wrote this
adapter mid-run — five of five fields disagreed between the two sides, and
only wiki_slugs_as_dict() (distill_scope.py) had a shipped converter for even
one of them. This op is the single seam both the producer (distill.scope) and
the consumer (DoE's distill-harvest.workflow.js) can pin against, so neither
side's next edit silently reopens the hand-adapter.

Integrity counts (2026-08-06 example-retrieval-repo-em incident —
cross-repo/inbox/2026-08-06-example-retrieval-repo-em-distill-workflow-inputfile-silent-
truncation.md): an LLM relay silently returned 14 of 26 batches / 358 of 832
files, and the run self-reported a clean 100% scan-success gate because the
gate diffed the ALREADY-TRUNCATED table against itself. This op's emitted
payload therefore carries ``batch_count``/``total_file_count`` as first-class
declared fields — not derivable-on-request, not buried in a nested stat block
— so a consumer-side assertion (the consumer repo's own change, NOT made
here — see this op's own docstring "Negative-spec") can diff what it actually
received against what THIS op declared it emitted, closing the failure class
the truncation incident exposed. No cap on batch/file count is applied by
this op — NO SILENT CAPS; a caller-supplied ``manifest`` of any size is
translated whole.

Each batch also carries an additive ``relevantGuides`` list (distill.guide_index: the top
guides by term overlap with the batch's files, each with a heading outline); the handler reads
the guides and batch files for it, and translate_to_workflow_input stays pure over the list it is
handed.

Each batch's scan prompt (files or digests, plus listed guides) is held under the long-prompt tier
(distill.prompt_tier): ``fit_scan_tier`` digests an over-tier batch, then splits it, never past the
admission batch cap. The handler's one disk write is those digests, and only when a batch is over tier.

Negative-spec: performs no scan, no cohort computation, no other disk write
(translation over a caller-supplied manifest; the handler's reads are the guide
index above and file sizes), no LLM call, and does NOT add the consumer-side integrity assertion itself —
that assertion is DoE's distill-harvest.workflow.js's own change to make
against this op's batch_count/total_file_count fields; this op only makes the
declared counts available to be checked against.

Spec backlink: pln-claude-klabauter-driven-ceremony-redesig-c7fe9a § C10
Fold-in ask: cross-repo/inbox/2026-08-06-example-retrieval-repo-em-distill-workflow-
inputfile-silent-truncation.md
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from coordinator_core.distill.guide_index import relevant_guides_for_batches
from coordinator_core.distill.manifest_schema import check_schema_version
from coordinator_core.distill.prompt_tier import LONG_PROMPT_TIER_TOKENS, estimate_tokens, over_tier
from coordinator_core.distill.scan_digest import write_digests
from coordinator_core.ipc import register_op
from coordinator_core.ops.distill_scope import RUN_DIR_REL, wiki_slugs_as_dict

__all__ = [
    "CONSUMER_TOP_LEVEL_FIELDS",
    "CONSUMER_BATCH_FIELDS",
    "translate_to_workflow_input",
    "validate_workflow_input",
    "fit_scan_tier",
    "attach_relevant_guides",
    "guide_paths",
    "load_backlog_digests",
    "topic_units",
]

#: Required top-level fields of the CONSUMER (Workflow script) input shape —
#: the drift-detection reference this module's contract test asserts against.
CONSUMER_TOP_LEVEL_FIELDS: tuple[str, ...] = (
    "runId",
    "repoRoot",
    "batches",
    "wikiSlugs",
    "batch_count",
    "total_file_count",
    "wikiDirs",
    "wikiSlugCount",
)

#: Required per-batch fields of the CONSUMER input shape.
CONSUMER_BATCH_FIELDS: tuple[str, ...] = ("batchId", "files", "description", "formatHints")


def load_backlog_digests(repo_root: str | Path, run_id: str) -> dict[str, Any] | None:
    """The backlog pass's ``digests.json`` for ``run_id`` under ``repo_root``, or None when the
    file is absent, unreadable, or lacks a ``topics`` list (callers then keep the per-file path)."""
    from coordinator_core.ops.backlog_triage_pass import OUT_DIR_REL  # noqa: PLC0415

    if not run_id or ".." in run_id or "/" in run_id or "\\" in run_id:
        return None
    try:
        data = json.loads((Path(repo_root) / OUT_DIR_REL / run_id / "digests.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and isinstance(data.get("topics"), list) else None


def topic_units(digests: dict[str, Any]) -> list[dict[str, Any]]:
    """One synthesis unit per TopicDigest, in digest order."""
    return [
        {
            "topic": t["topic_key"],
            "text": t.get("text", ""),
            "tokens": t.get("tokens", 0),
            "paths": list(t.get("paths", [])),
        }
        for t in digests["topics"]
    ]


def _topic_batches(digests: dict[str, Any], hints: dict[str, Any]) -> list[dict[str, Any]]:
    units = topic_units(digests)
    return [
        {
            "batchId": f"topic-{idx}",
            "files": u["paths"],
            "description": f"Topic {idx} of {len(units)}: {u['topic']} ({u['tokens']} tokens)",
            "formatHints": dict(hints),
            "topic": u["topic"],
            "digest": u["text"],
        }
        for idx, u in enumerate(units, start=1)
    ]


def translate_to_workflow_input(
    manifest: dict[str, Any],
    *,
    repo_root: str,
    format_hints: dict[str, Any] | None = None,
    relevant_guides: list[list[dict[str, Any]]] | None = None,
    digests: dict[str, Any] | None = None,
) -> dict[str, Any]:
    check_schema_version(manifest)
    hints = dict(format_hints) if format_hints is not None else {}

    if digests is not None:
        # One unit per topic; the manifest's per-file batches are not consulted.
        topic_batches = _topic_batches(digests, hints)
        wiki_slugs = wiki_slugs_as_dict(manifest)
        return {
            "runId": manifest["run_id"],
            "repoRoot": repo_root,
            "batches": topic_batches,
            "wikiDirs": list(manifest["wiki_dirs"]),
            "wikiSlugs": wiki_slugs,
            "batch_count": len(topic_batches),
            "total_file_count": sum(len(b["files"]) for b in topic_batches),
            "wikiSlugCount": len(wiki_slugs),
        }

    producer_batches: list[list[str]] = manifest["batches"]
    batches_out: list[dict[str, Any]] = []
    for idx, files in enumerate(producer_batches, start=1):
        batch: dict[str, Any] = {
            "batchId": f"batch-{idx}",
            "files": list(files),
            "description": f"Batch {idx} of {len(producer_batches)} ({len(files)} files)",
            "formatHints": dict(hints),
        }
        if relevant_guides is not None:
            batch["relevantGuides"] = _fit_guides(batch, relevant_guides[idx - 1])
        batches_out.append(batch)

    wiki_slugs = wiki_slugs_as_dict(manifest)
    return {
        "runId": manifest["run_id"],
        "repoRoot": repo_root,
        "batches": batches_out,
        "wikiDirs": list(manifest["wiki_dirs"]),
        "wikiSlugs": wiki_slugs,
        "batch_count": len(batches_out),
        "total_file_count": sum(len(b["files"]) for b in batches_out),
        # The Workflow asserts its loaded slug count against this (halt wiki-slugs-dropped):
        # an LLM loader that drops the map would otherwise mint duplicate guides.
        "wikiSlugCount": len(wiki_slugs),
    }


#: Most compact-JSON bytes per `wikislugs-NNNN.json` shard. The loader relays a shard whole
#: to one agent, and that relay caps near 15 KB; an entry count cannot hold that bound,
#: since slug and path lengths vary (100 real entries measured 18,125 bytes).
SLUG_SHARD_MAX_BYTES = 12_000


def _compact(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _fit_guides(batch: dict[str, Any], guides: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """`guides` (best first) trimmed from the tail until the batch's compact JSON fits the relay
    cap; the hint yields so that a batch which fit without it is never refused because of it.
    Whichever of full guides or path-only guides (outline dropped) keeps more entries wins; the
    batch is measured as it stands, so digests attached beforehand are priced in."""

    def trimmed(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
        kept = list(candidates)
        while kept and len(_compact({**batch, "relevantGuides": kept, "file_count": len(batch["files"])}).encode()) > SLUG_SHARD_MAX_BYTES:
            kept.pop()
        return kept

    full = trimmed(guides)
    if len(full) == len(guides):
        return full
    path_only = trimmed([{k: v for k, v in g.items() if k != "outline"} for g in guides])
    return path_only if len(path_only) > len(full) else full


def attach_relevant_guides(
    payload: dict[str, Any], guides: list[list[dict[str, Any]]], *, max_batches: int | None = None
) -> list[str]:
    """Set each batch's ``relevantGuides`` from ``guides`` (one best-first list per batch), fitted
    to the relay cap, after sizing every scan prompt under the long-prompt tier (``fit_scan_tier``).
    Call after digests are attached: they share the batch file's budget and the tier projection.
    Returns the batchIds still over the tier (single files no split can shrink)."""
    return fit_scan_tier(payload, guides=guides, max_batches=max_batches)


def guide_paths(batch: dict[str, Any], all_guides: list[str]) -> list[str]:
    """Guide paths one scan agent is told to open: the batch's ``relevantGuides``, else every wiki guide."""
    listed = batch.get("relevantGuides")
    if listed is None:
        return all_guides
    return sorted({g["path"] if isinstance(g, dict) else g for g in listed})


def _stat_size(root: Path, rel: str, missing: list[str]) -> int:
    try:
        return (root / rel).stat().st_size
    except OSError:
        missing.append(rel)
        return 0


def _pack_files(files: list[str], cost: dict[str, int], overhead: int) -> list[list[str]]:
    """Consecutive groups of `files` whose prompt (`overhead` guide bytes plus file bytes) stays
    within the tier; a file over the tier alone still gets a group of its own."""
    groups: list[list[str]] = []
    current: list[str] = []
    used = overhead
    for rel in files:
        if current and over_tier(estimate_tokens(used + cost[rel])):
            groups.append(current)
            current, used = [], overhead
        current.append(rel)
        used += cost[rel]
    if current:
        groups.append(current)
    return groups


def fit_scan_tier(
    payload: dict[str, Any],
    *,
    guides: list[list[dict[str, Any]]] | None = None,
    digest_source: Callable[[list[str]], dict[str, str]] | None = None,
    max_batches: int | None = None,
) -> list[str]:
    """Size every batch's scan prompt (file bytes, or digest bytes, plus listed guide bytes) under
    LONG_PROMPT_TIER_TOKENS, in place. An over-tier batch first takes digests for its undigested
    files (``digest_source(files) -> {file: digest path}``), then, if still over, is split into
    consecutive batches renumbered ``batch-N``; ``batch_count`` and ``total_file_count``
    follow the files kept. A split that lifts the batch count past ``max_batches`` drops the
    trailing batches from the payload (the caller defers their files); never raises. With
    ``guides`` (one best-first list per original batch) each resulting batch's ``relevantGuides``
    is refitted to the relay cap; a lone file too large for the tier sheds its guides.
    Returns the batchIds still over the tier (a file larger than the tier alone)."""
    batches: list[dict[str, Any]] = payload["batches"]
    if guides is not None and len(guides) != len(batches):
        raise ValueError(f"guides has {len(guides)} lists for {len(batches)} batches")
    root = Path(payload["repoRoot"])
    all_guides = sorted(set(payload["wikiSlugs"].values()))
    missing: list[str] = []
    sizes: dict[str, int] = {}

    def size(rel: str) -> int:
        if rel not in sizes:
            sizes[rel] = _stat_size(root, rel, missing)
        return sizes[rel]

    produced: list[tuple[dict[str, Any], list[dict[str, Any]] | None]] = []
    split_any = False
    for pos, batch in enumerate(batches):
        full_guides = guides[pos] if guides is not None else None
        probe = {**batch, "relevantGuides": full_guides} if full_guides is not None else batch
        overhead = sum(size(g) for g in guide_paths(probe, all_guides))
        digests = dict(batch.get("digests") or {})

        def cost_of(files: list[str]) -> dict[str, int]:
            return {rel: size(digests.get(rel, rel)) for rel in files}

        cost = cost_of(batch["files"])
        if over_tier(estimate_tokens(overhead + sum(cost.values()))) and digest_source is not None:
            fresh = [f for f in batch["files"] if f not in digests]
            if fresh:
                digests.update(digest_source(fresh))
                cost = cost_of(batch["files"])
        if not over_tier(estimate_tokens(overhead + sum(cost.values()))):
            groups = [list(batch["files"])]
        else:
            groups = _pack_files(batch["files"], cost, overhead)
            if any(len(g) == 1 and over_tier(estimate_tokens(overhead + cost[g[0]])) for g in groups):
                # A lone over-tier file: guides are a hint and yield before the file does.
                groups = _pack_files(batch["files"], cost, 0)
                overhead = 0
                full_guides = []
        split_any = split_any or len(groups) > 1
        for group in groups:
            piece = {**batch, "files": group, "formatHints": dict(batch["formatHints"])}
            mapped = {f: digests[f] for f in group if f in digests}
            if mapped:
                piece["digests"] = mapped
            produced.append((piece, full_guides))

    if split_any and max_batches is not None:
        produced = produced[: max(max_batches, 1)]
    total = len(produced)
    still_over: list[str] = []
    out: list[dict[str, Any]] = []
    for n, (piece, full_guides) in enumerate(produced, start=1):
        if split_any:
            piece["batchId"] = f"batch-{n}"
            piece["description"] = f"Batch {n} of {total} ({len(piece['files'])} files)"
        if full_guides is not None:
            piece["relevantGuides"] = _fit_guides(piece, full_guides)
        spent = sum(size(piece["digests"].get(f, f) if "digests" in piece else f) for f in piece["files"])
        spent += sum(size(g) for g in guide_paths(piece, all_guides))
        if over_tier(estimate_tokens(spent)):
            still_over.append(piece["batchId"])
        out.append(piece)
    payload["batches"] = out
    payload["batch_count"] = len(out)
    payload["total_file_count"] = sum(len(b["files"]) for b in out)
    return still_over


def _slug_shards(slugs: dict[str, Any]) -> list[dict[str, Any]]:
    """Greedy split of `slugs` into chunks whose shard body stays within SLUG_SHARD_MAX_BYTES.
    One entry larger than the cap still gets a shard of its own, never dropped."""
    # Envelope priced at its widest counters so the running total never undercounts.
    envelope = len(_compact({"shard": 9999, "slug_count": len(slugs), "entries": {}}).encode())
    chunks: list[dict[str, Any]] = []
    chunk: dict[str, Any] = {}
    size = envelope
    for slug, path in slugs.items():
        entry = len(_compact({slug: path}).encode()) - 2 + (1 if chunk else 0)
        if chunk and size + entry > SLUG_SHARD_MAX_BYTES:
            chunks.append(chunk)
            chunk, size = {}, envelope
            entry -= 1
        chunk[slug] = path
        size += entry
    if chunk:
        chunks.append(chunk)
    return chunks


def shard_workflow_input(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Split a consumer payload into the on-disk sharded form: a small manifest plus
    `{filename: body}` for one `batch-NNNN.json` per batch and `wikislugs-NNNN.json` shards
    of at most SLUG_SHARD_MAX_BYTES compact-JSON bytes each. The manifest's counts are the integrity totals the
    Workflow re-sums from the shards (`input-count-mismatch`, `wiki-slugs-dropped`)."""
    files: dict[str, dict[str, Any]] = {}
    batch_files: list[str] = []
    for idx, batch in enumerate(payload["batches"], start=1):
        name = f"batch-{idx:04d}.json"
        files[name] = {**batch, "file_count": len(batch["files"])}
        # A batch is one unit of work and is never split; over the relay cap is a scope defect.
        if len(_compact(files[name]).encode()) > SLUG_SHARD_MAX_BYTES:
            raise ValueError(f"{name} exceeds {SLUG_SHARD_MAX_BYTES} bytes of compact JSON")
        batch_files.append(name)

    shard_files: list[str] = []
    for shard, chunk in enumerate(_slug_shards(payload["wikiSlugs"]), start=1):
        name = f"wikislugs-{shard:04d}.json"
        files[name] = {"shard": shard, "slug_count": len(chunk), "entries": chunk}
        shard_files.append(name)

    manifest = {
        "runId": payload["runId"],
        "repoRoot": payload["repoRoot"],
        "wikiDirs": payload["wikiDirs"],
        "batch_count": payload["batch_count"],
        "total_file_count": payload["total_file_count"],
        "wikiSlugCount": payload["wikiSlugCount"],
        "batchFiles": batch_files,
        "slugShardFiles": shard_files,
    }
    return manifest, files


def validate_workflow_input(payload: dict[str, Any]) -> list[str]:
    """Structural validator for the CONSUMER shape — the drift-detection half
    of the contract test. Returns a (possibly empty) error list; never
    raises. Checked against CONSUMER_TOP_LEVEL_FIELDS/CONSUMER_BATCH_FIELDS,
    so a renamed/dropped field on EITHER side that changes what
    translate_to_workflow_input emits fails this check immediately."""
    errors: list[str] = []
    if not isinstance(payload, dict):
        return [f"workflow-input payload must be a dict; got {type(payload).__name__}"]

    for field_name in CONSUMER_TOP_LEVEL_FIELDS:
        if field_name not in payload:
            errors.append(f"required field missing: {field_name!r}")
    if errors:
        return errors

    if not isinstance(payload["runId"], str) or not payload["runId"]:
        errors.append("runId must be a non-empty string")
    if not isinstance(payload["repoRoot"], str) or not payload["repoRoot"]:
        errors.append("repoRoot must be a non-empty string")
    if not isinstance(payload["wikiSlugs"], dict):
        errors.append("wikiSlugs must be a dict (slug -> path object map)")
    if not isinstance(payload["batch_count"], int):
        errors.append("batch_count must be an int")
    if not isinstance(payload["total_file_count"], int):
        errors.append("total_file_count must be an int")

    if not isinstance(payload["wikiDirs"], list) or not payload["wikiDirs"]:
        errors.append("wikiDirs must be a non-empty list")
    if isinstance(payload["wikiSlugs"], dict) and payload["wikiSlugCount"] != len(payload["wikiSlugs"]):
        errors.append(
            f"wikiSlugCount={payload['wikiSlugCount']} disagrees with "
            f"len(wikiSlugs)={len(payload['wikiSlugs'])} — integrity mismatch"
        )

    batches = payload["batches"]
    if not isinstance(batches, list):
        errors.append("batches must be a list")
        return errors

    for idx, batch in enumerate(batches):
        if not isinstance(batch, dict):
            errors.append(f"batches[{idx}] must be a dict")
            continue
        for field_name in CONSUMER_BATCH_FIELDS:
            if field_name not in batch:
                errors.append(f"batches[{idx}] missing field: {field_name!r}")
        if "files" in batch and not isinstance(batch["files"], list):
            errors.append(f"batches[{idx}].files must be a list")
        if "formatHints" in batch and not isinstance(batch["formatHints"], dict):
            errors.append(f"batches[{idx}].formatHints must be a dict")
        if "relevantGuides" in batch and not isinstance(batch["relevantGuides"], list):
            errors.append(f"batches[{idx}].relevantGuides must be a list")

    if isinstance(payload.get("batch_count"), int) and payload["batch_count"] != len(batches):
        errors.append(
            f"batch_count={payload['batch_count']} disagrees with "
            f"len(batches)={len(batches)} — integrity mismatch"
        )
    if isinstance(payload.get("total_file_count"), int):
        summed = sum(len(b["files"]) for b in batches if isinstance(b, dict) and isinstance(b.get("files"), list))
        if payload["total_file_count"] != summed:
            errors.append(
                f"total_file_count={payload['total_file_count']} disagrees with "
                f"summed batch files={summed} — integrity mismatch"
            )

    return errors


@register_op("distill.workflow_input")
def _handler(params: dict, repo_root: Any = None) -> dict:
    """distill.workflow_input handler.

    Params:
        manifest (dict, REQUIRED) — a distill.scope scope-manifest (the
            producer shape; typically loaded from distill.scope's own
            input_json_path result field by the caller).
        repo_root (str, REQUIRED) — repoRoot to stamp into the output; this
            op performs no repo derivation of its own (pure translation, no
            _OP_KEY_SCOPE dependency — the caller already resolved a repo
            root via distill.scope's own dispatch).
        format_hints (dict, optional) — applied uniformly to every batch.
        backlog_run_id (str, optional) — run whose ``state/scratch/backlog-pass/<id>/digests.json``
            to read (default the manifest's run_id). When that file exists, ``batches`` is one
            ``topic-N`` unit per TopicDigest (carrying ``topic`` and ``digest``) and no split runs; a topic whose digest is over the tier is named in
            ``over_tier_batches``. Absent, the per-file path above is unchanged.

    Batches whose scan prompt projects over the long-prompt tier are digested, then split
    (``fit_scan_tier``); the returned ``batch_count`` is over the resulting batches. Batches a split
    cannot bring under the tier (a lone file larger than it) are named in ``over_tier_batches``,
    present only when non-empty.

    The ``repo_root`` HANDLER ARG (engine-injected, per dispatch_message's
    calling convention) is ALWAYS None for this op — it carries no
    _OP_KEY_SCOPE entry, so scope defaults to "none" (a pure computation over
    caller-supplied data, per DEC-5/op-scoping precedent for translation-only
    ops, e.g. distill.curate_clusters) and is intentionally unused here. The
    output's ``repoRoot`` field instead comes from ``params["repo_root"]`` —
    the plain JSON-RPC param, not the engine-injected common_dir.
    """
    manifest = params.get("manifest")
    if not isinstance(manifest, dict):
        raise ValueError(
            "distill.workflow_input requires a 'manifest' param (a "
            "distill.scope scope-manifest dict)"
        )
    repo_root_param = params.get("repo_root")
    if not repo_root_param:
        raise ValueError("distill.workflow_input requires a non-empty 'repo_root' param")

    backlog_digests = load_backlog_digests(
        repo_root_param, params.get("backlog_run_id") or manifest["run_id"]
    )
    if backlog_digests is not None:
        payload = translate_to_workflow_input(
            manifest,
            repo_root=repo_root_param,
            format_hints=params.get("format_hints"),
            digests=backlog_digests,
        )
        errors = validate_workflow_input(payload)
        if errors:
            raise ValueError(
                "distill.workflow_input produced a payload that fails its own "
                f"contract check: {errors}"
            )
        still_over = [
            b["batchId"] for b in payload["batches"] if over_tier(estimate_tokens(len(b["digest"].encode("utf-8"))))
        ]
        if still_over:
            payload["over_tier_batches"] = still_over
        return payload

    wiki_slugs = wiki_slugs_as_dict(manifest)
    payload = translate_to_workflow_input(
        manifest,
        repo_root=repo_root_param,
        format_hints=params.get("format_hints"),
    )

    def digest_source(files: list[str]) -> dict[str, str]:
        written = write_digests(Path(repo_root_param), f"{RUN_DIR_REL}/{manifest['run_id']}", files)
        return {rel: digest_rel for rel, (digest_rel, _) in written.items()}

    still_over = fit_scan_tier(
        payload,
        guides=relevant_guides_for_batches(repo_root_param, manifest["batches"], wiki_slugs),
        digest_source=digest_source,
    )
    if still_over:
        payload["over_tier_batches"] = still_over
    errors = validate_workflow_input(payload)
    if errors:
        raise ValueError(
            "distill.workflow_input produced a payload that fails its own "
            f"contract check: {errors}"
        )
    return payload
