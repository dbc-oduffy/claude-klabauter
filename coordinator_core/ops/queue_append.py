"""
coordinator_core.ops.queue_append — per-entry queue YAML appender (queue.append op).

Purpose: Port of coordinator-queue-append. Writes ONE YAML entry to
``<repo_root>/state/<output_dir>/<ISO-date>-<slug>-<digest12>.yaml`` for the five
supported queue schemas: debt-backlog, bug-backlog, improvement-queue, lessons,
cross-repo-commitment. The trailing ``-<digest12>`` filename component is a content
digest (DR-213 D2(i) amendment, 2026-07-08) — see § Content digest below.

Byte-parity target: ``[DoE-claude] coordinator/bin/coordinator-queue-append``. NOTE:
byte parity covers file *content*, not filename — the digest component is an
intentional, documented divergence from the bash oracle's ``<date>-<slug>.yaml`` shape.

Schema routing (contract-derived, ``_output_dir_for_schema``): the output directory for
each schema is DERIVED from the DoE schema contract (``schema_validate.describe(<schema>)``
→ ``applies_to`` glob → dirname), not a hand-maintained table. Illustrative
examples of the current 5-schema set (any queue schema the DoE contract defines is
supported — the set is no longer enumerated here by hand):
    debt-backlog          → state/debt-backlog/<date>-<slug>-<digest12>.yaml
    bug-backlog           → state/bug-backlog/<date>-<slug>-<digest12>.yaml
    improvement-queue     → state/improvement-queue/<date>-<slug>-<digest12>.yaml
    lessons               → state/lessons/<date>-<slug>-<digest12>.yaml
    cross-repo-commitment → state/cross-repo-commitments/<date>-<slug>-<digest12>.yaml

Content digest (collision guard, DR-213 D2(i) amendment): the filename's trailing
``-<digest12>`` is a 12-hex-char SHA-1 prefix of the entry's semantic payload (all
finalized ``fields`` MINUS ``system:``/provenance, in fixed key order, using the
post-newline-normalization body). Two DISTINCT entries sharing a date+slug now produce
distinct filenames and both survive (fixes silent-overwrite); a genuine re-run of an
IDENTICAL entry still produces the same digest → same filename → ``os.replace``
overwrite → one file (idempotency preserved). See ``_content_digest`` for the exact
field-set and normalization order. NO disk read is involved — the digest is computed
entirely from in-hand params (DR-213 D4).

``_SCHEMA_CLI_NAME`` alias: ``lessons`` → ``lesson-entry`` (schema-cli.js name).

Store schemas (``workstream``, ``workstream-event``): field acceptance for these
two schemas (and any future DoE schema field not in the base ~30-param hand list)
is CONTRACT-DERIVED via ``append_queue_entry(**schema_fields)`` — only fields
``schema_validate.describe(<schema>)`` actually declares (required or optional)
for the given schema are accepted, never a second hand-maintained field-name
list (see ``append_queue_entry``'s own docstring). ``coordinator_root_path`` is
required by both store schemas and defaults to the literal ``"."`` (repo-root-
relative auto-resolve value the cockpit contract declares) when the caller does
not override it — see that field's dedicated comment in ``append_queue_entry``
for the 2026-07-22 fold-correctness outage this must not re-arm. Filename
keying and write-primitive selection for these two schemas (single-file-per-id
overwrite vs. append-only-by-session, C4) are now branched in ``_output_path``
and ``append_queue_entry``'s write-primitive dispatch: ``workstream`` writes
``<workstream_id>.yaml`` via ``_write_out_path_overwrite``; ``workstream-event``
writes ``<date>-<workstream>-<session>.yaml`` via ``_write_out_path_excl``.
Neither uses the content-digest guard below (see ``_output_path``'s own
comment for why). ``workstream_id``/``workstream``/``session`` are validated
against a path-traversal-safe allowlist (``_validate_workstream_identifier``,
AC14) before any path construction.
Spec backlink: pln-teach-the-native-queue-append--8bd701 § C4

Output path precedence:
    1. ``QUEUE_APPEND_OUTPUT_ROOT`` env var (test isolation), in-process route only
       -- a warm server's inherited copy is deliberately ignored
       (``_output_root_override``).
    2. ``queue_scope == "central"`` → claude-klabauter repo root via ``CLAUDE_KLABAUTER_ROOT`` env or
       ``machine-local get repos.claude_klabauter``; raises ``_ClaudeKlabauterUnresolvable``
       when unresolvable (caller degrades gracefully, WARN+skip exit 0).
    3. Project scope: ``main_worktree_root(caller_repo_root)``; meta-repo
       (caller_worktree == ~/.claude) re-routes to claude-klabauter root.

MUTATING op: writes coordinator substrate ONLY (per-entry YAML in one of five named
state/ subdirs). NEVER writes into rag's relational store (dual-write ban, DR-208 /
tri-plane DD#1). No queue index or in-memory state retained (store-less-ness invariant).

YAML serialization: hand-serialized ordered string formatting, NOT yaml.dump. This
preserves byte-identical parity with the bash CLI's ``echo``/``printf``-built YAML,
including the unquoted ``created:`` date field (AC12).

Caller repo_root threading (F1 / P9 analog): handler third arg receives
``git_common_dir(caller_worktree)`` via the ``_OP_KEY_SCOPE: common_dir`` mechanism
(ipc.py). Handler calls ``main_worktree_root(repo_root)`` before any path construction.

Registered as ``queue.append`` in ops/__init__.py and classified ``OpClass.MUTATING``
in authz/classification.py (same dispatch, strang-08 C1+C2).

Negative-spec (DR-213 § D2):
    - NO ``id`` field generated; the filename is the canonical entry handle.
    - NO dedup pre-check; op is write-always. Same date+slug+content → os.replace
      overwrites (idempotent, one file). Same date+slug with DISTINCT content →
      different digest → different filename (both survive, no overwrite). Dedup for
      near-duplicate-but-not-identical entries lives in the coordinator-lesson-add
      wrapper, preserved DoE-side.
    - NO rag store write (dual-write ban).
    - NO HTTP route (Gate 6; UDS-only).
    - NO cwd-based repo resolution; always uses caller_worktree from repo_root param.

Spec backlink: pln-strang-08-queue-append-strangl-2a3499 § C1
Parity oracle: [DoE-claude] coordinator/bin/coordinator-queue-append
DR authority: docs/decisions/DR-213-queue-write-substrate-carveout.md
"""


from __future__ import annotations

MUTATES = [
    "state/debt-backlog/*.yaml",
    "state/bug-backlog/*.yaml",
    "state/improvement-queue/*.yaml",
    "state/lessons/*.yaml",
    "state/cross-repo-commitments/*.yaml",
]  # date+slug+content-digest-keyed new entries; data-dependent filename set per schema

import datetime
import functools
import hashlib
import json
import logging
import os

from coordinator_core.engine_root import (
    coordinator_engine_root_env,
    engine_source_root as _engine_source_root,
    is_published_engine_mirror as _is_published_engine_mirror,
)
from coordinator_core.telemetry import op_latency
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags, same_path
import sys
import tempfile
from pathlib import Path
from typing import Optional

# PARSE-only, never a serializer — the F2 pin (below, § YAML serialization
# helpers) forbids yaml.dump/safe_dump on the write path; yaml.safe_load is
# used exclusively by the C2 round-trip gate in _build_yaml to VALIDATE the
# already-hand-composed document, never to construct it.
import yaml

from coordinator_core._settings_home import settings_home
from coordinator_core.frontmatter import schema_validate
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.session_context import resolve_current_session_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Schema output directory routing
# ---------------------------------------------------------------------------

# CLI schema name aliases → schema-cli.js schema name.
# "lessons" is presented to callers as --schema lessons but registered as lesson-entry.
_SCHEMA_CLI_NAME: dict[str, str] = {
    "lessons": "lesson-entry",
}

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SLUG_MAX_CHARS = 40
_SUBPROCESS_TIMEOUT_SECS = 15

# Env var overrides for test isolation.
_QUEUE_APPEND_OUTPUT_ROOT_ENV = "QUEUE_APPEND_OUTPUT_ROOT"
_MACHINE_LOCAL_IMPL_ENV = "MACHINE_LOCAL_IMPL"
_CLAUDE_HOME_ENV = "CLAUDE_HOME"


