"""
coordinator_core.ops.fleet.memo_compose — memo.compose native UDS op handler.

Purpose: Fill in (or refine) the body of an EXISTING outbox draft memo staged
by memo.draft, re-deriving its `summary:` frontmatter field with a
PROSE-FIRST rule that skips heading/HTML-comment/blank lines and takes the
first sentence of actual prose (footgun #4 — the prior heading-first
derivation could emit a `##`-prefixed line as the summary). An explicit
`summary` param always wins over derivation. Local-tree write only (edits the
CALLING repo's own state/memo-outbox/<topic>.md in place) — never touches a
receiver's cross-repo/inbox/. Ported from the example-doctrine-repo cross-repo-memo CLI's
`compose` verb per the 2026-07-17 DR-210 Option-A boundary move. UDS-only
(no HTTP surface). Registered as "memo.compose" via @register_op;
classification and _OP_KEY_SCOPE entry are wired in C7.

Spec backlink:
    docs/plans/2026-07-21-memo-tool-rebuild-full-ownership.md § C5 (AC5)
    DR-210: docs/decisions/DR-210-claude-klabauter-native-tooling-ownership-strangler.md
        § Amendment 2026-07-21 (receiver-resolution + compose/draft/list move)
    Parity source: example-doctrine-repo coordinator/bin/cross-repo-memo _cmd_compose (~line 2615)
        — NOTE: example-doctrine-repo's compose is an editor-open helper (os.execvp $EDITOR), which
        has no engine-side analog (spawn-per-call, non-interactive). This native
        op instead takes the finished body as a wire param and performs the
        actual content fill-in that a human would otherwise type into $EDITOR —
        the ergonomic divergence AC7 explicitly permits (contract-conformance,
        not byte/behavior-identity with the current CLI).
    Footgun #4 source: cross-repo/inbox/2026-07-17-example-retrieval-repo-em-cross-repo-
        memo-cli-footguns.md

Negative-spec:
  - Does NOT write into a receiver's cross-repo/inbox/ tree — memo.send owns
    that surface exclusively.
  - Does NOT grow a fleet-wide memo index (DR-210 Open-Q §2 store-less-ness;
    mirrors AC8/strang-03 C6's test_no_memo_index pattern).
  - Does NOT create a new draft — memo.draft owns creation (O_EXCL); memo.compose
    requires the draft to already exist and fails loud when it does not.
  - Does NOT silently erase the optional fields memo.draft staged (2026-07-28
    fix — same defect class as memo.draft's own 2026-07-21 scoped_to drop, on
    the rewrite path instead of the create path). This handler re-composes the
    ENTIRE frontmatter block, so anything not handed back to
    `compose_draft_frontmatter` vanishes; `_CARRIED_DRAFT_FIELDS` /
    `_read_carried_fields` carry `in_reply_to`, `scoped_to`, `space` and
    `supersedes` across the rewrite verbatim. Before this fix, composing a
    draft that declared `scoped_to` or `in_reply_to` returned exit_code:0 and
    delivered a memo without them.
  - Does NOT promote status: draft -> open — that transition belongs to
    memo.send (reading the finished outbox file); memo.compose always writes
    status: draft back out, however many times it is called.
  - Does NOT exec an editor or do anything interactive — the engine is
    spawn-per-call and headless; the "compose" verb's finished-body-in, is
    passed as an explicit wire param, not typed into $EDITOR.
  - Does NOT silently truncate an EXPLICITLY authored `summary` over
    `_SUMMARY_MAX_CHARS` (2026-07-22 fix, root-caused via cross-repo/inbox/
    2026-07-22-claude-central-em-snippet-sync-adoption-and-body-drop-
    verdict.md). A DERIVED summary (the `summary` param omitted) is
    untouched — `derive_prose_summary` already self-caps. Deliberate
    divergence from any clamp/truncate behavior in example-doctrine-repo's mirror
    (`cross-repo-memo:1810-1830`'s parity note) — the former silent
    `[:_SUMMARY_MAX_CHARS - 1] + "…"` clamp is exactly the defect the routed
    memo root-caused.
    2026-08-07 PM ruling (AC9, supersedes AC7 — docs/plans/2026-08-07-memo-
    summary-cap-warn-at-draft.md § C4): `_validate_compose_params` no
    longer fails loud on an over-cap explicit summary — it WARNS and
    SUBSTITUTES the body-derived summary in its place, echoing the author's
    original text back verbatim on the result envelope
    (`summary_cap_advisory` / `summary_over_cap_original`). Substitution is
    never truncation — the invariant above survives unchanged, it is just
    enforced by substitution now rather than refusal.
  - Does NOT let a draft compose to completion with a resolved-empty summary
    (DEC-1, 2026-07-24 memo-ownership-and-redesign plan) — summary (explicit
    or derived) is UNCONDITIONALLY required (present + non-empty) by the
    time a draft is composed, front-loading memo.send's own send-time
    summary gate. This is a SEND-adjacent gate only: it does not touch the
    schema `required` array or receiver-side validation, and an existing
    on-disk memo lacking summary still validates and actions normally.
    kind stays UNGATED here — memo.compose never accepted a kind param and
    does not newly require the underlying draft to carry one; kind's
    UNCONDITIONAL requirement is enforced at memo.send only (memo_send.py),
    where kind IS a param.
"""

