"""
coordinator_core.ops.queue_promote — lessons-outbox appender (queue.promote op).

Purpose: Port of coordinator-lesson-promote. Writes ONE YAML entry to
``<doe_root>/state/lessons-outbox/<ISO-ts-safe>-<slug>.yaml`` for drain by the
/learn-lessons --central procedure. DoE-claude is the central lessons repo — the
outbox is NOT claude-klabauter-rooted (see the "Claude-Klabauter root" note below, corrected
2026-07-22 after 72 outbox YAMLs from a DoE-claude session landed in claude-klabauter's own
``state/lessons-outbox/`` — cross-repo contamination from a misrouted root).

Byte-parity target: ``[DoE-claude] coordinator/bin/coordinator-lesson-promote``.

This is a DISTINCT writer op from ``queue.append``. Its write contract diverges
materially (design decision, strang-08 Design decisions section):
    - Schema:      ``lessons-outbox`` (NOT the ``lesson-entry`` alias used by queue.append).
    - ``id`` field: uuid4 (queue.append's negative-spec DROPS id entirely).
    - No ``system:`` provenance block (queue.append always emits one).
    - Filename:    ``<ISO-ts-safe>-<slug>-<digest12>.yaml`` (full UTC timestamp, colons/plus →
                   hyphens, plus a 12-hex-char content digest — see ``_content_digest`` below;
                   this diverges from the legacy bash oracle's undigested filename, an
                   intentional non-byte-parity change scoped to the concurrency-safety plan).
    - Validation:  argparse-choices (in-process ``schema_validate.describe()`` enum read
                   at call time); no schema-cli.js --validate delegation.
    - Write:       upgraded to atomic temp+os.replace (non-observable output-byte change;
                   legacy was plain open() — atomicity is safe to add, not a parity break).
    - Env override: ``LESSON_PROMOTE_OUTBOX_ROOT`` (not QUEUE_APPEND_OUTPUT_ROOT).
    - DoE root: NO cwd fallback on unresolvable DoE-claude root (C12 negative-spec,
      mirrored onto the DoE-rooted seam — same negative-spec as the claude-klabauter-rooted
      queue.append central scope, just against the DoE resolver instead).

Output path: ``<outbox_root>/<ISO-ts-safe>-<slug>-<digest12>.yaml``
    ISO-ts-safe: ``now(utc).isoformat(timespec="seconds")`` with ``:`` and ``+`` → ``-``.
    Slug: ``_slug_from_title`` (lower, non-[a-z0-9] → '-', strip, 40 chars).
    digest12: ``_content_digest`` — first 12 hex chars of a SHA-1 over the lesson's
        stable semantic fields (excludes uuid4 ``id`` and ``created`` timestamp).

YAML format: ``---`` / fields / ``---`` fence (``_compose_yaml``), distinct from
queue.append's fenceless format.

MUTATING op: writes coordinator substrate ONLY (``state/lessons-outbox/`` per-entry YAML).
NEVER writes into rag's relational store (dual-write ban, DR-208 / tri-plane DD#1).
No in-memory state retained (store-less-ness invariant).

Caller repo_root threading (F1): handler third arg receives ``git_common_dir(caller_worktree)``
via ``_OP_KEY_SCOPE: common_dir`` (ipc.py). The outbox root always routes to the
DoE-claude central root (lessons-outbox is central state owned by DoE-claude, NOT
Claude-klabauter — claude-klabauter is just one of many senders), so ``caller_worktree`` is not used for
path routing — but the DoE-claude root MUST be resolvable; unresolvable → WARN+skip
exit 0.

Registered as ``queue.promote`` in ops/__init__.py and classified ``OpClass.MUTATING``
in authz/classification.py (same dispatch, strang-08 C1+C2).

Spec backlink: pln-strang-08-queue-append-strangl-2a3499 § C2
Parity oracle: [DoE-claude] coordinator/bin/coordinator-lesson-promote
DR authority: docs/decisions/DR-213-queue-write-substrate-carveout.md
"""


from __future__ import annotations

GENERATES = []  # writes only into DoE-claude's central state/lessons-outbox/, never claude-klabauter's own tree

