"""Section porter — CrossRepoMemoSummary (envelope key: ``cross_repo_memos``).

Emits a summary of BOTH the ACTIONABLE inbox memo set (``cross-repo/inbox/*.md``, ``type=
cross-repo-memo``) AND the terminal-flipped archived set (``cross-repo/archive/*.md``,
``type=archived-memo``) via the in-process ``records.query`` op — one query per bucket,
merged into a single ``cross_repo_memos`` array with a per-row ``archived`` discriminator.
Archived-set emission and the capped ``decision_note`` field below are 2026-07-24 additions
(DEC-2/DEC-3, plan `2026-07-24-cross-repo-memo-ownership-and-redesign.md` chunk C6) — so
"what have I promised & how was it closed" has a feed, and the answer's SUBSTANCE (not just
its existence) travels.

2026-07-24 chunk C8 (same plan, PM-directed "no deferrals") widens this again: beyond the
capped ``decision_note``, each row also carries the memo's FULL body content (the markdown
after the frontmatter block) so the fleet can content-search memo prose, not just filter on
frontmatter fields. ``records.query`` (the op ``_query_records`` below drives) never returns
body text — see ``_read_memo_body`` — so this section re-reads the source file directly and
re-derives the body via the same byte-parity ``parse_frontmatter`` port ``records.query``
itself uses internally. Bounded/streaming-safe: a body is capped at ``_BODY_MAX_CHARS`` and
truncated (never silently dropped) if a memo body is pathologically large; the everyday case
(a normal-sized memo) emits the body in full. The existing per-record ``content_hash`` change
signal (stamped generically over EVERY section's records post-collect by
``envelope._stamp_content_hash`` against the full source file, i.e. frontmatter + body
bytes together — a broader scope than the frontmatter-stripped ``body`` field a consumer
actually receives) lets a downstream consumer detect "the source file's bytes changed since
some other observation." Review: code-reviewer (F6) — tightened: this is a weaker,
differently-scoped signal than "is this shown `body` truncated" — a consumer cannot use
``content_hash`` to verify truncation directly, since the hash covers frontmatter bytes the
consumer never receives. No separate hash-only-fallback field is added here regardless.

Required fields (all 10, per cross-repo-memo-summary.schema.json): repo,
coordinator_root_path, title, from, to, status, created, kind, related, provenance.
Additive-optional fields (present with a default, never required — reader-first, no
version-desync): ``archived`` (bool, default False — True for archive-sourced rows),
``decision_note`` (str | None — capped excerpt of the frontmatter ``decision_note`` field,
present only when the source memo carries one; absent/None on plain asks with no
disposition yet), and ``body`` (str | None — the memo's full markdown body, capped at
``_BODY_MAX_CHARS`` and truncated with a trailing ellipsis when oversized; absent/None only
when the source file could not be re-read, e.g. deleted between the records.query scan and
this section's body-read pass).
A record is VALID only when title/from/to/status/created are strings, ``status`` is within
{open, in_progress, actioned}, and ``kind`` (default "ask") is within {ask, consult, fyi}.
Records that fail land in the malformed quarantine ({path, reason}) — the exact
partition the bash select()/negated-select() pair produces.

Port of: emit-cockpit-snapshot.sh (DoE 07eedcfb, 2026-07-19) — § SECTION 8.7,
  CrossRepoMemoSummary. Byte/semantic parity port.
Spec backlink: pln-tc-3-emission-stack-python-por-c9595b § P11
Contract spec: docs/plans/2026-06-23-cockpit-contract-ext-wave2-emit-and-queue-migration.md § C9b
Spec backlink: pln-take-ownership-of-the-cross-re-ac97ef § C6
Spec backlink: pln-take-ownership-of-the-cross-re-ac97ef § C8 —
  full memo-body content emission (bounded/capped), beyond the capped decision_note.

Node-subprocess retirement: this section originally shelled out to
``node COORDINATOR_ROOT/bin/query-records.js --type cross-repo-memo``. It now calls
``coordinator_core.ops.records_query``'s in-process ``records.query`` op handler directly —
no ``node`` binary, no subprocess spawn. See ``_query_records`` / ``_invoke_records_query``
below for the calling convention and the loop-reentrancy caveat this repoint introduces.
"""