def _output_root_override() -> "str | None":
    """The ``QUEUE_APPEND_OUTPUT_ROOT`` test-isolation redirect, but ONLY
    when this process is the one the caller ran in.

    ``QUEUE_APPEND_OUTPUT_ROOT`` is a property of a CALLING process (a test
    redirecting its own writes into a tmpdir). Under the warm engine the op
    executes in a long-lived server process instead, whose environment was
    inherited from whichever session happened to spawn it -- so an env var
    one session exported becomes a standing redirect for every OTHER
    session's writes served by that process, for as long as it lives. That
    is not hypothetical: bug-backlog rows landed in
    ``pytest-of-<user>/pytest-*/…/state/bug-backlog/`` twice, from two
    different shells, while the CLI printed a normal repo path and exited 0
    -- a silently lost write, which for a queue whose whole job is not
    losing items is the worst available failure.

    Refusing the env read on the warm-server route is the whole fix: a
    genuine in-process test caller (``execution_route() == IN_PROCESS``,
    which is every non-server process) is unaffected, and the CLI never
    reaches the native path with this var set anyway -- it forces the legacy
    in-process write when it is present, so the only way this env var can
    reach a served handler is by leaking off the server.

    Never a raise: an override that cannot be honoured is dropped, and the
    write lands where it should have all along.
    """
    override = os.environ.get(_QUEUE_APPEND_OUTPUT_ROOT_ENV)
    if not override:
        return None
    if op_latency.execution_route() != op_latency.IN_PROCESS:
        return None
    return override


class _ClaudeKlabauterUnresolvable(RuntimeError):
    """Raised when CLAUDE_KLABAUTER_ROOT cannot be resolved via env var or machine-local registry.

    Callers catch this and degrade gracefully (WARN+skip, exit 0) per AC6.
    Spec backlink: pln-stop-the-rot-claude-klabauter-state-home-placement-4cc787 § AC13
    """


# ---------------------------------------------------------------------------
# schema_validate bridge (in-process — replaces the former schema-cli.js
# subprocess bridge; see coordinator_core.frontmatter.schema_validate)
# ---------------------------------------------------------------------------


# Review: code-reviewer — _schema_cli_describe was called up to 5x per append for the
# identical schema (unmemoized), each paying full Node process-startup cost; safe to
# cache (Finding 1). The in-process schema_validate.describe() call is far cheaper
# than the former Node subprocess spawn, but the memo is retained: it is now a pure
# function of ``schema_name`` alone (no external path/env dependency to key on, since
# describe() reads claude-klabauter's own fixed vendored schema set), so the cache stays trivially
# correct and the repeat-call defense-in-depth pattern elsewhere in this module (see
# ``_validate`` / ``append_queue_entry``) keeps paying off as cache hits, not re-work.
def _schema_cli_describe(schema_name: str) -> dict:
    """Describe ``schema_name`` via ``schema_validate.describe`` (in-process, cached).

    Returns ``{required: [...], optional: [...], enums: {...}, applies_to: ...}`` with
    ORDERED arrays preserving schema declaration order.

    Raises:
        RuntimeError — schema_validate.describe() rejects the schema name (wraps the
            underlying ValueError so callers can keep a single except-RuntimeError
            handling shape, matching the former subprocess-bridge error surface).
    """
    return _schema_cli_describe_cached(schema_name)


def _reset_schema_cli_cache() -> None:
    """Test-only helper: clear the ``_schema_cli_describe`` memo.

    The cache is interpreter-lifetime state; under pytest it must be dropped between
    tests. Wired into the suite-root autouse reset in ``coordinator_core/conftest.py``.
    """
    _schema_cli_describe_cached.cache_clear()


@functools.lru_cache(maxsize=None)
def _schema_cli_describe_cached(schema_name: str) -> dict:
    """Call ``schema_validate.describe(schema_name)`` and cache the result.

    CACHED (process-lifetime, ``functools.lru_cache``): the returned dict is a SHARED
    object across all callers within this process — callers must treat it read-only
    (``.get()`` / iteration only, never in-place mutation) or they will corrupt the
    cached value for every subsequent caller.
    """
    try:
        return schema_validate.describe(schema_name)
    except ValueError as exc:
        raise RuntimeError(
            f"queue.append: schema introspection failed for {schema_name!r}: {exc}"
        ) from exc


def _output_dir_for_schema(schema_name: str) -> str:
    """Derive the state/<queue> output dir from the schema contract's applies_to glob.

    Replaces the former hardcoded _SCHEMA_OUTPUT_DIRS table (removed) — the engine now
    consumes DoE's schema contract (schema-cli --describe applies_to) rather than keeping a
    private hand-copy that drifted from it (2026-07-11 cross-repo-commitment gap). Adding a
    new queue schema in DoE now propagates here for free — zero recurring claude-klabauter work.

    applies_to shape is a uniform ``state/<dir>/*.yaml`` glob; the output dir is its dirname.
    Raises ValueError when describe fails (surfacing the underlying cause) or applies_to
    is absent/malformed.
    """
    cli_name = _SCHEMA_CLI_NAME.get(schema_name, schema_name)
    try:
        described = _schema_cli_describe(cli_name)
    except RuntimeError as exc:
        # Review: code-reviewer — do not relabel infra failures (node missing, timeout,
        # non-JSON describe output) as "unknown schema": the schema may be perfectly
        # valid and the operator would waste time checking spelling instead of their
        # Node install. Surface the underlying cause text instead (Finding 5).
        raise ValueError(
            f"queue.append: could not resolve output dir for schema {schema_name!r}: {exc}"
        ) from exc

    applies_to = described.get("applies_to")
    # Review: code-reviewer — this op only ever writes YAML queue entries (state/<dir>/
    # *.yaml); the .yaml/state/ suffix requirement below is a deliberate scope boundary,
    # not an accidental byproduct of the current 5 schemas' shape (Finding 3).
    if not applies_to or not isinstance(applies_to, str):
        raise ValueError(
            f"queue.append: schema {schema_name!r} has no usable applies_to location "
            f"(got {applies_to!r})."
        )
    # Review: code-reviewer — require a subdirectory segment under state/ (>= 2 slashes)
    # so a malformed-but-suffix-matching applies_to like "state/*.yaml" (no queue
    # subdirectory) fails the guard instead of silently dirname()-ing to top-level
    # "state" (Finding 4).
    if (
        not applies_to.startswith("state/")
        or not applies_to.endswith("/*.yaml")
        or applies_to.count("/") < 2
    ):
        raise ValueError(
            f"queue.append: schema {schema_name!r} applies_to {applies_to!r} is not a "
            f"YAML queue location (queue.append only writes state/<dir>/*.yaml queue "
            f"entries)"
        )

    # applies_to is a POSIX-style contract identifier ("state/<dir>/*.yaml"), not a
    # filesystem path — os.path.dirname() alone leaves its forward slashes intact on
    # Windows (ntpath treats "/" as a valid separator without rewriting it), which then
    # mixes with the native "\\" from a later os.path.join(root, output_dir) call.
    # normpath() coerces it to the platform's own separator before it is used to build
    # a real on-disk path.
    return os.path.normpath(os.path.dirname(applies_to))


def _schema_cli_validate(
    schema_name: str, fields: dict
) -> tuple[bool, list[schema_validate.ErrorDict]]:
    """Validate ``fields`` against ``schema_name`` via ``schema_validate.validate`` (in-process).

    Returns ``(ok, errors)`` — ``errors`` is the list of ``{field, error, hint}`` dicts
    schema_validate.validate() returns on rejection (formerly a list of pre-flattened
    "field: error" strings from schema-cli.js's stdout contract; callers below already
    only inspect ``errors[0]``, which schema_validate returns as a structured dict with
    an ``"error"`` key carrying the same message text the old regex matches targeted).

    Raises:
        RuntimeError — schema_validate.validate() rejects the schema name.
    """
    try:
        result = schema_validate.validate(schema_name, fields)
    except ValueError as exc:
        raise RuntimeError(
            f"queue.append: schema validation failed for {schema_name!r}: {exc}"
        ) from exc

    if result.get("ok"):
        return True, []

    errors = result.get("errors") or []
    return False, errors


# ---------------------------------------------------------------------------
# YAML serialization helpers (byte-parity with coordinator-queue-append)
#
# F2 pin: do NOT route through yaml.safe_dump or any YAML library — that would
# normalize quoting, reorder keys, and silently break byte-parity (including the
# unquoted ``created:`` date field, AC12).
# ---------------------------------------------------------------------------


def _yaml_quote_string(value: str) -> str:
    """Wrap a string in double-quotes when it contains YAML-special characters.

    Mirrors coordinator-queue-append._yaml_quote_string exactly.

    Negative-spec: a whitespace-preceded ``#`` introduces an inline YAML comment at
    ANY column; the ``(^|\\s)#`` scan covers both leading and embedded cases.
    An unquoted ``2026-07-05`` date literal is NOT quoted — this is the AC12
    unquoted-created: invariant. ``#`` is deliberately absent from the start-char
    set below — the ``(^|\\s)#`` scan is the SOLE ``#`` gate, and its ``^`` branch
    already covers a leading ``#``.

    Start-char set covers every YAML indicator that cannot begin a plain scalar:
    ``|>!&*{}[]'`` (backtick) ``"%@?,`` — plus a trailing ``:`` (a lone ``:`` is
    also caught by this, since it both starts and ends with ``:``).
    """
    if not value:
        return '""'
    needs_quoting = (
        ": " in value
        or value != value.strip()
        or value[0] in "|>!&*{}[]'`\"%@?,"
        or value.endswith(":")
        or re.search(r"(^|\s)#", value) is not None
        or value.startswith("- ")
        or "\n" in value
        or value.lower() in ("true", "false", "null", "yes", "no", "~")
        or re.fullmatch(r"-?\d+(\.\d+)?", value) is not None
    )
    if needs_quoting:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _yaml_block_scalar(value: str) -> str:
    """Format a multi-line string as a YAML strip-chomped literal block scalar (``|-``).

    Mirrors coordinator-queue-append._yaml_block_scalar.
    Strip chomping (``|-``) matches migrate-queues-to-base.py:317 and preserves
    exact byte-fidelity (clip chomping ``|`` adds a trailing newline on round-trip).
    """
    lines = value.splitlines()
    indented = "\n".join("  " + line if line else "" for line in lines)
    return "|-\n" + indented


