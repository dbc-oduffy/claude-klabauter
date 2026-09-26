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
]

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
from coordinator_core.win_portability import same_path
import tempfile
from pathlib import Path
from typing import Optional

# used exclusively by the C2 round-trip gate in _build_yaml to VALIDATE the
import yaml

from coordinator_core._claude_klabauter_root import _machine_local_get
from coordinator_core.frontmatter import schema_validate
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.session_context import resolve_current_session_id

logger = logging.getLogger(__name__)


_SCHEMA_CLI_NAME: dict[str, str] = {
    "lessons": "lesson-entry",
}


_SLUG_MAX_CHARS = 40
_SUBPROCESS_TIMEOUT_SECS = 15

_QUEUE_APPEND_OUTPUT_ROOT_ENV = "QUEUE_APPEND_OUTPUT_ROOT"
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
    if _is_swept_tmp_root(override):
        raise _StaleIsolationRoot(
            f"queue.append: {_QUEUE_APPEND_OUTPUT_ROOT_ENV}={override!r} resolves "
            f"under the system temp directory and no longer exists -- refusing a "
            f"write that would silently recreate and then abandon it."
        )
    return override


def _is_swept_tmp_root(path: str) -> bool:
    try:
        real = os.path.realpath(path)
        tmp = os.path.realpath(tempfile.gettempdir())
    except OSError:
        return False
    under_tmp = real == tmp or real.startswith(tmp + os.sep)
    return under_tmp and not os.path.isdir(path)


class _StaleIsolationRoot(RuntimeError):
    """Raised when QUEUE_APPEND_OUTPUT_ROOT names a swept temp-dir override
    (see `_output_root_override`). Never caught for graceful degradation --
    unlike `_ClaudeKlabauterUnresolvable`, a stale isolation root is not a normal
    "engine unregistered" state; it means this process's environment cannot
    be trusted for THIS write, and the caller must fail loud.
    """


class _ClaudeKlabauterUnresolvable(RuntimeError):
    """Raised when CLAUDE_KLABAUTER_ROOT cannot be resolved via env var or machine-local registry.

    Callers catch this and degrade gracefully (WARN+skip, exit 0) per AC6.
    Spec backlink: pln-stop-the-rot-claude-klabauter-state-home-placement-4cc787 § AC13
    """


def _schema_cli_describe(schema_name: str) -> dict:
    return _schema_cli_describe_cached(schema_name)


def _reset_schema_cli_cache() -> None:
    _schema_cli_describe_cached.cache_clear()


@functools.lru_cache(maxsize=None)
def _schema_cli_describe_cached(schema_name: str) -> dict:
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
        raise ValueError(
            f"queue.append: could not resolve output dir for schema {schema_name!r}: {exc}"
        ) from exc

    applies_to = described.get("applies_to")
    if not applies_to or not isinstance(applies_to, str):
        raise ValueError(
            f"queue.append: schema {schema_name!r} has no usable applies_to location "
            f"(got {applies_to!r})."
        )
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

    return os.path.normpath(os.path.dirname(applies_to))


def _schema_cli_validate(
    schema_name: str, fields: dict
) -> tuple[bool, list[schema_validate.ErrorDict]]:
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


def _yaml_quote_string(value: str) -> str:
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
    lines = value.splitlines()
    indented = "\n".join("  " + line if line else "" for line in lines)
    return "|-\n" + indented


def _emit_system_block(system: dict) -> str:
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
    if not items:
        return f"{key}: []"
    lines = [f"{key}:"]
    for item in items:
        lines.append(f"  - {item_key}: {_yaml_quote_string(item[item_key])}")
    return "\n".join(lines)


def _offending_field_for_yaml_error(exc: "yaml.YAMLError", line_owners: list[str]) -> str:
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
    line_owners: list[str] = []
    for key in emit_order:
        value = fields.get(key)
        if value is None:
            continue
        if key == "system" and isinstance(value, dict):
            line = _emit_system_block(value)
        elif key == "deliverables" and isinstance(value, list):
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


def _content_digest(schema_name: str, fields: dict) -> str:
    described = _schema_cli_describe(_SCHEMA_CLI_NAME.get(schema_name, schema_name))
    required: list[str] = described.get("required") or []
    optional: list[str] = described.get("optional") or []

    emit_order = list(required)
    for opt in optional:
        if opt in fields and fields[opt] is not None:
            if opt not in emit_order:
                emit_order.append(opt)

    ordered_content = {key: fields.get(key) for key in emit_order if key != "system"}
    content_key = json.dumps(ordered_content, ensure_ascii=False, sort_keys=False)
    full_hash = hashlib.sha1(content_key.encode("utf-8")).hexdigest()
    return full_hash[:12]


def _slug_from_title(title: str) -> str:
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:_SLUG_MAX_CHARS].rstrip("-")


def _today_iso() -> str:
    """Return today's date in YYYY-MM-DD format."""
    return datetime.date.today().isoformat()


# _WORKSTREAM_IDENTIFIER_RE exactly. No path separators, no leading dot/hyphen,
_WORKSTREAM_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")

_WORKSTREAM_CREATED_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _validate_workstream_created(value: str) -> None:
    """Reject a non-ISO-date ``created`` value before it reaches ``_output_path``.

    ``created`` is interpolated into the
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

    The docstring previously
    overclaimed a traversal rationale for both call sites without distinguishing
    them; the CLI's parallel docstring already draws this distinction.

    This validates DATE SHAPE only
    (``YYYY-MM-DD``, ASCII digits); it does not confirm the value is a real
    calendar date (e.g. ``"9999-99-99"`` passes). Not a traversal risk either way.
    """
    if not value or not _WORKSTREAM_CREATED_DATE_RE.fullmatch(value):
        raise ValueError(
            f"queue.append: created must match {_WORKSTREAM_CREATED_DATE_RE.pattern} "
            f"(ISO date, YYYY-MM-DD), got {value!r}"
        )


