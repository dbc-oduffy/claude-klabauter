from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


STAGE_KINDS: frozenset[str] = frozenset(
    {"triage", "refute-close", "fix", "verify", "commit", "undo"}
)

VERIFY_MODES: frozenset[str] = frozenset({"agent", "op"})


WHERE_OPERATORS: frozenset[str] = frozenset({"==", "in", "present", "absent", "contains"})

RESERVED_KEYS: frozenset[str] = frozenset({"@stem", "@unkeyed"})


KNOB_NAMES: frozenset[str] = frozenset(
    {
        "where",
        "limit",
        "concurrency",
        "extra_verification",
        "batch_size",
        "triage_depth",
        "window",
        "max_agent_calls",
        "budget_tokens",
    }
)

OVERRIDABLE_KNOBS: frozenset[str] = frozenset({"where", "limit", "budget_tokens"})

APPETITE_PRESETS: frozenset[str] = frozenset({"hunt", "standard", "sweep"})

TRIAGE_DEPTHS: frozenset[str] = frozenset({"shallow", "standard", "deep"})

TSHIRT_SIZES: frozenset[str] = frozenset({"XS", "S", "M", "L", "XL", "XXL"})

PLAN_WEIGHT_FLOOR: str = "M"


UNIVERSAL_HANDBACK_TYPES: frozenset[str] = frozenset(
    {
        "budget-exhausted",
        "verify-failed",
        "rejected-after-retry",
        "widen-exhausted",
        "peer-dirty",
        "commit-failed",
        "manifest-stale",
        "stage-dead",
        "baton",
        "needs-judgment",
    }
)

# STAGE_OUTCOMES: a closed-vocabulary publish, one map from stage kind to its
# fixed here. `fix`'s engine-mapped outcomes (`NEEDS_PLAN` -> `baton`, a
STAGE_OUTCOMES: dict[str, frozenset[str]] = {
    "triage": frozenset({"profile-declared"}),
    "refute-close": frozenset({"confirmed", "refuted"}),
    "verify": frozenset({"pass", "fail"}),
    "fix": frozenset(
        {"done", "NEEDS_WIDER_SCOPE", "PEER_DIRTY", "NOT_REPRODUCED", "baton", "needs-judgment"}
    ),
    "commit": frozenset({"committed", "commit-failed"}),
    "undo": frozenset({"undone"}),
}


SOURCE_OPS: frozenset[str] = frozenset({"lessons.extract"})
VERIFY_OPS: frozenset[str] = frozenset({"lessons.verify_extraction"})
REGENERATE_OPS: frozenset[str] = frozenset({"doctrine.surface_split_regenerate"})

OP_RUNNER_AGENT_TYPE: str = "coordinator:queue-grind-op-runner"


ENGINE_CONCURRENCY_CEILING: int = 8
assert ENGINE_CONCURRENCY_CEILING <= 16


RECEIPT_QUEUE_KEYS: tuple[str, ...] = (
    "queue",
    "profile",
    "profile_digest",
    "appetite",
    "resolved_knobs",
    "manifest_digest",
    "source",
    "reemit",
)


LEDGER_LINE_FIELDS: tuple[str, ...] = (
    "profile",
    "row_id",
    "digest",
    "stage",
    "verdict",
    "outcome",
    "evidence_file",
    "run_stamp",
)

TRIAGE_RECORD_FIELDS: tuple[str, ...] = (
    "row",
    "verdict",
    "evidence",
    "tshirt_size",
    "sizing_evidence",
    "tradeoff",
    "declared_files",
    "fix_plan",
)


CLOSURE_BLOCK_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"status_field", "closed_values", "stamp_fields"}
)

CLOSURE_CLOSING_BRANCHES: frozenset[str] = frozenset({"fix", "refute-close"})

ABSENT_SENTINELS_KEY: str = "absent_sentinels"


def _vocab_out_dir(out_dir: str | os.PathLike[str] | None = None) -> Path:
    if out_dir is not None:
        return Path(out_dir).resolve()
    return (Path(__file__).resolve().parents[2] / "schema").resolve()


