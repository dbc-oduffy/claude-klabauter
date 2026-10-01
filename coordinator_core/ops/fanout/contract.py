"""
coordinator_core.ops.fanout.contract — the fanout-manifest.v1 contract.

Purpose: the single loader/validator for `coordinator_core/contract/fanout-manifest.v1.schema.json`
plus every constant, builder and TypedDict that compose, census and reconcile share, so no
two of them can disagree on a tag, a roster or an output shape.

Negative spec: no file read beyond the one cached schema load, no process spawn, no network,
no op registration. `jsonschema` and `yaml` import lazily, on first validation or parse.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, Optional, TypedDict

_SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "contract" / "fanout-manifest.v1.schema.json"

SCHEMA_ID = "fanout-manifest.v1"

# Repo names every child carries; the schema has no field that removes one.
STANDARD_CHILD_ROSTER = ("project-rag", "coordinator-claude", "claude-klabauter")

# The one place a correction lands when a live create_session call shows a key mismatch.
CREATE_SESSION_ARG_KEYS = (
    "title",
    "source_url",
    "environment_id",
    "model",
    "permission_mode",
    "tags",
    "prompt",
)

# status_bucket values; anything outside both sets (and not running) maps to census state `unknown`.
DONE_BUCKETS = frozenset({"completed", "review_ready"})
FAILED_BUCKETS = frozenset({"failed", "errored", "cancelled"})

TAG_JOB = "fanout"
TAG_WORKER = "fanout-worker"
TAG_PARENT = "fanout-parent"
TAG_FOCUS = "fanout-focus"

DEFAULT_MAX_CONCURRENT = 6


class CreateSessionArgs(TypedDict):
    title: str
    source_url: str
    environment_id: str
    model: str
    permission_mode: str
    tags: list[str]
    prompt: str


class ComposeAction(TypedDict):
    worker_id: str
    idempotency_key: str
    create_session: CreateSessionArgs


class ComposeResult(TypedDict):
    job_id: str
    actions: list[ComposeAction]


class CensusRow(TypedDict):
    worker_id: str
    state: str  # missing | running | done | failed | duplicate | unknown
    session_ids: list[str]
    status_bucket: Optional[str]
    status_detail: Optional[str]
    cost_usd: Optional[float]
    checked_in: bool
    channel_url: Optional[str]


class CensusResult(TypedDict):
    job_id: str
    rows: list[CensusRow]
    strays: list[str]


class _ReconcileActionRequired(TypedDict):
    kind: str  # spawn | archive | flag | await_checkin | broadcast
    reason: str


class ReconcileAction(_ReconcileActionRequired, total=False):
    worker_id: str
    session_id: str
    idempotency_key: str


class ReconcileResult(TypedDict):
    actions: list[ReconcileAction]


CENSUS_STATES = ("missing", "running", "done", "failed", "duplicate", "unknown")
RECONCILE_KINDS = ("spawn", "archive", "flag", "await_checkin", "broadcast")


@functools.cache
def load_schema() -> dict:
    """The fanout-manifest schema, read from disk exactly once per process."""
    return json.loads(_SCHEMA_PATH.read_text(encoding="utf-8"))


@functools.cache
def _validator() -> Any:
    import jsonschema

    schema = load_schema()
    cls = jsonschema.validators.validator_for(schema)
    cls.check_schema(schema)
    return cls(schema)


def parse_manifest_yaml(text: str) -> Any:
    import yaml

    return yaml.safe_load(text)


def _json_path(parts: Any) -> str:
    out = "$"
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else f".{part}"
    return out


def validate_manifest(manifest: Any) -> dict:
    """Return the validated manifest object; accepts a dict or a YAML string.

    Raises ValueError naming the failing JSON path.
    """
    if isinstance(manifest, str):
        try:
            manifest = parse_manifest_yaml(manifest)
        except Exception as exc:
            raise ValueError(f"$: manifest is not valid YAML: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ValueError("$: manifest must be an object")
    errors = sorted(_validator().iter_errors(manifest), key=lambda e: [str(p) for p in e.absolute_path])
    if errors:
        err = errors[0]
        raise ValueError(f"{_json_path(err.absolute_path)}: {err.message}")
    seen: set[str] = set()
    for idx, worker in enumerate(manifest["workers"]):
        if worker["id"] in seen:
            raise ValueError(f"$.workers[{idx}].id: duplicate worker id {worker['id']!r}")
        seen.add(worker["id"])
    return manifest


def effective_roster(manifest: dict, worker: dict) -> list[str]:
    """STANDARD_CHILD_ROSTER, then manifest repos, then worker repos; order-stable, deduplicated."""
    out: list[str] = []
    for name in (*STANDARD_CHILD_ROSTER, *manifest.get("repos", ()), *worker.get("repos", ())):
        if name not in out:
            out.append(name)
    return out


def job_tag(job_id: str) -> str:
    return f"{TAG_JOB}:{job_id}"


def worker_tag(worker_id: str) -> str:
    return f"{TAG_WORKER}:{worker_id}"


def parent_tag(parent_session_id: str) -> str:
    return f"{TAG_PARENT}:{parent_session_id}"


def focus_tag(focus: str) -> str:
    return f"{TAG_FOCUS}:{focus}"


def worker_tags(manifest: dict, worker: dict) -> list[str]:
    """Identity tags, then the worker's own; order-stable and deduplicated."""
    out: list[str] = []
    for tag in (
        job_tag(manifest["job_id"]),
        worker_tag(worker["id"]),
        parent_tag(manifest["parent"]["session_id"]),
        focus_tag(worker["focus"]),
        *worker.get("tags", ()),
    ):
        if tag not in out:
            out.append(tag)
    return out


def session_title(job_id: str, worker_id: str) -> str:
    return f"{job_id}/{worker_id}"


def idempotency_key(job_id: str, worker_id: str) -> str:
    return f"{job_id}/{worker_id}"