def _validate_workstream_identifier(param_name: str, value: str) -> None:
    if not value or not _WORKSTREAM_IDENTIFIER_RE.fullmatch(value):
        raise ValueError(
            f"queue.append: {param_name} must match {_WORKSTREAM_IDENTIFIER_RE.pattern} "
            f"(no path separators, no leading dot, non-empty), got {value!r}"
        )


def _claude_home() -> str:
    """Return the ~/.claude root, honouring CLAUDE_HOME env var for test isolation."""
    override = os.environ.get(_CLAUDE_HOME_ENV)
    if override:
        return override
    return os.path.join(os.path.expanduser("~"), ".claude")


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
    if (
        override
        and op_latency.execution_route() == op_latency.IN_PROCESS
        and not _is_published_engine_mirror(override)
    ):
        return override
    source_root = _engine_source_root()
    if source_root:
        return source_root
    val = _machine_local_get("repos.claude_klabauter")
    return _refuse_published_mirror(val) if val else None


def _refuse_published_mirror(root: str) -> str:
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
    return same_path(a, b)


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
            claude_klabauter_root = _claude_klabauter_root()
            if claude_klabauter_root is None:
                raise _ClaudeKlabauterUnresolvable(
                    "cannot route meta-repo per-repo state to claude-klabauter: "
                    + _claude_klabauter_root_unresolved_detail()
                )
            base = os.path.join(claude_klabauter_root, output_dir)
        else:
            base = os.path.join(str(caller_worktree), output_dir)
    else:
        # caller_worktree is None — _OP_KEY_SCOPE entry may be missing; fallback
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


def _validate(schema_name: str, fields: dict) -> None:
    _output_dir_for_schema(schema_name)

    effective_fields = {k: v for k, v in fields.items() if v is not None and v != ""}
    cli_schema_name = _SCHEMA_CLI_NAME.get(schema_name, schema_name)
    ok, errors_list = _schema_cli_validate(cli_schema_name, effective_fields)

    if not ok:
        if not errors_list:
            raise ValueError("queue.append: validation failed (no error detail returned)")

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


# coordinator/bin/lib/cli_shared.COLLISION_RETRY_CAP verbatim (not imported —
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
    _output_dir_for_schema(schema)

    if schema == "lessons" and status is None:
        status = "open"

    if created is None:
        created = _today_iso()

    if body is not None:
        body = body.replace("\\n", "\n")

    if session_id is None:
        session_id = (resolve_current_session_id() or "").strip()

    if from_repo is None and schema != "cross-repo-commitment":
        if caller_worktree is not None:
            from_repo = os.path.basename(str(caller_worktree)) + "-em"
        else:
            from_repo = "unknown-sender-em"

    system: dict = {}
    if session_id:
        system["created_by_session"] = session_id
    if created_by_agent:
        system["created_by_agent"] = created_by_agent
    system["linked_sessions"] = [session_id] if session_id else []
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

    described = _schema_cli_describe(_SCHEMA_CLI_NAME.get(schema, schema))
    contract_field_names = set(described.get("required") or []) | set(
        described.get("optional") or []
    )
    for key, value in schema_fields.items():
        if key in contract_field_names and key not in fields:
            fields[key] = value
        elif key not in contract_field_names:
            logger.warning(
                "queue.append: schema_fields key %r is not declared by schema %r "
                "(required or optional) — dropping it silently would otherwise "
                "hide a typo; check the field name against the schema contract.",
                key,
                schema,
            )

    # workstream.schema.json's deliverables is a BLOCK-MAP (object-with-text
    if fields.get("deliverables"):
        fields["deliverables"] = [
            item if isinstance(item, dict) else {"text": item}
            for item in fields["deliverables"]
        ]

    for _identifier_param in ("workstream_id", "workstream", "session"):
        _identifier_value = fields.get(_identifier_param)
        if _identifier_value is not None:
            _validate_workstream_identifier(_identifier_param, str(_identifier_value))

    if schema in ("workstream-event", "workstream"):
        _validate_workstream_created(str(created))

    # coordinator_root_path — THE LANDMINE (2026-07-22 fold-correctness outage).
    # coordinator-queue-append's _WORKSTREAM_STORE_SCHEMAS branch stamps (see
    if "coordinator_root_path" in contract_field_names and not fields.get(
        "coordinator_root_path"
    ):
        fields["coordinator_root_path"] = "."

    _validate(schema, fields)

    yaml_content = _build_yaml(schema, fields)

    digest12 = _content_digest(schema, fields)

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

    # definitions (single-file-per-id, a second write REWRITES the canonical file)
    if schema == "workstream-event":
        final_path = _write_out_path_excl(out_path, yaml_content)
    else:
        final_path = _write_out_path_overwrite(out_path, yaml_content)

    slug = _slug_from_title(title) if title is not None else None
    return {"out_path": final_path, "schema": schema, "slug": slug, "title": title}


# JSON-RPC handler


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
    caller_worktree: Optional[Path] = None
    if repo_root is not None:
        caller_worktree = main_worktree_root(repo_root)

    session_id = params.get("session_id")
    if session_id is None:
        session_id = (resolve_current_session_id() or "").strip()

    tags = params.get("tags")
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(",") if t.strip()]
    elif tags is not None and not isinstance(tags, list):
        tags = None

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

    # coordinator_core.ipc's module-level comment above `_SCOPE_TOUCH_PATHS_KEY`).
    result["_scope_touch_paths"] = [result["out_path"]]
    return result
