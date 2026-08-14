"""
coordinator_core.ops.fleet.backfill_memo_disposition — fleet.backfill_dispositionless_memos op.

Purpose: backfill `realized_by`/`actioned_note` frontmatter onto the 34 memos
flipped `open`/`in_progress` -> `actioned` by hand-edit across four commits
(70a54f9b, 47bc7eed, 989bf41b, ca9d3e83) on 2026-07-26, with no disposition field
attached. All 34 already carry a REAL disposition — a reply memo, a
state/handoffs/ baton, or a landing commit — traced by a triage sweep and
persisted at state/audits/2026-07-26-dispositionless-memo-flip-sink-mapping.md
(commit 1d9559c0). This op writes that traced disposition onto disk through
frontmatter primitives, the same write surface memo_transition.py uses, rather
than a hand `Edit` of the 34 files — a hand-edited backfill would reproduce the
exact defect it exists to repair (and, since block_memo_status_hand_edit.py
(C3 of the same plan) is live, would now be refused outright for the status
field; this op never touches status:, only the disposition fields).

C5 does not depend on memo.transition's `resolve` verb (C1): `resolve` requires
status: open (see memo_transition.py:_resolve's precondition) and these 34 are
already `status: actioned` — the archival preconditions `resolve`/`_action` both
assume simply do not hold here. This is its own narrow verb: locate each named
memo under cross-repo/archive/, verify status: actioned, and write exactly ONE
of realized_by/actioned_note per the traced mapping, inside a single
locked_rmw closure (same lock/write primitive memo_transition.py uses, no
second concurrent-write surface introduced).

Idempotent — outcome-predicate no-op pattern (docs/wiki/idempotent-op-design-catalogue.md
row 1): a memo that already carries any of decision/decision_note/realized_by/
actioned_note (_DISPOSITION_FIELDS, imported from archive_actioned_memos.py so
the vocabulary exists exactly once) fails the "needs a disposition" predicate by
construction and is skipped, never overwritten. A second run over the same 34
is therefore a no-op by construction, not by an explicit already-ran tracker.

Self-registration: importing this module calls
register_op("fleet.backfill_dispositionless_memos", _handler) as a side-effect.
Added to coordinator_core/ops/__init__.py's eager-import list to trigger
registration at process start.

Spec backlink: pln-give-the-memo-disposition-flip-e580c2 § C5

Negative-spec:
  - Does NOT git mv, git add, or git commit anything — unlike the DR-211 archival
    trio (archive_actioned_memos et al.), this op edits frontmatter FIELDS on
    memos that are already resident in cross-repo/archive/; there is no file
    move, so DR-211's archival-writer bounds (D1 sanctioned-ops list, D2 five
    bounds) do not apply to this op — it is a plain frontmatter write, the same
    shape memo.transition's claim/action/release/resolve verbs already make
    (classification.py's "memo.transition" entry is the precedent this op's own
    classification entry mirrors).
  - Does NOT touch status: — the 34 memos are already `status: actioned`; this
    op writes only realized_by or actioned_note, never re-derives or rewrites
    status.
  - Does NOT overwrite an existing disposition field — see idempotency note
    above. A memo whose traced disposition would DIFFER from an existing field
    value is still skipped, not reconciled; this op is a one-shot backfill for
    memos that have NONE of the four fields, not a general disposition editor.
  - Does NOT scan cross-repo/archive/ for candidates — the 34-entry table below
    is the complete, closed input (sourced from the persisted triage mapping,
    not re-derived at runtime). A memo outside this table is untouched.
  - Does NOT use raw bare 8-hex heuristics against the mapping file at runtime —
    the table below is a literal, reviewed transcription, not a markdown-table
    parse of the audit file (which would silently re-derive machine-readable
    truth from a doc surface the same way this whole incident began).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    split_frontmatter,
)
from coordinator_core.frontmatter.schema_validate import (
    format_validation_errors,
    validate_memo_cross_fields,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.fleet.archive_actioned_memos import _DISPOSITION_FIELDS

import asyncio
import yaml

_LOG = logging.getLogger(__name__)
_LOG.addHandler(logging.NullHandler())


# ---------------------------------------------------------------------------
# The 34-entry backfill table — literal transcription of
# state/audits/2026-07-26-dispositionless-memo-flip-sink-mapping.md (commit
# 1d9559c0), NOT a runtime parse of that file (see module negative-spec).
#
# Field choice per the mapping's own notes: a sink_kind containing "commit"
# carries realized_by:<sha> (the landing commit is the claim-of-record); every
# other sink_kind (reply-memo, baton, spinoff, terminal-ack, and combinations
# without a commit) carries actioned_note naming the artifact or, for
# terminal-ack rows, the fact that nothing was owed.
# ---------------------------------------------------------------------------

BACKFILL_TABLE: dict[str, dict[str, str]] = {
    "2026-07-26-example-store-repo-em-workday-complete-backfill-directives-misinvoked.md": {
        "realized_by": "baf6c672",
    },
    "2026-07-25-doe-claude-em-orient-assemble-phantom-verbs.md": {
        "actioned_note": "spinoff: state/handoffs/2026-07-26-orient-assemble-p1-p2-residue-four-unrel.md",
    },
    "2026-07-25-doe-claude-em-schema-validator-keyword-gap.md": {
        "realized_by": "4f7a2ae2",
    },
    "2026-07-25-doe-claude-em-planless-dispatch-sidecar-provisioning.md": {
        "actioned_note": (
            "reply memo: DoE 2026-07-26-claude-klabauter-em-planless-dispatch-sidecar-fallback-exists.md"
        ),
    },
    "2026-07-24-doe-claude-em-b4-author-fork-seam-and-apply-base-heads-up.md": {
        "actioned_note": (
            "reply memo: DoE 2026-07-26-claude-klabauter-em-b4-author-fork-seam-decision.md; "
            "follow-on 17e130a1, 2fdee742"
        ),
    },
    "2026-07-23-claude-central-em-review-diff-freeze-op-wanted.md": {
        "realized_by": "8d0a4285",
    },
    "2026-07-23-claude-central-em-distill-active-reference-provenance-exclusion.md": {
        "realized_by": "2afd28e8",
    },
    "2026-07-23-claude-central-em-learn-lessons-skill-repaired-and-drain-leg-verified.md": {
        "realized_by": "d6571942",
    },
    "2026-07-22-claude-central-em-adopt-portability-gates.md": {
        "actioned_note": "reply memo: DoE 2026-07-26-claude-klabauter-em-portability-gates-already-discharged.md",
    },
    "2026-07-22-claude-central-em-tests-returned-22-not-40-split-subject-kept.md": {
        "actioned_note": "spinoff: state/handoffs/2026-07-26-split-subject-test-seam-audit-13-files-n.md",
    },
    "2026-07-25-example-market-data-repo-em-competitor-identifier-wire-cc.md": {
        "actioned_note": (
            "baton: state/handoffs/2026-07-21_190743_track-b-competitor-uid-anchor-migration.md; "
            "reply memo: example-market-data-repo 2026-07-26-claude-klabauter-em-competitor-url-field-bilateral-gate.md"
        ),
    },
    "2026-07-25-example-cockpit-repo-em-claude-klabauter-durable-priority-artifact.md": {
        "actioned_note": "spinoff: state/handoffs/2026-07-26-cockpit-durable-priority-artifact-chain-.md",
    },
    "2026-07-25-example-cockpit-repo-em-claude-klabauter-sovereign-tracker-store.md": {
        "actioned_note": "spinoff: state/handoffs/2026-07-26-cockpit-sovereign-action-item-tracker-st.md",
    },
    "2026-07-26-example-cockpit-repo-em-fleet-state-memo-ingest-landed.md": {
        "actioned_note": "reply memo: cockpit 2026-07-26-claude-klabauter-em-memo-body-projection-scope-accepted.md",
    },
    "2026-07-26-doe-claude-em-memo-send-summary-cap-discoverable-at-draft-time.md": {
        "realized_by": "892045f4",
    },
    "2026-07-26-doe-claude-em-rollup-scan-incomplete-widened.md": {
        "realized_by": "0b9c5b6c",
    },
    "2026-07-26-doe-claude-em-handoff-schema-2-1-0-carried-items-bump.md": {
        "realized_by": "0268d303",
    },
    "2026-07-26-example-cockpit-repo-em-dr084-p4-relay-status.md": {
        "realized_by": "a0bf045e",
    },
    "2026-07-23-example-cockpit-repo-em-dr084-disposed-successors-corpus-answers.md": {
        "actioned_note": "baton: state/handoffs/2026-07-22_152148_0b60bae8-0b1b-42cd-bcde-8581920e349b.md",
    },
    "2026-07-25-doe-claude-em-zero-tool-use-store-records-every-count.md": {
        "realized_by": "244e4046",
    },
    "2026-07-26-doe-claude-em-hooks-twin-retirement-confirm.md": {
        "realized_by": "4f20a1a2",
    },
    "2026-07-26-doe-claude-em-denode-lint-frontmatter-confirm.md": {
        "realized_by": "c79e66cd",
    },
    "2026-07-26-doe-claude-em-fleet-capability-gate3-correction.md": {
        "actioned_note": (
            "reply memo: DoE 2026-07-26-claude-klabauter-em-gate3-exemplar-already-landed-no-relay-owed.md "
            "(relay moot -- rag 33fa28182)"
        ),
    },
    "2026-07-26-doe-claude-em-excision-memo-spinoff-stood-down-2026-07-08.md": {
        "actioned_note": "terminal-ack: declines receiver-side build; nothing owed",
    },
    "2026-07-26-doe-claude-em-handoff-chain-exit-code-confirm.md": {
        "actioned_note": "terminal-ack: answers a claude-klabauter question; no edit needed",
    },
    "2026-07-26-doe-claude-em-tri-plane-citation-sweep-gap.md": {
        "actioned_note": "terminal-ack: no such convention exists; DoE retains authorship",
    },
    "2026-07-26-doe-claude-em-nine-baton-fixed-ack.md": {
        "actioned_note": "terminal-ack: ack of four claude-klabauter fixes against 05bc8c8c",
    },
    "2026-07-26-doe-claude-em-ceremony-c18-subsumed.md": {
        "actioned_note": "terminal-ack: self-declaring no-op; subsumed by companion memo",
    },
    "2026-07-26-doe-claude-em-coverage-gate-ack.md": {
        "actioned_note": "terminal-ack: pure ack of a claude-klabauter fix",
    },
    "2026-07-26-doe-claude-em-docgen-oss-reply.md": {
        "actioned_note": "terminal-ack: agreement citing DR-074 precedent",
    },
    "2026-07-26-doe-claude-em-guard-cluster-near-miss-ack.md": {
        "actioned_note": "terminal-ack: pure ack; related fixes 8fb0c481, e88bc98b",
    },
    "2026-07-26-doe-claude-em-guard-false-positives-ack.md": {
        "actioned_note": "terminal-ack: pure ack; underlying fix e88bc98b",
    },
    "2026-07-26-example-cockpit-repo-em-content-hash-not-load-bearing-c9-reset-goal-filter.md": {
        "actioned_note": "terminal-ack: reply to our own memo; ask of us was a deprioritisation",
    },
    "2026-07-26-example-retrieval-repo-em-workstate-store-projection-confirmed-dormant-exception.md": {
        "actioned_note": "terminal-ack: confirms our rule; follow-up is rag-resident",
    },
}


def _disposition_field(entry: dict[str, str]) -> tuple[str, str]:
    """Return the (field_name, value) pair for a BACKFILL_TABLE entry.

    Every entry carries exactly one of realized_by/actioned_note — enforced by
    construction of the table above, re-asserted here so a future edit to the
    table that accidentally adds both (or neither) fails loud instead of
    silently writing an arbitrary one.
    """
    if "realized_by" in entry and "actioned_note" not in entry:
        return "realized_by", entry["realized_by"]
    if "actioned_note" in entry and "realized_by" not in entry:
        return "actioned_note", entry["actioned_note"]
    raise ValueError(
        f"backfill_memo_disposition: table entry must carry exactly one of "
        f"realized_by/actioned_note, got keys {sorted(entry.keys())!r}"
    )


def _apply_one(memo_path: Path, field: str, value: str, repo_root: Path) -> dict:
    """Write ``field: value`` onto ``memo_path`` inside one locked_rmw closure.

    Skips (outcome-predicate no-op) if the memo already carries any of
    _DISPOSITION_FIELDS. Fails loud (no write) on missing/unparseable
    frontmatter, an unexpected status, or a post-write cross-field validation
    error — never silently corrupts a memo to make the backfill "succeed".
    """
    if not memo_path.is_file():
        return {"id": memo_path.name, "reason": "memo-not-found"}

    _noop_reason: list[Optional[str]] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {memo_path}")

        status = read_fm_field_unquoted(split.fm_text, "status")
        if status != "actioned":
            raise MutateAbort(
                f'unexpected status "{status or "(missing)"}" for {memo_path.name} '
                "— expected actioned (this op backfills already-archived memos only)"
            )

        if any(read_fm_field(split.fm_text, f) is not None for f in _DISPOSITION_FIELDS):
            _noop_reason[0] = "already-has-disposition"
            return old_text

        fm_text = insert_fm_field(split.fm_text, field, value, "status", numeric_quoting=True)

        try:
            fm_dict = yaml.safe_load(fm_text) or {}
        except Exception as exc:  # noqa: BLE001
            raise MutateAbort(f"YAML parse error after writing {field}: {exc}") from exc
        errors = validate_memo_cross_fields(fm_dict)
        if errors:
            raise MutateAbort(
                f"cross-field validation failed after writing {field} on {memo_path.name}: "
                f"{format_validation_errors(errors)}"
            )

        return rebuild(split, fm_text)

    try:
        new_text = locked_rmw(memo_path, _mutate, repo_root=repo_root)
    except MutateAbort as exc:
        return {"id": memo_path.name, "reason": str(exc.args[0]) if exc.args else "mutate-abort"}
    except LockTimeout as exc:
        return {"id": memo_path.name, "reason": str(exc)}
    except FileNotFoundError:
        return {"id": memo_path.name, "reason": "memo-not-found"}

    if _noop_reason[0] is not None:
        return {"id": memo_path.name, "reason": _noop_reason[0]}

    written_split = split_frontmatter(new_text)
    written = written_split is not None and read_fm_field_unquoted(written_split.fm_text, field) is not None
    if not written:
        return {"id": memo_path.name, "reason": f"INTERNAL ERROR — {field} not present post-write"}

    return {"id": memo_path.name, field: value}


def backfill_dispositionless_memos(worktree_root: Path) -> dict:
    """Apply BACKFILL_TABLE against cross-repo/archive/ under worktree_root.

    Re-runnable (AC10): a memo already carrying a disposition field is skipped,
    so a second call over the same table is a no-op by construction.

    Returns {"applied": [...], "skipped": [...], "failed": [...]}, each a list
    of {"id": <filename>, ...} dicts.
    """
    archive_dir = worktree_root / "cross-repo" / "archive"
    applied: list[dict] = []
    skipped: list[dict] = []
    failed: list[dict] = []

    for filename in sorted(BACKFILL_TABLE):
        field, value = _disposition_field(BACKFILL_TABLE[filename])
        memo_path = archive_dir / filename
        result = _apply_one(memo_path, field, value, worktree_root)

        if "reason" not in result:
            applied.append(result)
        elif result["reason"] in ("already-has-disposition",):
            skipped.append(result)
        else:
            failed.append(result)

    return {"applied": applied, "skipped": skipped, "failed": failed}


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("fleet.backfill_dispositionless_memos")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """fleet.backfill_dispositionless_memos — backfill the 34 traced dispositions.

    Takes no params — the 34-entry table is the complete, closed input (see
    module negative-spec). ``repo_root`` arrives as the git common dir (same
    handler-arg convention as the other fleet.* ops); the worktree is derived
    via main_worktree_root, never from params.
    """
    if repo_root is None:
        _LOG.error("fleet.backfill_dispositionless_memos: repo_root handler arg is None")
        return {"exit_code": 1, "applied": [], "skipped": [], "failed": [], "error": "repo_root is None"}

    common_dir = Path(repo_root) if not isinstance(repo_root, Path) else repo_root
    worktree = main_worktree_root(common_dir)

    result = await asyncio.to_thread(backfill_dispositionless_memos, worktree)
    return {"exit_code": 0, **result}