from __future__ import annotations
import sys

import os
import tempfile
from pathlib import Path
from typing import Optional

from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import (
    build_act_result,
    build_dry_run_result,
    build_setup_error_result,
    main_worktree_root,
)
from coordinator_core.ops.fleet.memo_draft import (
    _OUTBOX_DIRNAME,
    compose_draft_frontmatter,
)
from coordinator_core.ops.fleet.memo_send import _TOPIC_SLUG_RE, _yaml_quote
from coordinator_core.ops.fleet._memo_summary import (
    derive_prose_summary,
    is_placeholder_summary,
    validate_explicit_summary,
)
from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.frontmatter.schema_validate import parse_yaml

# Optional frontmatter fields memo.compose must CARRY THROUGH a rewrite rather
# than re-derive. memo.compose re-composes the whole frontmatter block from
# `compose_draft_frontmatter`, so any field it does not explicitly hand back to
# that composer is silently erased from the draft — the same defect class the
# 2026-07-21 memo.draft fix closed (memo.draft accepted `scoped_to` and wrote a
# draft without it), reappearing on the rewrite path instead of the create
# path. Enumerated here rather than inlined so adding a new optional draft
# field is a one-line change with one obvious place to make it.
_CARRIED_DRAFT_FIELDS = ("in_reply_to", "scoped_to", "space", "supersedes")


def _read_carried_fields(fm_text: str) -> dict:
    """Extract the optional draft fields memo.compose must preserve verbatim.

    Parses the draft's own frontmatter as YAML rather than reading each field
    as a raw line: `scoped_to` is a nested mapping and `supersedes` may be a
    sequence, and neither survives `read_fm_field`'s single-scalar-line read.
    A draft whose frontmatter does not parse to a mapping yields `{}` — the
    caller has already rejected an unparseable draft on the `split_frontmatter`
    path above, so this is a defensive floor, not a live degradation mode.
    """
    try:
        parsed = parse_yaml(fm_text)
    except Exception:
        return {}
    if not isinstance(parsed, dict):
        return {}
    return {
        key: parsed[key]
        for key in _CARRIED_DRAFT_FIELDS
        if parsed.get(key) not in (None, "", [], {})
    }

_MODE = "compose"


# ---------------------------------------------------------------------------
# Outbox frontmatter unquote (symmetric with memo_send._yaml_quote)
# ---------------------------------------------------------------------------