def _emit_system_block(system: dict) -> str:
    """Emit the ``system:`` provenance block with 2-space child indentation.

    Field order (per spec): created_by_session (if present), created_by_agent
    (if present), linked_sessions, linked_commits (if present), provenance_completeness.
    Empty linked_sessions list emits as ``linked_sessions: []`` (not null key).

    Mirrors coordinator-queue-append._emit_system_block exactly.
    Spec backlink: docs/plans/2026-06-26-queue-schema-unify.md § C2 STEP 2
    """
    child_lines: list[str] = []
    for k, v in system.items():
        if v is None:
            continue
        if isinstance(v, list):
            if not v:
                child_lines.append(f"  {k}: []")
            else:
                items = "\n".join(f"    - {_yaml_quote_string(str(i))}" for i in v)
                child_lines.append(f"  {k}:\n{items}")
        elif isinstance(v, str):
            child_lines.append(f"  {k}: {_yaml_quote_string(v)}")
        else:
            child_lines.append(f"  {k}: {v}")
    return "system:\n" + "\n".join(child_lines)


def _emit_yaml_field(key: str, value) -> str:
    """Emit a single YAML field line or block scalar, with type dispatch.

    - None values are skipped (caller must check before calling).
    - Multi-line strings → literal block scalar (``|-``).
    - Lists → block sequence with 2-space indent.
    - Scalars → quoted only when YAML-special.

    Mirrors coordinator-queue-append._emit_yaml_field exactly.
    """
    if value is None:
        return ""
    if isinstance(value, list):
        if not value:
            return f"{key}: []"
        items = "\n".join(f"  - {_yaml_quote_string(str(item))}" for item in value)
        return f"{key}:\n{items}"
    if isinstance(value, str) and "\n" in value:
        return f"{key}: {_yaml_block_scalar(value)}"
    if isinstance(value, str):
        return f"{key}: {_yaml_quote_string(value)}"
    return f"{key}: {value}"


def _emit_block_map_list_field(key: str, items: list[dict], item_key: str = "text") -> str:
    """Emit a YAML block-sequence of single-key mappings: ``- text: "..."`` per item.

    ``items`` is a list of single-key dicts — the schema-validated in-memory shape
    workstream ``deliverables`` fields carry. This is also the form
    ``schema_validate.parse_yaml``'s list-item-mapping reader accepts. Distinct from
    ``_emit_yaml_field``'s plain scalar-list branch (``- "value"``), which stays in
    use for plain-string list fields (``specs``, ``dependency_annotations``).

    Negative-spec: do NOT emit the inline flow-map form (``- {text: "..."}``) — the
    frontmatter validator rejects it.

    Mirrors coordinator-queue-append._emit_block_map_list_field exactly.
    """
    if not items:
        return f"{key}: []"
    lines = [f"{key}:"]
    for item in items:
        lines.append(f"  - {item_key}: {_yaml_quote_string(item[item_key])}")
    return "\n".join(lines)


def _offending_field_for_yaml_error(exc: "yaml.YAMLError", line_owners: list[str]) -> str:
    """Map a ``yaml.YAMLError``'s mark back to the field that composed that line.

    ``line_owners[i]`` names the field key responsible for the i-th (0-indexed)
    physical line of the document ``_build_yaml`` composed — see that
    function's ``line_owners`` construction. Falls back to a placeholder when
    the error carries no mark or the mark falls outside the tracked range
    (should not happen for a document this module itself composed, but this
    is diagnostic text, not a load-bearing invariant).

    Mirrors coordinator-queue-append._offending_field_for_yaml_error exactly.
    """
    mark = getattr(exc, "problem_mark", None) or getattr(exc, "context_mark", None)
    if mark is not None and 0 <= mark.line < len(line_owners):
        return line_owners[mark.line]
    return "<unknown field>"


def _build_yaml(schema_name: str, fields: dict) -> str:
    """Construct the YAML document string for a queue entry.

    Field order: required fields first (schema insertion order via schema-cli.js
    --describe), then optional fields present in the data (schema insertion order),
    system block last.

    Fail-loud round-trip gate (C2, docs/plans/2026-08-11-queue-append-quoter-gap-
    and-the-unparsea.md): the composed document is ``yaml.safe_load``-parsed
    before being returned. On a ``yaml.YAMLError`` this RAISES a ``ValueError``
    naming the offending field — it does not warn, log-and-continue, or return
    the malformed document. A warning on a corpus writer is how the unparseable-
    YAML class this gate closes accumulated unnoticed in the first place.

    Byte-parity is preserved BY CONSTRUCTION: this is parse-to-CHECK only — the
    parsed object is discarded and the ORIGINAL composed string is what gets
    returned on success. No ``yaml.dump``/``yaml.safe_dump`` anywhere on this
    path (F2 pin, this module's § YAML serialization helpers, queue_append.py:
    294-297 at authoring time).

    Mirrors coordinator-queue-append._build_yaml exactly.
    """
    described = _schema_cli_describe(_SCHEMA_CLI_NAME.get(schema_name, schema_name))
    required: list[str] = described.get("required") or []
    optional: list[str] = described.get("optional") or []

    emit_order = list(required)
    for opt in optional:
        if opt in fields and fields[opt] is not None:
            if opt not in emit_order:
                emit_order.append(opt)

    lines = []
    # line_owners[i] names the field key that produced the i-th physical line
    # of the eventual "\n".join(lines) document — the offending-field lookup
    # the round-trip gate below uses to name a field in its raised error.
    line_owners: list[str] = []
    for key in emit_order:
        value = fields.get(key)
        if value is None:
            continue
        if key == "system" and isinstance(value, dict):
            line = _emit_system_block(value)
        elif key == "deliverables" and isinstance(value, list):
            # workstream.schema.json requires block-map items ({text: "..."}) —
            # distinct from specs/dependency_annotations, which stay plain strings.
            # Review: review-integrator (Finding 2) — the prior truthiness-gated
            # dispatch (`value and isinstance(value[0], dict)`) fell through to
            # _emit_yaml_field for an explicit empty list, which emits a bare
            # "deliverables:\n" (parses as null, not []). Normalization upstream
            # already guarantees dict-shape whenever the list is non-empty, so
            # gating on isinstance(list) alone routes [] through
            # _emit_block_map_list_field's own "if not items: return f'{key}: []'"
            # shortcut instead.
            line = _emit_block_map_list_field(key, value)
        else:
            line = _emit_yaml_field(key, value)
        if line:
            lines.append(line)
            line_owners.extend([key] * (line.count("\n") + 1))

    document = "\n".join(lines) + "\n"
    try:
        yaml.safe_load(document)
    except yaml.YAMLError as exc:
        offending_field = _offending_field_for_yaml_error(exc, line_owners)
        raise ValueError(
            f"queue.append: composed YAML document failed to parse — offending "
            f"field: {offending_field!r}. Fix the value passed for that field. "
            f"Underlying parser error: {exc}"
        ) from exc
    return document


# ---------------------------------------------------------------------------
# Content digest (collision guard — DR-213 D2(i) amendment)
# ---------------------------------------------------------------------------


def _content_digest(schema_name: str, fields: dict) -> str:
    """Compute the 12-hex-char content digest used to disambiguate the output filename.

    Reuses the in-repo ``_goal_id`` precedent (goal_append.py:77-86) at the algorithm
    level (SHA-1, 12-hex truncation) but NOT at the key-construction level: the content-key
    is a structured JSON serialization of the ordered semantic field-set, not a hand-joined
    pipe-delimited string — see Review: code-reviewer below.

    Field-set + normalization order (pinned, DR-213 D2(i) amendment — see plan
    docs/plans/2026-07-08-concurrency-safe-strangled-op-writes.md § "The collision-guard
    shape"):
        1. Computed over the finalized ``fields`` dict MINUS the ``system`` key — excludes
           provenance so cross-session writes of the same logical entry still dedup.
        2. Fixed key order — reuses ``_build_yaml``'s ``emit_order`` construction (required
           fields first, then optional fields in their declared schema-cli order) so the
           same logical entry always yields the same content-key regardless of dict
           iteration order.
        3. Uses the same normalized ``fields["body"]`` value used for file content
           (post-``\\n``→``\n`` normalization) — never the raw pre-normalization input.

    NO disk read — the digest is computed entirely from in-hand params (DR-213 D4;
    op remains write-always / additive-create, not a dedup pre-check).
    """
    described = _schema_cli_describe(_SCHEMA_CLI_NAME.get(schema_name, schema_name))
    required: list[str] = described.get("required") or []
    optional: list[str] = described.get("optional") or []

    emit_order = list(required)
    for opt in optional:
        if opt in fields and fields[opt] is not None:
            if opt not in emit_order:
                emit_order.append(opt)

    # Review: code-reviewer — hand-joined "key=value" pipe strings had no delimiter
    # escaping; free-text fields (body, risk, proposed_action, etc.) containing '|' or
    # '=' could collide two distinct entries onto one digest. Structured JSON serialization
    # handles internal escaping so no field value can inject a false separator (Finding 1).
    ordered_content = {key: fields.get(key) for key in emit_order if key != "system"}
    content_key = json.dumps(ordered_content, ensure_ascii=False, sort_keys=False)
    full_hash = hashlib.sha1(content_key.encode("utf-8")).hexdigest()
    return full_hash[:12]