import datetime
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.schema_validate import describe as _describe_schema
from coordinator_core.ipc import register_op
from coordinator_core.ops.coordinator_doe_root import coordinator_doe_root
from coordinator_core.ops.fleet._common import main_worktree_root

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SLUG_MAX_CHARS = 40

# Env var override for test isolation.
_OUTBOX_ROOT_ENV = "LESSON_PROMOTE_OUTBOX_ROOT"


class _DoeUnresolvable(RuntimeError):
    """Raised when the DoE-claude central root cannot be resolved.

    Callers catch this and degrade gracefully (WARN+skip, exit 0) per AC6.
    Negative-spec: NO cwd fallback — stop-the-rot C12 closes the cwd-fallback landmine;
    the same negative-spec applies here even though C12 was written against the
    claude-klabauter-rooted queue.append central scope (see ``coordinator_doe_root``'s own
    fail-loud-to-None contract for the DoE-side rung chain).
    Spec backlink: pln-stop-the-rot-claude-klabauter-state-home-placement-4cc787 § AC13
    """


# ---------------------------------------------------------------------------
# Output path helper
# ---------------------------------------------------------------------------


def _outbox_root() -> str:
    """Return the lessons-outbox directory path.

    Resolution:
        1. ``LESSON_PROMOTE_OUTBOX_ROOT`` env var (test isolation).
        2. ``<doe_root>/state/lessons-outbox/`` via ``coordinator_doe_root()`` — the
           lessons-outbox is central state owned by DoE-claude (the central lessons
           repo), NOT claude-klabauter. Matches the documented CLI oracle contract
           (``coordinator-lesson-promote``'s ``_outbox_root()``, which resolves via
           ``coordinator_registry.doe_root()``).

    Raises:
        _DoeUnresolvable — when the DoE-claude root is unresolvable and no env override.

    Negative-spec: DOES NOT fall back to cwd-relative state/ when the DoE root is
    unresolvable — that silent fallback was the landmine closed by stop-the-rot C12,
    mirrored here against the DoE-side resolver.

    Spec backlink: pln-stop-the-rot-claude-klabauter-state-home-placement-4cc787 § C12 / AC13
    Parity oracle: [DoE-claude] coordinator/bin/coordinator-lesson-promote § _outbox_root
    """
    override = os.environ.get(_OUTBOX_ROOT_ENV)
    if override:
        return override
    doe = coordinator_doe_root()
    if doe is None:
        raise _DoeUnresolvable(
            "repos.doe_claude not set in machine-local registry and REPO_DOE_CLAUDE env var not set"
        )
    return os.path.join(doe, "state", "lessons-outbox")


# ---------------------------------------------------------------------------
# Slug and timestamp helpers
# ---------------------------------------------------------------------------


def _slug_from_title(title: str) -> str:
    """Sanitize a title into a filesystem-safe slug (40 chars max).

    Mirrors coordinator-lesson-promote._slug_from_title:
        lowercase → collapse non-[a-z0-9] runs to '-' → strip leading/trailing '-'
        → truncate to 40 chars → rstrip trailing '-' left by truncation.
    """
    slug = title.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    slug = slug.strip("-")
    return slug[:_SLUG_MAX_CHARS].rstrip("-")


def _now_iso() -> str:
    """Return current UTC datetime as ISO 8601 string (seconds precision).

    Mirrors coordinator-lesson-promote._now_iso.
    """
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _ts_for_filename(iso_ts: str) -> str:
    """Convert an ISO timestamp to a filesystem-safe filename component.

    Replaces ``:`` and ``+`` with ``-`` for cross-platform filename safety.
    e.g. ``2026-06-15T10:30:00+00:00`` → ``2026-06-15T10-30-00-00-00``

    Mirrors coordinator-lesson-promote._ts_for_filename.
    """
    return re.sub(r"[:+]", "-", iso_ts)


# ---------------------------------------------------------------------------
# YAML serialization (byte-parity with coordinator-lesson-promote)
#
# F2 pin: ordered string formatting, NOT yaml.safe_dump.
# This format uses ``---`` / ``---`` fences (distinct from queue.append's fenceless format).
# ---------------------------------------------------------------------------


