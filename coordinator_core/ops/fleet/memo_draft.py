"""
coordinator_core.ops.fleet.memo_draft — memo.draft native UDS op handler.

Purpose: Stage a NEW schema-valid outbox draft memo (status: draft) into the
CALLING repo's own state/memo-outbox/ tree — a local, non-cross-tree write.
Ported from the DoE cross-repo-memo CLI's `draft` verb per the 2026-07-17
DR-210 Option-A boundary move (claude-klabauter owns receiver-resolution + compose/
draft/list, not just send). UDS-only (no HTTP surface). Registered as
"memo.draft" via @register_op; classification and _OP_KEY_SCOPE entry are
wired in C7.

Spec backlink:
    docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C5 (AC5)
    DR-210: docs/decisions/DR-210-claude-klabauter-native-tooling-ownership-strangler.md
        § Amendment 2026-07-21 (receiver-resolution + compose/draft/list move)
    Parity source: DoE coordinator/bin/cross-repo-memo.py _cmd_draft /
        _compose_outbox_frontmatter (~line 1900-2090); footgun #4 source:
        cross-repo/inbox/2026-07-17-example-retrieval-repo-em-cross-repo-memo-cli-footguns.md

Negative-spec:
  - Does NOT write into a receiver's cross-repo/inbox/ tree — memo.send
    (mutating, cross-tree) owns that surface exclusively. memo.draft writes
    ONLY the CALLING repo's own state/memo-outbox/ (a local-tree write, not
    a cross-tree one — see C7's COMPUTE_ONLY classification rationale).
  - Does NOT grow a fleet-wide memo index — one file per draft topic, no
    aggregate manifest (DR-210 Open-Q §2 store-less-ness; mirrors AC8/
    strang-03 C6's test_no_memo_index pattern).
  - Does NOT validate --to against the receiver registry by default — the
    portable-draft default (classify_receiver absent or False) is UNCHANGED: a
    draft is still creatable when the receiver is unresolved on this machine,
    mirroring the DoE CLI's own "unresolved receivers still draft" fallthrough
    (a portable draft is valid on a machine where 'to' doesn't yet resolve).
    OPTIONAL receiver classification (classify_receiver: true) reuses the SAME
    resolution authority memo.send uses (_memo_resolver.resolve_receiver_inbox)
    to reject a publish-target or unknown `to` at draft time instead of
    deferring the error to send — see _classify_receiver_for_draft. This is a
    caller-opt-in hardening, not a change to the default: memo.list's own
    enumeration/near-match surface (C2/C4) is unaffected.
  - Does NOT overwrite an existing draft of the same topic — O_EXCL fail-loud
    (mirrors DoE _cmd_draft; memo.compose is the edit path for an existing draft).
  - Does NOT commit into the calling repo's tree — plain local file write,
    same non-committing posture as memo.send's receiver-side write.
  - Does NOT silently drop `scoped_to` (2026-07-21 break-class fix — memo.draft
    previously accepted `scoped_to_*` params and wrote a draft WITHOUT them,
    exit_code:0; same "frontmatter key vanishes silently with exit 0" defect
    class memo.send's C9/A11 fix closed). `scoped_to` is presence-triggered
    optional (mirrors memo.send._validate_scoped_to): absent entirely passes;
    present must be the COMPLETE triple (artifact + exactly one of
    version|sha + seam) or the draft call fails loud — see
    memo_draft._validate_scoped_to. When present and complete it renders as a
    REAL nested YAML mapping (memo_send._render_extra_field), never a
    double-quoted scalar.
"""

from __future__ import annotations

import datetime
import os
from pathlib import Path
from typing import Any, Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import (
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    main_worktree_root,
)
from coordinator_core.ops.fleet._memo_summary import (
    SUMMARY_PLACEHOLDER,
    _SUMMARY_MAX_CHARS,
    validate_explicit_summary,
)
from coordinator_core.ops.fleet._memo_resolver import (
    AmbiguousReceiverError,
    RegistryReadError,
    read_publish_mirror_owners as _read_publish_mirror_owners,
    resolve_receiver_inbox as _resolve_receiver_inbox,
    suggest_nearest_receiver as _suggest_nearest_receiver,
    unique_nearest_receiver as _unique_nearest_receiver,
)
from coordinator_core.ops.fleet._memo_compose import (
    _ENGINE_ACTOR_ID,
    _SCOPED_TO_KNOWN_SUBKEYS,
    _TOPIC_SLUG_RE,
    _VALID_KINDS,
    _normalize_in_reply_to,
    _render_extra_field,
    _validate_space_param,
    _validate_supersedes_param,
    _yaml_quote,
)

_MODE = "draft"

# Outbox path components — mirrors DoE's state/memo-outbox/<topic>.md convention
# (coordinator_core/ops/workday_start_cross_repo_memo_outbox_surface.py resolves
# the SAME directory for its stale-draft nudge; do not diverge from that path).
_OUTBOX_DIRNAME = ("state", "memo-outbox")

# Generator-provenance: O_EXCL-creates a NEW draft at the CALLING repo's own
# state/memo-outbox/<topic>.md -- one file per topic, a data-dependent set of
# tracked paths.
MUTATES = ["state/memo-outbox/*.md"]