# ---------------------------------------------------------------------------
# Slug and date helpers
# ---------------------------------------------------------------------------


def _slug_from_title(title: str) -> str:
    """Sanitize a title into a filesystem-safe slug (40 chars max).

    Mirrors coordinator-queue-append._slug_from_title:
        lowercase → collapse non-[a-z0-9] runs to '-' → strip leading/trailing '-'
        → truncate to 40 chars → rstrip('-') (truncation can leave trailing hyphen).
    """
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:_SLUG_MAX_CHARS].rstrip("-")


def _today_iso() -> str:
    """Return today's date in YYYY-MM-DD format."""
    return datetime.date.today().isoformat()


# ---------------------------------------------------------------------------
# Workstream-store filename-component validation (AC14, path-traversal guard)
# ---------------------------------------------------------------------------

# Conservative allowlist charset — mirrors coordinator-queue-append's
# _WORKSTREAM_IDENTIFIER_RE exactly. No path separators, no leading dot/hyphen,
# non-empty.
_WORKSTREAM_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

# `created` is a DATE, not a bare identifier — the identifier allowlist above is
# the wrong oracle for it (dashes are load-bearing date separators, not an
# arbitrary charset). Used by ``_validate_workstream_created`` below.
#
# Review: code-reviewer (Finding 1/2) — `\d` on a str pattern matches any Unicode
# category-Nd digit (fullwidth, Devanagari, etc.), not just [0-9]; and `.match()`
# against a `$`-terminated pattern accepts one trailing "\n" that `.fullmatch()`
# would reject. [0-9] (over re.ASCII) is more obviously scoped at this call site,
# and .fullmatch() closes the trailing-newline gap without touching the pattern.
_WORKSTREAM_CREATED_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _validate_workstream_created(value: str) -> None:
    """Reject a non-ISO-date ``created`` value before it reaches ``_output_path``.

    Review: review-integrator (Finding 1) — ``created`` is interpolated into the
    ``workstream-event`` filename (``{created}-{workstream}-{session}.yaml``) by
    ``_output_path`` but is not a declared property of workstream-event.schema.json
    and the schema has no top-level ``additionalProperties: false``, so ``_validate``
    raises nothing for an arbitrary ``created`` string. Without this guard a caller
    supplying ``created="../../../evil"`` reaches ``_output_path`` unvalidated and
    ``os.makedirs`` creates directories outside ``state/workstreams/events/``. This is
    a narrower, date-shaped guard rather than a fourth entry in
    ``_validate_workstream_identifier``'s allowlist tuple — a bare identifier charset
    would incorrectly reject the very date separators this field requires.

    Callers MUST invoke this before any path construction for workstream-event,
    same discipline as ``_validate_workstream_identifier``. For the ``workstream``
    schema call site, ``created`` is not a filename component (see ``_output_path``'s
    ``else`` branch) — this is validation-coverage symmetry only, not a traversal
    guard there.

    Review: coordinator:code-reviewer (Finding 3) — the docstring previously
    overclaimed a traversal rationale for both call sites without distinguishing
    them; the CLI's parallel docstring already draws this distinction.

    Review: review-integrator (Finding 6) — this validates DATE SHAPE only
    (``YYYY-MM-DD``, ASCII digits); it does not confirm the value is a real
    calendar date (e.g. ``"9999-99-99"`` passes). Not a traversal risk either way.
    """
    if not value or not _WORKSTREAM_CREATED_DATE_RE.fullmatch(value):
        raise ValueError(
            f"queue.append: created must match {_WORKSTREAM_CREATED_DATE_RE.pattern} "
            f"(ISO date, YYYY-MM-DD), got {value!r}"
        )


def _validate_workstream_identifier(param_name: str, value: str) -> None:
    """Reject path-traversal-shaped values for workstream-store filename components.

    Applies to ``workstream_id`` / ``workstream`` / ``session`` — all three are
    interpolated directly into filenames by ``_output_path``'s ``workstream`` /
    ``workstream-event`` branches (Part 1). Rejects empty values, path separators,
    ``".."`` segments, and leading dots via the allowlist charset above — fails
    loud (``ValueError``) rather than silently sanitizing.

    Ports coordinator-queue-append._validate_workstream_identifier: same regex,
    same rejection surface, but raises ``ValueError`` instead of calling
    ``parser.error`` — this op has no argparse parser. The regression net asserts
    ``pytest.raises(ValueError, match=r"workstream_id")``, so the message MUST name
    the offending parameter.

    Callers MUST invoke this before any path construction (``_output_path``,
    ``os.makedirs``, or either write-primitive helper below) — see the call site
    in ``append_queue_entry``.

    Spec backlink: pln-teach-the-native-queue-append--8bd701 § C4, AC14
    """
    # Review: coordinator:code-reviewer — .match() against a `$`-anchored
    # pattern lets a trailing "\n" through (Python's `$` is satisfied before a
    # single trailing newline); .fullmatch() requires the whole string consumed.
    if not value or not _WORKSTREAM_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"queue.append: {param_name} must match {_WORKSTREAM_IDENTIFIER_RE.pattern} "
            f"(no path separators, no leading dot, non-empty), got {value!r}"
        )


# ---------------------------------------------------------------------------
# Claude-Klabauter root and machine-local helpers
# ---------------------------------------------------------------------------


