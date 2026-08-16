"""
coordinator_core.ops.handoff_backfill_claim_stamp — "handoff.backfill_claim_stamp" op.

Purpose: a handoff that was picked up and worked but whose claim stamp never
landed (`claimed_at`/`claimed_by` absent) has no terminal route today —
`supersede` refuses on DR-242 (`archival.claimed_or_shipped` reads only the
parent's own frontmatter, which still says `ready_to_fire`), and
`handoff.reconcile_close_terminal` refuses because a live lineage edge makes
`closed_reason: displaced` schema-forbidden (that op's own step-0 guard names
this exact gap as a KNOWN GAP it declines to solve, on the explicit grounds
that "DR-242's discriminator is claude-klabauter's to own"). This op is the
verb that closes the gap FROM THE OTHER SIDE: instead of routing around
DR-242, it makes the frontmatter record accurate, so the gate then passes
honestly on its own terms — `supersede` afterwards runs completely
unmodified (AC5).

Reported by example-retrieval-repo-em (source_memo:
2026-08-11-example-retrieval-repo-em-handoff-lineage-terminal-unroutable.md), who hit
both this gap and the `baton_assemble` predecessor_id defect (this plan's C2,
a separate module) in one chain.

Spec backlink: pln-a-claim-stamp-backfill-verb-an-a345d2,
chunk C1, AC1-AC7.

Writes exactly three frontmatter fields via `locked_rmw`: `claimed_at`,
`claimed_by`, and `status_reason` (an EXISTING schema field, carrying the
attesting session id and the verified evidence SHAs — see AC3). Evidence is
verified, not merely recorded (AC2): every `--evidence-commit` must resolve
via `git cat-file --batch-check` in this repo, or the op refuses with no write.

Post-mutation validation is scoped to what this op actually writes, not the
whole document (see `_validate_backfilled_fields`). The intended INPUT to
this op is a legacy record that predates a schema addition — validating it
against the CURRENT full handoff schema (`required: [title, created, branch,
status, predecessor]` plus every cross-field rule) before repairing it would
refuse the exact record this op exists to repair, on fields it never
touches. `_validate_backfilled_fields` is a local, op-owned check of
`claimed_at`/`claimed_by`/`status_reason` shape only — it does NOT call
`coordinator_core.frontmatter.schema_validate` or
`handoff_transition._validate_fm`'s full-document `validate_frontmatter`,
and does not change either of those shared paths for any other caller.

Idempotency (AC4): a target already `archival.claimed_or_shipped` (ledger-
first, frontmatter-fallback — see that predicate's own docstring) is a clean
no-op at exit 0, never a second stamp layered over an existing one. This op
calls `archival.claimed_or_shipped_at_path` strictly READ-ONLY for that
check — it never imports or modifies `archival.py` itself.

Negative-spec (Anti-scope, this plan):
  - Does NOT write a claim-ledger entry. `claimed_or_shipped_at_path` is
    ledger-first with a frontmatter-mirror fallback, so a ledger write would
    ALSO satisfy that gate — and would be wrong: the ledger records LIVE
    claims, and a reconstructed stamp names a session that is long dead.
    Manufacturing a live claim on a dead holder is a worse defect than the
    one this op exists to fix. Frontmatter only, via `locked_rmw` against the
    handoff's own file — no `coordinator_core.claim_state` /
    `coordinator_core.session.claims` write of any kind.
  - Does NOT introduce a new frontmatter key. `status_reason` is an existing
    handoff-schema field; the attesting session and evidence SHAs are
    composed into its existing free-text value, mirroring the boundary
    `archive_stamp`'s own realization-correction path already honours (a
    correction clause folded into an existing free-text field rather than a
    new key, because handoff frontmatter SHAPE is coordinator-claude's
    contract, not claude-klabauter's).
  - Does NOT touch DR-242, `archival.claimed_or_shipped[_at_path]`, or the
    `supersede` gate in any way — those are called read-only, or not at all.
  - Does NOT write `shipped_in` (DR-096: `archive_stamp.stamp_shipped_in` is
    its sole writer).
  - Does NOT batch — exactly one handoff per call, mirroring the sibling
    `handoff.reconcile_close_terminal` op's single-handoff contract.
"""

