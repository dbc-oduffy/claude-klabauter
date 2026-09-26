"""
coordinator_core.ops.fleet._memo_compose — shared memo-composition primitives.

Purpose: the single home for the frontmatter-composition, filename, and
param-normalization logic every memo-writing surface (memo.draft,
memo.compose, memo.list, `gate_liveness.emit_discharge`,
`contract.emit_memo_schema`, `ops.ceremony.branch_resolution`) shares. This
module used to live inside `coordinator_core.ops.fleet.memo_send` (the
memo.send op handler carried it as a "shared-helper home" alongside its own
send-only logic) — split out here so every sibling keeps a working import
independent of that module's fate. Nothing in this file is a registered op; it
has no `@register_op` and no MUTATES/writes surface of its own — each caller's
own op module owns that.

CORRECTION, 2026-08-26. This paragraph previously said the split happened
"when memo.send was killed (PM ruling 2026-08-23: killed ops die outright, no
stub)", and described `memo_send.py` as deleted. **That is not the state of
the tree and was not when it was written.** `memo.send` is REGISTERED AND
LIVE — `memo_send.py` exists, carries `@register_op("memo.send")`, and is
present on all four registration surfaces in the published mirror as well as
here; it is ABSENT from `op_budget_suspension.SUSPENDED_OPS`, whose only row
is `session.boot_sweep`. Verified independently in the mirror by doe-claude-em
(2026-08-26) after this docstring sent two EMs down a dead-name hunt: it was
cited across a repo boundary as evidence that a memo op had been killed, and
reasoning was built on it before anyone resolved the op.

Whether the 2026-08-23 ruling was reversed, or never covered this op, is a
question for the PM and is NOT answered here — but no live op is contradicting
a live kill, because there is no live kill. A docstring is not where an op's
liveness should be read from in the first place: resolve the name
(`ipc.get_op_handler`, which raises `OpSuspendedError` for a killed op) and
read `SUSPENDED_OPS`.

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

from coordinator_core.session.machinery_paths import (
    LEGACY_MEMO_OUTBOX_RELDIR,
    MEMO_OUTBOX_RELDIR,
)
from coordinator_core.ops.fleet._common import build_setup_error_result
from coordinator_core.ops.fleet.memo_kinds import VALID_KINDS as _CANONICAL_VALID_KINDS
from coordinator_core.ops.fleet._memo_resolver import (
    AmbiguousReceiverError,
    RegistryReadError,
    canonical_receiver_id as _canonical_receiver_id,
    publish_mirror_path_match as _publish_mirror_path_match,
    read_publish_mirrors as _read_publish_mirrors,
    read_registry_repos as _read_registry_repos,
    resolve_receiver_inbox as _resolve_receiver_inbox,
)
from coordinator_core._repo_root_probe import resolve_repo_root as _resolve_repo_root
from coordinator_core.frontmatter.schema_validate import parse_frontmatter
from coordinator_core.ops.fleet._memo_summary import (
    derive_prose_summary,
    is_placeholder_summary,
    validate_explicit_summary,
)


def body_opens_frontmatter(body: str) -> bool:
    normalized = body.replace("\r\n", "\n")
    return parse_frontmatter(normalized)["frontmatter"] is not None

_LOG = logging.getLogger(__name__)

# Mirrors cross-repo-memo CLI _TOPIC_SLUG_RE exactly — enforces the same
# YYYY-MM-DD-<topic>.md filename contract (5-lockstep-site invariant).
_TOPIC_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9\-]*$")

_SENDER_SLUG_INVALID_RE = re.compile(r"[^a-z0-9-]+")
_SENDER_SLUG_RUN_DASH_RE = re.compile(r"-{2,}")
_TOPIC_DOUBLED_DATE_PREFIX_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-)+")

def _resolve_engine_sender_id(root: Optional[str] = None) -> str:
    """Resolve the SENDING REPO'S OWN identity for a defaulted (falsy) `from_id`.

    `root`, when supplied, is the CALLER'S OWN resolved worktree (e.g.
    memo.draft's `caller_worktree`, itself derived from the op's `repo_root`
    param) — the warm resident engine (DR-315) serves callers from several
    repos out of one process, so identity MUST be derived from the caller's
    own repo_root, never from this process's ambient `os.getcwd()` (which
    reflects the engine's own launch directory, not whichever caller's
    request is in flight). `root` is only left to default to the ambient-cwd
    probe (`_resolve_repo_root()`) for a genuinely rootless caller (e.g.
    memo.list's preview, which has no `repo_root` param at all).

    Replaces the old `_ENGINE_ACTOR_ID = "claude-klabauter-engine"` literal (deleted
    2026-09-12, DoE `e267d18336`). That literal was justified in this
    module's history by a "DoE Ask-1 concurrence: consumers key on the file
    at the path, not on the writing process — an engine actor-id in from: is
    sufficient" — doe-claude-bc WITHDREW that concurrence at `e267d18336`.
    The publish transform rewrites repo names as a CONTENT rewrite (`claude-klabauter`
    -> `claude-klabauter`), so a literal constant is rewritable by the same
    transform regardless of what string it holds — the published engine
    shipped memos signed `claude-klabauter-engine`, a wire identity no
    receiver table knows, observed landing unreplyable in real inboxes. The
    fix is not a better constant; it is resolving to a REGISTERED RECEIVER,
    which `cross-repo-memo --list-receivers` can mechanically check.

    Resolution:
      1. The current process's git repo root (`_resolve_repo_root` — parent-
         chain walk, cwd-memoized, no subprocess spawn on the hot path; see
         that module's docstring).
      2. If that root is a registered publish-target mirror
         (`_publish_mirror_path_match`), resolve to the mirror's declared
         `.owner` — the receiver table's own answer for this exact case:
         `claude-klabauter-em` (a mirror, not a receiver) is owned by
         `claude-klabauter-em`, so a published-mirror engine resolves to the
         OWNER, never the mirror's own unaddressable alias.
      3. Otherwise, the ordinary repo-root -> EM-id resolution
         (`coordinator_core.machine_resolver.em_id_for_root`), against the
         locally-registered `repos.*` table.

    Never raises (mirrors every other reader `resolve_sender_id` degrades
    through) — a registry-read failure or an unresolvable root degrades to
    `em_id_for_root`'s own `None`-root sentinel (`"unknown-sender-em"`),
    which the compose-time assertion (`resolve_and_assert_sender_id`) then
    refuses to ship rather than silently signing a memo with it.
    """
    from coordinator_core.machine_resolver import em_id_for_root

    if not root:
        try:
            root = _resolve_repo_root()
        except Exception:  # noqa: BLE001 — identity resolution must never raise
            root = None
    if root:
        try:
            mirror_key = _publish_mirror_path_match(Path(root))
        except Exception:  # noqa: BLE001 — identity resolution must never raise
            mirror_key = None
        if mirror_key:
            owner = _read_publish_mirrors().get(mirror_key, {}).get("owner")
            if owner:
                return owner
    try:
        all_repos = _read_registry_repos()
    except RegistryReadError:
        all_repos = {}
    return em_id_for_root(root, all_repos)


def resolve_sender_id(from_id: Optional[str], root: Optional[str] = None) -> str:
    raw = from_id or _resolve_engine_sender_id(root)
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


def resolve_and_assert_sender_id(from_id: Optional[str], root: Optional[str] = None) -> str:
    """Like `resolve_sender_id`, but for a compose call that will actually
    WRITE a memo: when `from_id` was defaulted (falsy), additionally asserts
    the resolved identity is one `cross-repo-memo --list-receivers` accepts
    on this machine, raising `ValueError` at compose time rather than
    shipping an unaddressable `from:` — the invariant DoE `e267d18336`
    establishes: an engine-composed memo's `from:` must be a name the
    receiver resolver accepts, mechanically checkable at compose time, not a
    constant trusted to survive the publish transform.

    `root`: the caller's own resolved repo worktree — see `resolve_sender_id`
    and `_resolve_engine_sender_id` for why this must be threaded through
    rather than left to the ambient-cwd fallback under the warm resident
    engine, which serves several callers' repos out of one process.

    Caller-SUPPLIED `from_id` values pass straight through `resolve_sender_id`
    unchecked — this assertion is scoped to the engine's own defaulted
    identity, the wire-identity defect this function exists to close, not a
    general `from:` validator.

    Reuses `_memo_resolver.resolve_receiver_inbox` — the SAME receiver-
    vocabulary authority `memo.list --list-receivers`/`_resolve_candidate`
    already resolve through — rather than a second copy of the receiver
    vocabulary. Degrades to permissive (returns the resolved id unchecked) on
    `RegistryReadError`/`AmbiguousReceiverError` — a registry-read hiccup
    should not itself block a send that `resolve_sender_id` already degraded
    gracefully through.

    An unaccepted defaulted identity now WARNS and composes anyway, rather
    than raising. This repo's own CLAUDE.md already rules the symmetric
    receiver-side case: "Where no peer EM is reachable `memo.send` warns
    once and then sends: there a memo is a record, not a dispatch." Making
    an unaddressable SENDER a hard compose-time refusal was stricter than
    that doctrine sitting right next to it. DoE `e267d18336` asked that
    `from:` hold a RESOLVED receiver rather than a constant trusted to
    survive the publish transform — `resolve_sender_id` already satisfies
    that; refusing to compose at all was an addition beyond the ask, not
    part of it.
    """
    was_defaulted = not from_id
    resolved = resolve_sender_id(from_id, root)
    if not was_defaulted:
        return resolved
    try:
        inbox_dir, _receiver_repo_path, _all_repos = _resolve_receiver_inbox(resolved)
    except (RegistryReadError, AmbiguousReceiverError) as exc:
        _LOG.warning(
            "resolve_and_assert_sender_id: compose-time addressability "
            "assertion for defaulted sender %r SKIPPED (degrading to "
            "permissive, NOT raising) due to a registry-read failure; "
            "underlying error: %s: %s",
            resolved,
            type(exc).__name__,
            exc,
        )
        return resolved
    if inbox_dir is None:
        _LOG.warning(
            "resolve_and_assert_sender_id: engine-defaulted sender identity "
            "%r is not a name `cross-repo-memo --list-receivers` accepts on "
            "this machine — composing anyway (NOT raising; an unaddressable "
            "sender is a record, not a blocked dispatch, matching this "
            "repo's own no-reachable-peer-EM receiver-side posture).",
            resolved,
        )
    return resolved


def _normalize_in_reply_to(value: str) -> str:
    return Path(value.strip()).name


_SCOPED_TO_KNOWN_SUBKEYS = frozenset({"artifact", "version", "sha", "seam"})


def _validate_space_param(op_mode: str, value: Any, dry_run: bool):
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        return None, build_setup_error_result(
            op_mode, dry_run,
            f"memo.{op_mode}: space must be a non-empty string when supplied",
        )
    return value.strip(), None


_SUPERSEDES_ANCHORS = (
    "state/cross-repo/inbox/",
    "state/cross-repo/archive/",
    "cross-repo/inbox/",
    "cross-repo/archive/",
    f"{MEMO_OUTBOX_RELDIR}/",
    f"{LEGACY_MEMO_OUTBOX_RELDIR}/",
)

_ABSOLUTE_REF_RE = re.compile(r"^(?:/|[A-Za-z]:[\\/])")


def _normalize_supersedes_ref(ref: str) -> str:
    """Reduce a host-absolute `supersedes` reference to a portable one.

    A `supersedes` value is READ ON A DIFFERENT MACHINE than it was written
    on -- it names a memo in the receiver's tree, and receiver and sender are
    routinely different hosts and different platforms. An absolute path is
    therefore never portable, and the failure is silent: it simply resolves
    nowhere, so a reader cannot tell a superseded memo from a live one.

    OBSERVED, not hypothetical (doe-claude-em, 2026-08-26): five memos in
    DoE's inbox carry a `supersedes` of the form
    `/Users/<user>/X/DoE-claude/cross-repo/inbox/<name>.md` -- a macOS-shaped
    absolute path for a repo that lives on a Windows drive root on the box
    that received it. DoE runs a `guard-foreign-platform-paths` hook for
    exactly this shape, but inbound memo bodies are not on its beat.

    Rule, deliberately narrow so no valid value changes:
      - Not absolute -> returned VERBATIM. A repo-relative path or a bare
        memo id is already portable and is not this function's business.
      - Absolute and containing a known repo anchor -> truncated to the
        anchor, yielding the repo-relative path the receiver can resolve.
      - Absolute with no anchor -> the basename, which is what a reader
        actually matches on and is recoverable by search.

    Negative-spec: this NEVER rejects. A `supersedes` naming a memo the
    receiver cannot resolve is bad; refusing to send the superseding memo at
    all is worse, because the correction is the thing being withheld.
    """
    ref = ref.strip()
    if not _ABSOLUTE_REF_RE.match(ref):
        return ref
    unified = ref.replace("\\", "/")
    for anchor in _SUPERSEDES_ANCHORS:
        idx = unified.find(anchor)
        if idx != -1:
            return unified[idx:]
    return unified.rsplit("/", 1)[-1] or ref


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
            cleaned.append(_normalize_supersedes_ref(entry))
        if cleaned:
            supersedes = cleaned[0] if len(cleaned) == 1 else cleaned
    elif supersedes_raw is not None:
        if not isinstance(supersedes_raw, str):
            return None, build_setup_error_result(
                op_mode, dry_run,
                f"memo.{op_mode}: supersedes must be a string or a list of "
                f"strings, got {type(supersedes_raw).__name__}",
            )
        supersedes = _normalize_supersedes_ref(supersedes_raw) or None
    return supersedes, None


def _sender_slug(sender: str) -> str:
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
    fallback — a sender always resolves to a non-empty default (see
    `resolve_sender_id`/`_resolve_engine_sender_id`) before this function is
    called, so a sender that sanitizes to empty here means a caller-supplied
    from_id consisting
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


def _yaml_quote(value: str) -> str:
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\n", "\\n")
        .replace("\r", "\\r")
        .replace("\t", "\\t")
    )
    escaped = re.sub(
        r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]",
        lambda m: f"\\u{ord(m.group()):04x}",
        escaped,
    )
    return f'"{escaped}"'


def _yaml_scalar(value: Any) -> str:
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
    if isinstance(value, (dict, list)):
        block_lines = _render_yaml_block(value, 2)
        return "\n".join([f"{key}:"] + block_lines)
    return f"{key}: {_yaml_scalar(value)}"


# read `_memo_compose._VALID_KINDS`; the value is not defined here.
_VALID_KINDS = _CANONICAL_VALID_KINDS

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
    if summary is not None and is_placeholder_summary(summary):
        summary = None
    if summary is None:
        summary = derive_prose_summary(body)
    else:
        # Fail loud, never truncate an EXPLICITLY authored summary — this
        error = validate_explicit_summary("send_backstop", summary)
        if error:
            raise ValueError(error)

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
        f"kind: {_yaml_quote(kind)}",
    ]
    if supersedes:
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