def _claude_home() -> str:
    """Return the ~/.claude root, honouring CLAUDE_HOME env var for test isolation."""
    override = os.environ.get(_CLAUDE_HOME_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude")


def _machine_local_impl() -> str:
    """Return the path to _machine_local.py, honouring MACHINE_LOCAL_IMPL for tests."""
    # TODO(33cf462): de-dup _claude_home/_machine_local_impl into shared module
    # (queued: state/improvement-queue/2026-07-06-claude-klabauter-live-root-shared-helper-extraction.yaml)
    override = os.environ.get(_MACHINE_LOCAL_IMPL_ENV)
    if override:
        return override
    settings_home_impl = os.path.join(settings_home(), "bin", "_machine_local.py")
    if os.path.exists(settings_home_impl):
        return settings_home_impl
    return os.path.join(_claude_home(), "bin", "_machine_local.py")


def _machine_local_get(key: str) -> Optional[str]:
    """Call ``machine-local get <key>`` and return the value, or None on failure."""
    impl = _machine_local_impl()
    try:
        result = subprocess.run(
            [sys.executable, impl, "get", key],
            capture_output=True,
            text=True,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _machine_local_get: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return result.stdout.strip()


def _claude_klabauter_root() -> Optional[str]:
    """Resolve the claude-klabauter repo root.

    Resolution chain:
        1. ``COORDINATOR_ENGINE_ROOT`` env var (via the accessor) — trusted
           as-is, but ONLY when this process is the one the caller ran in
           (see below).
        2. ``machine-local get repos.claude_klabauter``.
        3. Returns None when unresolvable; callers degrade gracefully (WARN+skip).

    The engine-root env var is a property of a CALLING process, same as
    ``QUEUE_APPEND_OUTPUT_ROOT`` (see ``_output_root_override``'s docstring for the
    full warm-server hazard). Under the warm engine this op executes in a
    long-lived server process whose environment was inherited from whichever
    session happened to spawn it, so trusting the raw read under warm serving
    would name the SPAWNER's root rather than the current caller's — the write
    exits 0, prints a normal path, and lands nowhere the caller can see.
    ``execution_route() == IN_PROCESS`` is true for every non-server process, so
    the env var stays honoured everywhere except the served route, which falls
    through to the machine-local registry lookup instead (correct in both routes,
    since it resolves the true repo root rather than a caller-scoped override).

    Spec backlink: pln-stop-the-rot-claude-klabauter-state-home-placement-4cc787 § AC13
    """
    override = (coordinator_engine_root_env(__name__) or "").strip()
    if override and op_latency.execution_route() == op_latency.IN_PROCESS:
        return _refuse_published_mirror(override)
    # Rung 1.5: the transform-proof key. Under the publish identifier
    # transform the registry key in Rung 2 below is rewritten to name the
    # published mirror, so the published engine resolves "the central repo"
    # to itself and this write is lost. `engine.source_root` contains no repo
    # token and survives publish intact, so the mirror-run engine reaches the
    # live tree here rather than falling through to a refusal. Absent on a
    # consumer install, where Rung 2 is already correct.
    source_root = _engine_source_root()
    if source_root:
        return source_root
    val = _machine_local_get("repos.claude_klabauter")
    return _refuse_published_mirror(val) if val else None


def _refuse_published_mirror(root: str) -> str:
    """Refuse a resolved root that is the published engine mirror.

    This op does NOT route through ``coordinator_core.state_root``, so it does
    not inherit that module's published-mirror guard — it resolves its own root
    and writes to it directly. Under the publish identifier transform the
    registry key this resolver reads is rewritten to name the mirror, so the
    published engine resolves "the central repo" to ITSELF and central-scope
    entries land in a gitignored build artifact: exit 0, plausible printed
    path, content readable by nobody. Two entries were lost that way before
    anyone noticed, and only because one happened to trip an unrelated guard.

    Raising ``_ClaudeKlabauterUnresolvable`` degrades through the op's existing
    skip-with-reason path, so the caller gets the remediation instead of a
    write it will never find again.

    Spec backlink: state/bug-backlog/2026-08-20-central-scope-queue-entries-land-in-the-6a0c80dedc44.yaml
    Inventory: state/audits/2026-08-21-transform-resolved-writer-inventory.md
    """
    if not _is_published_engine_mirror(root):
        return root
    raise _ClaudeKlabauterUnresolvable(
        f"queue-append resolved its target repo to the PUBLISHED engine mirror "
        f"('{root}'), not a live working tree — refusing to write a queue entry "
        f"into a build artifact, where it would be gitignored and lost. "
        f"Remediate: run this from a live working-tree checkout, or set the "
        f"engine-root environment variable to one."
    )


def _claude_klabauter_root_unresolved_detail() -> str:
    """Diagnostic detail for a `_claude_klabauter_root()` miss (F12, staff-eng review
    2026-08-20) -- names the missing registry key explicitly, and
    distinguishes "genuinely unset" from "COORDINATOR_ENGINE_ROOT is set but
    discarded under warm serving" (the latter reads as a silent no-write
    otherwise: the env var IS present, just not the current caller's, per
    `_claude_klabauter_root`'s own docstring on the warm-server hazard).

    No fallback rung is added here -- a wrong root writing into a stranger's
    tree is worse than a skipped write (accepted as-is; direction-class per
    the review). This only makes the skip diagnosable.
    """
    override = (coordinator_engine_root_env(__name__) or "").strip()
    if override and op_latency.execution_route() != op_latency.IN_PROCESS:
        return (
            f"repos.claude_klabauter not set in machine-local registry; "
            f"COORDINATOR_ENGINE_ROOT env var IS set ({override!r}) but discarded "
            f"under warm serving "
            f"(execution_route={op_latency.execution_route()!r} != IN_PROCESS) -- a "
            "warm-served process inherits its SPAWNER's environment, not the "
            "current caller's, so trusting it here risks writing into a stranger's "
            "tree. Register the key: machine-local set repos.claude_klabauter "
            "/path/to/claude-klabauter"
        )
    if not override:
        return "repos.claude_klabauter not set in machine-local registry and COORDINATOR_ENGINE_ROOT env var not set"
    return "repos.claude_klabauter not set in machine-local registry"


def _same_path(a: str, b: str) -> bool:
    """Thin alias onto ``coordinator_core.win_portability.same_path`` -- the
    consolidated primitive (state/sizings/2026-08-07-path-equality-
    consolidates-onto-one-prim.yaml). Promoted from realpath-only to
    samefile-then-fallback semantics: broader (junction-aware) equality is
    correct here since this call site only checks "is caller_worktree the
    meta-repo home", where a junction-aliased home must compare equal."""
    return same_path(a, b)


# ---------------------------------------------------------------------------
# Output path resolution
# ---------------------------------------------------------------------------


def _output_path(
    schema_name: str,
    title: str,
    caller_worktree: Optional[Path],
    queue_scope: Optional[str],
    digest12: str,
    *,
    workstream_id: Optional[str] = None,
    workstream: Optional[str] = None,
    session: Optional[str] = None,
    created: Optional[str] = None,
) -> str:
    """Compute the full output path for a new queue entry.

    Precedence (mirrors coordinator-queue-append._output_path):
        1. ``QUEUE_APPEND_OUTPUT_ROOT`` env override (test isolation) -- honoured
           ONLY on the in-process route; see ``_output_root_override``.
        2. ``queue_scope == "central"`` → claude-klabauter root. Raises ``_ClaudeKlabauterUnresolvable``
           when CLAUDE_KLABAUTER_ROOT cannot be resolved — caller degrades gracefully.
        3. Project scope → ``caller_worktree``:
           - ``caller_worktree == ~/.claude`` (meta-repo) → route to claude-klabauter root.
           - Other worktree → use worktree directly.
           - ``caller_worktree is None`` (op not in _OP_KEY_SCOPE yet) → fall back to
             CLAUDE_KLABAUTER_ROOT or raise _ClaudeKlabauterUnresolvable (no cwd fallback in daemon context).

    Filename keying (C4, docs/plans/2026-08-03-queue-append-workstream-store-schemas.md):
        - ``workstream`` (definition): ``<workstream_id>.yaml`` — no date, no digest.
          Single-file-per-id by design (a second write to the same id REWRITES the
          canonical file — see ``_write_out_path_overwrite``); a content digest would
          defeat that, forking a second file for a merely-edited definition. This is a
          deliberate omission, not an oversight — do not "restore" the digest here.
        - ``workstream-event``: ``<date>-<workstream>-<session>.yaml`` — no digest.
          Events are already disambiguated by workstream+date+session (and, on a
          genuine base-path collision, by ``_write_out_path_excl``'s retry-suffix
          loop) — a content digest is redundant collision-avoidance for this schema
          and is deliberately not applied here either.
        - Every other (pre-existing) schema: unchanged content-keyed
          ``<date>-<slug>-<digest12>.yaml`` shape below (AC8).

    Filename is content-keyed (DR-213 D2(i) amendment, 2026-07-08) for the schemas NOT
    covered by the branch above: the trailing ``-<digest12>`` component disambiguates
    distinct same-date+slug entries so they coexist as separate files instead of
    silently overwriting one another, while a genuine re-run of an identical entry
    still dedups to one file (same digest, same filename, ``os.replace`` overwrite).
    See ``_content_digest`` for digest mechanics.

    Spec backlink: pln-strang-08-queue-append-strangl-2a3499 § C1 / AC13
    Spec backlink (content-keying): pln-concurrency-safe-writes-for-th-c7ca9f § C1
    Spec backlink (store-schema filename keying): pln-teach-the-native-queue-append--8bd701 § C4
    """
    output_dir = _output_dir_for_schema(schema_name)
    override_root = _output_root_override()

    if override_root:
        base = os.path.join(override_root, output_dir)
    elif queue_scope == "central":
        if schema_name not in ("improvement-queue", "lessons"):
            raise RuntimeError(
                f"queue.append: central queue_scope only valid for improvement-queue or lessons, "
                f"got {schema_name!r}"
            )
        claude_klabauter_root = _claude_klabauter_root()
        if claude_klabauter_root is None:
            raise _ClaudeKlabauterUnresolvable(_claude_klabauter_root_unresolved_detail())
        base = os.path.join(claude_klabauter_root, output_dir)
    elif caller_worktree is not None:
        home = _claude_home()
        if _same_path(str(caller_worktree), home):
            # Meta-repo caller → route to claude-klabauter (stop-the-rot taxonomy).
            claude_klabauter_root = _claude_klabauter_root()
            if claude_klabauter_root is None:
                raise _ClaudeKlabauterUnresolvable(
                    "cannot route meta-repo per-repo state to claude-klabauter: "
                    + _claude_klabauter_root_unresolved_detail()
                )
            base = os.path.join(claude_klabauter_root, output_dir)
        else:
            # Sibling repo → per-repo state stays in the repo itself.
            base = os.path.join(str(caller_worktree), output_dir)
    else:
        # caller_worktree is None — _OP_KEY_SCOPE entry may be missing; fallback
        # to claude-klabauter root (daemon context has no meaningful cwd anchor).
        claude_klabauter_root = _claude_klabauter_root()
        if claude_klabauter_root is None:
            raise _ClaudeKlabauterUnresolvable(
                "caller_worktree not provided; cannot resolve output path: "
                + _claude_klabauter_root_unresolved_detail()
            )
        base = os.path.join(claude_klabauter_root, output_dir)

    if schema_name == "workstream":
        filename = f"{workstream_id}.yaml"
    elif schema_name == "workstream-event":
        filename = f"{created}-{workstream}-{session}.yaml"
    else:
        date_str = _today_iso()
        slug = _slug_from_title(title)
        filename = f"{date_str}-{slug}-{digest12}.yaml"
    return os.path.join(base, filename)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(schema_name: str, fields: dict) -> None:
    """Validate field values against the schema via ``schema_validate.validate`` (in-process).

    Mirrors coordinator-queue-append._validate.
    Strips None/"" values before passing (absent keys = missing required fields).
    """
    # Fail-fast unknown-schema guard (its raise IS the check; the real output-dir
    # computation happens later in _output_path).
    # Review: code-reviewer — this duplicates the guard call in append_queue_entry and
    # _output_path; intentional defense-in-depth across independently-callable boundaries
    # (_validate and _output_path are each called on their own, e.g. in tests) — do not
    # "simplify" this away. With Finding 1's memoization these repeat calls are cache
    # hits, not extra re-derivation (Finding 2).
    _output_dir_for_schema(schema_name)

    effective_fields = {k: v for k, v in fields.items() if v is not None and v != ""}
    cli_schema_name = _SCHEMA_CLI_NAME.get(schema_name, schema_name)
    ok, errors_list = _schema_cli_validate(cli_schema_name, effective_fields)

    if not ok:
        if not errors_list:
            raise ValueError("queue.append: validation failed (no error detail returned)")

        # errors_list is schema_validate's structured [{field, error, hint}, ...] shape
        # (see _schema_cli_validate docstring) — dispatch on the "error" text directly
        # rather than regex-parsing a pre-flattened "field: error" string.
        first_error = errors_list[0]
        field = first_error.get("field")
        error_text = first_error.get("error", "")

        if error_text == "required field missing":
            raise ValueError(f"queue.append: missing required field: {field}")

        m_enum = re.match(r'^invalid enum value "(.+)"$', error_text)
        if m_enum:
            value = m_enum.group(1)
            try:
                described = _schema_cli_describe(cli_schema_name)
                enums = described.get("enums") or {}
                allowed = enums.get(field) or []
            except RuntimeError:
                allowed = []
            if allowed:
                raise ValueError(
                    f"queue.append: invalid value for {field!r}: {value!r}. "
                    f"Valid values: {', '.join(str(v) for v in allowed)}."
                )
            raise ValueError(f"queue.append: invalid value for {field!r}: {value!r}.")

        raise ValueError(f"queue.append: {field}: {error_text}")


# ---------------------------------------------------------------------------
# Write primitives (C4 — definition overwrite vs. event create-only)
# ---------------------------------------------------------------------------

# Bounded retry attempts before _write_out_path_excl fails loud. Mirrors
# coordinator/bin/lib/cli_shared.COLLISION_RETRY_CAP verbatim (not imported —
# this module has no dependency on the coordinator/bin/lib CLI-shared package).
_COLLISION_RETRY_CAP = 1000


def _write_out_path_overwrite(out_path: str, content: str) -> str:
    """Write ``content`` to ``out_path`` via write-temp + atomic rename, OVERWRITING
    any existing file at ``out_path``.

    Distinct from ``_write_out_path_excl`` (create-only, retry-with-suffix on
    collision): this helper is for definition files that are genuinely
    REWRITTEN in place — a second write to the same ``workstream_id`` must
    update the single canonical ``<id>.yaml``, not fork a ``<id>-2.yaml``
    sibling. Last-write-wins is acceptable here per the single-writer,
    low-contention assumption — now ratified under DR-257.

    This is also the pre-existing atomic-write shape this module already used
    unconditionally before C4 (mkstemp + os.replace) — extracting it into a
    named helper does not change its behavior for the five pre-existing
    schemas (AC8), which keep resolving through this same function.

    Negative-spec: do NOT swap this in for ``workstream-event`` writes — events
    are append-only-by-design (two events for the same base path must BOTH
    survive under distinct filenames); overwrite semantics here would
    silently drop one event's history.

    Spec backlink: pln-teach-the-native-queue-append--8bd701 § C4
    Parity oracle: coordinator/bin/coordinator-queue-append._write_out_path_overwrite
    """
    directory = os.path.dirname(out_path) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=os.path.basename(out_path) + ".",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        os.replace(tmp_path, out_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    return out_path


def _write_out_path_excl(out_path: str, content: str) -> str:
    """Write ``content`` to ``out_path`` using an exclusive-create + retry-with-suffix loop.

    Ports coordinator-queue-append._write_out_path_excl (itself a thin wrapper
    over ``bin/lib/cli_shared.write_path_excl``) — inlined here rather than
    importing the CLI-shared module, since this engine module has no dependency
    on ``coordinator/bin/lib``.

    Wired to ``workstream-event`` only (C4): events are append-only-by-design,
    so two events sharing a base path (same ``<date>-<workstream>-<session>``
    key) must BOTH survive under distinct filenames rather than one silently
    clobbering the other.

    Negative-spec: do NOT swap this for a plain ``os.replace()``/``open("w")``
    — that silently clobbers a same-key concurrent write. Do NOT swap this for
    a bare fail-loud ``FileExistsError`` either — this op is a terminal writer
    with no retry path of its own, so failing loud on the FIRST collision would
    drop the entry rather than preserve it; retry-with-suffix is required.

    Returns the actual path written (== ``out_path`` unless a collision suffix
    was used).

    Spec backlink: pln-teach-the-native-queue-append--8bd701 § C4
    Parity oracle: coordinator/bin/lib/cli_shared.write_path_excl
    """
    root, ext = os.path.splitext(out_path)
    candidate = out_path
    attempt = 1
    while True:
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            attempt += 1
            if attempt > _COLLISION_RETRY_CAP:
                raise FileExistsError(
                    f"queue.append: refusing to drop entry — exhausted "
                    f"{_COLLISION_RETRY_CAP} collision-retry attempts for base path "
                    f"{out_path!r}. All candidate filenames already exist. "
                    f"Tried {out_path!r} through {root!r}-{_COLLISION_RETRY_CAP}{ext!r}."
                ) from None
            candidate = f"{root}-{attempt}{ext}"
            continue
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        return candidate


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def append_queue_entry(
    schema: str,
    *,
    title: Optional[str] = None,
    body: Optional[str] = None,
    status: Optional[str] = None,
    created: Optional[str] = None,
    from_repo: Optional[str] = None,
    surface: Optional[str] = None,
    proposed_action: Optional[str] = None,
    tags: Optional[list] = None,
    evidence: Optional[str] = None,
    closed_at: Optional[str] = None,
    closed_by: Optional[str] = None,
    source: Optional[str] = None,
    risk: Optional[str] = None,
    severity: Optional[str] = None,
    why_blocked: Optional[str] = None,
    repro_steps: Optional[str] = None,
    environment: Optional[str] = None,
    change_kind: Optional[str] = None,
    queue_scope: Optional[str] = None,
    scope: Optional[str] = None,
    target_wiki: Optional[str] = None,
    proposed_target: Optional[str] = None,
    trigger: Optional[str] = None,
    why: Optional[str] = None,
    how_to_apply: Optional[str] = None,
    committed_by: Optional[str] = None,
    memo: Optional[str] = None,
    commitment: Optional[str] = None,
    observed: Optional[str] = None,
    created_by_agent: Optional[str] = None,
    caller_worktree: Optional[Path] = None,
    session_id: Optional[str] = None,
    **schema_fields: object,
) -> dict:
    """Append one YAML entry to the appropriate queue state directory.

    Byte-parity port of coordinator-queue-append main() write path.

    Parameters mirror coordinator-queue-append CLI flags (underscored).
    ``caller_worktree`` replaces the CLI's cwd-based git root detection (F1/AC13).
    ``session_id`` is pre-resolved by the caller (daemon env may differ from CLI env).

    ``title``/``body`` are keyword-optional (not positional-required): the
    ``workstream-event`` schema requires neither (see workstream-event.schema.json),
    so the shared base signature cannot mandate them for every schema. The five
    pre-existing schemas are unaffected — every in-repo caller already passes both
    by keyword (grepped before this change), and each of those schemas still
    requires ``title``/``body`` via the contract (``_validate`` raises "missing
    required field" if omitted, same as always for those five).

    ``**schema_fields`` — CONTRACT-DERIVED plumbing for fields the base ~30-param
    hand list above does not name (workstream_id, workstream, field, value,
    sequence, session, deliverables, specs, dependency_annotations, supersedes,
    coordinator_root_path, and any future DoE schema field). Only keys the
    contract actually declares (``describe(schema).required``/``.optional``) for
    THIS ``schema`` are accepted into the emitted entry; anything else is
    silently dropped here (with a WARN log, see the merge loop below) — a
    caller typo on a REQUIRED field surfaces downstream as _validate's own
    "missing required field" error; a typo on an OPTIONAL field does NOT (there
    is no missing-field signal for a field the schema never required), which is
    why the merge loop emits its own warning rather than relying on _validate
    alone (see ``_output_dir_for_schema``, the in-file precedent for
    contract-derivation over a hand-copy, and the module docstring).

    Decision record — ``session`` (workstream-event's required lexical-tiebreaker
    field, part of the emitted document AND a C4 filename component) is NOT the
    same value as ``session_id`` (this op's already-resolved provenance field,
    used only for the ``system:`` block the base five schemas emit). They are
    kept as two distinct values on purpose: workstream-event does not declare a
    ``system`` property at all (see workstream-event.schema.json — no ``system``
    key), so its ``session`` field is a caller-supplied ``schema_fields`` entry,
    never auto-derived from ``session_id``. Mirrors the CLI oracle, which also
    never derives ``--session`` from its own ``_resolve_session_id()``.

    Returns:
        {out_path: str, schema: str, slug: str, title: str}

    Raises:
        ValueError — invalid schema, missing required field, invalid enum value.
        RuntimeError — schema-cli.js unreachable or infra error.
        _ClaudeKlabauterUnresolvable — central scope and CLAUDE_KLABAUTER_ROOT unresolvable (caller
            catches and degrades gracefully).
    """
    # Validate schema. Fail-fast unknown-schema guard (its raise IS the check; the
    # real output-dir computation happens later in _output_path).
    # Review: code-reviewer — same intentional defense-in-depth as _validate's guard
    # call below; see that call site's comment (Finding 2).
    _output_dir_for_schema(schema)

    # Apply schema-specific status defaults.
    if schema == "lessons" and status is None:
        status = "open"

    # Resolve created date.
    if created is None:
        created = _today_iso()

    # Normalise body newlines (CLI does this for \\n → \n). body is optional
    # (workstream-event does not declare a body property) — guard the None case.
    if body is not None:
        body = body.replace("\\n", "\n")

    # Resolve session_id when not pre-supplied — via the canonical resolver,
    # never a raw os.environ read. Under warm serving the process environment
    # names the server's spawner rather than this request's caller, so an env
    # read stamps queue entries with a stranger's authorship;
    # `resolve_current_session_id` reads the per-request identity
    # `warm.entry_seam.per_request_state` binds and falls through to the same
    # env ladder cold.
    if session_id is None:
        session_id = (resolve_current_session_id() or "").strip()

    # Resolve from_repo fallback. cross-repo-commitment forbids from_repo entirely
    # (it uses committed_by for the sibling identity instead) — mirrors the DoE
    # CLI's explicit strip of from_repo for this schema.
    if from_repo is None and schema != "cross-repo-commitment":
        if caller_worktree is not None:
            from_repo = os.path.basename(str(caller_worktree)) + "-em"
        else:
            from_repo = "unknown-sender-em"

    # Build system provenance block.
    system: dict = {}
    if session_id:
        system["created_by_session"] = session_id
    if created_by_agent:
        system["created_by_agent"] = created_by_agent
    system["linked_sessions"] = [session_id] if session_id else []
    # linked_commits: omitted — not available at write time.
    system["provenance_completeness"] = "complete" if session_id else "unknown"

    fields: dict = {
        "created": created,
        "title": title,
        "body": body,
        "status": status,
        "from_repo": from_repo,
        "surface": surface,
        "proposed_action": proposed_action,
        "closed_at": closed_at,
        "closed_by": closed_by,
        "tags": tags,
        "evidence": evidence,
        "source": source,
        "risk": risk,
        "severity": severity,
        "why_blocked": why_blocked,
        "repro_steps": repro_steps,
        "environment": environment,
        "change_kind": change_kind,
        "queue_scope": queue_scope,
        "scope": scope,
        "target_wiki": target_wiki,
        "proposed_target": proposed_target,
        "trigger": trigger,
        "why": why,
        "how_to_apply": how_to_apply,
        "committed_by": committed_by,
        "memo": memo,
        "commitment": commitment,
        "observed": observed,
        "system": system,
    }

    # Contract-derived plumbing (G3/G4): merge any caller-supplied schema_fields
    # the contract declares (required or optional) for THIS schema — e.g.
    # workstream_id/workstream/field/value/sequence/session/deliverables/specs/
    # dependency_annotations/supersedes/coordinator_root_path for the
    # workstream/workstream-event schemas. Deliberately NOT a hand-maintained
    # field-name list — see this function's docstring and _output_dir_for_schema
    # (the in-file precedent for contract-derivation over a hand-copy). A key
    # the contract does not declare for this schema is silently dropped here; a
    # required field the caller omits still surfaces via _validate below.
    # Review: review-integrator (Finding 4) — safe to call describe() here without
    # re-checking schema validity: _output_dir_for_schema(schema) above already
    # fail-fasted (ValueError) if `schema` were unresolvable, so this describe()
    # call can never be the first one to observe an unknown schema. Do not reorder
    # this above that guard — the two describe() call sites raise different
    # exception types for the same underlying failure (ValueError here vs.
    # RuntimeError via _schema_cli_describe_cached), so a reorder would silently
    # flip test_append_queue_entry_unknown_schema_raises_value_error's expected
    # exception type (see Finding 4 in the reviewer sidecar).
    described = _schema_cli_describe(_SCHEMA_CLI_NAME.get(schema, schema))
    contract_field_names = set(described.get("required") or []) | set(
        described.get("optional") or []
    )
    for key, value in schema_fields.items():
        if key in contract_field_names and key not in fields:
            fields[key] = value
        elif key not in contract_field_names:
            # Review: code-reviewer (Finding 1) — WARN, do not raise: an optional
            # contract field not yet reflected in a stale schema cache would
            # otherwise be rejected here even though it is legitimately valid
            # (forward-compat), so a hard failure is the wrong shape. A warning
            # still leaves a signal for a genuine typo (e.g. "supercedes" for
            # "supersedes") instead of the field silently vanishing.
            logger.warning(
                "queue.append: schema_fields key %r is not declared by schema %r "
                "(required or optional) — dropping it silently would otherwise "
                "hide a typo; check the field name against the schema contract.",
                key,
                schema,
            )

    # workstream.schema.json's deliverables is a BLOCK-MAP (object-with-text
    # items), not a plain string list — mirror coordinator-queue-append's
    # workstream branch conversion so schema_validate and _build_yaml's
    # block-map emission see the same {"text": ...} shape regardless of
    # whether the caller already supplied block-map dicts or plain strings.
    if fields.get("deliverables"):
        fields["deliverables"] = [
            item if isinstance(item, dict) else {"text": item}
            for item in fields["deliverables"]
        ]

    # Path-traversal guard (AC14) — MUST run before any path construction
    # (_output_path, os.makedirs, either write-primitive helper). workstream_id/
    # workstream/session become filename components in _output_path's `workstream`
    # / `workstream-event` branches (Part 1); reject traversal-shaped values here,
    # before _content_digest/_output_path/os.makedirs are ever reached. Only
    # validates a component when the caller actually supplied it — an absent
    # required field still surfaces via _validate's own "missing required field"
    # error below, not this guard.
    for _identifier_param in ("workstream_id", "workstream", "session"):
        _identifier_value = fields.get(_identifier_param)
        if _identifier_value is not None:
            _validate_workstream_identifier(_identifier_param, str(_identifier_value))

    # Review: review-integrator (Finding 1) — `created` is also a filename component
    # for `workstream-event` (_output_path's f"{created}-{workstream}-{session}.yaml"
    # branch) but is a bare keyword param, never routed through schema_fields/the
    # contract, so it sits outside the identifier-allowlist loop above. Validate it
    # as an ISO date here, before _content_digest/_output_path/os.makedirs, mirroring
    # the AC14 guard's placement discipline.
    # Review: review-integrator (Finding 7) — `workstream`'s `created` is not a
    # filename component (no traversal exposure), but is required and otherwise
    # left asymmetrically unvalidated next to the discipline just applied above;
    # validated here too for validation-coverage symmetry (informational only).
    if schema in ("workstream-event", "workstream"):
        _validate_workstream_created(str(created))

    # coordinator_root_path — THE LANDMINE (2026-07-22 fold-correctness outage).
    # VALUE is the repo-root-relative form the cockpit contract declares: the
    # literal "." for the auto-resolve case, byte-identical to what
    # coordinator-queue-append's _WORKSTREAM_STORE_SCHEMAS branch stamps (see
    # that CLI's `coordinator_root_path = (...)` assignment comment). Only
    # defaulted when the contract declares this field for `schema` (workstream /
    # workstream-event today); a no-op for schemas that don't. NEVER derived
    # from caller_worktree (that yields a basename — see from_repo's fallback
    # above) and NEVER an absolute path (an absolute value minted a
    # machine-specific coordinator_root_path -> a distinct repo_fk per
    # machine/checkout for one logical repo -- the outage this must not
    # re-arm). caller_worktree remains the filesystem anchor for the OUTPUT
    # PATH (_output_path), never the source of this field's value. No
    # `git rev-parse` shell-out here, ever.
    if "coordinator_root_path" in contract_field_names and not fields.get(
        "coordinator_root_path"
    ):
        fields["coordinator_root_path"] = "."

    # Validate.
    _validate(schema, fields)

    # Build YAML content (ordered string formatting — F2 / AC12).
    yaml_content = _build_yaml(schema, fields)

    # Compute content digest (no disk read — in-hand params only, DR-213 D4).
    digest12 = _content_digest(schema, fields)

    # Compute output path (_ClaudeKlabauterUnresolvable propagates to caller).
    out_path = _output_path(
        schema,
        title,
        caller_worktree,
        queue_scope,
        digest12,
        workstream_id=fields.get("workstream_id"),
        workstream=fields.get("workstream"),
        session=fields.get("session"),
        created=created,
    )
    os.makedirs(os.path.dirname(out_path), exist_ok=True)

    # Write-primitive selection (C4). `workstream-event` is append-only-by-design
    # (two events sharing a base path must BOTH survive) -> exclusive-create +
    # retry-with-suffix (_write_out_path_excl). Every other schema — `workstream`
    # definitions (single-file-per-id, a second write REWRITES the canonical file)
    # AND the five pre-existing content-keyed schemas (AC8, unchanged behavior) —
    # keeps the write-temp + atomic-rename overwrite shape this module already used
    # unconditionally before C4 (_write_out_path_overwrite).
    if schema == "workstream-event":
        final_path = _write_out_path_excl(out_path, yaml_content)
    else:
        final_path = _write_out_path_overwrite(out_path, yaml_content)

    # Latent-bug fix (C4, in-scope carve-out): title is None for workstream-event
    # (that schema declares no title property — see append_queue_entry's own
    # docstring), so an unconditional _slug_from_title(title) call here raised
    # AttributeError on title.lower() the moment C2/C3 made this schema's fields
    # validate successfully (pre-C2/C3 a "missing required field" error always
    # fired before this line was reached, masking the crash). slug is title-
    # derived metadata only, never a filename component for either store schema
    # (see _output_path's `workstream`/`workstream-event` branches) — None is a
    # safe, correct result for a schema with no title.
    slug = _slug_from_title(title) if title is not None else None
    return {"out_path": final_path, "schema": schema, "slug": slug, "title": title}


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("queue.append")
def _queue_append_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC ``queue.append`` handler — write a queue YAML entry.

    MUTATING (writes per-entry YAML to one of five state/ subdirs).
    Delegates to ``append_queue_entry()``.

    ``repo_root`` receives ``git_common_dir(caller_worktree)`` via the
    ``_OP_KEY_SCOPE: common_dir`` mechanism (ipc.py). The handler calls
    ``main_worktree_root(repo_root)`` to derive the caller's worktree root
    before any ``state/`` path construction (F1 / AC13 — never daemon cwd).

    Required params:
        schema  (str) — any queue schema the DoE schema contract defines (output dir
            is contract-derived via schema-cli --describe applies_to, not a hardcoded
            list). Current schemas: debt-backlog, bug-backlog, improvement-queue,
            lessons, cross-repo-commitment, workstream, workstream-event.
        title   (str) — one-line entry summary. Required by contract for every
            schema except workstream-event (``_validate`` enforces this per-schema,
            not this handler).
        body    (str) — multi-line description. Same per-schema requiredness note
            as ``title`` (workstream/workstream-event both omit ``body``).

    Optional params (mirror coordinator-queue-append CLI flags):
        status, created, from_repo, surface, proposed_action, tags (list), evidence,
        closed_at, closed_by, source, risk, severity, why_blocked, repro_steps,
        environment, change_kind, queue_scope, scope, target_wiki, proposed_target,
        trigger, why, how_to_apply, committed_by, memo, commitment, observed,
        created_by_agent, session_id (str — caller-authoritative provenance; falls
            back to CLAUDE_CODE_SESSION_ID env when absent).

        Note: ``from_repo`` is forbidden for ``schema=cross-repo-commitment`` (the
        schema uses ``committed_by`` for the sibling identity instead); the handler
        suppresses the from_repo auto-resolution fallback for this schema.

        Any OTHER param name not listed above is passed through to
        ``append_queue_entry`` generically (as ``**schema_fields``) and accepted
        only if the DoE contract declares it for the given ``schema`` — e.g.
        workstream_id, workstream, field, value, sequence, session, deliverables,
        specs, dependency_annotations, supersedes, coordinator_root_path for the
        workstream/workstream-event schemas. This handler deliberately does not
        hand-list those field names (see ``append_queue_entry``'s own docstring
        and the module docstring's contract-derivation precedent).

    Returns:
        {out_path: str, schema: str, slug: str, title: str}

    On ``_ClaudeKlabauterUnresolvable``: logs WARN, returns ``{skipped: true, reason: "..."}``
    (exit 0 parity — graceful degrade per AC6).
    """
    # Derive caller's worktree root from the socket-authoritative common_dir.
    caller_worktree: Optional[Path] = None
    if repo_root is not None:
        caller_worktree = main_worktree_root(repo_root)

    # Resolve session_id: caller-authoritative param takes precedence, so
    # provenance is authoritative at the call site. The fallback resolves
    # through the canonical resolver rather than reading os.environ directly —
    # under warm serving the process environment names the server's spawner,
    # not this request's caller, and an env read here stamped queue entries
    # with a stranger's authorship.
    session_id = params.get("session_id")
    if session_id is None:
        session_id = (resolve_current_session_id() or "").strip()

    # Parse tags: accept list or comma-separated string.
    tags = params.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif tags is not None and not isinstance(tags, list):
        tags = None

    # Every param name append_queue_entry's base signature already names
    # explicitly (below) plus "tags" (parsed above). Anything else in `params`
    # (workstream_id, workstream, field, value, sequence, session, deliverables,
    # specs, dependency_annotations, supersedes, coordinator_root_path, or any
    # future DoE schema field) passes through generically via **schema_fields —
    # contract-derived acceptance happens inside append_queue_entry itself, not
    # here (see its docstring). Deliberately NOT a hand-maintained field list.
    _NAMED_PARAM_KEYS = frozenset(
        {
            "schema",
            "title",
            "body",
            "status",
            "created",
            "from_repo",
            "surface",
            "proposed_action",
            "tags",
            "evidence",
            "closed_at",
            "closed_by",
            "source",
            "risk",
            "severity",
            "why_blocked",
            "repro_steps",
            "environment",
            "change_kind",
            "queue_scope",
            "scope",
            "target_wiki",
            "proposed_target",
            "trigger",
            "why",
            "how_to_apply",
            "committed_by",
            "memo",
            "commitment",
            "observed",
            "created_by_agent",
            "session_id",
        }
    )
    schema_fields = {k: v for k, v in params.items() if k not in _NAMED_PARAM_KEYS}

    try:
        result = append_queue_entry(
            schema=params.get("schema", ""),
            title=params.get("title"),
            body=params.get("body"),
            status=params.get("status"),
            created=params.get("created"),
            from_repo=params.get("from_repo"),
            surface=params.get("surface"),
            proposed_action=params.get("proposed_action"),
            tags=tags,
            evidence=params.get("evidence"),
            closed_at=params.get("closed_at"),
            closed_by=params.get("closed_by"),
            source=params.get("source"),
            risk=params.get("risk"),
            severity=params.get("severity"),
            why_blocked=params.get("why_blocked"),
            repro_steps=params.get("repro_steps"),
            environment=params.get("environment"),
            change_kind=params.get("change_kind"),
            queue_scope=params.get("queue_scope"),
            scope=params.get("scope"),
            target_wiki=params.get("target_wiki"),
            proposed_target=params.get("proposed_target"),
            trigger=params.get("trigger"),
            why=params.get("why"),
            how_to_apply=params.get("how_to_apply"),
            committed_by=params.get("committed_by"),
            memo=params.get("memo"),
            commitment=params.get("commitment"),
            observed=params.get("observed"),
            created_by_agent=params.get("created_by_agent"),
            caller_worktree=caller_worktree,
            session_id=session_id,
            **schema_fields,
        )
    except _ClaudeKlabauterUnresolvable as exc:
        # AC6: graceful-degrade on unresolvable COORDINATOR_ENGINE_ROOT — WARN + skip, exit 0.
        logger.warning(
            "queue.append: COORDINATOR_ENGINE_ROOT unresolvable — skipping write: %s. "
            "Remediation: set COORDINATOR_ENGINE_ROOT or run "
            "'machine-local set repos.claude_klabauter /path/to/claude-klabauter'.",
            exc,
        )
        return {"skipped": True, "reason": str(exc)}

    # Self-report scope-touch contract (design (b), 2026-08-04 — see
    # coordinator_core.ipc's module-level comment above `_SCOPE_TOUCH_PATHS_KEY`).
    # `out_path` is the ONE file this call actually wrote (append_queue_entry's
    # write primitive is a single write-temp + atomic-rename, never a partial
    # write) — declare exactly that, never an intended/broader surface.
    # `queue_scope == "central"` routes `out_path` to the claude-klabauter root
    # regardless of the caller's own worktree; as of the 2026-08-04 F1 fix,
    # `_record_self_reported_touches` anchors containment on the CALLER's OWN
    # repo, so this declaration is SKIPPED (logged, never recorded) whenever
    # the caller's worktree isn't the claude-klabauter root itself. Deliberate — see
    # the ipc.py contract comment for why cross-repo recording was unsound.
    # `dispatch_message` strips this key before the wire envelope is built.
    result["_scope_touch_paths"] = [result["out_path"]]
    return result
