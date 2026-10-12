"""Survey run contract: stages, decision ids, exit codes, digest, structure and agent-results validation.

Plain data and dataclasses; the only I/O is reading the schema file. Every other module in
the package, and the DoE workflow template, builds against these names.
"""
from __future__ import annotations

import functools
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

CEREMONY = "architecture_survey"
RUN_DIR_NAME = "architecture-survey-runs"
TEMPLATE = "architecture-survey.mjs"

LAUNCH_STAGES: tuple[str, ...] = (
    "mode",
    "atlas-inventory",
    "source",
    "structure",
    "extraction",
    "index-currency",
    "refresh-set",
)

FINISH_STAGES: tuple[str, ...] = (
    "agent-results",
    "render",
    "completeness",
    "citation-paths",
    "citation-atlas",
    "attestation-restore",
    "attestation",
    "health-ledger",
    "commit",
    "scratch-clean",
    "receipt",
)

MODES: tuple[str, ...] = ("bootstrap", "refresh", "targeted")
SOURCES: tuple[str, ...] = ("rag", "cartography")

NEEDS: tuple[str, ...] = ("system-map", "edges", "file-index", "dead-paths", "last-touch")
REQUIRED_NEEDS: tuple[str, ...] = ("system-map", "edges")

DECISION_IDS: tuple[str, ...] = (
    "mode",
    "structure-source",
    "index-currency",
    "scope",
    "refresh-set",
    "citation-paths",
    "citation-atlas",
    "clarity",
    "attestation",
    "completeness",
    "ledger-stamp",
    "commit",
)

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_IN_FLIGHT = 3
EXIT_TEMPLATE_MISSING = 4
EXIT_RUN_FAILED = 5

SCHEMA_PATH = Path(__file__).resolve().parents[1] / "contract" / "architecture-survey-run-digest.schema.json"


@dataclass(frozen=True)
class Decision:
    """One gate's decision: the value chosen, the rule that chose it, and the evidence."""

    id: str
    chosen: str
    rule: str
    evidence: str

    def to_row(self) -> dict[str, str]:
        return asdict(self)


def owed_decisions(mode: str, source: str) -> tuple[str, ...]:
    """Decision ids a run owes in its receipt: all, less those the mode and source exempt."""
    if mode not in MODES:
        raise ValueError(f"unknown mode: {mode}")
    if source not in SOURCES:
        raise ValueError(f"unknown source: {source}")
    skip: set[str] = set()
    if mode == "bootstrap":
        skip.add("refresh-set")
    if mode == "targeted":
        skip.add("ledger-stamp")
    if source == "cartography":
        skip.add("index-currency")
    return tuple(d for d in DECISION_IDS if d not in skip)


@functools.cache
def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def _validate(obj: Any, def_name: str) -> list[str]:
    from coordinator_core.frontmatter.schema_validate import _validate_json_schema_node

    errors = _validate_json_schema_node(obj, {"$ref": f"#/$defs/{def_name}"}, _schema())
    return [f"{e['field']}: {e['error']}" for e in errors]


def validate_digest(obj: Any) -> list[str]:
    """Return schema errors for a digest; an empty list means valid."""
    return _validate(obj, "digest")


def validate_structure(obj: Any) -> list[str]:
    """Return schema errors for a structure.json object; an empty list means valid."""
    return _validate(obj, "structure")


def validate_agent_results(obj: Any, source: str | None = None) -> list[str]:
    """Return schema errors for an agent-results object; an empty list means valid.

    `scope` is required when `source` is `cartography`; omit `source` to skip that check.
    """
    errors = _validate(obj, "agent_results")
    if source == "cartography" and isinstance(obj, dict) and "scope" not in obj:
        errors.append("scope: required on source cartography")
    return errors


def build_digest(
    *,
    run_id: str,
    repo_root: str,
    state: str,
    mode: str,
    system: str | None,
    scratch_dir: str,
    finish_argv: list[str],
    decisions: list[Decision],
    existing_atlas: dict[str, Any],
    source: str,
    predicate: dict[str, Any],
    structure_path: str | None,
    refresh_set: list[dict[str, Any]],
    consume_gate: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble a digest and raise ValueError listing schema errors if it is invalid."""
    digest = {
        "run_id": run_id,
        "repo_root": repo_root,
        "state": state,
        "mode": mode,
        "system": system,
        "scratch_dir": scratch_dir,
        "finish_argv": list(finish_argv),
        "decisions": [d.to_row() for d in decisions],
        "existing_atlas": existing_atlas,
        "source": source,
        "predicate": predicate,
        "structure_path": structure_path,
        "refresh_set": refresh_set,
        "consume_gate": consume_gate,
    }
    errors = validate_digest(digest)
    if errors:
        raise ValueError("invalid digest: " + "; ".join(errors))
    return digest