# Placeholder body written into a fresh draft — guides the human/agent toward
# memo.compose (fill in body) then memo.send (deliver). Mirrors DoE's
# _cmd_draft placeholder comment.
#
# The summary-cap sentence below (2026-07-26 draft-time-discoverability fix,
# cross-repo/inbox/2026-07-26-doe-claude-em-memo-send-summary-cap-
# discoverable-at-draft-time.md) surfaces `_SUMMARY_MAX_CHARS` in the body the
# author is actually editing. A trailing YAML comment on the `summary:` line
# itself (the memo's first-suggested shape) was tried and rejected: both
# `coordinator_core.frontmatter.primitives.read_fm_field` and DoE's
# `cross-repo-memo` CLI `_parse_outbox_file` are line-oriented, no-comment-
# aware parsers — a trailing `# ...` on the summary line reads back as part
# of the field's VALUE (verified: `read_fm_field` returns
# '""  # one line, <= 120 chars' for a `summary: ""  # one line, <= 120
# chars` line), corrupting every downstream reader (memo.compose's summary
# re-derivation, the CLI's outbox validator). The body placeholder has no
# such parsing contract, so the notice lives here instead.
_BODY_PLACEHOLDER = (
    "<!-- Compose your memo body here (memo.compose), then deliver it via "
    "memo.send. -->\n"
    f"<!-- summary: one line, <= {_SUMMARY_MAX_CHARS} chars — memo.draft "
    f"WARNS (advisory, still writes) on an explicitly-authored summary over "
    f"the cap and keeps it out of summary: (recoverable here in the body "
    f"instead); memo.compose and memo.send still hard-REFUSE one. A summary "
    f"derived from your body at memo.compose time self-truncates instead. "
    f"-->\n"
    "<!-- Claims about the receiver's tree — their repo is co-located and "
    "readable, so READ IT rather than disclosing that you didn't.\n"
    "     ABSENCE claim ('X is not in your tree'): name the scope you "
    "actually searched.\n"
    "     POSITIVE claim whose truth-condition is on their disk ('this "
    "routes/seeds/lands into yours'): cite file:line plus the branch or\n"
    "     commit you read it at, or send it as a question instead. Watch "
    "the custody verbs — seed, project, land, publish, route — they take\n"
    "     YOUR data as subject while the fact they assert lives on THEIRS. "
    "Resolve their branch live (git -C <peer> rev-parse\n"
    "     --abbrev-ref HEAD); a plan's `branch:` frontmatter is where it was "
    "authored, not their current checkout.\n"
    "     (multi-channel-claim-discipline.md § Generalization, § The fourth "
    "quadrant). -->\n"
)


# ---------------------------------------------------------------------------
# scoped_to validation — mirrors memo_send._validate_scoped_to (2026-07-21 fix,
# routed via the memo.draft "silently drops scoped_to" break-class finding —
# same defect class as memo.send's C9/A11 unknown-frontmatter-key drop). The
# _SCOPED_TO_KNOWN_SUBKEYS frozenset is IMPORTED from memo_send (single source
# of truth for the sub-key shape); the error MESSAGES here are deliberately
# memo.draft-namespaced rather than reusing memo_send._validate_scoped_to
# directly — that function's error text is hardcoded "memo.send: ..." and
# would misattribute a draft-time failure to the send op. The validation
# LOGIC (presence-triggered completeness: scoped_to absent entirely passes;
# scoped_to present must be the COMPLETE triple — artifact + exactly one of
# version|sha + seam — or the draft fails loud) is a byte-for-byte mirror.
#
# Negative-spec: does NOT accept a partial triple as "good enough" (same
# rejection as memo.send) — a draft carrying an incomplete pin is exactly the
# shape this gate exists to reject, never coerced into "treat as absent" or
# "treat as complete".
# ---------------------------------------------------------------------------

