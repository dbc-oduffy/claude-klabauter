"""
grind_vocab — the queue-grind engine's vocabulary contract: closed sets,
constants, and pinned emission. Constants and `emit_vocab` only, no other
logic (overengineering-reviewer #7, docs/plans/2026-09-21-bug-blitz-emitter-
engine-leg.md's C1 row — a three-file package for frozensets plus one writer
had no second consumer to justify the split).

Every closed set here is a design-time membership contract other modules
validate profiles and stage output against (`grind_profile.py`'s
`validate_graph`, `grind_stages.py`'s per-kind composers, `queue_select.py`'s
`where` evaluator). `emit_vocab` publishes the same vocabulary as JSON so
DoE's profile schema (`queue-grind-profile.schema.json`) imports it rather
than hand-duplicating it — the two agree by construction (§ Design §
Vocabulary contract, EM note 2).

Negative-spec: this module owns no runtime behaviour beyond `emit_vocab` —
no selector, no composer, no op registry, no CLI. A future consumer needing
logic over these sets belongs in its own module, importing from here.

Spec backlink: docs/plans/2026-09-21-bug-blitz-emitter-engine-leg.md § Design
§ Vocabulary contract, EM note 3, Tasks § C1.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Stage library (§ Design § Stage library)
# ---------------------------------------------------------------------------

STAGE_KINDS: frozenset[str] = frozenset(
    {"triage", "refute-close", "fix", "verify", "commit", "undo"}
)

VERIFY_MODES: frozenset[str] = frozenset({"agent", "op"})

# ---------------------------------------------------------------------------
# Selector (§ Design § Selector)
# ---------------------------------------------------------------------------

WHERE_OPERATORS: frozenset[str] = frozenset({"==", "in", "present", "absent", "contains"})

RESERVED_KEYS: frozenset[str] = frozenset({"@stem", "@unkeyed"})

# ---------------------------------------------------------------------------
# Appetite / knobs (§ Design § Profile, `resolve_appetite`)
# ---------------------------------------------------------------------------

# `budget_tokens` is a knob (it is overridable, and only a knob can be) even
# though `resolve_appetite`'s own enumeration sentence names the other eight;
# the body's "Only `where`, `limit` and `budget_tokens` are overridable"
# clause is the second source that puts it in this set.
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

# The plan names no exhaustive depth vocabulary beyond "the engine's
# shallowest depth" (§ Design § Profile, § Answers to DoE's addendum
# questions #4) and no other chunk pins a literal depth string, so this
# three-value shallow-to-deep set is the minimal closed vocabulary that
# satisfies "shallowest" having a well-defined member.
TRIAGE_DEPTHS: frozenset[str] = frozenset({"shallow", "standard", "deep"})

TSHIRT_SIZES: frozenset[str] = frozenset({"XS", "S", "M", "L", "XL", "XXL"})

PLAN_WEIGHT_FLOOR: str = "M"

# ---------------------------------------------------------------------------
# Hand-back (§ Design § Hand-back)
# ---------------------------------------------------------------------------

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
# own closed outcome set (EM-authored, DoE request). `triage`'s single member
# is the profile's own verdict vocabulary, checked against the profile, not
# fixed here. `fix`'s engine-mapped outcomes (`NEEDS_PLAN` -> `baton`, a
# non-empty tradeoff -> `needs-judgment`) are represented by their mapped
# names -- this describes what a stage's edge can carry forward, not its raw
# agent-return vocabulary.
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

# ---------------------------------------------------------------------------
# Ops (§ Design § Ops)
# ---------------------------------------------------------------------------

SOURCE_OPS: frozenset[str] = frozenset({"lessons.extract"})
VERIFY_OPS: frozenset[str] = frozenset({"lessons.verify_extraction"})
REGENERATE_OPS: frozenset[str] = frozenset({"doctrine.surface_split_regenerate"})

OP_RUNNER_AGENT_TYPE: str = "coordinator:queue-grind-op-runner"

# ---------------------------------------------------------------------------
# Engine design beyond the DR (§ Design § Engine design beyond the DR,
# EM note 7)
# ---------------------------------------------------------------------------

ENGINE_CONCURRENCY_CEILING: int = 8
assert ENGINE_CONCURRENCY_CEILING <= 16  # eng-director F4, kept pinned per overengineering-reviewer #5

# ---------------------------------------------------------------------------
# Cross-repo receipt contract (EM note 6, eng-director F1) -- a rename here
# fails the golden instead of passing silently.
# ---------------------------------------------------------------------------

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

# ---------------------------------------------------------------------------
# Ledger / triage record shapes (§ Design § Row verbs, § Stage library)
# ---------------------------------------------------------------------------

# One JSONL ledger line per `grind-row append` call:
# `--profile P --row-id R --digest D --stage S --verdict V --outcome O
#  --evidence-file F --run-stamp T`.
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

# The per-batch triage record written to <run_dir>/records/<batch-id>.json,
# one entry per row (§ Design § Stage library, `triage` row).
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

# ---------------------------------------------------------------------------
# Required profile blocks (EM note 3): the `closure` block a profile must
# carry (status field, closed value(s), stamp fields) since the closer
# cannot be queue-agnostic without one, plus the `absent_sentinels` setting
# key (§ Design § Selector, "Absent sentinels").
# ---------------------------------------------------------------------------

CLOSURE_BLOCK_REQUIRED_FIELDS: frozenset[str] = frozenset(
    {"status_field", "closed_values", "stamp_fields"}
)

# `closed_values` is a map keyed by the closing branch, one value per branch
# that can reach a close (EM note 3: "lessons close differently per branch").
CLOSURE_CLOSING_BRANCHES: frozenset[str] = frozenset({"fix", "refute-close"})

ABSENT_SENTINELS_KEY: str = "absent_sentinels"

# ---------------------------------------------------------------------------
# Emission
# ---------------------------------------------------------------------------


def _vocab_out_dir(out_dir: str | os.PathLike[str] | None = None) -> Path:
    if out_dir is not None:
        return Path(out_dir).resolve()
    return (Path(__file__).resolve().parents[2] / "schema").resolve()


def _handback_schema() -> dict[str, Any]:
    """Build `queue-grind-handback.schema.json`'s document.

    Mirrors § Design § Hand-back's document shape:
    `{schema, profile, appetite, handed_back: [{row, type, reason}],
    settled: [{row, outcome, sha}], counts, spend}`. `type` enumerates the
    ten universal hand-back types as its known minimum; a profile is free to
    add its own kebab-case types (§ Design § Hand-back), so the schema does
    not close the enum -- it pins the universal floor other JSON-Schema
    consumers (DoE's profile schema) build on.
    """
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
    """Write `grind-vocab.json` and `queue-grind-handback.schema.json` to
    `out_dir` (default `<repo-root>/schema`; no env override --
    overengineering-reviewer #7, no consumer names an env-directed vocab
    emission). Serialised as `json.dumps(indent=2, sort_keys=True) + "\\n"`
    with `newline="\\n"`, mirroring `contract.cockpit_schema.emit_schema`.

    Returns `{"grind-vocab": <path>, "queue-grind-handback-schema": <path>}`.
    """
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