def _yaml_str(value: str) -> str:
    """Serialize a string value for YAML.

    Multi-line → strip-chomped block scalar (``|-``).
    Single-line → quoted when YAML-special.

    Mirrors coordinator-lesson-promote._yaml_str.
    """
    if "\n" in value:
        indented = "\n".join("  " + line if line.strip() else "" for line in value.splitlines())
        return "|-\n" + indented
    needs_quoting = any(
        c in value
        for c in ('"', "'", ":", "#", "{", "}", "[", "]", ",", "&", "*", "?", "|", ">", "!", "%", "@", "`")
    )
    needs_quoting = (
        needs_quoting
        or value != value.strip()
        or value.lower() in ("true", "false", "null", "yes", "no")
    )
    if needs_quoting:
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _compose_yaml(fields: dict) -> str:
    """Compose a YAML document from an ordered dict of fields with ``---`` fences.

    Handles ``str`` and ``list[str]`` values. None values are serialized as empty string.

    Mirrors coordinator-lesson-promote._compose_yaml.
    """
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            lines.append(f"{key}: ")
        elif isinstance(value, list):
            if not value:
                lines.append(f"{key}: []")
            else:
                lines.append(f"{key}:")
                for item in value:
                    lines.append(f"  - {_yaml_str(item)}")
        else:
            lines.append(f"{key}: {_yaml_str(str(value))}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Content digest (collision guard — DR-213 D2(i) amendment)
# ---------------------------------------------------------------------------


def _content_digest(fields: dict) -> str:
    """Compute the 12-hex-char content digest used to disambiguate the output filename.

    Mirrors ``queue_append._content_digest`` (DR-213 D2(i) amendment, shared queue-writer
    class — see plan docs/plans/2026-07-08-concurrency-safe-strangled-op-writes.md § "The
    collision-guard shape"; the DR-213 amendment text landed by C1 covers this op too,
    no separate DR edit here).

    Field-set (pinned): SHA-1 over a structured JSON serialization of the ordered
    semantic field-set, built ONLY from the lesson's stable semantic fields, truncated
    to the first 12 hex chars. Fixed key order: ``from_repo``, ``change_kind``,
    ``target_wiki``, ``title``, ``body``, ``scope_tags``, ``evidence`` — the same order
    every call, regardless of dict-construction order, so the same logical lesson always
    yields the same digest.

    Deliberately EXCLUDED (per-write-varying provenance, not semantic content):
        - ``id``      — uuid4, regenerated every call; hashing it would make two
                        genuine re-runs of the SAME logical lesson never dedup.
        - ``created``  — timestamp, varies per call for the same reason.
    queue.promote has no ``system:`` block (unlike queue.append) so there is no
    provenance sub-dict to strip; ``id``/``created`` play that role here instead.

    Uses the same normalized ``fields["body"]`` value used for file content — never a
    raw pre-normalization input (queue.promote does no body normalization distinct from
    the caller-supplied value, so this is a straight pass-through of ``fields["body"]``).

    NO disk read — computed entirely from in-hand params (DR-213 D4; op remains
    write-always / additive-create, not a dedup pre-check).
    """
    # Review: code-reviewer — derive emit_order from fields.keys() minus provenance
    # (id/created) rather than a hardcoded literal list, so a future field added to
    # promote_lesson's fields dict (line ~502) participates automatically instead of
    # silently dropping out of the digest (Finding 5). fields is built in fixed
    # insertion order (see "byte-parity" comment at promote_lesson), so key order here
    # is deterministic across calls.
    emit_order = [k for k in fields if k not in ("id", "created")]

    # Review: code-reviewer — hand-joined "key=value" pipe strings had no delimiter
    # escaping; free-text fields (body, title, etc.) containing '|' or '=' could collide
    # two distinct entries onto one digest. Structured JSON serialization handles
    # internal escaping so no field value can inject a false separator (Finding 1).
    ordered_content = {key: fields.get(key) for key in emit_order}
    content_key = json.dumps(ordered_content, ensure_ascii=False, sort_keys=False)
    full_hash = hashlib.sha1(content_key.encode("utf-8")).hexdigest()
    return full_hash[:12]


# ---------------------------------------------------------------------------
# Core write logic
# ---------------------------------------------------------------------------


def _validate_change_kind(change_kind: str) -> None:
    """Validate change_kind against the lessons-outbox schema enum.

    Mirrors coordinator-lesson-promote argparse-choices validation:
    derives valid values from an in-process ``schema_validate.describe("lessons-outbox")``
    call at call time (native replacement for the former ``schema-cli.js --describe``
    subprocess shell-out).

    Raises:
        ValueError — if change_kind is not in the schema's enum.
        RuntimeError — if the schema cannot be described (unexpected schema structure).
    """
    described = _describe_schema("lessons-outbox")
    try:
        valid_values: tuple = tuple(described["enums"]["change_kind"])
    except (KeyError, TypeError) as exc:
        raise RuntimeError(
            f"queue.promote: unexpected schema structure for 'lessons-outbox': {exc}"
        ) from exc
    if change_kind not in valid_values:
        raise ValueError(
            f"queue.promote: invalid change_kind {change_kind!r}. "
            f"Valid values: {', '.join(valid_values)}."
        )


def promote_lesson(
    title: str,
    body: str,
    change_kind: str,
    target_wiki: str,
    *,
    scope_tags: Optional[list] = None,
    evidence: Optional[str] = None,
    from_repo: Optional[str] = None,
    caller_worktree: Optional[Path] = None,
    entry_id: Optional[str] = None,
    created: Optional[str] = None,
) -> dict:
    """Write a lessons-outbox YAML entry.

    Byte-parity port of coordinator-lesson-promote._write_entry + main() write path.

    Parameters:
        title        — one-line lesson title.
        body         — lesson body prose.
        change_kind  — kind of change (validated against schema enum).
        target_wiki  — central wiki path this lesson targets.
        scope_tags   — optional list of scope tags.
        evidence     — optional evidence reference.
        from_repo    — from_repo identity; defaults to caller_worktree basename + "-em".
        caller_worktree — caller's worktree root (for from_repo fallback; not used for
                          path routing since outbox is always central claude-klabauter state).
        entry_id     — uuid4 string; if None, a fresh uuid is generated.
        created      — ISO timestamp; if None, now(utc) is generated.

    Returns:
        {out_path: str, entry_id: str, from_repo: str, change_kind: str, target_wiki: str}

    Raises:
        ValueError — invalid change_kind.
        RuntimeError — schema description unavailable (unexpected schema structure).
        _DoeUnresolvable — DoE-claude root unresolvable (caller degrades gracefully).
    """
    # Validate change_kind.
    _validate_change_kind(change_kind)

    # Generate deterministic fields when not supplied (by caller or test harness).
    if entry_id is None:
        entry_id = str(uuid.uuid4())
    if created is None:
        created = _now_iso()

    # Resolve from_repo.
    if from_repo is None:
        if caller_worktree is not None:
            from_repo = os.path.basename(str(caller_worktree)) + "-em"
        else:
            from_repo = "unknown-sender-em"

    # Resolve outbox root (_DoeUnresolvable propagates to caller).
    outbox = _outbox_root()
    os.makedirs(outbox, exist_ok=True)

    # Build fields in fixed insertion order (byte-parity with lesson-promote._write_entry).
    fields: dict = {
        "id": entry_id,
        "created": created,
        "from_repo": from_repo,
        "change_kind": change_kind,
        "target_wiki": target_wiki,
        "title": title,
        "body": body,
    }
    if scope_tags:
        fields["scope_tags"] = scope_tags
    if evidence:
        fields["evidence"] = evidence

    # Compute content digest (no disk read — in-hand params only, DR-213 D4).
    digest12 = _content_digest(fields)

    ts_safe = _ts_for_filename(created)
    slug = _slug_from_title(title)
    # Filename is content-keyed (DR-213 D2(i) amendment, 2026-07-08): the trailing
    # -<digest12> disambiguates distinct same-second+slug entries (both survive) while
    # a genuine re-run of an identical lesson still dedups to one file. See
    # _content_digest for field-set/exclusion rationale.
    filename = f"{ts_safe}-{slug}-{digest12}.yaml"
    out_path = os.path.join(outbox, filename)

    content = _compose_yaml(fields)

    # Upgraded from plain open() to atomic temp+os.replace (output bytes identical;
    # atomicity is non-observable in file content — safe upgrade, not a parity break).
    dir_path = os.path.dirname(out_path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_path, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, out_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            print(f"skip: promote_lesson: os.unlink(tmp_path) failed: {sys.exc_info()[1]}", file=sys.stderr)
            pass
        raise

    return {
        "out_path": out_path,
        "entry_id": entry_id,
        "from_repo": from_repo,
        "change_kind": change_kind,
        "target_wiki": target_wiki,
    }


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("queue.promote")
def _queue_promote_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC ``queue.promote`` handler — write a lessons-outbox YAML entry.

    MUTATING (writes per-entry YAML to state/lessons-outbox/).
    Delegates to ``promote_lesson()``.

    ``repo_root`` receives ``git_common_dir(caller_worktree)`` via the
    ``_OP_KEY_SCOPE: common_dir`` mechanism (ipc.py). The outbox always routes to
    claude-klabauter central state, so ``caller_worktree`` is used only for ``from_repo`` fallback,
    not for path routing (F1 / AC13).

    Required params:
        title        (str) — one-line lesson title.
        body         (str) — lesson body prose.
        change_kind  (str) — kind of change; validated against schema enum at call time.
        target_wiki  (str) — central wiki path this lesson targets.

    Optional params:
        scope_tags (list or comma-separated str), evidence (str), from_repo (str).

    Returns:
        {out_path: str, entry_id: str, from_repo: str, change_kind: str, target_wiki: str}

    On ``_DoeUnresolvable``: logs WARN, returns ``{skipped: true, reason: "..."}``.
    """
    # Derive caller's worktree from socket-authoritative common_dir.
    caller_worktree: Optional[Path] = None
    if repo_root is not None:
        caller_worktree = main_worktree_root(repo_root)

    # Parse scope_tags: accept list or comma-separated string.
    scope_tags = params.get("scope_tags")
    if isinstance(scope_tags, str):
        scope_tags = [t.strip() for t in scope_tags.split(",") if scope_tags.strip() and t.strip()]
    elif scope_tags is not None and not isinstance(scope_tags, list):
        scope_tags = None

    evidence = params.get("evidence") or None

    try:
        result = promote_lesson(
            title=params.get("title", ""),
            body=params.get("body", ""),
            change_kind=params.get("change_kind", ""),
            target_wiki=params.get("target_wiki", ""),
            scope_tags=scope_tags,
            evidence=evidence,
            from_repo=params.get("from_repo"),
            caller_worktree=caller_worktree,
        )
    except _DoeUnresolvable as exc:
        # AC6: graceful-degrade on unresolvable DoE-claude root — WARN + skip, exit 0.
        logger.warning(
            "queue.promote: DoE-claude root unresolvable — skipping write: %s. "
            "Remediation: set REPO_DOE_CLAUDE or run "
            "'machine-local set repos.doe_claude /path/to/DoE-claude'.",
            exc,
        )
        return {"skipped": True, "reason": str(exc)}

    # Self-report scope-touch contract (design (b), 2026-08-04 — see
    # coordinator_core.ipc's module-level comment above `_SCOPE_TOUCH_PATHS_KEY`).
    # `out_path` is the ONE file this call actually wrote (promote_lesson's write
    # primitive is a single write-temp + atomic-rename) — declare exactly that.
    # This IS a cross-repo write (the DoE-claude outbox, not the caller's own
    # worktree) — as of the 2026-08-04 F1 fix, `_record_self_reported_touches`
    # anchors containment on the CALLER's OWN repo, so this declaration is
    # SKIPPED (logged, never recorded) whenever the caller's worktree isn't
    # the DoE-claude root itself. That is deliberate, not a bug: recording a
    # claim in a repo this caller has no standing in was reproduced stealing
    # a live native session's own file in that repo (see the ipc.py contract
    # comment). The write still lands on disk; it stays an orphan at the
    # DoE-claude sink, which owns its own adoption path for that residual.
    # `dispatch_message` strips this key before the wire envelope is built.
    result["_scope_touch_paths"] = [result["out_path"]]
    return result