from __future__ import annotations

import datetime
import logging
from pathlib import Path
from typing import Optional, Sequence

import yaml

from coordinator_core.archival import claimed_or_shipped, claimed_or_shipped_at_path
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    replace_fm_field,
    split_frontmatter,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops._path_guard import contained_path
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.handoff_transition import _resolve_path
from coordinator_core.session.core import resolve_session_id
from coordinator_core.wire_paths import rel_id as _wire_rel_id

_LOG = logging.getLogger(__name__)


def _err(msg: str) -> dict:
    """Return an exit_code=1 refusal envelope — no write attempted."""
    _LOG.warning("handoff.backfill_claim_stamp: %s", msg)
    return {
        "exit_code": 1,
        "applied": False,
        "already_claimed_or_shipped": False,
        "error": msg,
        "message": None,
    }


def _usage_error(msg: str) -> dict:
    """Return an exit_code=2 usage-error envelope (invalid params)."""
    _LOG.warning("handoff.backfill_claim_stamp: usage error: %s", msg)
    return {
        "exit_code": 2,
        "applied": False,
        "already_claimed_or_shipped": False,
        "error": msg,
        "message": None,
    }


def _validate_backfilled_fields(fm_text: str) -> list:
    """Narrow, op-local validation gate — checks ONLY the shape of the three
    fields this op writes (`claimed_at`, `claimed_by`, `status_reason`),
    never the whole document against the full handoff schema.

    Why this exists (the defect this fixes): `handoff_transition._validate_fm`
    runs `validate_frontmatter(fm_dict, _SCHEMA_PATH)` — full-document
    validation, including `required: [title, created, branch, status,
    predecessor]` and every cross-field rule in the schema. This op's entire
    purpose is repairing a LEGACY record that predates a schema addition;
    gating the repair on the record already satisfying the CURRENT full
    schema means it can never fire on the exact input it exists to fix. A
    record missing some unrelated later-added field, or one a cross-field
    rule now flags, would refuse here even though `claimed_at`/`claimed_by`/
    `status_reason` are written correctly.

    Deliberately does NOT call `coordinator_core.frontmatter.schema_validate`
    or any full-document validator — see this op module's docstring, "Why
    not shared validation": `schema_validate.py` is a hard external
    dependency (DoE imports `validate_frontmatter_obj` by path) and its
    leniency is contract; narrowing that path would be a global behaviour
    change under one caller's remit. This function is local to THIS op and
    checks nothing beyond what THIS op actually writes.

    Returns a (possibly empty) list of error dicts, same `{field, error,
    hint}` shape `handoff_transition._validate_fm` uses, so the existing
    `MutateAbort`/`format_validation_errors` call sites downstream need no
    change. Empty → valid.
    """
    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error in frontmatter: {exc}", "hint": ""}]

    errors: list = []

    claimed_at = fm_dict.get("claimed_at")
    if not isinstance(claimed_at, str) or not claimed_at.strip():
        errors.append(
            {
                "field": "claimed_at",
                "error": f"expected a non-empty string, got {claimed_at!r}",
                "hint": "claimed_at must be an ISO 8601 timestamp string",
            }
        )

    claimed_by = fm_dict.get("claimed_by")
    if not isinstance(claimed_by, str) or not claimed_by.strip():
        errors.append(
            {
                "field": "claimed_by",
                "error": f"expected a non-empty string, got {claimed_by!r}",
                "hint": "claimed_by must be a non-empty session id string",
            }
        )

    status_reason = fm_dict.get("status_reason")
    if status_reason is not None and not isinstance(status_reason, str):
        errors.append(
            {
                "field": "status_reason",
                "error": f"expected a string or null, got {status_reason!r}",
                "hint": "status_reason must be a string (or absent/null)",
            }
        )

    return errors