from __future__ import annotations

import asyncio
import warnings
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops.emit.context import EmitContext
from coordinator_core.ops.records_query import _handler as _records_query_handler

from ._shared import normalize_frontmatter

_STATUS_ENUM = ("open", "in_progress", "actioned")
_KIND_ENUM = ("ask", "consult", "fyi")
_MALFORMED_REASON = "missing required field or invalid status/kind enum"

# The two records.query record types this section merges into one emitted array.
# 'cross-repo-memo' is the actionable inbox set (cross-repo/inbox/*.md); 'archived-memo'
# is the terminal-flipped mirror (cross-repo/archive/*.md) — registered in the native
# records.query op by plan chunk C3 (2026-07-24). Each row's ``archived`` field records
# which bucket it came from.
_INBOX_RECORD_TYPE = "cross-repo-memo"
_ARCHIVED_RECORD_TYPE = "archived-memo"

# Cap on the emitted ``decision_note`` excerpt (chars). Bounded/capped by design — this is
# NOT full memo-body emission (see module docstring); a long decision_note is truncated
# with a trailing ellipsis rather than dropped, so the answer's substance still travels in
# summary form even when the full text doesn't fit.
_DECISION_NOTE_MAX_CHARS = 500

# Cap on the emitted ``body`` field (chars). 2026-07-24 chunk C8 — the everyday case (a
# normal-sized memo) emits the full body well under this cap; the cap exists solely as a
# bounded-emission safety valve against a pathologically large memo body, so one oversized
# memo cannot blow up the fleet-wide snapshot's size or a downstream consumer's parse
# budget. A truncated body carries a trailing ellipsis, never a silent drop — see
# ``_cap_body``. 50k chars comfortably covers every real cross-repo memo observed on disk
# (memos are short-form asks/decisions, not long-form docs) while still bounding the
# pathological case.
_BODY_MAX_CHARS = 50_000

# Reason string stamped on the synthetic malformed-bucket marker row emitted when the
# in-process records.query invocation itself fails (see collect() below). Distinguishes
# "query broke" from "query ran and validly returned zero/malformed memo rows" for a
# consumer scanning malformed_records.cross_repo_memos by substring or by the
# query_failed flag.
_QUERY_FAILED_REASON_PREFIX = "records.query op query failed"


def _invoke_records_query_sync(params: dict, repo_root: Path) -> dict:
    """Drive the ``records.query`` op handler to completion synchronously.

    ``coordinator_core.ops.records_query._handler`` is a plain ``def`` — pure
    synchronous filesystem/frontmatter logic with no actual suspension point —
    so it is called directly here regardless of whether this section's
    ``collect(ctx)`` is invoked standalone or from inside the already-running
    event loop driving ``artifact.emit`` (``ops/artifact_emit.py``).

    Guards against a future regression where ``records_query._handler`` grows
    an actual ``await``: a coroutine returned here would otherwise propagate
    as opaque "data" into the emit envelope, or fail downstream with a poor
    diagnostic. The coroutine is closed before raising so it never emits an
    "never awaited" warning.
    """
    result = _records_query_handler(params=params, repo_root=repo_root)
    if asyncio.iscoroutine(result):
        result.close()
        raise RuntimeError(
            "records_query._handler has become a coroutine again (expected a plain "
            "synchronous return). _invoke_records_query_sync calls it directly and "
            "requires a synchronous handler — either restore a coroutine-driving shim "
            "here or make records_query._handler synchronous again."
        )
    return result


