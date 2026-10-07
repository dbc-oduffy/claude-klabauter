"""Names shared by the dispatch.emit pipeline route: manifest v1 types, constants, refusal type.

Signatures implemented in their own modules:
  pipeline_manifest.load_manifest(content_root: Path, pipeline: str) -> Manifest
  pipeline_manifest.validate(manifest: Manifest, inputs: PipelineInputs) -> Schedule
  pipeline_compose.compose_pipeline_script(manifest: Manifest, inputs: PipelineInputs,
                  schedule: Schedule, *, run_id: str, agent_type_host: str | None) -> str

PLACEHOLDER_RE matches every `{{...}}` token, closed-set or not, so a caller can
collect the unknown ones as refusals; group 1 is the stripped inner token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT

__all__ = [
    "SCHEMA_VERSION", "MANIFEST_SUFFIX", "PIPELINES_DIR", "subject_key",
    "PLACEHOLDER_RE", "FAN_OUT_NONE", "FAN_OUT_PER_SUBJECT", "FAN_OUT_OVER",
    "SCOPE_PRE", "SCOPE_SUBJECT", "SCOPE_POST", "RUN_ID_PREFIX", "RUN_DIR_ROOT",
    "FlagSpec", "FanOut", "When", "Stage", "Manifest", "PipelineInputs", "Schedule",
    "PipelineEmitRefused", "SUBJECTS_MODE_SEQUENTIAL", "WHEN_FLAG", "WHEN_RETURN", "WHEN_NONEMPTY",
    "ITEM_MARK", "subject_slug", "stage_phase", "VALIDATOR_TOKEN", "validator_bound",
]

SCHEMA_VERSION = 1
MANIFEST_SUFFIX = ".manifest.yaml"
PIPELINES_DIR = "pipelines"
PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")
FAN_OUT_NONE, FAN_OUT_PER_SUBJECT, FAN_OUT_OVER = "none", "per_subject", "over"
SCOPE_PRE, SCOPE_SUBJECT, SCOPE_POST = "pre", "subject", "post"
RUN_ID_PREFIX = "pipeline-"
SUBJECTS_MODE_SEQUENTIAL = "sequential"
WHEN_FLAG, WHEN_RETURN, WHEN_NONEMPTY = "flag", "return", "nonempty"
ITEM_MARK = "\x01item\x01"
VALIDATOR_TOKEN = "validator"
_SLUG_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class FlagSpec:
    """A declared flag: the values a caller may pass, and the default when it passes none."""

    allowed: tuple[str | bool, ...]
    default: str | bool | None


@dataclass(frozen=True)
class FanOut:
    """kind is FAN_OUT_*; source and field name `stage.<source>.return.<field>` for FAN_OUT_OVER.

    FAN_OUT_OVER with source None is the subject form `subject.<field>`: the stage fans out over
    that list field of each subject, fixed at emit time. With `inputs` set it is the list form
    `inputs.<inputs>`: one agent per element of that named input list, fixed at emit time.
    """

    kind: str
    source: str | None = None
    field: str | None = None
    inputs: str | None = None

    @property
    def over_subject(self) -> bool:
        return self.kind == FAN_OUT_OVER and self.source is None and self.inputs is None

    @property
    def over_inputs(self) -> bool:
        return self.kind == FAN_OUT_OVER and self.inputs is not None

    @property
    def emit_time(self) -> bool:
        """The element list is known at emit time, so every prompt is filled before the run."""
        return self.over_subject or self.over_inputs


@dataclass(frozen=True)
class When:
    """A run condition: kind WHEN_FLAG (ref is a flag name), WHEN_RETURN (a boolean return field,
    ref `stage.<id>.return.<field>`) or WHEN_NONEMPTY (a list return field); equals is the
    comparison value for flag and return (return defaults to true)."""

    kind: str
    ref: str
    equals: str | bool | None = None

    @property
    def stage(self) -> str | None:
        return None if self.kind == WHEN_FLAG else self.ref.split(".")[1]

    @property
    def field(self) -> str | None:
        return None if self.kind == WHEN_FLAG else self.ref.split(".")[3]


@dataclass(frozen=True)
class Stage:
    """One agent() call site; template and schema are names keyed into Manifest (an inline schema
    is keyed `<inline:<stage id>>`). model None keeps the agent definition's pinned model;
    agent_type_from `inputs.<roster>` replaces agent_type with a per-item lookup; phase None is
    the stage id; max_concurrent None fans out unchunked."""

    id: str
    agent_type: str | None
    model: str | None
    template: str
    schema: str | None
    web_caller: bool
    depends_on: tuple[str, ...]
    fan_out: FanOut
    output: str
    phase: str | None = None
    agent_type_from: str | None = None
    effort: str | None = None
    max_concurrent: int | None = None
    when: When | None = None
    optional: bool = False


@dataclass(frozen=True)
class Manifest:
    """A loaded pipeline manifest; templates and schemas hold the file contents the stages name."""

    pipeline: str
    root: Path
    flags: Mapping[str, FlagSpec]
    stages: tuple[Stage, ...]
    templates: Mapping[str, str]
    schemas: Mapping[str, dict]
    sha256: str
    description: str | None = None
    subjects_mode: str | None = None
    lists: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PipelineInputs:
    """Caller inputs: the brief file path (POSIX; `{{brief}}` substitutes the path, never the text), the subjects, a repo-relative POSIX scratch_dir, flag values, named lists.

    A `strings` list holds scalar strings; a `roster` list holds `{slug, agent_type}` objects.

    A subject is a string key, or an object whose `subject` field is the key and whose other
    fields (list fields feed `fan_out: {over: subject.<field>}`) ride along.

    `validator` is a command line the caller wants run as a step; `{{validator}}` substitutes it, and
    a stage that names that token is emitted only when it is given (see `validator_bound`).
    """

    brief: str
    subjects: tuple[str | dict, ...]
    scratch_dir: str
    flags: Mapping[str, str | bool]
    lists: Mapping[str, tuple] = field(default_factory=dict)
    validator: str | None = None


@dataclass(frozen=True)
class Schedule:
    """scope maps stage id to SCOPE_*; levels maps SCOPE_* to Kahn levels of stage ids; skipped
    holds the stages a flag condition removed (they appear in neither scope nor levels)."""

    scope: Mapping[str, str]
    levels: Mapping[str, tuple[tuple[str, ...], ...]]
    skipped: frozenset[str] = frozenset()


def subject_key(subject: str | dict) -> str:
    """The subject's key: the string itself, or an object's `subject` field."""
    return subject["subject"] if isinstance(subject, dict) else subject


def subject_slug(key: str) -> str:
    """A filesystem-safe lowercase slug of a subject key."""
    return _SLUG_RE.sub("-", key.lower()).strip("-") or "subject"


def validator_bound(stage: Stage, manifest: Manifest) -> bool:
    """True when the stage's template or output names `{{validator}}`: the stage runs only under a caller validator."""
    texts = (manifest.templates.get(stage.template, ""), stage.output)
    return any(VALIDATOR_TOKEN == tok for text in texts for tok in PLACEHOLDER_RE.findall(text))


def stage_phase(stage: Stage) -> str:
    """The Workflow phase title of a stage."""
    return stage.phase or stage.id


class PipelineEmitRefused(ValueError):
    """An unemittable manifest or input; reasons holds every refusal, one line each."""

    def __init__(self, reasons: list[str]):
        super().__init__("\n".join(reasons))
        self.reasons = reasons

    def __str__(self) -> str:
        return "\n".join(self.reasons)