def _validate_scoped_to(dry_run: bool, value: Any):
    """Validate the optional `scoped_to` param; return None on pass, else an error envelope.

    See module-level comment above this function for the full rationale
    (mirrors memo_send._validate_scoped_to's logic with memo.draft-namespaced
    error messages).
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.draft: scoped_to must be a mapping, got {type(value).__name__}",
        )
    unknown_subkeys = set(value.keys()) - _SCOPED_TO_KNOWN_SUBKEYS
    if unknown_subkeys:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.draft: scoped_to has unrecognized sub-key(s) {sorted(unknown_subkeys)} "
            f"— known sub-keys: {sorted(_SCOPED_TO_KNOWN_SUBKEYS)}",
        )
    for subkey in _SCOPED_TO_KNOWN_SUBKEYS:
        sub_val = value.get(subkey)
        if sub_val is not None and not isinstance(sub_val, str):
            return build_setup_error_result(
                _MODE, dry_run,
                f"memo.draft: scoped_to.{subkey} must be a string, got "
                f"{type(sub_val).__name__}",
            )
    artifact = value.get("artifact")
    if not artifact:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.draft: scoped_to.artifact is required (non-empty string) "
            "whenever scoped_to is present — a draft declaring scoped_to at all "
            "is a change-control memo and must carry the complete triple "
            "(artifact + exactly one of version|sha + seam), or omit scoped_to "
            "entirely for a directional ask",
        )
    seam = value.get("seam")
    if not seam:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.draft: scoped_to.seam is required (non-empty string) "
            "whenever scoped_to is present — see scoped_to.artifact error for "
            "the complete-triple rationale",
        )
    version = value.get("version")
    sha = value.get("sha")
    if bool(version) == bool(sha):
        reason = "neither version nor sha was supplied" if not version and not sha \
            else "both version and sha were supplied"
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.draft: scoped_to requires exactly one of version|sha ({reason}) "
            f"— see scoped_to.artifact error for the complete-triple rationale",
        )
    return None


# ---------------------------------------------------------------------------
# Optional receiver classification (classify_receiver: true) — C5 AC5 addition
#
# Reuses the SAME resolution authority memo.send uses (_memo_resolver) so a
# draft that classifies "clean" here is guaranteed to classify "clean" at
# send time too — the two verbs can never disagree about whether `to` resolves.
#
# Negative-spec: does NOT change the portable-draft default. This function is
# ONLY called when the caller explicitly passes classify_receiver: true — the
# default (absent/False) path never calls it, preserving the "unresolved
# receivers still draft" fallthrough documented in the module docstring.
# ---------------------------------------------------------------------------

#: rejection_class enum — the ONLY four values this module ever emits on the
#: `rejection_class` envelope key (2026-07-21 cross-repo split, DoE
#: claude-central-em consult; ambiguous-receiver added same day after PM
#: review caught the invariant gap in the initial three-value cut — an
#: undiscriminated fourth branch made "present iff classification rejection"
#: false as documented). Stable, greppable, cross-repo-contract strings:
#: a consumer that has never read our source must be able to branch on these
#: without parsing log text. Do NOT reuse a value across two different causes,
#: and do NOT rename an existing value once a consumer depends on it — treat
#: this tuple as append-only.
REJECTION_CLASS_PUBLISH_TARGET = "publish_target_rejected"
REJECTION_CLASS_UNKNOWN_RECEIVER = "unknown_receiver"
REJECTION_CLASS_REGISTRY_ERROR = "registry_error"
REJECTION_CLASS_AMBIGUOUS_RECEIVER = "ambiguous_receiver"


def _classify_receiver_for_draft(to: str, dry_run: bool):
    """Validate `to` against the shared receiver-resolution authority.

    Returns one of three shapes:
      - `None` when `to` resolves cleanly as typed (proceed to draft as
        normal — this covers both a directly-registered sibling repo and a
        central-receiver id, since resolve_receiver_inbox already unifies
        both branches).
      - `str` — 2026-07-24 papercut fix (sibling-EM report): when `to` does
        NOT resolve as typed but `_memo_resolver.unique_nearest_receiver`
        finds EXACTLY ONE registered candidate within similarity cutoff (e.g.
        'claude-klabauter-em' -> 'claude-klabauter-em'), that candidate is auto-accepted
        rather than rejected — the caller MUST substitute this string for the
        original `to` before proceeding (see `_memo_draft`'s call site). A
        one-line "resolved 'X' -> 'Y'" note belongs on stderr at the CLI
        layer, not inside this op's frozen wire envelope. Two or more
        candidates within cutoff is a genuine ambiguity, not an auto-accept —
        falls through to the UNKNOWN RECEIVER envelope below unchanged.
      - `dict` — a build_setup_error_result envelope, with a distinct,
        greppable reason string per failure class (logged daemon-side per
        build_setup_error_result's contract — the frozen wire envelope carries no
        reason field; see test_memo_send.py's caplog-based assertion pattern for
        the established convention this mirrors), AND a distinct `rejection_class`
        wire field (2026-07-21 addition — see module-level REJECTION_CLASS_*
        constants) so a caller can branch on the failure class WITHOUT parsing
        log text:

      - PUBLISH-TARGET REJECTED (rejection_class="publish_target_rejected"):
        `to` resolves to a publish.mirrors.* owner (an outward OSS
        distribution mirror, not an EM working tree) — mirrors DoE
        cross-repo-memo's _cmd_draft publish-target rejection (DoE: exit 1).
      - UNKNOWN RECEIVER (rejection_class="unknown_receiver"): `to` does not
        resolve to any registered receiver on this machine (nor auto-accept
        to a unique did-you-mean candidate — see the `str` case above), and
        is not a publish-target — mirrors DoE's unknown-receiver rejection
        (DoE: exit 2), including the same "did you mean?" nearest-match
        suggestion resolve_receiver_inbox's sibling suggest_nearest_receiver
        already produces for memo.send/memo.list.
      - REGISTRY ERROR (rejection_class="registry_error"): the machine-local
        registry file(s) exist but could not be read/parsed — mirrors DoE's
        registry-error rejection (DoE: exit 3).
      - AMBIGUOUS RECEIVER (rejection_class="ambiguous_receiver"): `to`
        resolves to more than one candidate in the machine-local registry —
        _resolve_receiver_inbox raised AmbiguousReceiverError. This IS a
        receiver-classification rejection (the caller cannot proceed to draft
        without disambiguating `to`), so it carries rejection_class exactly
        like the other three — leaving it bare would falsify the "present
        iff classification rejection" invariant below (a real defect caught
        in PM review of the initial 3-value cut).

    Publish-target is checked FIRST (mirrors DoE's _classify_receiver
    ordering): mirrors were removed from repos.* by the 2026-06-30
    registry-publish-vs-working-targets migration, so in practice the two
    checks never overlap — but ordering publish-target first keeps the
    mirror-rejection message authoritative regardless.

    Negative-spec: `rejection_class` is added on ALL FOUR envelopes this
    function can return (every one is a receiver-classification rejection —
    see the sweep note below). It is NEVER added by ordinary param-validation
    setup errors (e.g. `_validate_draft_params`'s `classify_receiver must be
    bool` path, or _memo_draft's missing-repo_root check) — those are not
    returned by this function at all. The field's presence is itself
    meaningful: present iff the exit_code:1 envelope originated from THIS
    function. See _memo_draft's docstring Returns section for the invariant
    statement.

    Sweep (2026-07-24, re-verified after the unique-did-you-mean auto-accept
    fix): this function has exactly four return points that produce a
    setup-error envelope — publish-target rejection, RegistryReadError,
    AmbiguousReceiverError, and unknown-receiver — one `return None` (the
    "resolves cleanly as typed" success path), and one `return <str>` (the
    "resolves via unique did-you-mean auto-accept" success path, no envelope
    either). All four error envelopes are stamped; no fifth error path
    exists. Nothing outside this function ever sets `rejection_class`.
    """
    normalized = to.strip().lower()

    mirror_owners = _read_publish_mirror_owners()
    if normalized in mirror_owners:
        owner = mirror_owners[normalized]
        result = build_setup_error_result(
            _MODE, dry_run,
            f"memo.draft: PUBLISH-TARGET REJECTED — {to!r} is an outward OSS "
            f"distribution mirror (publish.mirrors.*), not an EM working tree. "
            f"A memo dropped there is invisible to any EM and gets clobbered "
            f"on the next publish run. Route this concern to its owner "
            f"instead: {owner!r}.",
        )
        result["rejection_class"] = REJECTION_CLASS_PUBLISH_TARGET
        return result

    try:
        inbox_dir, receiver_repo_path, all_repos = _resolve_receiver_inbox(to)
    except RegistryReadError as exc:
        result = build_setup_error_result(
            _MODE, dry_run,
            f"memo.draft: machine-local registry could not be read: {exc.reason} "
            f"(no folder-scan fallback — fix the registry file or re-run "
            f"machine-local setup).",
        )
        result["rejection_class"] = REJECTION_CLASS_REGISTRY_ERROR
        return result
    except AmbiguousReceiverError as exc:
        result = build_setup_error_result(_MODE, dry_run, f"memo.draft: {exc}")
        result["rejection_class"] = REJECTION_CLASS_AMBIGUOUS_RECEIVER
        return result

    if inbox_dir is not None:
        return None  # resolves cleanly as typed — proceed

    unique_match = _unique_nearest_receiver(to, all_repos)
    if unique_match is not None:
        return unique_match  # unambiguous did-you-mean — auto-accept, proceed

    suggestion = _suggest_nearest_receiver(to, all_repos)
    suggestion_clause = f" Did you mean {suggestion!r}?" if suggestion else ""
    result = build_setup_error_result(
        _MODE, dry_run,
        f"memo.draft: UNKNOWN RECEIVER — {to!r} does not resolve to any "
        f"registered receiver on this machine.{suggestion_clause} Register "
        f"the receiver repo first (machine-local set repos.<name> "
        f"<abs-path-to-repo>), or check for a typo in `to`.",
    )
    result["rejection_class"] = REJECTION_CLASS_UNKNOWN_RECEIVER
    return result


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------

def _validate_draft_params(params: dict):
    """Validate memo.draft params; return an 11-tuple or a build_setup_error_result dict.

    Required: dry_run (bool), topic (slug), to (str), title (str), kind
    (validated against the DR-214/D2-6 enum; required since 2026-08-25 to
    match memo.send's own gate — see the inline note at the check).
    Optional: summary,
    scoped_to (validated via presence-triggered completeness, see _validate_scoped_to),
    classify_receiver (bool, default False — see _classify_receiver_for_draft),
    in_reply_to (str, optional — normalized to a bare basename via
    memo_send._normalize_in_reply_to; NOT existence-checked at draft time —
    that gate is send-time only, see memo_send._validate_in_reply_to_exists,
    since a draft may be staged before the sender's own inbox/archive state
    is settled), space (str, optional — see memo_send._validate_space_param),
    supersedes (str | list[str], optional — see
    memo_send._validate_supersedes_param).

    Returns (dry_run, topic, to, title, summary, kind, scoped_to,
    classify_receiver, in_reply_to, space, supersedes, summary_cap_advisory)
    on success, or an exit_code:1 setup-error envelope dict on any
    validation failure. These are plain param-validation failures, NOT
    receiver-classification rejections — the envelope this function returns
    NEVER carries a `rejection_class` field (see _classify_receiver_for_draft
    / _memo_draft's Returns section for that invariant).

    `summary_cap_advisory` (str | None) is the message from
    `_memo_summary.validate_explicit_summary("draft", summary)` — None when
    summary is absent or within cap, else the over-cap message. An over-cap
    explicit summary no longer fails this function loud (2026-08-07 warn-at-
    draft split) — the caller (`_memo_draft`) decides how to surface the
    advisory and keep the original text recoverable without writing it into
    `summary:` (AC1, AC2).
    """
    dry_run = params.get("dry_run")
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.draft: dry_run must be bool, got " + repr(type(dry_run).__name__),
        )

    topic = params.get("topic")
    if not topic or not isinstance(topic, str):
        return build_setup_error_result(
            _MODE, dry_run, "memo.draft: topic is required (non-empty string)",
        )
    if not _TOPIC_SLUG_RE.fullmatch(topic):
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.draft: topic {topic!r} is invalid — must match [a-z0-9][a-z0-9-]* "
            f"(lowercase alphanum and hyphens only, starting with alphanum). "
            f"Path chars (/, .., absolute paths) are not permitted.",
        )

    to = params.get("to")
    if not to or not isinstance(to, str):
        return build_setup_error_result(
            _MODE, dry_run, "memo.draft: to (receiver EM identity) is required (non-empty string)",
        )

    title = params.get("title")
    if not title or not isinstance(title, str):
        return build_setup_error_result(
            _MODE, dry_run, "memo.draft: title is required (non-empty string)",
        )

    summary: Optional[str] = params.get("summary") or None
    # 2026-08-07 warn-at-draft split (docs/plans/2026-08-07-memo-summary-cap-
    # warn-at-draft.md § C2): an over-cap EXPLICITLY authored summary no
    # longer fails the draft loud — memo.compose/memo.send still hard-refuse
    # (unchanged, Anti-scope), but memo.draft is a staging step the author
    # can still edit before delivery, so it advises instead. The advisory
    # message (None when summary is absent or in-cap) is carried through to
    # the handler via this tuple's last element; the ORIGINAL summary text
    # is also returned unchanged here — the handler, not this function,
    # decides how to keep it out of `summary:` while still writing it
    # somewhere recoverable (AC1, AC2).
    summary_cap_advisory = validate_explicit_summary("draft", summary)

    # `kind` is REQUIRED here, matching memo.send's own gate on the same
    # field. Drafting without it mints an artifact this op's own send verb
    # will refuse — nine such drafts had to be backfilled by hand
    # (state/bug-backlog/2026-08-25-the-memo-outbox-does-not-clean-itself-up-
    # after-a-send.yaml). Defaulting to `ask` is the wrong half to give: the
    # reader-side `ask` default exists for RECEIVED memos that predate the
    # field, and ask/proposal are premise-bearing, so a silently-mislabelled
    # fyi buys a real sender-side premise check it never needed.
    kind: Optional[str] = params.get("kind") or None
    if kind is None:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.draft: kind is required (one of: {', '.join(_VALID_KINDS)}). "
            f"memo.send refuses a draft without it, so a kindless draft can "
            f"never be delivered.",
        )
    if kind not in _VALID_KINDS:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.draft: kind {kind!r} is not a valid enum value "
            f"(must be one of: {', '.join(_VALID_KINDS)}).",
        )

    scoped_to = params.get("scoped_to")
    scoped_to_error = _validate_scoped_to(dry_run, scoped_to)
    if scoped_to_error is not None:
        return scoped_to_error

    classify_receiver = params.get("classify_receiver", False)
    if not isinstance(classify_receiver, bool):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.draft: classify_receiver must be bool when present, got "
            + repr(type(classify_receiver).__name__),
        )

    in_reply_to_raw = params.get("in_reply_to")
    in_reply_to: Optional[str] = None
    if in_reply_to_raw is not None:
        if not isinstance(in_reply_to_raw, str) or not in_reply_to_raw.strip():
            return build_setup_error_result(
                _MODE, dry_run,
                "memo.draft: in_reply_to must be a non-empty string when supplied",
            )
        in_reply_to = _normalize_in_reply_to(in_reply_to_raw)

    # space / supersedes (2026-07-28) — the two sender-declared fields the
    # inbox-blitz proposal asked for, offered here so a drafting EM is prompted
    # for them at authoring time rather than having to hand-add them after
    # memo.send. Both are deliberately un-vocabulary-checked: `space` is a
    # grouping hint the receiver may override, and a supersession reference is
    # a memo basename this op cannot resolve (the sender's draft may name a
    # memo in the RECEIVER's tree). Shape checks only — validation shared with
    # memo.send (Review: code-reviewer Finding 2, slice 1) via
    # memo_send._validate_space_param / _validate_supersedes_param.
    supersedes, supersedes_error = _validate_supersedes_param(
        _MODE, params.get("supersedes"), dry_run,
    )
    if supersedes_error is not None:
        return supersedes_error

    space, space_error = _validate_space_param(_MODE, params.get("space"), dry_run)
    if space_error is not None:
        return space_error

    return (
        dry_run, topic, to, title, summary, kind, scoped_to, classify_receiver,
        in_reply_to, space, supersedes, summary_cap_advisory,
    )


# ---------------------------------------------------------------------------
# Composition — draft frontmatter (status: draft; placeholder body)
# ---------------------------------------------------------------------------

def compose_draft_frontmatter(
    *,
    from_id: str,
    to: str,
    title: str,
    today: str,
    summary: Optional[str],
    kind: Optional[str],
    scoped_to: Optional[dict] = None,
    in_reply_to: Optional[str] = None,
    space: Optional[str] = None,
    supersedes: Optional[str | list[str]] = None,
) -> str:
    """Compose the YAML frontmatter block for a NEW outbox draft.

    status is always "draft" (never "open" — this is a local, undelivered
    staging file; memo.send is what promotes it to a delivered "open" memo).
    summary's KEY is always present. When `summary` is None (no usable
    summary resolves — nothing supplied, or the caller deliberately withheld
    an over-cap one to keep it out of this field), the VALUE written is
    `_memo_summary.SUMMARY_PLACEHOLDER` — the self-measuring ruler, not `""`
    (2026-08-07 AC3; previously an empty string) — so a fresh draft's
    `summary:` line prompts the author with the cap inline rather than
    looking like a filled-in blank. `memo.compose`/`memo.send` treat the
    placeholder as absent and fall through to body-derivation (C3), so it
    can never reach a delivered memo.

    scoped_to (2026-07-21 fix — memo.draft was silently dropping this block;
    same defect class memo.send's C9/A11 fix closed for unknown params):
    when present, rendered via memo_send._render_extra_field as a REAL nested
    YAML mapping — never coerced through _yaml_quote into a single
    double-quoted scalar string — so it round-trips as a mapping on re-parse.
    Rendered strictly AFTER kind: (mirrors _compose_memo's field ordering).

    supersedes / space (2026-07-28 inbox-blitz addition): rendered strictly
    AFTER in_reply_to: and BEFORE scoped_to:, mirroring _compose_memo's own
    ordering so a draft's frontmatter and the memo memo.send eventually
    delivers agree field-for-field. `supersedes` accepts the bare-string or
    list form and renders each the same way _compose_memo does.

    in_reply_to (2026-07-25 write-side addition): when present, rendered as a
    plain top-level scalar line, strictly AFTER kind: and BEFORE scoped_to:
    (mirrors memo_send._compose_memo's field ordering). Value is expected
    pre-normalized to a bare basename by the caller (memo_draft._memo_draft,
    via memo_send._normalize_in_reply_to) — this composer only renders what
    it is given, same discipline as scoped_to above.

    summary is capped at _SUMMARY_MAX_CHARS the same way memo.compose /
    memo.send / --self-receipt cap it, so all four summary-writing paths
    agree on the same shared cap (defense-in-depth — a draft's summary is
    re-derived and re-capped from scratch at memo.compose/memo.send time
    regardless, so this alone was never a path to an over-cap delivered memo).
    As of 2026-07-26, `_memo_draft`'s own `_validate_draft_params` already
    fails loud on an explicitly-authored over-cap summary before this
    function is ever called from that path, so the truncation below is
    unreachable via memo.draft — it is retained purely as the same
    defense-in-depth belt-and-braces every other summary-writing path
    already carries (and memo.compose's resolved_summary, already validated
    <= cap by the time it reaches here, never exercises it either).

    Mirrors DoE cross-repo-memo._compose_outbox_frontmatter. topic lives in
    the filename, NOT in frontmatter (same convention as _compose_memo).
    """
    resolved_summary = summary if summary is not None else SUMMARY_PLACEHOLDER
    if len(resolved_summary) > _SUMMARY_MAX_CHARS:
        resolved_summary = resolved_summary[: _SUMMARY_MAX_CHARS - 1] + "…"

    lines = [
        "---",
        f"title: {_yaml_quote(title)}",
        f"from: {_yaml_quote(from_id)}",
        f"to: {_yaml_quote(to)}",
        f"created: {today}",
        "status: draft",
        "delivery_mode: receiver-repo",
        f"summary: {_yaml_quote(resolved_summary)}",
    ]
    if kind is not None:
        lines.append(f"kind: {_yaml_quote(kind)}")
    if in_reply_to:
        lines.append(f"in_reply_to: {_yaml_quote(in_reply_to)}")
    if supersedes:
        # Mirrors _compose_memo's own rendering: a list becomes a real nested
        # YAML sequence via _render_extra_field, never a _yaml_quote'd scalar
        # that would round-trip as one bogus reference instead of N.
        if isinstance(supersedes, list):
            lines.append(_render_extra_field("supersedes", supersedes))
        else:
            lines.append(f"supersedes: {_yaml_quote(supersedes)}")
    if space:
        lines.append(f"space: {_yaml_quote(space)}")
    if scoped_to:
        lines.append(_render_extra_field("scoped_to", scoped_to))
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# File write helper — O_EXCL, fail-loud on existing draft
# ---------------------------------------------------------------------------

def _write_draft_file(target_path: Path, content: str) -> None:
    """Write draft content to target_path with O_EXCL-style exclusive create.

    Raises:
        FileExistsError: if target_path already exists — fail-loud (mirrors
            DoE _cmd_draft: an existing draft is edited via memo.compose or
            removed via discard, never silently clobbered by a second draft call).
    """
    target_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(target_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@register_op("memo.draft")
def _memo_draft(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'memo.draft' UDS op handler.

    Stage a NEW schema-valid draft memo into the CALLING repo's own
    state/memo-outbox/<topic>.md. Local-tree write (not cross-tree) — the
    calling repo is both the writer and the target, unlike memo.send.

    repo_root arg: git common dir from _OP_KEY_SCOPE = "common_dir" (wired
    in C7). Used to derive the caller's worktree via main_worktree_root(common_dir)
    — this IS the write target for memo.draft (unlike memo.send, where
    repo_root is ancillary and the receiver path is always registry-derived).

    dry_run:true  → validate params; compute target path; report collision
                    state; return candidate preview envelope WITHOUT any write.
    dry_run:false → validate + O_EXCL write of a fresh draft file.

    Params (all wire-supplied via JSON-RPC params dict):
        dry_run (bool, required): preview (true) vs. act (false).
        topic   (str, required):  topic slug — [a-z0-9][a-z0-9-]* only.
        to      (str, required):  receiver EM identity (e.g. "example-retrieval-repo-em").
                                   NOT validated against the receiver registry by
                                   default — a draft may name an unresolved
                                   receiver; memo.send enforces resolution. See
                                   classify_receiver below for the OPTIONAL
                                   draft-time validation opt-in.
        title   (str, required):  memo title.
        from_id (str, optional):  sender identity; defaults to "claude-klabauter-engine".
        summary (str, optional):  tl;dr ≤120 chars; left empty-string when absent
                                   (filled in / re-derived by memo.compose once
                                   a body exists — footgun #4).
        kind    (str, REQUIRED):  ask | consult | fyi | proposal. Gated here
                                  as well as at send, so `draft` cannot mint
                                  an artifact `send` will refuse.
        scoped_to (dict, optional): {artifact, exactly one of version|sha, seam} —
                                    nested mapping, round-trips as YAML (mirrors
                                    memo.send). Presence-triggered completeness:
                                    omit entirely for a directional ask; supply
                                    the COMPLETE triple for a change-control
                                    memo — a partial triple fails loud (2026-07-21
                                    fix — memo.draft previously silently dropped
                                    this field, exit_code:0, same defect class
                                    as memo.send's C9/A11 finding).
        classify_receiver (bool, optional): default False (the portable-draft
                                    default — UNCHANGED, see negative-spec).
                                    When true, `to` is classified using the
                                    SAME resolution authority memo.send uses
                                    (_memo_resolver.resolve_receiver_inbox) —
                                    see _classify_receiver_for_draft. A
                                    publish-target or unknown `to` fails the
                                    draft loud instead of deferring the error
                                    to send.
        in_reply_to (str, optional): basename (or path, normalized to
                                    basename) of the inbound memo this draft
                                    will reply to when sent. Normalized here
                                    (memo_send._normalize_in_reply_to) but NOT
                                    existence-checked at draft time — that
                                    gate is send-time only
                                    (memo_send._validate_in_reply_to_exists),
                                    since a draft may be staged before the
                                    sender's own inbox/archive state settles.
        space   (str, optional):     sender-declared thread/problem-space hint,
                                    offered at draft time so the sender names
                                    the thread they already know they are in.
                                    Non-authoritative — the receiver may
                                    override it — so it is shape-checked only,
                                    never matched against a vocabulary.
        supersedes (str | list[str], optional): prior memo reference(s) this
                                    draft retires. List entries that are not
                                    non-empty strings fail loud rather than
                                    being pruned. NOT existence-checked here
                                    for the same reason in_reply_to isn't at
                                    draft time — and additionally because a
                                    superseded memo commonly lives in the
                                    RECEIVER's tree, which this op cannot read.

    Returns:
        Both the dry_run preview candidate and the act-path acted item carry
        an additive `summary_cap_advisory` field (str | None, 2026-08-07
        warn-at-draft split) — None on a clean/absent summary, else the
        `_memo_summary.validate_explicit_summary("draft", ...)` message when
        the explicit `summary` param is over `_SUMMARY_MAX_CHARS`. This NEVER
        blocks the draft (memo.compose/memo.send still hard-refuse — see
        their own C3 handling); the over-cap text is kept out of `summary:`
        and preserved verbatim in the draft body instead (AC1, AC2).

        On a classify_receiver:true rejection, the exit_code:1 setup-error
        envelope carries an ADDITIONAL `rejection_class` wire field (str,
        2026-07-21 addition — DoE claude-central-em consult: their CLI
        previously mapped these to distinct process exit codes and could not
        reconstruct the split once collapsed to a single exit_code:1). One of:
            "publish_target_rejected" — `to` resolves to a publish.mirrors.*
                owner (DoE's prior exit 1).
            "unknown_receiver"        — `to` does not resolve to any
                registered receiver (DoE's prior exit 2).
            "registry_error"          — the machine-local registry could not
                be read/parsed (DoE's prior exit 3).
            "ambiguous_receiver"      — `to` resolves to more than one
                candidate in the machine-local registry
                (AmbiguousReceiverError).
        See module-level REJECTION_CLASS_* constants — this is a stable,
        greppable, append-only cross-repo contract; do not rename an existing
        value or reuse one across two different causes.

        Invariant: `rejection_class` is present iff the exit_code:1 envelope
        came from _classify_receiver_for_draft (a receiver-classification
        rejection). It is additive and non-breaking — every existing envelope
        field (exit_code, mode, dry_run, candidates, acted, skipped, failed)
        keeps its current name/type/value; a consumer that ignores the new
        key sees no change. It is NEVER present on an ordinary
        param-validation setup error (missing/bad-typed field, invalid kind,
        malformed scoped_to, non-bool classify_receiver, missing repo_root,
        etc.) — those setup errors are NOT receiver-classification rejections
        and must not carry this field.

    Negative-spec (see module docstring for the full set):
        - Does NOT write into a receiver's inbox — local outbox only.
        - Does NOT validate `to` by default (classify_receiver absent/False) —
          the portable-draft default is unchanged; memo.list/C2/C4 own
          receiver enumeration/near-match independently of this opt-in.
        - Does NOT overwrite an existing draft (O_EXCL fail-loud).
        - Does NOT silently drop scoped_to (2026-07-21 fix) — it either
          arrives in the draft file as a complete nested mapping, or the
          whole draft call fails loud via _validate_scoped_to.
        - Does NOT add `rejection_class` to non-classification setup errors
          (2026-07-21 addition) — the field's presence is itself meaningful:
          present iff this was a receiver-classification rejection from
          _classify_receiver_for_draft. A param-validation failure (e.g.
          `classify_receiver must be bool`) never carries it.
    """
    validated = _validate_draft_params(params)
    if isinstance(validated, dict):
        return validated  # exit_code:1 setup-error envelope

    (dry_run, topic, to, title, summary, kind, scoped_to, classify_receiver,
     in_reply_to, space, supersedes, summary_cap_advisory) = validated

    if classify_receiver:
        classification = _classify_receiver_for_draft(to, dry_run)
        if isinstance(classification, dict):
            return classification  # exit_code:1 setup-error envelope
        if isinstance(classification, str):
            # Unique did-you-mean auto-accept (2026-07-24 papercut fix) —
            # substitute the resolved id so the draft's `to:` frontmatter and
            # the acted-envelope `to` field both carry the RESOLVED receiver,
            # never the caller's unresolved literal. The CLI diffs the acted
            # envelope's `to` against the raw `--to` it sent to print the
            # "resolved 'X' -> 'Y'" stderr note — see _cmd_draft.
            to = classification

    if repo_root is None:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.draft: no repo_root supplied — memo.draft writes into the CALLING "
            "repo's own state/memo-outbox/ and requires a resolved worktree "
            "(common_dir-keyed op).",
        )
    caller_worktree = main_worktree_root(Path(repo_root))

    from_id: str = params.get("from_id") or _ENGINE_ACTOR_ID
    today = datetime.date.today().isoformat()

    outbox_dir = caller_worktree.joinpath(*_OUTBOX_DIRNAME)
    target_path = outbox_dir / f"{topic}.md"

    collision_exists = target_path.exists()

    if dry_run:
        return build_dry_run_result(_MODE, [{
            "id": str(target_path),
            "topic": topic,
            "to": to,
            "target_path": str(target_path),
            "collision": collision_exists,
            "note": (
                "collision: an outbox draft with this topic already exists — "
                "use memo.compose to edit it."
                if collision_exists else None
            ),
            # Additive, non-fatal notice (2026-08-07 warn-at-draft split —
            # mirrors _classify_receiver_for_draft's rejection_class additive
            # field): present iff the explicit `summary` param is over cap.
            # Never blocks the draft — see `summary_cap_advisory` below for
            # the act-path handling of the same condition.
            "summary_cap_advisory": summary_cap_advisory,
        }])

    # ── act path ──────────────────────────────────────────────────────────
    if collision_exists:
        return build_act_result(_MODE, [], [], [{
            "id": str(target_path),
            "reason": (
                f"collision: outbox draft {topic!r} already exists at {target_path} "
                f"— refuse (no clobber). Use memo.compose to edit it, or remove "
                f"the file to start over."
            ),
        }])

    # 2026-08-07 warn-at-draft split (AC1, AC2): an over-cap explicit summary
    # is NEVER written into `summary:` (that would be the silent truncation
    # this surface exists to prevent) — it is withheld from
    # compose_draft_frontmatter (which then writes SUMMARY_PLACEHOLDER, same
    # as the no-summary-supplied case) and instead preserved verbatim in the
    # draft BODY, ahead of the usual placeholder, so the author can recover
    # and shorten it at memo.compose time. summary_cap_advisory (already
    # computed by _validate_draft_params) rides the acted-item envelope
    # unchanged either way.
    frontmatter_summary = summary if summary_cap_advisory is None else None
    body_prefix = ""
    if summary_cap_advisory is not None:
        body_prefix = (
            "<!-- memo.draft: your summary was "
            f"{len(summary)} chars (cap {_SUMMARY_MAX_CHARS}) — it was NOT "
            "written into summary: above (that would silently truncate it); "
            "your original text is preserved below for you to shorten and "
            "move back at memo.compose time.\n"
            f"original over-cap summary: {summary}\n"
            "-->\n"
        )

    content = compose_draft_frontmatter(
        from_id=from_id, to=to, title=title, today=today,
        summary=frontmatter_summary, kind=kind,
        scoped_to=scoped_to, in_reply_to=in_reply_to, space=space,
        supersedes=supersedes,
    ) + "\n" + body_prefix + _BODY_PLACEHOLDER

    try:
        _write_draft_file(target_path, content)
    except FileExistsError:
        return build_act_result(_MODE, [], [], [{
            "id": str(target_path),
            "reason": (
                f"collision (race): outbox draft {topic!r} appeared between "
                f"collision-check and O_EXCL write — refuse (no clobber)."
            ),
        }])
    except OSError as exc:
        return build_act_result(_MODE, [], [], [{
            "id": str(target_path),
            "reason": f"write-failed: {exc}",
        }])

    result = build_act_result(
        _MODE,
        [{
            "id": str(target_path), "written": True, "topic": topic, "to": to,
            # Additive, non-fatal notice (2026-08-07 warn-at-draft split) —
            # present iff the explicit `summary` param was over cap; the
            # draft was still written (see body_prefix above for where the
            # original text landed). Never present on a clean draft.
            "summary_cap_advisory": summary_cap_advisory,
        }],
        [],
        [],
    )
    # Scope-touch declaration (2026-08-05 engine-ops-declare-what-they-write
    # plan, C1) — memo.draft creates exactly one state/memo-outbox/ path per
    # successful call; see coordinator_core/ops/queue_append.py's own
    # `_scope_touch_paths` line for the reference pattern this follows.
    result["_scope_touch_paths"] = [str(target_path)]
    return result
