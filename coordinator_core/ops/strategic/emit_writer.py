"""
coordinator_core.ops.strategic.emit_writer — read-canonical -> wrap-in-ProvenanceEnvelope
-> write helper for the "strategic.emit" op.

Purpose: the sole read/write helper for strategic.emit. Reads the CURATED canonical
`<target_root>/state/strategic/self-description.yaml` (never the `.draft.yaml` — that is
strategic.generate's write surface, see draft_writer.py's negative-spec) and, if present,
wraps it into a `strategic-emission.json` feed record carrying a ProvenanceEnvelope built via
`EmitContext.provenance(...)`. If the canonical is absent, returns a typed no-op — never writes
a file, never crashes, never synthesizes/curates/repairs content.

Wire contract (pinned, docs/plans/2026-07-12-strategic-feed-emission... stub § Wire contract):
    provenance: {
        source_kind: "coordinator_artifact",
        repo:        ctx.repo_name,
        ref:         None,                     # REQUIRED-null for coordinator_artifact
        path:        "state/strategic/self-description.yaml",
        observed_at: ctx.observed_at,
        derivation:  "parsed",
    }
Top-level feed object: {schema_version, emitted_at, emitted_by_machine, coordinator_root_path,
strategic_self_descriptions: [<record>]}, one section array. The two feeds intentionally
diverge on this shape: 24ed2b31 removed the top-level `coordinator_root_path` scalar from
`envelope._empty_skeleton` (zero readers on the SnapshotEnvelope/cockpit-emission.json
pipeline), but this module's own `strategic-emission.json` feed keeps its top-level
`coordinator_root_path` — asserted present by `tests/test_strategic_emit.py` — because it
is a separate, unvalidated-against-SnapshotEnvelope schema with its own live consumer.

Negative-spec:
  - Does NOT read `.draft.yaml` — only the curated canonical filename.
  - Does NOT write an empty/placeholder feed when the canonical is absent — no file at all.
  - Does NOT put curation-status in the ProvenanceEnvelope — the canonical's own
    per-field `provenance` markers (curated|generated|asserted) ride as record DATA inside
    the wrapped `self_description` payload, untouched by this module.

`observed_at` is always the `EmitContext.resolve`-produced `Z`-suffixed UTC string; rag's
provenance validator normalizes `Z` -> `+00:00` before parsing (see example-retrieval-repo
`core/workstate_store/provenance.py`) — do not assume other ISO-8601 offset shapes are
exercised by this module's tests.
# Review: code-reviewer (Finding 3) — noting the exact observed_at shape this module relies on.

Spec backlink: docs/plans/strategic-feed-emission stub.md (tasks/strategic-feed-emission/stub.md)
"""

from __future__ import annotations

GENERATES = [{"artifact": "state/strategic-emission.json", "stamp_key": "emitted_at", "sources": ["state/strategic/self-description.yaml"]}]

import json
from pathlib import Path
from typing import Optional

import yaml

from coordinator_core.ops.emit.context import EmitContext

CANONICAL_REL = "state/strategic/self-description.yaml"

SCHEMA_VERSION = "1.0.0"

OUTPUT_NAME = "strategic-emission.json"

FEED_ARRAY_KEY = "strategic_self_descriptions"


def read_canonical(target_root: Path) -> Optional[dict]:
    """Read+parse the curated canonical, or return None if it is absent.

    Never touches `self-description.draft.yaml` — only `CANONICAL_REL`.

    Raises:
        ValueError: canonical is present but is not valid YAML, or parses to a
            non-dict (e.g. a bare list or scalar) — a present-but-garbage canonical
            fails loud rather than crashing opaquely or silently degrading (matches
            the surrounding detect-then-fail-loud discipline).
            # Review: code-reviewer (Finding 1) — read_canonical previously let
            # yaml.YAMLError bubble uncrafted out of the JSON-RPC handler, or silently
            # forwarded a non-dict parse result.
    """
    canonical_path = Path(target_root) / CANONICAL_REL
    if not canonical_path.is_file():
        return None
    try:
        parsed = yaml.safe_load(canonical_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValueError(
            f"strategic.emit: canonical at {canonical_path} is not valid YAML"
        ) from exc
    if not isinstance(parsed, dict):
        raise ValueError(
            f"strategic.emit: canonical at {canonical_path} did not parse to a mapping "
            f"(got {type(parsed).__name__})"
        )
    return parsed


def build_feed(ctx: EmitContext, canonical: dict) -> dict:
    """Wrap `canonical` into the top-level strategic-emission.json feed object.

    One record in `FEED_ARRAY_KEY`: the full canonical dict as `self_description`, alongside
    its `provenance` envelope built via `ctx.provenance(...)`. The canonical's own per-field
    curation-status markers stay untouched inside `self_description` (record DATA, not
    envelope data — see module docstring negative-spec).
    """
    record = {
        "self_description": canonical,
        "provenance": ctx.provenance(
            source_kind="coordinator_artifact",
            path=CANONICAL_REL,
            derivation="parsed",
            ref=None,
        ),
    }
    return {
        "schema_version": SCHEMA_VERSION,
        "emitted_at": ctx.observed_at,
        "emitted_by_machine": ctx.hostname,
        "coordinator_root_path": str(ctx.repo_root),
        FEED_ARRAY_KEY: [record],
    }


def emit_strategic_feed(ctx: EmitContext) -> dict:
    """Read the canonical, and if present, write `strategic-emission.json`; else typed no-op.

    Returns:
        {"status": "no-canonical", "emitted": 0} when the canonical is absent — no file
        written, no crash.
        {"status": "emitted", "emitted": 1, "out": <path str>} when the canonical is present
        and the feed was written to `<ctx.central_state_root>/strategic-emission.json`.
    """
    canonical = read_canonical(ctx.repo_root)
    if canonical is None:
        return {"status": "no-canonical", "emitted": 0}

    feed = build_feed(ctx, canonical)

    out_path = Path(ctx.central_state_root) / OUTPUT_NAME
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(feed, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return {"status": "emitted", "emitted": 1, "out": str(out_path)}