def _unquote(raw: str) -> str:
    """Reverse memo_send._yaml_quote's double-quote + backslash-escape scheme.

    read_fm_field() (frontmatter.primitives) returns the raw text after the
    key, INCLUDING the surrounding double quotes this codebase's composers
    always emit (memo_send._yaml_quote / memo_draft.compose_draft_frontmatter
    both always double-quote) — this undoes exactly that, not general YAML.
    """
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        inner = raw[1:-1]
        return (
            inner.replace('\\"', '"')
            .replace("\\n", "\n")
            .replace("\\r", "\r")
            .replace("\\t", "\t")
            .replace("\\\\", "\\")
        )
    return raw  # unquoted bare scalar (e.g. status: draft, created: 2026-07-21)


# ---------------------------------------------------------------------------
# Param validation
# ---------------------------------------------------------------------------

def _validate_compose_params(params: dict):
    """Validate memo.compose params; return a 3-tuple or a setup-error dict.

    Required: dry_run (bool), topic (slug), body (str; empty permitted).
    Optional: summary (explicit override; always wins over derivation).

    Returns (dry_run, topic, body, summary) on success.
    """
    dry_run = params.get("dry_run")
    if not isinstance(dry_run, bool):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.compose: dry_run must be bool, got " + repr(type(dry_run).__name__),
        )

    topic = params.get("topic")
    if not topic or not isinstance(topic, str):
        return build_setup_error_result(
            _MODE, dry_run, "memo.compose: topic is required (non-empty string)",
        )
    if not _TOPIC_SLUG_RE.fullmatch(topic):
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.compose: topic {topic!r} is invalid — must match [a-z0-9][a-z0-9-]* "
            f"(lowercase alphanum and hyphens only, starting with alphanum). "
            f"Path chars (/, .., absolute paths) are not permitted.",
        )

    body = params.get("body")
    if not isinstance(body, str):
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.compose: body is required (string; empty string is permitted)",
        )

    summary: Optional[str] = params.get("summary") or None
    summary_cap_advisory: Optional[str] = None
    summary_over_cap_original: Optional[str] = None
    # A placeholder-valued summary (memo.draft's self-measuring ruler) is
    # ABSENT, not an explicit value — fall through to derivation rather than
    # length-checking or emitting the ruler into a delivered memo (AC5,
    # docs/plans/2026-08-07-memo-summary-cap-warn-at-draft.md § C3).
    if is_placeholder_summary(summary):
        summary = None
    else:
        # 2026-08-07 PM ruling (AC9, supersedes AC7 — docs/plans/2026-08-07-
        # memo-summary-cap-warn-at-draft.md § C4): an over-cap EXPLICITLY
        # authored summary no longer refuses the compose — it WARNS and
        # SUBSTITUTES the body-derived summary in its place, echoing the
        # author's original text back verbatim (never truncated — Anti-
        # scope: substitution is not truncation). Setting `summary` to None
        # here routes the handler's own resolution below onto
        # `derive_prose_summary(body)`, exactly as if the caller had omitted
        # summary entirely — the underivable-body case is handled by the
        # handler's existing DEC-1 gate (message updated to name both
        # conditions when this advisory is present).
        error = validate_explicit_summary("compose", summary)
        if error:
            summary_cap_advisory = error
            summary_over_cap_original = summary
            summary = None

    return (dry_run, topic, body, summary, summary_cap_advisory, summary_over_cap_original)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