def _query_records(ctx: EmitContext, record_type: str) -> "tuple[list[dict], Optional[str]]":
    """Return ``(records, query_error)`` from the in-process ``records.query`` op for the
    given ``record_type`` (``cross-repo-memo`` for the inbox bucket, ``archived-memo`` for
    the archive bucket — see ``_INBOX_RECORD_TYPE`` / ``_ARCHIVED_RECORD_TYPE``).

    ``query_error`` is ``None`` on success — including a genuinely empty result, where the
    query ran and validly returned zero records — and a short diagnostic string on ANY
    failure mode (unknown/dropped type, an unexpected exception from the handler, or a
    malformed non-list ``records`` payload).

    Mirrors the original bash ``… 2>/dev/null || echo "[]"`` posture: emission never aborts
    on failure, and ``records`` collapses to ``[]`` on every failure path exactly as before.
    What changes is that the caller (``collect()``) receives an explicit failure signal
    instead of an indistinguishable empty list — a query failure and "there are genuinely
    no cross-repo memos" no longer look identical downstream (see collect() for how
    ``query_error`` surfaces into ``malformed_records.cross_repo_memos`` plus a loud
    ``warnings.warn``, the module's existing degraded-condition convention — see
    ``coordinator_core/ops/emit/envelope.py``'s ``warnings.warn`` call sites).

    Record source resolution mirrors the other in-process sections' ``subprocess_root``
    convention: ``ctx.subprocess_root`` (frozen-fixture test isolation) takes precedence
    over ``ctx.repo_root``. The op handler's ``repo_root`` param is the git COMMON DIR
    (it derives the worktree root via ``.parent`` internally, per
    ``coordinator_core.ops.fleet._common.main_worktree_root``) — so the resolved root is
    joined with ``.git`` before being passed in, matching every other in-process
    ``records.query`` caller's convention (see ``ops/tests/test_records_query_parity.py``).

    Negative-spec: does NOT raise — a hard raise here would abort the whole emit
    (envelope.build has no per-section try/except, per the backlogs-section
    list-frontmatter-crash lesson).
    """
    root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root
    repo_root_arg = Path(root) / ".git"
    params = {"type": record_type, "limit": 0, "format": "json"}
    try:
        result = _invoke_records_query_sync(params, repo_root_arg)
    except SystemExit as exc:
        # _handler sys.exit(1)s on an unknown/unsupported type — e.g. a future regression
        # that drops the type back out of _TYPE_TO_GLOB. Fail-open, not fail-loud.
        return [], f"records.query op exited (SystemExit code={exc.code})"
    except Exception as exc:  # noqa: BLE001 — fail-open: any handler failure must not abort emission
        return [], f"records.query op raised {type(exc).__name__}: {exc}"
    data = result.get("records") if isinstance(result, dict) else None
    if not isinstance(data, list):
        got = type(result).__name__ if result is not None else "NoneType"
        return [], f"records.query op returned non-list records payload ({got})"
    return data, None


def _is_str(value) -> bool:
    return isinstance(value, str)


def _kind_default(fm: dict):
    """Mirror jq // operator — return value unless null/false. Defaults to "ask"."""
    kind = fm.get("kind")
    if kind is None or kind is False:
        return "ask"
    return kind


def _related_default(fm: dict):
    """Mirror jq // operator — return value unless null/false. Defaults to []."""
    related = fm.get("related")
    if related is None or related is False:
        return []
    return related


def _is_valid(fm: dict) -> bool:
    """Reproduce the bash valid-select() predicate (bash:2120-2128) exactly.

    Applies identically to inbox and archived rows — both buckets share the same core
    memo-shape fields; ``archived`` is a bucket tag injected by the caller, not part of
    this predicate.
    """
    return (
        isinstance(fm.get("title"), str)
        and isinstance(fm.get("from"), str)
        and isinstance(fm.get("to"), str)
        and isinstance(fm.get("status"), str)
        and fm.get("status") in _STATUS_ENUM
        and isinstance(fm.get("created"), str)
        and _kind_default(fm) in _KIND_ENUM
    )


