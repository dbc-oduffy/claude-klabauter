"""
coordinator_core.ops.fleet._memo_compose — shared memo-composition primitives.

Purpose: the single home for the frontmatter-composition, filename, and
param-normalization logic every memo-writing surface (memo.draft,
memo.compose, memo.list, `gate_liveness.emit_discharge`,
`contract.emit_memo_schema`, `ops.ceremony.branch_resolution`) shares. This
module used to live inside `coordinator_core.ops.fleet.memo_send` (the
memo.send op handler carried it as a "shared-helper home" alongside its own
send-only logic) — split out here when memo.send was killed (PM ruling
2026-08-23: killed ops die outright, no stub) so every live sibling keeps a
working import after that module's deletion. Nothing in this file is a
registered op; it has no `@register_op` and no MUTATES/writes surface of its
own — each caller's own op module owns that.

Negative-spec: does NOT contain anything specific to memo.send's cross-tree
delivery (containment check, delivery commit, sent-ledger) — that logic died
with memo.send and was not ported here. A symbol only memo.send itself used
does not belong in this module.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ops.fleet._common import build_setup_error_result
from coordinator_core.ops.fleet._memo_resolver import (
    AmbiguousReceiverError,
    RegistryReadError,
    canonical_receiver_id as _canonical_receiver_id,
)
from coordinator_core.ops.fleet._memo_summary import (
    derive_prose_summary,
    is_placeholder_summary,
    validate_explicit_summary,
)

_LOG = logging.getLogger(__name__)

# Topic slug: filesystem-safe, no path-traversal chars.
# Mirrors cross-repo-memo CLI _TOPIC_SLUG_RE exactly — enforces the same
# YYYY-MM-DD-<topic>.md filename contract (5-lockstep-site invariant).
_TOPIC_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")

# DR-026 sender-namespacing: byte-for-byte port of DoE cross-repo-memo
# _memo_filename's sanitization regexes (coordinator/bin/cross-repo-memo.py ~line 1410-1418).
_SENDER_SLUG_INVALID_RE = re.compile(r"[^a-z0-9-]+")
_SENDER_SLUG_RUN_DASH_RE = re.compile(r"-{2,}")
_TOPIC_DOUBLED_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-)+")

# Engine actor-id for from: when the caller does not supply from_id.
# The asyncio engine has no EM session identity; this is the engine-actor sentinel.
# DoE Ask-1 concurrence: consumers key on the file at the path (schema-valid frontmatter),
# not on the writing process — an engine actor-id in from: is sufficient.
_ENGINE_ACTOR_ID = "claude-klabauter-engine"


def resolve_sender_id(from_id: Optional[str]) -> str:
    """Resolve the caller-declared sender identity, defaulting to the engine actor.

    Single authority for the `from_id or _ENGINE_ACTOR_ID` default every
    memo-writing op applies to its own `from_id` param. memo.list's
    resolution-mode preview (`memo_list._resolve_candidate`) calls this SAME
    function — rather than hardcoding `_ENGINE_ACTOR_ID` — so a caller that
    declares `from_id` to memo.list's preview and later to a real send gets a
    byte-identical `resolved_filename`/actual-write filename pair.

    A falsy `from_id` (None or empty string) resolves to `_ENGINE_ACTOR_ID` —
    the asyncio engine has no EM session identity of its own; this is the
    sentinel it signs sends with when the caller declines to declare one.

    Sender-side canonicalization: when the resolved identity is itself a
    central/redirect alias (the DoE seat sending FROM e.g. `claude-central-em`
    or a redirect alias), it is canonicalized to the SAME repo-matching
    central id the receiver-side addressee gate uses
    (`_memo_resolver.canonical_receiver_id`) — otherwise outbound filenames
    from that one seat split across whichever alias each caller happened to
    pass as `from_id`, defeating the DR-026 sender-namespace de-duplication
    this function's filename consumers rely on. Degrades to the raw
    (uncanonicalized) identity on `RegistryReadError`/`AmbiguousReceiverError`
    — sender-slug canonicalization is a filename-namespacing convenience, not
    the addressee-gate correctness surface, so it must never raise out of a
    function every memo-writing op's param validation calls unconditionally.
    """
    raw = from_id or _ENGINE_ACTOR_ID
    try:
        return _canonical_receiver_id(raw)
    except (RegistryReadError, AmbiguousReceiverError) as exc:
        _LOG.warning(
            "resolve_sender_id: sender-slug canonicalization degraded to raw "
            "id %r (falling back, NOT raising — sender-slug is filename "
            "namespacing, not the addressee gate); underlying error: %s: %s",
            raw,
            type(exc).__name__,
            exc,
        )
        return raw


def _normalize_in_reply_to(value: str) -> str:
    """Normalize a caller-supplied `in_reply_to` value to a bare basename.

    Accepts either a bare basename (`2026-07-25-foo.md`) or a path
    (`cross-repo/inbox/2026-07-25-foo.md`, an absolute path, etc.) — the
    emitted frontmatter value is always just the basename, matching what
    `coordinator_core.pickup_assemble._candidate_is_linked` matches against
    (basename or basename-minus-`.md`).
    """
    return Path(value.strip()).name


# scoped_to sub-keys — presence-triggered completeness (2026-07-21 fix):
# scoped_to as a WHOLE is optional; the moment the caller supplies ANY
# scoped_to_* field it is declaring a change-control memo, and the FULL
# triple — artifact + exactly one of (version|sha) + seam — becomes
# required. A partial triple fails loud; it is never silently completed.
_SCOPED_TO_KNOWN_SUBKEYS = frozenset({"artifact", "version", "sha", "seam"})


def _validate_space_param(op_mode: str, value: Any, dry_run: bool):
    """Validate/normalize the optional `space` param — shared by every
    memo-composing op.

    `space` (2026-07-28) is a sender-declared thread/problem-space hint,
    deliberately unvalidated against any vocabulary: it is a grouping hint
    the receiver may override, not a taxonomy. Only the "non-empty string
    when supplied" shape check applies (mirrors campaign_id's posture).

    Args:
        op_mode: the caller's own `_MODE` constant — used both as the
            `build_setup_error_result` mode field and to compose the
            op-namespaced message prefix ("memo.<op_mode>: ...").
        value: the raw `space` param (params.get("space")).
        dry_run: passed straight through to build_setup_error_result.

    Returns:
        (normalized_value_or_None, error_envelope_or_None) — normalized_value
        is the stripped string on pass (or None when value was None/absent);
        error_envelope is a build_setup_error_result dict on failure, else
        None. Exactly one of the two return slots is non-None.
    """
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, build_setup_error_result(
            op_mode, dry_run,
            f"memo.{op_mode}: space must be a non-empty string when supplied",
        )
    return value.strip(), None


def _validate_supersedes_param(op_mode: str, supersedes_raw: Any, dry_run: bool):
    """Validate/normalize the optional `supersedes` param — shared by every
    memo-composing op.

    `supersedes` accepts a bare string (the original shape) or a list of
    references (widened 2026-07-28 — one memo can retire several earlier
    ones, the observed shape of a thread that ends in a correction).

    Unified rule (EM correction, 2026-07-28):
      - A bare BLANK/whitespace-only string is treated as ABSENT — normalizes
        to `None`, no error.
      - A blank or non-string ENTRY INSIDE A NON-EMPTY LIST still fails loud,
        with the index in the message. That is not absence, it is a
        malformed list — silently pruning it would leave a live ask looking
        retired, which is the whole reason this rule exists.
      - A non-string, non-list `supersedes` (e.g. an int) still fails loud in
        both paths.

    A single-element list collapses to the bare string so every downstream
    consumer (filename disambiguation, frontmatter rendering) sees the
    pre-existing shape unchanged.

    Args:
        op_mode: the caller's own `_MODE` constant — see
            `_validate_space_param` for the same convention.
        supersedes_raw: the raw `supersedes` param.
        dry_run: passed straight through to build_setup_error_result.

    Returns:
        (normalized_value_or_None, error_envelope_or_None) — same two-slot
        contract as `_validate_space_param`.
    """
    supersedes: Optional[str | list[str]] = None
    if isinstance(supersedes_raw, list):
        cleaned: list[str] = []
        for idx, entry in enumerate(supersedes_raw):
            if not isinstance(entry, str) or not entry.strip():
                return None, build_setup_error_result(
                    op_mode, dry_run,
                    f"memo.{op_mode}: supersedes[{idx}] must be a non-empty "
                    f"string (got {entry!r}) — a supersession list is never "
                    f"silently pruned; fix or drop the entry",
                )
            cleaned.append(entry.strip())
        if cleaned:
            supersedes = cleaned[0] if len(cleaned) == 1 else cleaned
    elif supersedes_raw is not None:
        if not isinstance(supersedes_raw, str):
            return None, build_setup_error_result(
                op_mode, dry_run,
                f"memo.{op_mode}: supersedes must be a string or a list of "
                f"strings, got {type(supersedes_raw).__name__}",
            )
        # Blank/whitespace-only bare string is ABSENCE, not an error — see
        # unified rule above.
        supersedes = supersedes_raw.strip() or None
    return supersedes, None


# ---------------------------------------------------------------------------
# DR-026 sender-namespaced receiver filename
# ---------------------------------------------------------------------------

def _sender_slug(sender: str) -> str:
    """Slug-sanitize a sender identity for filename namespacing (DR-026).

    Byte-for-byte port of DoE cross-repo-memo._memo_filename's sanitization:
    lowercase, collapse any run of non-[a-z0-9-] chars to a single dash,
    collapse consecutive dashes, strip leading/trailing dashes.

    Spec backlink: DR-026 (DoE-claude docs/decisions/DR-026-cross-repo-memo-
    receiver-filename-namespace.md); DoE coordinator/bin/cross-repo-memo.py
    _memo_filename (~line 1410-1412).
    """
    if not sender:
        return ""
    return _SENDER_SLUG_RUN_DASH_RE.sub(
        "-", _SENDER_SLUG_INVALID_RE.sub("-", sender.lower())
    ).strip("-")


def _memo_filename(today: str, sender: str, topic: str) -> str:
    """Compose the DR-026 sender-namespaced receiver filename: <date>-<sender>-<topic>.md.

    DR-026: folds the sender into the receiver filename so N-repo broadcast
    replies with an identical topic slug on the same day do not collide
    (cross-sender both survive; same-sender still fails loud via the existing
    O_EXCL guard — this function only changes the pre-collision filename
    shape, not the collision semantics).

    Also ports DoE's doubled-date-prefix strip: a topic may already carry a
    leading YYYY-MM-DD- prefix (e.g. reused from a prior dated filename) —
    strip a RUN of leading date prefixes before prepending today's date, so
    the result is never a doubled <date>-<date>-<topic>.md.

    Negative-spec / deviation from DoE: DoE's _memo_filename falls back to a
    bare <date>-<topic>.md when the sanitized sender reduces to empty (its
    "defensive empty-sender fallback"). This port does NOT replicate that
    fallback — a sender always resolves to a non-empty default
    (_ENGINE_ACTOR_ID) before this function is called, so a sender that
    sanitizes to empty here means a caller-supplied from_id consisting
    entirely of punctuation/non-ASCII chars. Silently degrading to the
    pre-DR-026 filename shape in that case would silently defeat the
    namespacing guarantee this port exists to provide; failing loud instead
    surfaces the malformed from_id to the caller.

    Raises:
        ValueError: if sender sanitizes to an empty slug (see deviation note above).
    """
    sanitized = _sender_slug(sender)
    if not sanitized:
        raise ValueError(
            f"from_id {sender!r} sanitizes to an empty sender slug — a "
            f"DR-026 namespaced filename requires a non-empty sender identity."
        )
    stripped_topic = _TOPIC_DOUBLED_DATE_PREFIX_RE.sub("", topic)
    return f"{today}-{sanitized}-{stripped_topic}.md"


# ---------------------------------------------------------------------------
# YAML rendering
# ---------------------------------------------------------------------------

def _yaml_quote(value: str) -> str:
    """Double-quote a string for YAML, escaping backslashes, double-quotes, control chars.

    Mirrors memo_compose._yaml_quote (DoE shared lib, bin/lib/memo_compose.py).
    Inlined here to avoid a cross-repo import dependency while the DoE resolver
    surface is pending. Both implementations must stay in sync with the memo schema.

    Sync note: this copy adds ASCII control-char escaping (0x00-0x08, 0x0B, 0x0C,
    0x0E-0x1F, 0x7F → \\uXXXX) that the DoE memo_compose._yaml_quote may lack —
    if DoE's copy is updated to fix the same gap, re-sync the two implementations.

    Negative-spec: ALWAYS wraps in double-quotes (never bare YAML) — memo frontmatter
    requires unambiguous quoting. Do not switch to bare YAML or single-quote form.
    """
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    # Escape remaining ASCII control chars (0x00-0x08, 0x0B, 0x0C, 0x0E-0x1F, 0x7F)
    # that are invalid in YAML 1.1 double-quoted strings.
    escaped = re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]",
        lambda m: f"\\u{ord(m.group()):04x}",
        escaped,
    )
    return f'"{escaped}"'


def _yaml_scalar(value: Any) -> str:
    """Render a leaf scalar (str/bool/int/float/None) for YAML — the leaf case
    the structural renderer below bottoms out at.

    Strings recurse through `_yaml_quote` (the always-double-quote scalar
    renderer). Non-string scalar types get their normal YAML literal spelling
    — booleans/null are lowercase, numbers are bare.
    """
    if isinstance(value, str):
        return _yaml_quote(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return repr(value)
    raise TypeError(
        f"cannot render {value!r} ({type(value).__name__}) as a YAML "
        f"scalar — unsupported extra-field value type."
    )


def _render_yaml_block(value: Any, indent: int) -> list[str]:
    """Render dict/list `value` as indented YAML block lines, recursing to
    `_yaml_scalar` at the leaves.

    Sibling structural renderer to `_yaml_quote`: `_yaml_quote` stays the
    SCALAR renderer; this function is what lets a nested mapping (e.g.
    `scoped_to: {artifact, version, seam}`) round-trip as a real YAML mapping
    instead of being forced through `_yaml_quote` into a single
    double-quoted scalar string.
    """
    pad = " " * indent
    lines: list[str] = []
    if isinstance(value, dict):
        for key, sub_value in value.items():
            if isinstance(sub_value, (dict, list)):
                lines.append(f"{pad}{key}:")
                lines.extend(_render_yaml_block(sub_value, indent + 2))
            else:
                lines.append(f"{pad}{key}: {_yaml_scalar(sub_value)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                nested = _render_yaml_block(item, indent + 2)
                lines.append(f"{pad}-")
                lines.extend(nested)
            else:
                lines.append(f"{pad}- {_yaml_scalar(item)}")
    return lines


def _render_extra_field(key: str, value: Any) -> str:
    """Render one declared-extra frontmatter field (e.g. `scoped_to`) as a
    single multi-line YAML fragment, dict/list values as a nested block,
    scalars inline on the `key:` line.
    """
    if isinstance(value, (dict, list)):
        block_lines = _render_yaml_block(value, 2)
        return "\n".join([f"{key}:"] + block_lines)
    return f"{key}: {_yaml_scalar(value)}"


# kind enum — mirrors DoE cross-repo-memo._VALID_KINDS (~line 1774).
_VALID_KINDS = ("ask", "consult", "fyi", "proposal")

# Single source of truth for the two frontmatter literals that matter most for
# receiver-lifecycle correctness. `_compose_memo`'s self-validation call and
# its emitted `lines` list both reference these constants (never separate
# literals) so the validated values and the emitted values cannot silently
# diverge.
_STATUS_OPEN = "open"
_DELIVERY_MODE_RECEIVER_REPO = "receiver-repo"


def _self_validate_frontmatter_fields(
    *,
    title: str,
    from_id: str,
    to: str,
    created: str,
    status: str,
    delivery_mode: str,
    summary: Optional[str],
    kind: Optional[str],
) -> list[str]:
    """Defense-in-depth frontmatter self-check before write (invariant b).

    The engine bypasses the session-side PreToolUse Write hook that would
    otherwise validate outgoing memo frontmatter against the (DoE-owned,
    NOT vendored here) cross-repo memo schema — so every composing op must
    self-enforce the required-field shape before every write.

    Mirrors DoE cross-repo-memo._validate_outbox_frontmatter's field-presence
    semantics (~line 1777-1815): title/from/to/created/delivery_mode must be
    non-empty; status must literally equal "open" for a receiver-side
    delivery memo; summary's KEY must be present but MAY be empty; kind is
    valid-or-absent against the DR-214/D2-6 enum.

    Returns a list of error strings; empty list = valid.
    """
    errors: list[str] = []
    for field_name, value in (
        ("title", title),
        ("from", from_id),
        ("to", to),
        ("created", created),
        ("delivery_mode", delivery_mode),
    ):
        if not value:
            errors.append(f"required field '{field_name}' missing or empty")
    if summary is None:
        errors.append("required field 'summary' missing")
    if status != "open":
        errors.append(f"status must be 'open', got: {status!r}")
    if kind is not None and kind not in _VALID_KINDS:
        errors.append(
            f"kind {kind!r} is not a valid enum value "
            f"(must be one of: {', '.join(_VALID_KINDS)}; absent is also valid)"
        )
    return errors


def _compose_memo(
    *,
    from_id: str,
    to: str,
    topic: str,
    title: str,
    body: str,
    kind: str,
    summary: Optional[str],
    supersedes: Optional[str | list[str]],
    today: str,
    scoped_to: Optional[dict] = None,
    campaign_id: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    space: Optional[str] = None,
    sent_by: Optional[str] = None,
) -> str:
    """Compose a schema-valid cross-repo memo document (frontmatter + body).

    Schema-valid: to: / from: / status: open / delivery_mode: receiver-repo / kind:
    frontmatter per the cross-repo memo schema (D2 criterion 6, DoE Ask-1
    concurrence condition 1). topic lives in the filename, NOT in frontmatter
    (same as cross-repo-memo CLI convention).

    `today` is passed in by the caller (a single `datetime.date.today()` call
    at the call site) so the filename date and `created:` frontmatter field
    cannot diverge across midnight.

    Total emission over declared params, fail-loud on unknown params, and
    nested-mapping support for `scoped_to` are claude-klabauter-owned ergonomic
    divergences (A11) from DoE's memo_compose, not a byte-identical mirror.
    The nine canonical fields below keep their CURRENT fixed order and
    quoting (DR-026 / schema lockstep + the strang-03 round-trip fixture both
    depend on it) — `scoped_to` renders strictly AFTER `kind:`/`supersedes:`.

    Negative-spec: status is ALWAYS 'open' (never 'actioned', 'draft', or
    'closed') — this composes a delivery memo, never a self-receipt.

    campaign_id (DEC-3/C7, optional, additive): when supplied, renders as its
    own frontmatter line AFTER `supersedes:` and BEFORE `scoped_to:` — the
    fixed nine-field core above is untouched. Never validated for shape
    beyond non-empty-string (enforced by the caller, not here) — this
    composer only renders what it is given.

    in_reply_to (2026-07-25, optional, additive): when supplied, renders as
    its own frontmatter line AFTER `campaign_id:` and BEFORE `scoped_to:` —
    the value this composer receives is expected to already be normalized to
    a bare basename (see `_normalize_in_reply_to`); this composer only
    renders what it is given. Consumed by
    `coordinator_core.pickup_assemble._candidate_is_linked` (basename or
    basename-minus-`.md`, case-insensitive match).

    sent_by (C7, docs/plans/2026-08-13-session-identity-earns-its-keep.md):
    when supplied, renders as its own frontmatter line AFTER `in_reply_to:`
    and BEFORE `scoped_to:` — mirrors `picked_up_by` on the receive path.
    Resolved by the CALLER at send time — this composer never resolves
    session identity itself, same negative-spec as every other
    identity-bearing field it only renders. Optional in the schema (never
    required) — omitted entirely when falsy.
    """
    # Derive summary via the shared prose-first rule (footgun #4) when not
    # provided — skips ATX headings/blank/HTML-comment lines and takes the
    # first prose sentence, so composed-summary and derived-summary paths
    # stay consistent.
    # A placeholder-valued summary reaching this defense-in-depth backstop is
    # ABSENT, not an explicit value — sentinel to None so it falls into the
    # `if summary is None` derivation branch below rather than the
    # length-check one.
    if summary is not None and is_placeholder_summary(summary):
        summary = None
    if summary is None:
        summary = derive_prose_summary(body)
    else:
        # Fail loud, never truncate an EXPLICITLY authored summary — this
        # raise is the defense-in-depth backstop for a direct caller that
        # bypasses the op's own send-time cap check.
        error = validate_explicit_summary("send_backstop", summary)
        if error:
            raise ValueError(error)

    # Invariant b — self-validate before composing (defense-in-depth; the
    # engine bypasses the session-side PreToolUse Write hook, so nothing else
    # checks this).
    fm_errors = _self_validate_frontmatter_fields(
        title=title,
        from_id=from_id,
        to=to,
        created=today,
        status=_STATUS_OPEN,
        delivery_mode=_DELIVERY_MODE_RECEIVER_REPO,
        summary=summary,
        kind=kind,
    )
    if fm_errors:
        raise ValueError(
            "_compose_memo: composed frontmatter failed self-validation: "
            + "; ".join(fm_errors)
        )

    lines = [
        "---",
        f"title: {_yaml_quote(title)}",
        f"from: {_yaml_quote(from_id)}",
        f"to: {_yaml_quote(to)}",
        f"created: {today}",
        f"status: {_STATUS_OPEN}",
        f"delivery_mode: {_DELIVERY_MODE_RECEIVER_REPO}",
        f"summary: {_yaml_quote(summary)}",
        f"kind: {_yaml_quote(kind)}",   # required field — D2-6
    ]
    if supersedes:
        # List form (2026-07-28) renders through _render_extra_field as a real
        # nested YAML sequence — never _yaml_quote'd into one scalar string,
        # which would round-trip as a single bogus reference rather than N.
        # The string form keeps its exact pre-existing single-line shape.
        if isinstance(supersedes, list):
            lines.append(_render_extra_field("supersedes", supersedes))
        else:
            lines.append(f"supersedes: {_yaml_quote(supersedes)}")
    if space:
        lines.append(f"space: {_yaml_quote(space)}")
    if campaign_id:
        lines.append(f"campaign_id: {_yaml_quote(campaign_id)}")
    if in_reply_to:
        lines.append(f"in_reply_to: {_yaml_quote(in_reply_to)}")
    if sent_by:
        lines.append(f"sent_by: {_yaml_quote(sent_by)}")
    if scoped_to:
        lines.append(_render_extra_field("scoped_to", scoped_to))
    lines.append("---")
    frontmatter = "\n".join(lines) + "\n"
    return frontmatter + "\n" + body.rstrip("\n") + "\n"
