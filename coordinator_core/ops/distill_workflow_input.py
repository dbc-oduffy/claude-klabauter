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
the consumer (example-doctrine-repo's distill-harvest.workflow.js) can pin against, so neither
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

Negative-spec: performs no scan, no cohort computation, no disk read/write of
its own (pure dict-in/dict-out translation over a caller-supplied manifest),
no LLM call, and does NOT add the consumer-side integrity assertion itself —
that assertion is example-doctrine-repo's distill-harvest.workflow.js's own change to make
against this op's batch_count/total_file_count fields; this op only makes the
declared counts available to be checked against.

Spec backlink: docs/plans/2026-07-23-claude-klabauter-driven-ceremony-redesign.md § C10
Fold-in ask: cross-repo/inbox/2026-08-06-example-retrieval-repo-em-distill-workflow-
inputfile-silent-truncation.md
"""

from __future__ import annotations

from typing import Any

from coordinator_core.distill.manifest_schema import check_schema_version
from coordinator_core.ipc import register_op
from coordinator_core.ops.distill_scope import wiki_slugs_as_dict

__all__ = [
    "CONSUMER_TOP_LEVEL_FIELDS",
    "CONSUMER_BATCH_FIELDS",
    "translate_to_workflow_input",
    "validate_workflow_input",
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
)

#: Required per-batch fields of the CONSUMER input shape.
CONSUMER_BATCH_FIELDS: tuple[str, ...] = ("batchId", "files", "description", "formatHints")


def translate_to_workflow_input(
    manifest: dict[str, Any],
    *,
    repo_root: str,
    format_hints: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Translate a distill.scope scope-manifest (producer shape) into the
    Workflow script's consumer shape.

    Five field translations (the five that disagreed pre-adapter):
      - ``run_id`` -> ``runId``
      - flat-list ``batches`` -> ``[{batchId, files, description, formatHints}]``
        (batchId is "batch-<1-based index>", stable and deterministic — never
        derived from wallclock or content hash)
      - ``wiki_slugs`` (list of {"slug","path"}) -> ``wikiSlugs`` (flat
        slug -> path object map), via distill_scope.wiki_slugs_as_dict — the
        one converter that already shipped pre-this-op.
      - (new) ``repoRoot`` — the producer never emits this; the caller
        (whichever op/CLI invokes this translation after distill.scope) must
        supply it explicitly. Never inferred from cwd inside this pure
        translation function.
      - (new) ``batch_count`` / ``total_file_count`` — first-class integrity
        fields (see module docstring), computed from the SAME ``batches`` list
        being translated, never independently re-scanned.

    ``format_hints`` is applied uniformly to every emitted batch (defaults to
    an empty dict — this op does not invent format-hint content; that is a
    caller/consumer concern).

    Fails loud via check_schema_version when ``manifest["schema_version"]`` is
    a newer-than-known forward version this translation cannot safely read.
    """
    check_schema_version(manifest)
    hints = dict(format_hints) if format_hints is not None else {}

    producer_batches: list[list[str]] = manifest["batches"]
    batches_out: list[dict[str, Any]] = []
    for idx, files in enumerate(producer_batches, start=1):
        batches_out.append(
            {
                "batchId": f"batch-{idx}",
                "files": list(files),
                "description": f"Batch {idx} of {len(producer_batches)} ({len(files)} files)",
                "formatHints": dict(hints),
            }
        )

    return {
        "runId": manifest["run_id"],
        "repoRoot": repo_root,
        "batches": batches_out,
        "wikiSlugs": wiki_slugs_as_dict(manifest),
        "batch_count": len(batches_out),
        "total_file_count": sum(len(b["files"]) for b in batches_out),
    }


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
async def _handler(params: dict, repo_root: Any = None) -> dict:
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

    payload = translate_to_workflow_input(
        manifest,
        repo_root=repo_root_param,
        format_hints=params.get("format_hints"),
    )
    errors = validate_workflow_input(payload)
    if errors:
        raise ValueError(
            "distill.workflow_input produced a payload that fails its own "
            f"contract check: {errors}"
        )
    return payload