# Review: code-reviewer (F3) — _cap_decision_note/_cap_body were structurally identical
# (differing only in which module-level max-chars constant they compared against), a shape
# that drifts silently when one copy's strip/truncation rule changes and the other doesn't.
# Extracted to one tested implementation; the two public helpers are now thin wrappers.
def _cap(value: object, max_chars: int) -> Optional[str]:
    """Bound a string to ``max_chars``.

    Non-string / blank input returns ``None`` (field omitted). A value at or under the cap
    passes through UNCHANGED. A value OVER the cap is truncated with a trailing ellipsis,
    never dropped — bounded substance beats no substance.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped:
        return None
    if len(stripped) <= max_chars:
        return stripped
    return stripped[: max_chars - 1] + "…"


def _cap_decision_note(value: object) -> Optional[str]:
    """Bound a frontmatter ``decision_note`` string to ``_DECISION_NOTE_MAX_CHARS``.

    Plain asks with no disposition yet carry no decision_note, hence no field (see
    ``_cap``'s None-on-blank/non-string behavior). This is NOT full-body emission (see
    module docstring); it is a capped excerpt of one frontmatter field.
    """
    return _cap(value, _DECISION_NOTE_MAX_CHARS)


def _decision_note(fm: dict) -> Optional[str]:
    return _cap_decision_note(fm.get("decision_note"))


def _cap_body(value: Optional[str]) -> Optional[str]:
    """Bound a memo body string to ``_BODY_MAX_CHARS`` — see ``_cap``."""
    return _cap(value, _BODY_MAX_CHARS)


def _read_memo_body(ctx: EmitContext, rel_path: object) -> Optional[str]:
    """Re-read a memo's body (frontmatter block stripped) directly off disk.

    ``records.query`` (driven by ``_query_records`` above) returns only
    ``{path, frontmatter}`` per record — see
    ``coordinator_core.ops.records_query._build_record``'s return shape — it never surfaces
    body text. Full-body content search (C8) needs the raw markdown AFTER the frontmatter
    delimiter, so this helper re-reads the source file and re-derives the body via the SAME
    byte-parity ``parse_frontmatter`` port ``records.query`` uses internally — no second,
    independently-drifting parser.

    Known duplication (Review: code-reviewer F4, not re-architected in this pass):
    ``records.query`` already opened and frontmatter-parsed this same file to build the
    ``{path, frontmatter}`` row this helper is handed — this re-reads and re-parses it a
    second time solely to recover the body half ``records.query``'s return shape drops,
    doubling file I/O/parse cost per memo on every ``collect()`` call. A follow-up could
    extend ``records.query``'s per-record shape (or a params flag) to expose body content
    once per file and thread it through, which would also centralize the fail-open I/O
    path in one already-hardened place. Left as a documented follow-up, not a blocker for
    this chunk's scope.

    Root resolution mirrors ``_query_records``'s ``subprocess_root``-takes-precedence
    convention, but joins directly onto the WORKTREE root (not ``.git``) since this reads
    an ordinary tracked file, not the records.query op's git-common-dir param.

    Fail-open, never raises: a non-string/empty path, a missing/unreadable file, a
    non-UTF-8-decodable file, or a parse failure all return ``None`` — the record still
    emits with its metadata fields; only ``body`` is absent (mirrors ``decision_note``'s
    key-absent-on-None convention). This is a genuine read-failure fallback, distinct from
    the size-based cap in ``_cap_body``.
    """
    if not isinstance(rel_path, str) or not rel_path:
        return None
    root = ctx.subprocess_root if ctx.subprocess_root is not None else ctx.repo_root
    try:
        text = (Path(root) / rel_path).read_text(encoding="utf-8")
    # Review: code-reviewer (F1) — UnicodeDecodeError is a ValueError subclass, not an
    # OSError subclass; a bare `except OSError` let a non-UTF-8 memo body (binary paste,
    # mis-encoded copy, BOM-less UTF-16) propagate past this guard and crash the whole
    # collect() call (envelope.build has no per-section try/except). Widened to cover both.
    except (OSError, UnicodeDecodeError):
        return None
    try:
        parsed = parse_frontmatter(text)
    except Exception:  # noqa: BLE001 — fail-open: a body-read helper must never abort emission
        return None
    body = parsed.get("body") if isinstance(parsed, dict) else None
    if not isinstance(body, str):
        return None
    stripped = body.strip()
    return stripped or None


def _collect_bucket(
    ctx: EmitContext, record_type: str, archived: bool
) -> tuple[list[dict], list[dict]]:
    """Build (records, malformed) for one records.query bucket (inbox or archived).

    Valid and malformed partition the query-records output exactly (the malformed OR-chain
    at bash:2159-2166 is the De Morgan negation of the valid select at bash:2120-2128) —
    that partition logic is bucket-agnostic; only the source ``record_type`` and the
    stamped ``archived`` discriminator differ between the two callers in ``collect()``.
    """
    raw, query_error = _query_records(ctx, record_type)

    records: list[dict] = []
    malformed: list[dict] = []

    if query_error is not None:
        # Loud observability signal (module convention — see envelope.py's warnings.warn
        # call sites): a query failure must never look like "zero cross-repo memos exist"
        # to a consumer scanning logs or a human tailing emit output.
        warnings.warn(
            f"cross_repo_memos ({record_type}): {_QUERY_FAILED_REASON_PREFIX}: "
            f"{query_error}; emitting bucket as empty. This is a QUERY FAILURE, not an "
            "absence of cross-repo memos — see malformed_records.cross_repo_memos for the "
            "query_failed marker row.",
            stacklevel=3,
        )
        # Schema-valid marker row: malformed_records.cross_repo_memos items are
        # additionalProperties:{}-shaped free-form objects with no required keys
        # (cockpit-contract snapshot-envelope.schema.json), so this is not a new
        # contract field — it reuses the section's existing degraded-record channel to
        # carry a section-level (not per-record) failure signal. `path: None` marks it
        # as not-a-real-record; `query_failed: True` is the machine-checkable flag.
        malformed.append({
            "path": None,
            "reason": f"{_QUERY_FAILED_REASON_PREFIX}: {query_error}",
            "query_failed": True,
            "record_type": record_type,
        })

    for rec in raw:
        if not isinstance(rec, dict):
            continue
        fm = normalize_frontmatter(rec)
        path = rec.get("path")
        if _is_valid(fm):
            record = {
                "repo": ctx.repo_name,
                "coordinator_root_path": ".",
                "title": fm["title"],
                "from": fm["from"],
                "to": fm["to"],
                "status": fm["status"],
                "created": fm["created"][0:10],
                "kind": _kind_default(fm),
                "related": _related_default(fm),
                "provenance": ctx.provenance("local_fs", path=path, derivation="parsed"),
                "archived": archived,
            }
            # Key-absent-when-None, mirroring the sibling `content_hash` optional field's
            # own-emission convention: the vendored schema's `decision_note` property is a
            # bare `{"type": "string"}` (no null variant, per build_entity_schema's
            # Optional-non-nullable unwrap — matches Zod `.optional()` semantics of
            # key-absent, not `.nullable()`'s present-as-null). Setting the key to a literal
            # `None` here would fail jsonschema validation; omitting it entirely is correct.
            decision_note = _decision_note(fm)
            if decision_note is not None:
                record["decision_note"] = decision_note
            # Key-absent-when-None, same convention as decision_note above — see
            # _read_memo_body's docstring for why a fresh file read is needed at all
            # (records.query never returns body text).
            body = _cap_body(_read_memo_body(ctx, path))
            if body is not None:
                record["body"] = body
            records.append(record)
        else:
            malformed.append({
                "path": path,
                "reason": _MALFORMED_REASON,
                "frontmatter_keys": sorted(fm.keys()),
            })

    return records, malformed


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build (records, malformed) for the actionable inbox set PLUS the archived set.

    Two independent ``records.query`` calls (``cross-repo-memo`` / ``archived-memo``, see
    ``_collect_bucket``) are merged into one array; a query failure in either bucket is
    reported (query-failed marker + warning) without affecting the other bucket — a broken
    archive query never suppresses valid inbox rows and vice versa.
    """
    inbox_records, inbox_malformed = _collect_bucket(ctx, _INBOX_RECORD_TYPE, archived=False)
    archived_records, archived_malformed = _collect_bucket(
        ctx, _ARCHIVED_RECORD_TYPE, archived=True
    )
    return inbox_records + archived_records, inbox_malformed + archived_malformed