def _handback_schema() -> dict[str, Any]:
    return {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "title": "queue-grind-handback/1",
        "type": "object",
        "required": [
            "schema",
            "profile",
            "appetite",
            "handed_back",
            "settled",
            "counts",
            "spend",
        ],
        "properties": {
            "schema": {"const": "queue-grind-handback/1"},
            "profile": {"type": "string"},
            "appetite": {"type": "string", "enum": sorted(APPETITE_PRESETS)},
            "handed_back": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["row", "type", "reason"],
                    "properties": {
                        "row": {"type": "string"},
                        "type": {
                            "type": "string",
                            "anyOf": [
                                {"enum": sorted(UNIVERSAL_HANDBACK_TYPES)},
                                {"pattern": "^[a-z0-9]+(-[a-z0-9]+)*$"},
                            ],
                        },
                        "reason": {"type": "string"},
                    },
                },
            },
            "settled": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["row", "outcome", "sha"],
                    "properties": {
                        "row": {"type": "string"},
                        "outcome": {"type": "string"},
                        "sha": {"type": "string"},
                    },
                },
            },
            "counts": {"type": "object"},
            "spend": {
                "type": "object",
                "required": [
                    "output_tokens",
                    "agent_calls_total",
                    "agent_calls_by_stage_kind",
                ],
                "properties": {
                    "output_tokens": {"type": "integer"},
                    "agent_calls_total": {"type": "integer"},
                    "agent_calls_by_stage_kind": {
                        "type": "object",
                        "properties": {
                            kind: {"type": "integer"} for kind in sorted(STAGE_KINDS)
                        },
                    },
                },
            },
        },
    }


def emit_vocab(out_dir: str | os.PathLike[str] | None = None) -> dict[str, Path]:
    schema_dir = _vocab_out_dir(out_dir)
    schema_dir.mkdir(parents=True, exist_ok=True)

    vocab_document: dict[str, Any] = {
        "stage_kinds": sorted(STAGE_KINDS),
        "verify_modes": sorted(VERIFY_MODES),
        "where_operators": sorted(WHERE_OPERATORS),
        "knob_names": sorted(KNOB_NAMES),
        "overridable_knobs": sorted(OVERRIDABLE_KNOBS),
        "appetite_presets": sorted(APPETITE_PRESETS),
        "triage_depths": sorted(TRIAGE_DEPTHS),
        "tshirt_sizes": sorted(TSHIRT_SIZES),
        "plan_weight_floor": PLAN_WEIGHT_FLOOR,
        "universal_handback_types": sorted(UNIVERSAL_HANDBACK_TYPES),
        "stage_outcomes": {
            kind: sorted(outcomes) for kind, outcomes in sorted(STAGE_OUTCOMES.items())
        },
        "reserved_keys": sorted(RESERVED_KEYS),
        "source_ops": sorted(SOURCE_OPS),
        "verify_ops": sorted(VERIFY_OPS),
        "regenerate_ops": sorted(REGENERATE_OPS),
        "op_runner_agent_type": OP_RUNNER_AGENT_TYPE,
        "engine_concurrency_ceiling": ENGINE_CONCURRENCY_CEILING,
        "receipt_queue_keys": list(RECEIPT_QUEUE_KEYS),
        "ledger_line_fields": list(LEDGER_LINE_FIELDS),
        "triage_record_fields": list(TRIAGE_RECORD_FIELDS),
        "closure_block_required_fields": sorted(CLOSURE_BLOCK_REQUIRED_FIELDS),
        "closure_closing_branches": sorted(CLOSURE_CLOSING_BRANCHES),
        "absent_sentinels_key": ABSENT_SENTINELS_KEY,
    }

    vocab_path = schema_dir / "grind-vocab.json"
    vocab_path.write_bytes(
        (json.dumps(vocab_document, indent=2, sort_keys=True) + "\n").encode("utf-8")
    )

    handback_path = schema_dir / "queue-grind-handback.schema.json"
    handback_path.write_bytes(
        (json.dumps(_handback_schema(), indent=2, sort_keys=True) + "\n").encode("utf-8")
    )

    return {"grind-vocab": vocab_path, "queue-grind-handback-schema": handback_path}