@register_op("memo.compose")
def _memo_compose(params: dict, repo_root=None) -> dict:
    """JSON-RPC 'memo.compose' UDS op handler.

    Fill in (or refine) the body of an EXISTING draft at the CALLING repo's
    own state/memo-outbox/<topic>.md, re-deriving summary: via the
    prose-first rule (footgun #4) unless an explicit summary param is given.
    Rewrites the file in place; status stays "draft" (memo.send performs the
    draft -> open promotion). Requires the draft to already exist — use
    memo.draft to create one first.

    repo_root arg: git common dir from _OP_KEY_SCOPE = "common_dir" (wired in
    C7). Used to derive the caller's worktree via main_worktree_root(common_dir)
    — this IS the read/write target for memo.compose, same as memo.draft.

    dry_run:true  → validate params + existing draft; compute the derived/
                    explicit summary; return preview envelope WITHOUT any write.
    dry_run:false → validate + rewrite the draft file in place (atomic replace).

    Params (all wire-supplied via JSON-RPC params dict):
        dry_run (bool, required): preview (true) vs. act (false).
        topic   (str, required):  topic slug identifying the existing draft.
        body    (str, required):  the finished memo body (empty string permitted).
        summary (str, optional):  explicit tl;dr ≤120 chars; ALWAYS wins over
                                   derivation when supplied (footgun #4 honors
                                   an explicit --summary). Whether explicit or
                                   derived, the RESOLVED summary must be
                                   non-empty (DEC-1) — a body with no
                                   derivable prose and no explicit summary
                                   fails loud rather than composing an empty
                                   summary field.

    Negative-spec (see module docstring for the full set):
        - Does NOT let compose succeed when the resolved summary is empty
          (DEC-1 — see module docstring). kind stays ungated here (not a
          memo.compose param); its UNCONDITIONAL requirement is enforced at
          memo.send only.
        - Does NOT create a new draft (memo.draft's surface; fails loud if absent).
        - Does NOT promote status: draft -> open (memo.send's surface).
        - Does NOT write into a receiver's inbox — local outbox only.
    """
    validated = _validate_compose_params(params)
    if isinstance(validated, dict):
        return validated  # exit_code:1 setup-error envelope

    (dry_run, topic, body, explicit_summary, summary_cap_advisory,
     summary_over_cap_original) = validated

    if repo_root is None:
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.compose: no repo_root supplied — memo.compose reads/writes the "
            "CALLING repo's own state/memo-outbox/ and requires a resolved "
            "worktree (common_dir-keyed op).",
        )
    caller_worktree = main_worktree_root(Path(repo_root))
    outbox_dir = caller_worktree.joinpath(*_OUTBOX_DIRNAME)
    target_path = outbox_dir / f"{topic}.md"

    if not target_path.is_file():
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.compose: no outbox draft {topic!r} found at {target_path} "
            f"— use memo.draft to create one first.",
        )

    try:
        existing_content = target_path.read_text(encoding="utf-8")
    except OSError as exc:
        return build_setup_error_result(
            _MODE, dry_run, f"memo.compose: could not read {target_path}: {exc}",
        )

    split = split_frontmatter(existing_content)
    if split is None:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.compose: {target_path} has no parseable YAML frontmatter — "
            f"the draft is malformed. Fix it manually or discard and re-draft.",
        )

    from coordinator_core.frontmatter.primitives import read_fm_field

    status = read_fm_field(split.fm_text, "status")
    if status != "draft":
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.compose: {target_path} has status: {status!r}, expected "
            f"'draft' — memo.compose only edits undelivered drafts.",
        )

    raw_title = read_fm_field(split.fm_text, "title")
    raw_from = read_fm_field(split.fm_text, "from")
    raw_to = read_fm_field(split.fm_text, "to")
    raw_created = read_fm_field(split.fm_text, "created")
    raw_kind = read_fm_field(split.fm_text, "kind")

    if raw_title is None or raw_from is None or raw_to is None or raw_created is None:
        return build_setup_error_result(
            _MODE, dry_run,
            f"memo.compose: {target_path} is missing a required frontmatter field "
            f"(title/from/to/created) — the draft is malformed.",
        )

    title = _unquote(raw_title)
    from_id = _unquote(raw_from)
    to = _unquote(raw_to)
    created = raw_created  # bare YYYY-MM-DD, never quoted by our composers
    kind = _unquote(raw_kind) if raw_kind is not None else None
    # Note: kind stays optional at memo.compose (mirrors memo.draft — kind is
    # only UNCONDITIONALLY required at memo.send, DEC-1's actual send-time
    # gate; memo.compose never accepted a kind param and does not newly
    # require the underlying draft to carry one — see module docstring).

    if explicit_summary is not None:
        # No clamp here (2026-07-22 body-drop verdict memo fix) — an
        # over-cap EXPLICIT summary is substituted (never truncated) in
        # _validate_compose_params, above, so an over-cap value can never
        # reach this line. Silently truncating it here (the former
        # `[:_SUMMARY_MAX_CHARS - 1] + "…"` clamp) is the exact defect this
        # fix removes.
        resolved_summary = explicit_summary
    else:
        resolved_summary = derive_prose_summary(body)

    # DEC-1 — summary must resolve non-empty (present + non-empty), whether
    # supplied explicitly or derived from body. A body with no surviving
    # prose line (derive_prose_summary returns "") is no longer silently
    # written as an empty summary field — it fails loud, matching the
    # UNCONDITIONAL send-time requirement this front-loads.
    if not resolved_summary:
        if summary_cap_advisory is not None:
            # 2026-08-07 PM ruling (C4): the explicit summary was over cap
            # AND the body has no derivable prose to substitute in its
            # place — substitution has nothing to substitute. Name BOTH
            # conditions, not just "resolved empty" (never deliver an empty
            # summary).
            return build_setup_error_result(
                _MODE, dry_run,
                f"memo.compose: summary resolved empty — the explicit "
                f"summary was over cap ({summary_cap_advisory}) and the "
                f"body has no derivable prose sentence to substitute in its "
                f"place (DEC-1 requires a non-empty summary before a draft "
                f"can be sent).",
            )
        return build_setup_error_result(
            _MODE, dry_run,
            "memo.compose: summary resolved empty — supply an explicit "
            "summary or ensure body has a derivable prose sentence (DEC-1 "
            "requires a non-empty summary before a draft can be sent).",
        )

    if dry_run:
        return build_dry_run_result(_MODE, [{
            "id": str(target_path),
            "topic": topic,
            "target_path": str(target_path),
            "summary": resolved_summary,
            "collision": False,
            "note": None,
            # Additive, non-fatal notice (2026-08-07 warn-and-substitute PM
            # ruling, AC9) — present iff the explicit `summary` param was
            # over cap; None on a clean/absent/placeholder summary.
            "summary_cap_advisory": summary_cap_advisory,
            "summary_over_cap_original": summary_over_cap_original,
        }])

    # ── act path — rewrite frontmatter + body, keep status: draft ───────────
    new_frontmatter = compose_draft_frontmatter(
        from_id=from_id, to=to, title=title, today=created,
        summary=resolved_summary, kind=kind,
        **_read_carried_fields(split.fm_text),
    )
    new_content = new_frontmatter + "\n" + body.rstrip("\n") + "\n"

    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=str(outbox_dir), prefix=f".{topic}.", suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(new_content)
            # Review: code-reviewer — mkstemp defaults to 0o600; chmod to 0o644
            # before replace so compose doesn't silently narrow the draft's
            # permissions from memo_draft.py's 0o644 down to owner-only.
            os.chmod(tmp_path, 0o644)
            os.replace(tmp_path, str(target_path))
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                print(f"skip: _memo_compose: os.remove(tmp_path) failed: {sys.exc_info()[1]}", file=sys.stderr)
                pass
            raise
    except OSError as exc:
        return build_act_result(_MODE, [], [], [{
            "id": str(target_path),
            "reason": f"write-failed: {exc}",
        }])

    result = build_act_result(
        _MODE,
        [{
            "id": str(target_path), "written": True, "topic": topic,
            "summary": resolved_summary,
            # Additive, non-fatal notice (2026-08-07 warn-and-substitute PM
            # ruling, AC9) — mirrors the dry_run candidate's own pair of
            # fields above.
            "summary_cap_advisory": summary_cap_advisory,
            "summary_over_cap_original": summary_over_cap_original,
        }],
        [],
        [],
    )
    # Scope-touch declaration (2026-08-05 engine-ops-declare-what-they-write
    # plan, C1) — memo.compose rewrites exactly one state/memo-outbox/
    # path per successful call; see coordinator_core/ops/queue_append.py's
    # own `_scope_touch_paths` line for the reference pattern this follows.
    result["_scope_touch_paths"] = [str(target_path)]
    return result