def _verify_commits_batch(shas: Sequence[str], worktree: Path) -> dict[str, bool]:
    """Resolve MANY evidence commit shas against `worktree` in ONE
    `git cat-file --batch-check` invocation (AC2), N spawns to 1.

    Reuses `cutover_gate._git_cat_file_batch_check` — the shared
    `--batch-check` protocol written once for C14 and reused as-is here
    (C18); do not duplicate it.
    """
    from coordinator_core.ops.cutover_gate import _git_cat_file_batch_check

    return _git_cat_file_batch_check(worktree, list(shas))


def _verify_commit(sha: str, worktree: Path) -> bool:
    """Single-sha convenience wrapper over `_verify_commits_batch`. Never
    raises: any subprocess/OSError failure reads as "does not resolve",
    not as a verification pass (see `_git_cat_file_batch_check`)."""
    return _verify_commits_batch([sha], worktree)[sha]


@register_op("handoff.backfill_claim_stamp")
async def _handler(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC "handoff.backfill_claim_stamp" — reconstruct a missing claim
    stamp (`claimed_at`/`claimed_by`) on a handoff that was worked but never
    formally claimed, from caller-supplied, git-verified evidence.

    Params:
        handoff_path (str, required)      — absolute or repo-relative path;
                     must resolve under <worktree>/state/handoffs/
                     (`handoff_transition._resolve_path`'s own containment —
                     mutation verbs are live-only).
        evidence_commit (list[str], required, >=1) — one or more commit SHAs;
                     each must resolve in this repo (`git cat-file
                     --batch-check`, one call for the whole list) or the op
                     refuses with no write (AC2).
        attested_by (str, optional)       — session id to record as the
                     attesting session; defaults to
                     `coordinator_core.session.core.resolve_session_id()`.
                     Fail-loud (usage error) if that also resolves empty.

    Returns:
        exit_code (int) — 0 ok (fresh write OR the already-claimed-or-
                          shipped idempotent no-op); 1 refusal (evidence
                          missing/unverifiable, path escape, lock/validation
                          failure); 2 usage error (missing params).
        applied (bool)  — True iff this call wrote claimed_at/claimed_by/
                          status_reason.
        already_claimed_or_shipped (bool) — True on the AC4 no-op path.
        message (str)   — human-readable outcome summary.
    """
    handoff_path_raw: str = (params.get("handoff_path") or "").strip()
    evidence_commits_raw = params.get("evidence_commit") or []
    if isinstance(evidence_commits_raw, str):
        evidence_commits_raw = [evidence_commits_raw]
    evidence_commits = [str(sha).strip() for sha in evidence_commits_raw if str(sha).strip()]
    attested_by: str = (params.get("attested_by") or "").strip()

    if not handoff_path_raw:
        return _usage_error("'handoff_path' is required")
    if not evidence_commits:
        return _usage_error(
            "at least one '--evidence-commit <sha>' is required — this op "
            "refuses to backfill a claim stamp from no evidence at all"
        )
    if repo_root is None:
        return _err(
            "repo_root is required (handler called without socket-authoritative common_dir)"
        )

    worktree = main_worktree_root(repo_root)

    if not attested_by:
        attested_by = resolve_session_id()
    if not attested_by:
        return _usage_error(
            "'attested_by' was not supplied and could not be resolved from "
            "the current session (COORDINATOR_SESSION_ID / CLAUDE_SESSION_ID / "
            "CLAUDE_CODE_SESSION_ID all empty) — pass --attested-by explicitly"
        )

    # AC2: every evidence commit must resolve in THIS repo, or refuse with no
    # write. One batch-check call covers the whole evidence list (C18).
    verified = _verify_commits_batch(evidence_commits, worktree)
    unverifiable = [sha for sha in evidence_commits if not verified.get(sha)]
    if unverifiable:
        return _err(
            "the following --evidence-commit sha(s) do not resolve in this "
            f"repo (git cat-file --batch-check failed): {', '.join(unverifiable)} — no write attempted"
        )

    try:
        path = _resolve_path(handoff_path_raw, worktree)
    except Exception as exc:  # _PathNotContained is module-private to handoff_transition
        return _err(f"backfill_claim_stamp: {exc}")

    rel_id = _wire_rel_id(path, worktree)

    # AC4: already claimed-or-shipped is an idempotent no-op, exit 0, never a
    # second stamp over an existing one. Read-only call — see module docstring.
    # This is a fast pre-lock check only; see the in-lock recheck in mutate()
    # below for the TOCTOU-safe authoritative check.
    if claimed_or_shipped_at_path(str(path)):
        return {
            "exit_code": 0,
            "applied": False,
            "already_claimed_or_shipped": True,
            "error": None,
            "message": f"{rel_id} already claimed_or_shipped — no-op (no write attempted)",
        }

    at = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    evidence_note = (
        f"claim stamp backfilled by {attested_by} from verified evidence "
        f"commit(s): {', '.join(evidence_commits)}"
    )

    _state: dict = {"applied": False, "already_claimed_or_shipped": False}

    def mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(
                f"backfill_claim_stamp: no parseable YAML frontmatter in {handoff_path_raw}"
            )

        fm = split.fm_text

        # P2 TOCTOU fix: re-verify AC4's idempotency predicate against THIS
        # freshly-read, lock-held fm text, immediately before building any
        # write. The pre-lock check above is a fast-path only — a legitimate
        # claim landing in the window between that check and lock acquisition
        # must not be misreported as "backfilled" (mirrors
        # handoff_reconcile_close_terminal._close's own P1 TOCTOU fix: recheck
        # inside the locked_rmw mutate closure, atomically with the write it
        # gates, rather than trusting an unlocked pre-check).
        if claimed_or_shipped(fm):
            _state["applied"] = False
            _state["already_claimed_or_shipped"] = True
            return old_text  # byte-identical → locked_rmw skips the write

        # claimed_at — insert if absent, anchored after deployment_state
        # (same anchor `handoff_transition._claim` uses). A pure presence
        # test (not a comparison/parse), so `read_fm_field` (not the
        # comparison-safe `_unquoted` sibling — see that function's own
        # docstring) is the correct reader here, matching the `claimed_by`
        # presence test two lines below.
        if read_fm_field(fm, "claimed_at") is None:
            fm = insert_fm_field(fm, "claimed_at", at, "deployment_state")

        # claimed_by — insert if absent, anchored after claimed_at.
        if read_fm_field(fm, "claimed_by") is None:
            fm = insert_fm_field(fm, "claimed_by", attested_by, "claimed_at")

        # status_reason — an EXISTING schema field (AC3/Anti-scope: no new
        # key). Insert if absent (anchored after claimed_by); if already
        # present, append this attestation rather than clobbering whatever
        # prior text it carried.
        existing_status_reason = read_fm_field_unquoted(fm, "status_reason")
        if existing_status_reason:
            new_value = f"{existing_status_reason}; {evidence_note}"
            fm = replace_fm_field(fm, "status_reason", new_value)
        else:
            fm = insert_fm_field(fm, "status_reason", evidence_note, "claimed_by")

        errors = _validate_backfilled_fields(fm)
        if errors:
            from coordinator_core.frontmatter.schema_validate import format_validation_errors

            details = format_validation_errors(errors)
            raise MutateAbort(f"handoff frontmatter validation failed: {details}")

        _state["applied"] = True
        return rebuild(split, fm)

    try:
        locked_rmw(path, mutate, repo_root=repo_root)
    except FileNotFoundError:
        return _err(f"backfill_claim_stamp: handoff not found: {handoff_path_raw}")
    except LockTimeout as exc:
        return _err(
            f"backfill_claim_stamp: timed out waiting for file lock on {handoff_path_raw}: {exc}"
        )
    except MutateAbort as exc:
        return _err(exc.args[0] if exc.args else "backfill_claim_stamp: mutation aborted")

    if _state["already_claimed_or_shipped"]:
        return {
            "exit_code": 0,
            "applied": False,
            "already_claimed_or_shipped": True,
            "error": None,
            "message": (
                f"{rel_id} already claimed_or_shipped — no-op (no write attempted; "
                "detected by the in-lock recheck, not the pre-lock check)"
            ),
        }

    return {
        "exit_code": 0,
        "applied": _state["applied"],
        "already_claimed_or_shipped": False,
        "error": None,
        "message": f"backfilled claim stamp on {rel_id} (attested_by {attested_by})",
    }
