"""
coordinator_core.ops.memo_transition — memo lifecycle transition op (memo.transition op).

Purpose: Native Python port of DoE-claude coordinator/bin/memo-transition.js — atomic
cross-repo-memo lifecycle frontmatter transitions. Implements ``claim``, ``action``, and
``release`` verbs that mutate memo state using the coordinator_core frontmatter primitives,
byte-faithful to the node oracle. Also implements ``resolve``, a native-only verb with no
JS mirror (see Parity note below). No subprocess / node reach-back.

Parity oracle: DoE-claude coordinator/bin/memo-transition.js — covers claim/action/release
only. ``resolve`` is a native-only composition introduced by C1 of
docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md; claude-klabauter owns
cross-repo-memo tooling outright post-strangler-cut (DR-210), so this verb has no JS-side
oracle to stay byte-faithful to and none is expected. Do NOT read the module-level
"byte-faithful to the node oracle" claim above as covering ``resolve`` — it covers only the
three JS-mirrored verbs.
Spec backlink: pln-memo-transition-native-python--7e1dd0 (claim/action/release)
Spec backlink: pln-give-the-memo-disposition-flip-e580c2 (resolve, C1)

Verb contracts (mirrored from the JS spec, plus the native-only additions):
  claim   — open (or delivered, treated identically — see "delivered" note below)
              → in_progress; writes picked_up_at + picked_up_by.
  action  — in_progress → actioned; writes decision/decision_note/realized_by
              OR actioned_note (consult/fyi shape) OR, when the receiving end
              holds a confirmed supersession, ``superseded_by`` (status →
              superseded rather than actioned — the receiver-side write half
              of the memo schema's supersession pair; the pointer must name a
              memo present in this repo's own cross-repo/inbox/ or
              cross-repo/archive/, validated BEFORE any write, mirroring
              memo_send._validate_in_reply_to_exists). Preserves picked_up_by/at.
              An already-actioned memo re-actioned with ``correct_realization``
              truthy AND an UNCHANGED ``decision:`` may move ``realized_by``/
              ``decision_note`` only (evidence correction, e.g. a cited commit
              was later reverted); on an ``actioned_note``-shape memo (no
              ``decision:`` on disk) the same flag with no ``--decision`` may
              instead correct ``actioned_note`` in place — either way the
              superseded value is preserved inside a ``[correction ...]``
              clause, never dropped. A verdict or disposition-SHAPE change
              still fails loud regardless of the flag. See
              ``_handle_already_actioned``.
              An already-actioned/superseded memo re-actioned with
              ``supersede_note``+``supersede_realized_by`` (mutually exclusive
              with ``decision``/``actioned_note``/``superseded_by``) records
              an APPEND-ONLY reversal of the disposition itself — the
              original decision/actioned_note/realized_by are left untouched
              on disk; four new ``superseding_*``/``disposition_superseded``
              fields are anchored right after ``status`` so a reader hits the
              current truth first, superseded original as history beneath.
              See ``_handle_supersede`` / ``_apply_supersede_fields``.
  release — in_progress → open; removes picked_up_by + picked_up_at entirely.
  resolve — open (or delivered) → actioned in ONE locked_rmw closure (native-only,
              no JS mirror). Collapses claim+action into a single atomic write —
              no intermediate in_progress state is ever visible on disk. This is
              the same two-step ceremony (archive-stamp-cli claim-memo-stamp, then
              action-memo) that
              state/lessons/2026-07-24-memo-terminal-flip-is-a-two-step-transit-147cc531ae68.yaml
              documents as already-established convention, collapsed into one call.
  lift    — draft → open (native-only, no JS mirror). The one receiver-side move
              a hand-delivered ``status: draft`` memo has no other way to reach —
              see ``_lift``'s own docstring for the memo backlink.

"delivered" status (claim/resolve): a sender-side path can stamp a delivered
memo ``status: delivered`` rather than ``open`` — previously a lifecycle dead end
(state/cross-repo/archive/2026-09-02-example-cockpit-repo-em-memo-status-delivered-is-
a-lifecycle-dead-end-no-verb-accepts.md), since no verb accepted that value.
claim and resolve now accept it everywhere they accept "open".

``cwd`` (all verbs, optional): a relative ``memo`` anchors to it instead of this
process's own cwd (state/cross-repo/archive/2026-09-05-claude-klabauter-engine-
memo-transition-ignores-cwd-and-its-errors-misdirect.md). See ``_containment_check``.

Dup-key guard (C5): ≥2 status: keys before any mutation → fail-loud no-write.
Post-write self-verify: exactly 1 status: key must remain after write → INTERNAL ERROR.

Return contract (AC6, claude-klabauter-client op-result):
  {"exit_code": 0, "applied": bool,  "message": str, "commit_sha": str} — applied
    (the "commit_sha" key is additive — DR-273/C13 — and present ONLY when a
    real write landed and was committed this call; an idempotent no-op reply
    never carries it, and an existing consumer reading only exit_code/applied/
    message is unaffected)
  {"exit_code": 0, "applied": False, "message": str, "commit_sha": str,
   "resumed": True} — a STRANDED-WRITE RESUME (Defect 2, C5 of
    docs/plans/2026-08-06-writer-side-commit-ownership-lock-gap.md, AC5/AC10):
    the memo's frontmatter was already at this verb's terminal state on disk
    (a prior invocation wrote it but crashed/died before its own follow-up
    commit landed), and THIS call committed those already-validated bytes
    rather than writing anything new. Distinguished from a genuine idempotent
    no-op (no "resumed" key) purely by the additive "resumed" key — an
    existing consumer reading only exit_code/applied/message is unaffected.
  {"exit_code": 1, "applied": False, "error":   str} — error; no write performed,
    EXCEPT the one case where a real frontmatter write already landed and only
    its follow-up commit (or the post-commit SHA read) failed (see Commit
    ownership below and ``_err``'s docstring) — that failure text says so
    explicitly.

Containment gate (SECURITY): the resolved memo path must lie under a git-tracked
``cross-repo/`` or ``state/`` subtree. Runs inside asyncio.to_thread (blocking git
rev-parse must not run on the event loop — DR-212 D3).

Commit ownership (DR-273): every verb that lands a real write (claim/action/
release/resolve — never the idempotent no-op path) commits that write in one
explicit-pathspec follow-up commit of ONLY the memo path, using the git root
``_containment_check`` already resolves. This is the terminal committer for the
mutation — no downstream sweep (e.g. ``fleet.archive_actioned_memos``) should be
the first thing to commit a memo.transition write. The consumer-agnostic
contract is unchanged: the caller's ``repo_root`` param stays unused; the memo's
own git root is what commits.

surface_advisory wiring (AC4/AC5, P080-C2): ``action`` and ``resolve`` (including
``correct_realization`` and the stranded-write resume path) attach an additive
``surface_advisory`` reply key exactly when the call's params carry ``realized_by``
(changed or re-supplied unchanged) and ``coordinator_core.ops.memo.surface_advisory
:: surface_advisory`` returns a verdict rather than ``None`` for the call's committed
frontmatter. Computed via ONE call to that single named entry point, AFTER
``_commit_terminal_write`` returns (so it sees the committed frontmatter and never
runs inside ``locked_rmw`` — lock-hold time is unchanged) — see ``_attach_surface_
advisory``. The op does not repeat the SHA-shape check; that check lives solely
inside ``surface_advisory``.

Negative-spec:
  - Does NOT subprocess / shell out directly. No node, no cli_path, no fallback escape
    hatch. Two git READS exist in this module's own call graph, both routed through
    named helpers, never a bare subprocess.run in this file: ``_memo_path_dirty``'s
    ``git status --porcelain`` (stranded-write resume detection) and, additive as of
    P080-C2, ``surface_advisory``'s single ``git log`` touched-path read (see
    "surface_advisory wiring" below). Neither is this module spawning git itself.
  - Every field write in this module (status, picked_up_at, picked_up_by, decision,
    decision_note, realized_by, actioned_note) uses numeric_quoting=True — node's
    serializeYamlScalar (schema.js) has no separate quoting flag; it unconditionally
    quotes all-digit values (SHA-as-int defence) on every field it serializes. Python
    mirrors that unconditionally across every write in this module, not just realized_by.
  - Does NOT derive worktree root. This op is show_top-scoped; memo path comes from
    params["memo"], containment-gated. main_worktree_root is NOT used here.
  - Does NOT perform base-required JSON-schema validation. Cross-field rules only
    (memos are foreign-authored; a sender's base-field slip must never block the receiver).
  - ``resolve`` does NOT acquire or write ``.git/coordinator-sessions/memo-claims/`` — that
    is a separate claim surface owned by archive_stamp.py's cs_action_memo, consumed only
    at the archival-gate path, entirely out of this module's scope. resolve's exclusion
    story is locked_rmw plus the picked_up_by collision check inside the SAME closure,
    nothing else — it is not a second claim mechanism.
  - ``resolve`` does NOT call ``_claim()`` or ``_action()``. Each is a complete, independent
    locked_rmw cycle; composing them as two function calls would acquire the lock twice with
    the memo observably in_progress on disk between calls — a crash in that window strands
    the memo in_progress under a dead session, which ``_claim`` then refuses forever. Instead
    resolve reuses the shared field-write/validation helpers those two verbs are themselves
    built from (``_claim_stamp_fields``, ``_apply_action_fields``, ``_disposition_matches``,
    ``_validate_action_disposition``) inside its own single mutate closure.
"""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from coordinator_core.git.repo_root import show_toplevel as _show_toplevel
from coordinator_core.frontmatter.primitives import (
    insert_fm_field,
    read_fm_field,
    read_fm_field_unquoted,
    rebuild,
    remove_fm_field,
    replace_fm_field,
    serialize_yaml_scalar,
    split_frontmatter,
    unquote_yaml_scalar,
)
from coordinator_core.frontmatter.schema_validate import (
    ErrorDict,
    format_validation_errors,
    validate_memo_cross_fields,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.memo.surface_advisory import surface_advisory
from coordinator_core.memo_corpus import memo_corpus_root
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.fleet._memo_summary import _SUMMARY_MAX_CHARS
from coordinator_core.session import scope as session_scope
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.wire_paths import rel_id


_CREATIONFLAGS = no_console_creationflags()


# Containment gate (SECURITY)
# UDS-only + MUTATING op; caller is a local same-user coordinator session with

_ALLOWED_SUBTREES = ("cross-repo", "state")
# an interactive prompt never blocks on the daemon's inherited stdin, and CREATE_NO_WINDOW
_GIT_TIMEOUT_SECS = 30


def _containment_check(memo: str, cwd: str | None = None) -> tuple[Path, Path]:
    """Raise if the resolved memo path is not under a git repo's cross-repo/ or state/ subtree.

    Returns ``(resolved_memo_path, git_root)`` on success. ``git_root`` is what callers
    pass as ``repo_root`` to ``locked_rmw`` so the ``git_common_dir`` lru_cache is keyed
    on the repo root (not per-memo-directory, which would cause N subprocess calls
    per distinct memo directory instead of 1 for the process lifetime). The resolved
    memo path is what callers use for every subsequent filesystem operation, instead
    of re-deriving their own (inconsistent) resolution of ``memo``.

    ``cwd`` (state/cross-repo/archive/2026-09-05-claude-klabauter-engine-memo-
    transition-ignores-cwd-and-its-errors-misdirect.md): a RELATIVE ``memo`` is
    anchored to ``cwd`` when supplied, before ``.resolve()`` runs — never left to
    fall through to this process's own cwd (which, served warm, is the engine's
    own root, not the caller's repo). An absolute ``memo`` ignores ``cwd`` entirely.

    Steps:
        1. Anchor a relative memo path to ``cwd`` (if given), then resolve to absolute.
        2. Find the git toplevel of its parent directory.
        3. Accept iff the resolved path is relative to <git-root>/cross-repo or <git-root>/state.

    Raises:
        ValueError — if the path fails any step of containment.

    Returns:
        tuple[Path, Path] — (resolved memo path, resolved git repository root).
    """
    raw = Path(memo)
    if cwd and not raw.is_absolute():
        raw = Path(cwd) / raw

    m = raw.resolve()

    toplevel = _show_toplevel(cwd=str(m.parent))
    if not toplevel:
        raise ValueError(
            f"memo.transition: --memo outside containment (must be under a git repo "
            f"cross-repo/ or state/ subtree): {memo!r} (resolved {m}) — could not "
            f"determine git root"
        )

    git_root = Path(toplevel).resolve()
    for subtree in _ALLOWED_SUBTREES:
        if m.is_relative_to(git_root / subtree):
            return m, git_root

    raise ValueError(
        f"memo.transition: --memo outside containment (must be under a git repo "
        f"cross-repo/ or state/ subtree): resolved {m} is not under "
        f"{git_root}/cross-repo or {git_root}/state"
    )


def _ok(applied: bool, message: str, commit_sha: str | None = None) -> dict:
    reply = {"exit_code": 0, "applied": applied, "message": message}
    if commit_sha is not None:
        reply["commit_sha"] = commit_sha
    return reply


def _err(message: str) -> dict:
    return {"exit_code": 1, "applied": False, "error": message}


def _attach_surface_advisory(reply: dict, params: dict | None, content: str, git_root: Path) -> dict:
    if not params or not params.get("realized_by"):
        return reply
    split = split_frontmatter(content)
    if split is None:
        return reply
    try:
        fm_dict = yaml.safe_load(split.fm_text) or {}
    except yaml.YAMLError:
        return reply
    if not isinstance(fm_dict, dict):
        return reply
    advisory = surface_advisory(fm_dict, git_root)
    if advisory is not None:
        reply["surface_advisory"] = advisory
    return reply


# WORKTREE to decide what to stage, which is exactly the "commit whatever

def _commit_terminal_write(
    memo_path: Path, git_root: Path, verb: str, content: str,
    *, attributed_session_id: str | None = None,
) -> tuple[str | None, str | None]:
    """Commit ``content`` (the exact bytes this call authored/validated) as the
    memo's on-disk mutation, in one single-path follow-up commit.

    Called AFTER ``locked_rmw`` has already written ``content`` to disk (the real-write
    path) OR, on the stranded-write resume branch (``_resume_probe_and_commit``), after
    the verb's own idempotency comparison has already validated ``content`` against the
    verb's expected terminal state — this never commits unvalidated worktree content.

    ``content`` is passed straight through to ``git_native.commit_authored_content`` —
    no worktree read on ``memo_path`` happens anywhere in this call (DR-272 § 3.3 bound 2).

    ``attributed_session_id`` (state/bug-backlog/2026-08-18-scoped-git-commit-
    stamps-a-foreign-session-id-8d21f0c4e7b9.yaml) — OPTIONAL, passed straight
    through to ``commit_authored_content``'s own ``attributed_session_id``.
    Only ``claim``/``resolve`` — the two verbs whose params carry a caller-
    supplied ``session_id`` at all (see ``_handler``'s own params table) —
    have anything better to offer than the blind env-var read
    ``commit_authored_content`` falls back to; every other verb (``action``,
    ``release``, ``close``, and the stranded-write resume path shared by all
    of them) leaves this ``None``, reproducing the prior resolution
    byte-for-byte.

    Returns a ``(commit_sha, error)`` pair — exactly one of the two is
    non-``None``. On success, ``commit_sha`` comes directly from
    ``commit_authored_content``'s own ``stdout`` (no separate SHA read-back
    needed) and ``error`` is ``None``. On failure, ``commit_sha`` is ``None``
    and ``error`` is a human-readable message. A failure here does NOT mean
    the frontmatter write itself failed — it already landed on disk; only the
    follow-up commit did not. Callers surface a non-``None`` error as an
    `_err()` (see its docstring) so an uncommitted terminal write is never
    silently reported as a clean success.
    """
    try:
        pathspec = rel_id(memo_path.resolve(), git_root)
    except ValueError:
        pathspec = str(memo_path.resolve())

    message = f"memo.transition {verb}: {pathspec}\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(message)
        msg_path = fh.name

    try:
        commit_result = git_native.commit_authored_content(
            pathspec, content, msg_path, git_root,
            attributed_session_id=attributed_session_id,
        )
    finally:
        try:
            Path(msg_path).unlink()
        except OSError:
            pass

    if not commit_result.ok:
        return None, (
            f"memo.transition {verb}: frontmatter write applied but the follow-up "
            f"commit failed: {commit_result.stderr}"
        )

    # GATED ON `attributed_session_id`, NOT ON THE ENV FALLBACK, and that is
    # is a mis-attribution; RELEASING A CLAIM under a wrong id would drop a
    # NEGATIVE SPEC (mirrors `ceremony/commit_v2.py ::
    if attributed_session_id:
        try:
            session_scope.release_committed_claims(
                attributed_session_id, [pathspec], cwd=str(git_root)
            )
        except Exception:  # noqa: BLE001 -- see NEGATIVE SPEC above
            pass

    return commit_result.stdout.strip(), None


def _memo_path_dirty(git_root: Path, relpath: str) -> bool:
    """True iff ``relpath`` carries any uncommitted, PREVIOUSLY-TRACKED change
    (staged and/or unstaged) per ``git status --porcelain``.

    Port of the same "stranded flip" detector shape as
    ``plan_status_transition._plan_path_dirty`` — deliberately excludes a bare
    ``??`` (untracked) entry: a memo this op never touched is not a resume
    candidate. A REAL stranded write always shows as a MODIFIED entry (staged
    and/or unstaged), never a fresh ``??``, because a tracked memo file is
    already committed before any verb here ever mutates it.
    """
    result = git_native.status_porcelain(git_root)
    if not result.ok:
        return False
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        code, entry = line[:2], line[3:]
        if code == "??":
            continue
        if entry == relpath or entry.endswith(f" -> {relpath}"):
            return True
    return False


def _resume_probe_and_commit(
    memo_path: Path, git_root: Path, verb: str, content: str, resumed_message: str,
    *, attributed_session_id: str | None = None, params: dict | None = None,
) -> dict | None:
    """After a verb's own idempotency comparison finds the on-disk frontmatter
    already at the verb's expected terminal state, detect and recover a
    stranded uncommitted write from a prior crashed invocation (Defect 2) —
    distinct from a genuine no-op where the memo was already at rest before
    this call ever ran.

    ``content`` is the SAME lock-held-read text (``old_text``/``new_text`` from
    THIS invocation's own ``locked_rmw`` call) the caller's idempotency
    comparison (status match, or ``_disposition_matches`` for action/resolve)
    already validated against the verb's expected terminal state — this
    function never performs a second, unvalidated re-read of ``memo_path``.

    Returns ``None`` when ``memo_path`` is CLEAN in git — the caller must
    return its own genuine no-op reply unchanged. Returns a resumed-commit
    reply (``resumed: True``, ``commit_sha`` present, ``applied: False`` — no
    NEW frontmatter write landed this call, only a follow-up commit of an
    already-written one) when the path is dirty: a prior invocation's write
    landed on disk but was never committed, and this call closes that gap by
    committing the SAME validated bytes via ``git_native.commit_authored_content``
    (through ``_commit_terminal_write``).

    Fail-loud on commit failure: returns ``_err()`` (AC10) — the write really
    is stranded uncommitted on disk in that case, and the caller must not
    report success.

    ``attributed_session_id`` — OPTIONAL, passed straight through to
    ``_commit_terminal_write``'s own parameter of the same name. See that
    function's docstring for which verbs' callers have anything to pass.

    ``params`` — OPTIONAL (AC4, P080-C2), passed straight through to
    ``_attach_surface_advisory`` so a resumed stranded write carries the same
    additive ``surface_advisory`` key a fresh write would (only ``action``/
    ``resolve`` callers, whose ``params`` can carry ``realized_by``, pass it).
    """
    try:
        relpath = rel_id(memo_path.resolve(), git_root)
    except ValueError:
        relpath = str(memo_path.resolve())

    if not _memo_path_dirty(git_root, relpath):
        return None

    commit_sha, commit_error = _commit_terminal_write(
        memo_path, git_root, verb, content,
        attributed_session_id=attributed_session_id,
    )
    if commit_error is not None:
        return _attach_surface_advisory(_err(commit_error), params, content, git_root)

    reply = _ok(False, resumed_message, commit_sha=commit_sha)
    reply["resumed"] = True
    return _attach_surface_advisory(reply, params, content, git_root)


# duplicate-key guard silently UNDER-COUNTS — failing open on exactly the

_STATUS_KEY_RE = re.compile(r'^status:(?=[ \t]|\r?$)', re.MULTILINE)


def _count_status_keys(fm_text: str) -> int:
    return len(_STATUS_KEY_RE.findall(fm_text))


def _demote_kind_enum_finding(errors: list[ErrorDict], fm_dict: dict) -> list[ErrorDict]:
    """Filter an off-enum ``kind`` finding out of ``errors``, warning to stderr instead.

    Receiver tolerance ahead of a gate that itself stays strict — the same shape as
    ``_normalize_oversize_summary`` (PM ruling 2026-07-22): the AUTHORING-side enum
    (``_memo_cf_kind_enum``, both write guards, DoE's direct-file-path import of
    ``validate_frontmatter_obj``) stays a hard gate; this only softens the RECEIVER's
    post-mutation check so an already-landed memo with an unenumerated ``kind`` isn't
    stranded at claim/action/release/resolve. Unlike ``_normalize_oversize_summary``,
    this does NOT rewrite the field on disk — the memo's declared ``kind:`` value is
    never the receiver's to correct, only its to tolerate.

    Mirrors the wording ``pickup_assemble`` already applies at read time
    (``build_judgment_point``'s ``kind {kind_raw!r} unrecognized — defaulted to 'ask'``),
    so the stamp path and the assembler agree instead of disagreeing about the same memo.

    Negative-spec: does NOT touch ``_memo_cf_kind_enum`` — that validator, and the
    enum tuples ``coordinator/bin/test_pickup_kind_enum_parity.py`` gates, stay untouched.
    Only demotes a ``field == 'kind'`` entry; any other cross-field error in ``errors``
    passes through unchanged.
    """
    kept: list[ErrorDict] = []
    for error in errors:
        if error.get('field') == 'kind':
            kind_raw = fm_dict.get('kind')
            print(
                f"memo.transition: WARNING — kind {kind_raw!r} unrecognized — defaulted to 'ask'",
                file=sys.stderr,
            )
            continue
        kept.append(error)
    return kept


def _validate_memo_fm(fm_text: str) -> list[ErrorDict]:
    """Validate post-mutation frontmatter text.

    Node ordering (memo-transition.js:175-187):
    1. Single-status-key postcondition (dup-key post-mutation check).
    2. Cross-field rules via validate_memo_cross_fields.

    Returns a (possibly empty) list of error dicts. Empty → valid.
    Catches YAML parse errors and surfaces them as a synthetic error entry.

    Negative-spec: this is the POST-MUTATION check, not the pre-mutation dup-key guard.
    The pre-mutation guard (≥2 keys → fail-loud) runs in each verb before mutations.

    Receiver tolerance for an off-enum ``kind`` (see ``_demote_kind_enum_finding``): a
    ``field == 'kind'`` enum finding is demoted to a stderr warning here ONLY — it never
    reaches this function's caller as a fail-loud error, so a memo already on disk with
    e.g. ``kind: defect`` remains claimable/actionable/releasable/resolvable instead of
    stranded at every lifecycle step. ``validate_memo_cross_fields`` itself is untouched.
    """
    key_count = _count_status_keys(fm_text)
    if key_count != 1:
        return [{
            'field': 'status',
            'error': f'post-mutation frontmatter has {key_count} status: key(s) (expected exactly 1) — fix the frontmatter manually',
            'hint': '',
        }]

    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error in frontmatter: {exc}", "hint": ""}]

    errors = validate_memo_cross_fields(fm_dict)
    return _demote_kind_enum_finding(errors, fm_dict)


_BLOCK_SCALAR_INDICATOR_RE = re.compile(r'^[|>][+\-0-9]*$')


def _normalize_oversize_summary(fm_text: str, memo: str) -> str:
    """Truncate an over-cap ``summary:`` field before the post-mutation validation gate.

    Reads ``summary:`` unquoted (comparison-safe length) and, when it exceeds
    ``_SUMMARY_MAX_CHARS``, rewrites it truncated to the cap and emits a warning to
    stderr naming the memo path, the original length, and that it was truncated to
    fit the cap.

    Truncation semantics match the sender-side paths exactly (memo_send.py:779-780,
    memo_compose.py:248-249): ``summary[:CAP - 1] + "…"``. Shares the same
    ``_SUMMARY_MAX_CHARS`` constant those paths import from ``ops/fleet/_memo_summary.py``
    (no layering violation — that module has zero deps beyond ``re``), so the cap cannot
    silently drift between the sender-side derivation and this receiver-side normalization.

    Idempotent: an at-or-under-cap ``summary:`` is left byte-identical, no warning emitted.
    An absent ``summary:`` is a no-op.

    Block-scalar dispatch (P2-1, cross-repo review 2026-07-22): a ``summary: |`` /
    ``summary: >`` value is a hand-authoring shape ``read_fm_field_unquoted`` cannot see
    past — it reads only the bare indicator token off the key's own line (length 1),
    so the plain length check above would silently no-op on an over-cap block scalar,
    stranding the memo at ``_memo_cf_summary_length_cap`` downstream. Detected and routed
    to ``_normalize_block_scalar_summary`` before the plain-scalar length check runs.

    Spec backlink: cross-repo/inbox/2026-07-22-claude-central-em-two-asks-installer-seed-and-memo-stamp-normalization.md § Ask 2

    Negative-spec: does NOT touch any field but ``summary:``. Does NOT relax
    ``schema_validate._memo_cf_summary_length_cap`` — that validator stays strict; this
    helper only runs ahead of it so a cosmetic over-cap value never reaches it.
    """
    raw = read_fm_field(fm_text, "summary")
    if raw is None:
        return fm_text

    if _BLOCK_SCALAR_INDICATOR_RE.match(raw):
        return _normalize_block_scalar_summary(fm_text, memo)

    summary = unquote_yaml_scalar(raw)
    if summary is None or len(summary) <= _SUMMARY_MAX_CHARS:
        return fm_text

    original_len = len(summary)
    truncated = summary[: _SUMMARY_MAX_CHARS - 1] + "…"
    print(
        f"memo.transition: WARNING — {memo}: summary: exceeded {_SUMMARY_MAX_CHARS} chars "
        f"(was {original_len}); truncated to fit the cap",
        file=sys.stderr,
    )
    return replace_fm_field(fm_text, "summary", truncated, numeric_quoting=True)


def _normalize_block_scalar_summary(fm_text: str, memo: str) -> str:
    try:
        parsed = yaml.safe_load(fm_text) or {}
    except Exception:  # noqa: BLE001
        return fm_text

    value = parsed.get("summary")
    if value is None:
        return fm_text
    value = str(value)
    if len(value) <= _SUMMARY_MAX_CHARS:
        return fm_text

    original_len = len(value)
    flattened = " ".join(value.split())
    truncated = flattened[: _SUMMARY_MAX_CHARS - 1] + "…"
    print(
        f"memo.transition: WARNING — {memo}: summary: (block scalar) exceeded "
        f"{_SUMMARY_MAX_CHARS} chars (was {original_len}); flattened and truncated to fit the cap",
        file=sys.stderr,
    )
    new_line = f"summary: {serialize_yaml_scalar(truncated, numeric_quoting=True)}\n"
    replaced = _replace_block_scalar_span(fm_text, "summary", new_line)
    return replaced if replaced is not None else fm_text


def _replace_block_scalar_span(fm_text: str, key: str, new_line: str) -> str | None:
    text = fm_text if fm_text.endswith('\n') else fm_text + '\n'
    # and the caller silently left an OVER-CAP summary on disk — a fail-open on
    pattern = re.compile(
        r'^' + re.escape(key) + r':(?=[ \t]|\r?$)[ \t]*[|>][+\-0-9]*[ \t]*\r?\n'
        r'(?:(?:[ \t]+.*)?\r?\n)*',
        re.MULTILINE,
    )
    m = pattern.search(text)
    if not m:
        return None
    if m.group(0).partition('\n')[0].endswith('\r') and not new_line.endswith('\r\n'):
        new_line = new_line[:-1] + '\r\n' if new_line.endswith('\n') else new_line + '\r\n'
    return text[: m.start()] + new_line + text[m.end():]


def _claim_stamp_fields(fm_text: str, session_id: str, at: str) -> str:
    if read_fm_field(fm_text, "status") is None:
        fm_text = insert_fm_field(fm_text, "status", "in_progress", "title", numeric_quoting=True)
    else:
        fm_text = replace_fm_field(fm_text, "status", "in_progress", numeric_quoting=True)

    if read_fm_field(fm_text, "picked_up_at") is None:
        fm_text = insert_fm_field(fm_text, "picked_up_at", at, "status", numeric_quoting=True)

    if read_fm_field(fm_text, "picked_up_by") is None:
        fm_text = insert_fm_field(fm_text, "picked_up_by", session_id, "picked_up_at", numeric_quoting=True)

    return fm_text


def _claim(memo: str, session_id: str, at: str, cwd: str | None = None) -> dict:
    if not session_id or not session_id.strip():
        return _err(
            "claim requires a non-empty --session-id (empty picked_up_by would corrupt the claim gate)"
        )
    if not at or not at.strip():
        return _err("claim requires --at <ISO timestamp>")

    # BaseException handler which would emit a -32603 INTERNAL_ERROR with no result.exit_code.
    try:
        memo_path, git_root = _containment_check(memo, cwd)
    except ValueError as exc:
        return _err(str(exc))
    except subprocess.TimeoutExpired:
        return _err(f"claim: containment check timed out for --memo {memo!r}")

    if not memo_path.is_file():
        return _err(f"memo not found: {memo_path}")

    _sid = session_id.strip()
    _at = at.strip()
    _noop_result: list[dict | None] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {memo}")

        pre_dup_count = _count_status_keys(split.fm_text)
        if pre_dup_count >= 2:
            raise MutateAbort(
                f"memo has {pre_dup_count} status: keys — hand-collapse the duplicate before retrying\n"
                f"  (edit the frontmatter to leave exactly one status: line, then retry)"
            )

        status = read_fm_field(split.fm_text, "status")
        picked_up_by = read_fm_field_unquoted(split.fm_text, "picked_up_by")

        if status == "in_progress" and picked_up_by == _sid:
            _noop_result[0] = _ok(False, f"{memo} already in_progress (picked_up_by {_sid}) — no-op")
            return old_text

        if status == "in_progress":
            raise MutateAbort(
                f"memo is already in_progress (held by {picked_up_by or '(empty)'}); "
                "release it first or use a different session"
            )

        if status not in ("open", "delivered", None):
            raise MutateAbort(
                f'unexpected current status "{status}" for claim — expected open or delivered'
            )

        fm_text = split.fm_text

        fm_text = _claim_stamp_fields(fm_text, _sid, _at)

        fm_text = _normalize_oversize_summary(fm_text, memo)

        errors = _validate_memo_fm(fm_text)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"memo cross-field validation failed: {details}")

        return rebuild(split, fm_text)

    try:
        new_text = locked_rmw(memo_path, _mutate, repo_root=git_root)
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "claim: unknown mutation error")
    except LockTimeout as exc:
        return _err(str(exc))
    except FileNotFoundError:
        # INTERNAL_ERROR with no exit_code field (AC6/AC10 contract violation).
        return _err(f"memo not found: {memo_path}")

    if _noop_result[0] is not None:
        resumed_reply = _resume_probe_and_commit(
            memo_path, git_root, "claim", new_text,
            f"{memo} already in_progress (picked_up_by {_sid}) — resumed a stranded "
            "uncommitted write and committed it",
            attributed_session_id=_sid,
        )
        if resumed_reply is not None:
            return resumed_reply
        return _noop_result[0]

    written_split = split_frontmatter(new_text)
    if written_split is None or _count_status_keys(written_split.fm_text) != 1:
        return _err(
            f"INTERNAL ERROR — post-write status key count ≠ 1. Inspect {memo} immediately."
        )

    commit_sha, commit_error = _commit_terminal_write(
        memo_path, git_root, "claim", new_text, attributed_session_id=_sid,
    )
    if commit_error is not None:
        return _err(commit_error)

    return _ok(True, f"claimed {memo} (picked_up_by {_sid})", commit_sha=commit_sha)


def _validate_action_disposition(params: dict, verb: str = "action") -> dict | None:
    decision = params.get("decision")
    actioned_note = params.get("actioned_note")
    realized_by = params.get("realized_by")
    decision_note = params.get("decision_note")
    superseded_by = params.get("superseded_by")

    if decision_note and ("\n" in decision_note or "\r" in decision_note):
        return _err(
            f"{verb}: --decision-note must be single-line (no embedded \\n or \\r) — "
            "serialize_yaml_scalar does not support multi-line scalar values"
        )
    if actioned_note and ("\n" in actioned_note or "\r" in actioned_note):
        return _err(
            f"{verb}: --actioned-note must be single-line (no embedded \\n or \\r) — "
            "serialize_yaml_scalar does not support multi-line scalar values"
        )

    # append-only correction of an ALREADY-actioned memo's disposition
    # realized_by/decision_note under an UNCHANGED decision; this path
    supersede_note = params.get("supersede_note")
    supersede_realized_by = params.get("supersede_realized_by")

    if supersede_note and ("\n" in supersede_note or "\r" in supersede_note):
        return _err(
            f"{verb}: --supersede-note must be single-line (no embedded \\n or \\r) — "
            "serialize_yaml_scalar does not support multi-line scalar values"
        )

    if supersede_note or supersede_realized_by:
        if decision or actioned_note or superseded_by:
            return _err(
                f"{verb}: --supersede-note/--supersede-realized-by are mutually "
                "exclusive with --decision/--actioned-note/--superseded-by — "
                "supersede corrects an EXISTING disposition, it does not set a new one"
            )
        if not supersede_note:
            return _err(f"{verb}: --supersede-realized-by requires --supersede-note")
        if not supersede_realized_by:
            return _err(
                f"{verb}: --supersede-note requires --supersede-realized-by "
                "(a pointer to what realized the reversal: a commit SHA, a memo, or a baton)"
            )
        return None

    if superseded_by:
        if decision or actioned_note:
            return _err(
                f"{verb}: --superseded-by and --decision/--actioned-note are "
                "mutually exclusive"
            )
        return None

    if decision and actioned_note:
        return _err(f"{verb}: --decision and --actioned-note are mutually exclusive")
    if not decision and not actioned_note:
        return _err(
            f"{verb} requires either --decision <accepted|partial|declined> "
            "[--decision-note <text>] [--realized-by <ptr>] or --actioned-note <text>"
        )

    if decision:
        valid_decisions = ("accepted", "partial", "declined")
        if decision not in valid_decisions:
            return _err(
                f"{verb}: --decision must be one of: {', '.join(valid_decisions)} "
                f'(got "{decision}")'
            )
        if decision in ("accepted", "partial") and not realized_by:
            return _err(f"{verb}: --realized-by is required when --decision is {decision}")

    return None


def _disposition_matches(fm_text: str, params: dict) -> bool:
    decision = params.get("decision")
    decision_note = params.get("decision_note")
    realized_by = params.get("realized_by")
    actioned_note = params.get("actioned_note")
    superseded_by = params.get("superseded_by")

    if superseded_by:
        cur_superseded_by = unquote_yaml_scalar(read_fm_field(fm_text, "superseded_by"))
        return cur_superseded_by == superseded_by

    if decision:
        cur_decision = read_fm_field_unquoted(fm_text, "decision")
        cur_decision_note = read_fm_field(fm_text, "decision_note")
        cur_realized_by = read_fm_field(fm_text, "realized_by")
        return (
            cur_decision == decision
            and (unquote_yaml_scalar(cur_decision_note) or None) == (decision_note or None)
            and (unquote_yaml_scalar(cur_realized_by) or None) == (realized_by or None)
        )

    cur_actioned_note = read_fm_field(fm_text, "actioned_note")
    return (unquote_yaml_scalar(cur_actioned_note) or None) == (actioned_note or None)


def _apply_realization_correction(fm_text: str, params: dict) -> str:
    """Apply a ``--correct-realization`` correction: move ``realized_by``/``decision_note``
    ONLY, preserving the superseded ``realized_by`` value inside ``decision_note``.

    Preconditions (caller's responsibility — see ``_handle_already_actioned``):
    the memo is already ``actioned``, decision-shape (not ``actioned_note``-shape),
    with the SAME ``decision:`` value as ``params`` requests, and a DIFFERENT
    disposition overall (else the idempotent no-op branch would already have fired).

    Audit trail: whatever ``decision_note`` this write ends up carrying (the
    caller-supplied one, or the pre-existing one if the caller didn't supply a
    new one) has a ``[correction ...]`` clause appended — the superseded
    ``realized_by`` value is never silently dropped. No new frontmatter key is
    introduced.

    Clause text (AC11, P080-C2): conditional on whether ``realized_by`` actually
    moved. When ``params["realized_by"]`` equals the current on-disk value (a
    re-supplied-unchanged correction, e.g. C6's case — the SHA is right, only
    ``decision_note`` needed the record straightened out), the unconditional
    "realized_by superseded — was <sha>" wording would be an untrue durable
    claim while the field still carries that exact SHA. The clause is instead
    ``[correction <ts>: decision_note corrected]``. The moved-SHA path keeps
    today's ``realized_by superseded — was <sha>`` clause byte-for-byte.
    """
    cur_realized_by = unquote_yaml_scalar(read_fm_field(fm_text, "realized_by"))
    new_realized_by = params.get("realized_by")
    base_note = params.get("decision_note")
    if base_note is None:
        base_note = unquote_yaml_scalar(read_fm_field(fm_text, "decision_note")) or ""

    ts = datetime.now(timezone.utc).isoformat()
    if new_realized_by and new_realized_by == cur_realized_by:
        clause = f"[correction {ts}: decision_note corrected]"
    else:
        clause = (
            f"[correction {ts}: realized_by superseded — was {cur_realized_by or '(none)'}]"
        )
    combined_note = f"{base_note} {clause}".strip() if base_note else clause

    if read_fm_field(fm_text, "decision_note") is None:
        fm_text = insert_fm_field(fm_text, "decision_note", combined_note, "decision", numeric_quoting=True)
    else:
        fm_text = replace_fm_field(fm_text, "decision_note", combined_note, numeric_quoting=True)

    if new_realized_by:
        if read_fm_field(fm_text, "realized_by") is None:
            fm_text = insert_fm_field(
                fm_text, "realized_by", new_realized_by, "decision_note", numeric_quoting=True
            )
        else:
            fm_text = replace_fm_field(fm_text, "realized_by", new_realized_by, numeric_quoting=True)

    return fm_text


def _apply_note_correction(fm_text: str, params: dict) -> str:
    cur_note = unquote_yaml_scalar(read_fm_field(fm_text, "actioned_note")) or "(none)"
    new_note = params.get("actioned_note") or ""

    ts = datetime.now(timezone.utc).isoformat()
    clause = f"[correction {ts}: actioned_note superseded — was {cur_note}]"
    combined_note = f"{new_note} {clause}".strip() if new_note else clause

    if read_fm_field(fm_text, "actioned_note") is None:
        fm_text = insert_fm_field(fm_text, "actioned_note", combined_note, "status", numeric_quoting=True)
    else:
        fm_text = replace_fm_field(fm_text, "actioned_note", combined_note, numeric_quoting=True)

    return fm_text


# place a REVERSED verdict on an already-actioned memo is recorded. Distinct
# from --correct-realization above: that path corrects EVIDENCE
# (realized_by/decision_note) under an UNCHANGED decision, folded into
# read as a memo actioned once cleanly; the ORIGINAL disposition fields stay

def _apply_supersede_fields(fm_text: str, note: str, realized_by: str, at: str) -> str:
    anchor = "status"
    for field, value in (
        ("disposition_superseded", "true"),
        ("superseding_note", note),
        ("superseding_realized_by", realized_by),
        ("superseded_at", at),
    ):
        if read_fm_field(fm_text, field) is None:
            fm_text = insert_fm_field(fm_text, field, value, anchor, numeric_quoting=True)
        else:
            fm_text = replace_fm_field(fm_text, field, value, numeric_quoting=True)
        anchor = field
    return fm_text


def _handle_supersede(fm_text: str, params: dict) -> str | None:
    """Apply (or idempotently no-op) a supersede-disposition request against
    an already-actioned/superseded memo.

    Returns:
        None — idempotent no-op (the on-disk superseding_* fields already
            match ``params`` exactly); caller returns ``old_text`` unchanged.
        str  — the corrected ``fm_text`` with superseding_* fields written.

    Raises:
        MutateAbort — the memo's disposition was already superseded with a
        DIFFERENT note/realized_by/at (append-only: a second reversal is not
        this mechanism's job — hand-collapse if genuinely needed).
    """
    note = params["supersede_note"]
    realized_by = params["supersede_realized_by"]
    at = params.get("supersede_at") or datetime.now(timezone.utc).isoformat()

    already = unquote_yaml_scalar(read_fm_field(fm_text, "disposition_superseded"))
    if already and str(already).strip().lower() == "true":
        cur_note = unquote_yaml_scalar(read_fm_field(fm_text, "superseding_note"))
        cur_realized_by = unquote_yaml_scalar(read_fm_field(fm_text, "superseding_realized_by"))
        cur_at = unquote_yaml_scalar(read_fm_field(fm_text, "superseded_at"))
        if cur_note == note and cur_realized_by == realized_by and cur_at == at:
            return None
        raise MutateAbort(
            "memo disposition is already superseded with a different supersede record — "
            "cannot supersede twice (append-only: hand-collapse if a second reversal is "
            "genuinely needed)"
        )

    return _apply_supersede_fields(fm_text, note, realized_by, at)


def _handle_already_actioned(fm_text: str, params: dict, verb: str) -> str | None:
    if _disposition_matches(fm_text, params):
        return None

    if not params.get("correct_realization"):
        raise MutateAbort("memo is already actioned with a different disposition — cannot re-action")

    new_decision = params.get("decision")
    cur_decision = read_fm_field_unquoted(fm_text, "decision")

    if not new_decision:
        if cur_decision is not None or not params.get("actioned_note"):
            raise MutateAbort(
                f"{verb}: --correct-realization requires --decision matching the on-disk "
                "decision value on a decision-shape memo, or --actioned-note on an "
                "actioned_note-shape memo — the disposition shape cannot change"
            )
        return _apply_note_correction(fm_text, params)

    if cur_decision != new_decision:
        raise MutateAbort("memo is already actioned with a different disposition — cannot re-action")

    return _apply_realization_correction(fm_text, params)


def _closure_stamp(params: dict) -> str:
    at = params.get("at")
    if isinstance(at, str) and at.strip():
        return at.strip()
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _apply_action_fields(fm_text: str, params: dict) -> str:
    decision = params.get("decision")
    actioned_note = params.get("actioned_note")
    decision_note = params.get("decision_note")
    realized_by = params.get("realized_by")
    distill_fate = params.get("distill_fate")
    in_repo_capture = params.get("in_repo_capture")
    superseded_by = params.get("superseded_by")

    if superseded_by:
        fm_text = replace_fm_field(fm_text, "status", "superseded", numeric_quoting=True)
        if read_fm_field(fm_text, "superseded_by") is None:
            fm_text = insert_fm_field(
                fm_text, "superseded_by", superseded_by, "status", numeric_quoting=True
            )
        else:
            fm_text = replace_fm_field(fm_text, "superseded_by", superseded_by, numeric_quoting=True)
        return fm_text

    fm_text = replace_fm_field(fm_text, "status", "actioned", numeric_quoting=True)

    if read_fm_field(fm_text, "actioned_at") is None:
        fm_text = insert_fm_field(
            fm_text, "actioned_at", _closure_stamp(params), "status", numeric_quoting=True
        )

    if decision:
        if read_fm_field(fm_text, "decision") is None:
            fm_text = insert_fm_field(fm_text, "decision", decision, "status", numeric_quoting=True)
        else:
            fm_text = replace_fm_field(fm_text, "decision", decision, numeric_quoting=True)

        if decision_note:
            if read_fm_field(fm_text, "decision_note") is None:
                fm_text = insert_fm_field(
                    fm_text, "decision_note", decision_note, "decision", numeric_quoting=True
                )
            else:
                fm_text = replace_fm_field(
                    fm_text, "decision_note", decision_note, numeric_quoting=True
                )

        if realized_by:
            anchor = "decision_note" if decision_note else "decision"
            if read_fm_field(fm_text, "realized_by") is None:
                fm_text = insert_fm_field(
                    fm_text, "realized_by", realized_by, anchor, numeric_quoting=True
                )
            else:
                fm_text = replace_fm_field(
                    fm_text, "realized_by", realized_by, numeric_quoting=True
                )
    else:
        if read_fm_field(fm_text, "actioned_note") is None:
            fm_text = insert_fm_field(
                fm_text, "actioned_note", actioned_note, "status", numeric_quoting=True
            )
        else:
            fm_text = replace_fm_field(fm_text, "actioned_note", actioned_note, numeric_quoting=True)

    if distill_fate:
        if decision:
            anchor = "realized_by" if realized_by else ("decision_note" if decision_note else "decision")
        else:
            anchor = "actioned_note"
        if read_fm_field(fm_text, "distill_fate") is None:
            fm_text = insert_fm_field(fm_text, "distill_fate", distill_fate, anchor, numeric_quoting=True)
        else:
            fm_text = replace_fm_field(fm_text, "distill_fate", distill_fate, numeric_quoting=True)
    if in_repo_capture:
        if distill_fate:
            anchor = "distill_fate"
        elif decision:
            anchor = "realized_by" if realized_by else ("decision_note" if decision_note else "decision")
        else:
            anchor = "actioned_note"
        if read_fm_field(fm_text, "in_repo_capture") is None:
            fm_text = insert_fm_field(fm_text, "in_repo_capture", in_repo_capture, anchor, numeric_quoting=True)
        else:
            fm_text = replace_fm_field(fm_text, "in_repo_capture", in_repo_capture, numeric_quoting=True)

    return fm_text


def _normalize_superseded_by(value: str) -> str:
    return Path(value.strip()).name


def _validate_superseded_by_exists(git_root: Path, superseded_by: str) -> dict | None:
    corpus_root = Path(memo_corpus_root(str(git_root)))
    inbox_dir = corpus_root / "inbox"
    archive_dir = corpus_root / "archive"

    if (inbox_dir / superseded_by).is_file():
        return None
    if archive_dir.is_dir():
        for candidate in archive_dir.rglob(superseded_by):
            if candidate.is_file():
                return None

    return _err(
        f"action: superseded_by={superseded_by!r} does not match any memo in "
        f"this repo's own {inbox_dir} or {archive_dir} (searched recursively) — "
        f"superseded_by must name a memo present in this repo's memo-corpus "
        f"inbox/ or archive/. Check for a typo."
    )


def _action(memo: str, params: dict, cwd: str | None = None) -> dict:
    distill_fate = params.get("distill_fate")
    in_repo_capture = params.get("in_repo_capture")

    disposition_error = _validate_action_disposition(params, verb="action")
    if disposition_error is not None:
        return disposition_error

    try:
        memo_path, git_root = _containment_check(memo, cwd)
    except ValueError as exc:
        return _err(str(exc))
    except subprocess.TimeoutExpired:
        return _err(f"action: containment check timed out for --memo {memo!r}")

    if not memo_path.is_file():
        return _err(f"memo not found: {memo_path}")

    superseded_by = params.get("superseded_by")
    if superseded_by:
        normalized_superseded_by = _normalize_superseded_by(superseded_by)
        pointer_error = _validate_superseded_by_exists(git_root, normalized_superseded_by)
        if pointer_error is not None:
            return pointer_error
        params = {**params, "superseded_by": normalized_superseded_by}

    _noop_result: list[dict | None] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {memo}")

        pre_dup_count = _count_status_keys(split.fm_text)
        if pre_dup_count >= 2:
            raise MutateAbort(
                f"memo has {pre_dup_count} status: keys — hand-collapse the duplicate before retrying\n"
                f"  (edit the frontmatter to leave exactly one status: line, then retry)"
            )

        status = read_fm_field(split.fm_text, "status")

        # is idempotent; a DIFFERENT pointer fails loud via the same
        if params.get("supersede_note"):
            if status not in ("actioned", "superseded"):
                raise MutateAbort(
                    "action: --supersede-note requires the memo to already be actioned "
                    "or superseded — there is no disposition yet to supersede"
                )
            corrected = _handle_supersede(split.fm_text, params)
            if corrected is None:
                _noop_result[0] = _ok(False, f"{memo} disposition already superseded — no-op")
                return old_text
            fm_text = corrected
        elif status in ("actioned", "superseded"):
            corrected = _handle_already_actioned(split.fm_text, params, "action")
            if corrected is None:
                _noop_result[0] = _ok(False, f"{memo} already {status} at target disposition — no-op")
                return old_text
            fm_text = corrected
        else:
            if status != "in_progress":
                raise MutateAbort(
                    f'unexpected current status "{status or "(missing)"}" for action — expected in_progress'
                )

            # PRESERVE picked_up_by and picked_up_at — claim-of-record for the archived memo.
            fm_text = _apply_action_fields(split.fm_text, params)

        fm_text = _normalize_oversize_summary(fm_text, memo)

        errors = _validate_memo_fm(fm_text)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"memo cross-field validation failed: {details}")

        return rebuild(split, fm_text)

    try:
        new_text = locked_rmw(memo_path, _mutate, repo_root=git_root)
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "action: unknown mutation error")
    except LockTimeout as exc:
        return _err(str(exc))
    except FileNotFoundError:
        # acquire; locked_rmw raises FileNotFoundError → would escape as -32603 INTERNAL_ERROR.
        return _err(f"memo not found: {memo_path}")

    if _noop_result[0] is not None:
        resumed_reply = _resume_probe_and_commit(
            memo_path, git_root, "action", new_text,
            f"{memo} already actioned at target disposition — resumed a stranded "
            "uncommitted write and committed it",
            params=params,
        )
        if resumed_reply is not None:
            return resumed_reply
        return _noop_result[0]

    written_split = split_frontmatter(new_text)
    if written_split is None or _count_status_keys(written_split.fm_text) != 1:
        return _err(
            f"INTERNAL ERROR — post-write status key count ≠ 1. Inspect {memo} immediately."
        )
    if distill_fate:
        # `\r?$` for the same reason as _STATUS_KEY_RE: a CRLF present-but-empty
        df_count = len(re.findall(r'^distill_fate:(?=[ \t]|\r?$)', written_split.fm_text, re.MULTILINE))
        if df_count != 1:
            return _err(
                f"INTERNAL ERROR — post-write distill_fate: key count {df_count} (expected 1). "
                f"Inspect {memo} immediately."
            )
    if in_repo_capture:
        irc_count = len(re.findall(r'^in_repo_capture:(?=[ \t]|\r?$)', written_split.fm_text, re.MULTILINE))
        if irc_count != 1:
            return _err(
                f"INTERNAL ERROR — post-write in_repo_capture: key count {irc_count} (expected 1). "
                f"Inspect {memo} immediately."
            )
    if params.get("superseded_by"):
        sb_count = len(re.findall(r'^superseded_by:(?=[ \t]|\r?$)', written_split.fm_text, re.MULTILINE))
        if sb_count != 1:
            return _err(
                f"INTERNAL ERROR — post-write superseded_by: key count {sb_count} (expected 1). "
                f"Inspect {memo} immediately."
            )
    if params.get("supersede_note"):
        for field in (
            "disposition_superseded", "superseding_note",
            "superseding_realized_by", "superseded_at",
        ):
            count = len(re.findall(rf'^{field}:(?=[ \t]|\r?$)', written_split.fm_text, re.MULTILINE))
            if count != 1:
                return _err(
                    f"INTERNAL ERROR — post-write {field}: key count {count} (expected 1). "
                    f"Inspect {memo} immediately."
                )

    commit_sha, commit_error = _commit_terminal_write(memo_path, git_root, "action", new_text)
    if commit_error is not None:
        return _attach_surface_advisory(_err(commit_error), params, new_text, git_root)

    return _attach_surface_advisory(
        _ok(True, f"actioned {memo}", commit_sha=commit_sha), params, new_text, git_root
    )


def _release(memo: str, cwd: str | None = None) -> dict:
    try:
        memo_path, git_root = _containment_check(memo, cwd)
    except ValueError as exc:
        return _err(str(exc))
    except subprocess.TimeoutExpired:
        return _err(f"release: containment check timed out for --memo {memo!r}")

    if not memo_path.is_file():
        return _err(f"memo not found: {memo_path}")

    _noop_result: list[dict | None] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {memo}")

        pre_dup_count = _count_status_keys(split.fm_text)
        if pre_dup_count >= 2:
            raise MutateAbort(
                f"memo has {pre_dup_count} status: keys — hand-collapse the duplicate before retrying\n"
                f"  (edit the frontmatter to leave exactly one status: line, then retry)"
            )

        status = read_fm_field(split.fm_text, "status")

        if status == "open":
            _noop_result[0] = _ok(False, f"{memo} already open — no-op")
            return old_text

        if status != "in_progress":
            raise MutateAbort(
                f'unexpected current status "{status or "(missing)"}" for release — expected in_progress'
            )

        fm_text = split.fm_text

        fm_text = replace_fm_field(fm_text, "status", "open", numeric_quoting=True)

        fm_text = remove_fm_field(fm_text, "picked_up_by")
        fm_text = remove_fm_field(fm_text, "picked_up_at")

        fm_text = _normalize_oversize_summary(fm_text, memo)

        errors = _validate_memo_fm(fm_text)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"memo cross-field validation failed: {details}")

        return rebuild(split, fm_text)

    try:
        new_text = locked_rmw(memo_path, _mutate, repo_root=git_root)
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "release: unknown mutation error")
    except LockTimeout as exc:
        return _err(str(exc))
    except FileNotFoundError:
        # acquire; locked_rmw raises FileNotFoundError → would escape as -32603 INTERNAL_ERROR.
        return _err(f"memo not found: {memo_path}")

    if _noop_result[0] is not None:
        resumed_reply = _resume_probe_and_commit(
            memo_path, git_root, "release", new_text,
            f"{memo} already open — resumed a stranded uncommitted write and committed it",
        )
        if resumed_reply is not None:
            return resumed_reply
        return _noop_result[0]

    written_split = split_frontmatter(new_text)
    if written_split is None or _count_status_keys(written_split.fm_text) != 1:
        return _err(
            f"INTERNAL ERROR — post-write status key count ≠ 1. Inspect {memo} immediately."
        )

    commit_sha, commit_error = _commit_terminal_write(memo_path, git_root, "release", new_text)
    if commit_error is not None:
        return _err(commit_error)

    return _ok(True, f"released {memo} (status reset to open, claim cleared)", commit_sha=commit_sha)


def _lift(memo: str, cwd: str | None = None) -> dict:
    try:
        memo_path, git_root = _containment_check(memo, cwd)
    except ValueError as exc:
        return _err(str(exc))
    except subprocess.TimeoutExpired:
        return _err(f"lift: containment check timed out for --memo {memo!r}")

    if not memo_path.is_file():
        return _err(f"memo not found: {memo_path}")

    _noop_result: list[dict | None] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {memo}")

        pre_dup_count = _count_status_keys(split.fm_text)
        if pre_dup_count >= 2:
            raise MutateAbort(
                f"memo has {pre_dup_count} status: keys — hand-collapse the duplicate before retrying\n"
                f"  (edit the frontmatter to leave exactly one status: line, then retry)"
            )

        status = read_fm_field(split.fm_text, "status")

        if status == "open":
            _noop_result[0] = _ok(False, f"{memo} already open — no-op")
            return old_text

        if status != "draft":
            raise MutateAbort(
                f'unexpected current status "{status or "(missing)"}" for lift — expected draft'
            )

        fm_text = replace_fm_field(split.fm_text, "status", "open", numeric_quoting=True)

        fm_text = _normalize_oversize_summary(fm_text, memo)

        errors = _validate_memo_fm(fm_text)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"memo cross-field validation failed: {details}")

        return rebuild(split, fm_text)

    try:
        new_text = locked_rmw(memo_path, _mutate, repo_root=git_root)
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "lift: unknown mutation error")
    except LockTimeout as exc:
        return _err(str(exc))
    except FileNotFoundError:
        return _err(f"memo not found: {memo_path}")

    if _noop_result[0] is not None:
        resumed_reply = _resume_probe_and_commit(
            memo_path, git_root, "lift", new_text,
            f"{memo} already open — resumed a stranded uncommitted write and committed it",
        )
        if resumed_reply is not None:
            return resumed_reply
        return _noop_result[0]

    written_split = split_frontmatter(new_text)
    if written_split is None or _count_status_keys(written_split.fm_text) != 1:
        return _err(
            f"INTERNAL ERROR — post-write status key count ≠ 1. Inspect {memo} immediately."
        )

    commit_sha, commit_error = _commit_terminal_write(memo_path, git_root, "lift", new_text)
    if commit_error is not None:
        return _err(commit_error)

    return _ok(True, f"lifted {memo} (status set to open)", commit_sha=commit_sha)


def _close(memo: str, at: str, cwd: str | None = None) -> dict:
    try:
        memo_path, git_root = _containment_check(memo, cwd)
    except ValueError as exc:
        return _err(str(exc))
    except subprocess.TimeoutExpired:
        return _err(f"close: containment check timed out for --memo {memo!r}")

    if not at or not at.strip():
        return _err("close requires --at <ISO timestamp>")
    _at = at.strip()

    if not memo_path.is_file():
        return _err(f"memo not found: {memo_path}")

    _noop_result: list[dict | None] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {memo}")

        pre_dup_count = _count_status_keys(split.fm_text)
        if pre_dup_count >= 2:
            raise MutateAbort(
                f"memo has {pre_dup_count} status: keys — hand-collapse the duplicate before retrying\n"
                f"  (edit the frontmatter to leave exactly one status: line, then retry)"
            )

        status = read_fm_field(split.fm_text, "status")

        if status == "closed":
            _noop_result[0] = _ok(False, f"{memo} already closed — no-op")
            return old_text

        if status != "actioned":
            raise MutateAbort(
                f'unexpected current status "{status or "(missing)"}" for close — expected actioned'
            )

        decision = read_fm_field(split.fm_text, "decision")
        if not decision or not str(decision).strip():
            raise MutateAbort(
                f"{memo} has no decision on record — close requires an actioned memo "
                "with a decision (accepted/partial/declined), not an actioned_note-only memo"
            )

        fm_text = split.fm_text

        fm_text = replace_fm_field(fm_text, "status", "closed", numeric_quoting=True)

        if read_fm_field(fm_text, "closed_at") is None:
            fm_text = insert_fm_field(fm_text, "closed_at", _at, "status", numeric_quoting=True)
        else:
            fm_text = replace_fm_field(fm_text, "closed_at", _at, numeric_quoting=True)

        action_taken_at = read_fm_field(fm_text, "action_taken_at")
        if not action_taken_at or not str(action_taken_at).strip():
            if action_taken_at is None:
                fm_text = insert_fm_field(
                    fm_text, "action_taken_at", _at, "closed_at", numeric_quoting=True
                )
            else:
                fm_text = replace_fm_field(fm_text, "action_taken_at", _at, numeric_quoting=True)

        fm_text = _normalize_oversize_summary(fm_text, memo)

        errors = _validate_memo_fm(fm_text)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"memo cross-field validation failed: {details}")

        return rebuild(split, fm_text)

    try:
        new_text = locked_rmw(memo_path, _mutate, repo_root=git_root)
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "close: unknown mutation error")
    except LockTimeout as exc:
        return _err(str(exc))
    except FileNotFoundError:
        return _err(f"memo not found: {memo_path}")

    if _noop_result[0] is not None:
        resumed_reply = _resume_probe_and_commit(
            memo_path, git_root, "close", new_text,
            f"{memo} already closed — resumed a stranded uncommitted write and committed it",
        )
        if resumed_reply is not None:
            return resumed_reply
        return _noop_result[0]

    written_split = split_frontmatter(new_text)
    if written_split is None or _count_status_keys(written_split.fm_text) != 1:
        return _err(
            f"INTERNAL ERROR — post-write status key count ≠ 1. Inspect {memo} immediately."
        )

    commit_sha, commit_error = _commit_terminal_write(memo_path, git_root, "close", new_text)
    if commit_error is not None:
        return _err(commit_error)

    return _ok(True, f"closed {memo}", commit_sha=commit_sha)


def _resolve(memo: str, session_id: str, at: str, params: dict, cwd: str | None = None) -> dict:
    """Apply resolve transition: open → actioned, in ONE ``locked_rmw`` closure.

    Collapses the two-call claim-then-action ceremony
    (state/lessons/2026-07-24-memo-terminal-flip-is-a-two-step-transit-147cc531ae68.yaml
    documents ``archive-stamp-cli claim-memo-stamp`` then ``action-memo`` as the existing
    established convention) into ONE atomic mutate. No intermediate ``in_progress`` state
    is ever visible on disk between the memo's ``open`` and ``actioned`` states — the
    single mutate closure below:
      1. Runs ``_claim``'s collision/idempotency checks (in_progress held by another
         session → ``MutateAbort``; already actioned at the target disposition → no-op)
         against THIS lock acquisition, not a second one.
      2. Stamps picked_up_at/picked_up_by via ``_claim_stamp_fields`` (shared with
         ``_claim``).
      3. Applies ``_action``'s disposition field writes via ``_apply_action_fields``
         (shared with ``_action``) — its "requires in_progress" precondition is already
         satisfied by steps 1-2 within this SAME closure, never a second read.
      4. Runs the same ``_validate_memo_fm`` gate ``_claim``/``_action`` already run,
         once, before the single write.

    Does NOT call ``_claim()`` or ``_action()`` — see the module-level negative-spec:
    each is a complete ``locked_rmw`` cycle on its own, and composing them as two function
    calls would acquire the lock twice with the memo observably ``in_progress`` on disk
    between calls (a crash in that window strands the memo ``in_progress`` under a dead
    session, which ``_claim`` then refuses forever — the exact machinery-in-the-way state
    that produced the 2026-07-26 hand-edit this op exists to obsolete).

    Disposition is REQUIRED: validated via ``_validate_action_disposition`` (shared with
    ``_action``) BEFORE the lock is acquired — a resolve call with no disposition fails
    loud with no I/O at all.

    Live-claim refusal: if another session holds the memo (status in_progress with a
    DIFFERENT picked_up_by), resolve refuses rather than stealing
    (state/lessons/0000-00-00-before-actioning-an-inbound-cross-repo-m).

    Already-actioned re-action: shares ``_handle_already_actioned`` with ``_action`` —
    no-op on an exact disposition match, a narrow ``correct_realization`` evidence
    correction when ``decision:`` is unchanged, or fail-loud otherwise (verdict
    changes always fail loud, flag or no flag).

    Negative-spec: does NOT acquire or write ``.git/coordinator-sessions/memo-claims/`` —
    see the module-level negative-spec. resolve's exclusion story is locked_rmw plus the
    picked_up_by collision check inside this closure, nothing else.
    """
    disposition_error = _validate_action_disposition(params, verb="resolve")
    if disposition_error is not None:
        return disposition_error

    if not session_id or not session_id.strip():
        return _err(
            "resolve requires a non-empty --session-id (empty picked_up_by would corrupt the claim gate)"
        )
    if not at or not at.strip():
        return _err("resolve requires --at <ISO timestamp>")

    try:
        memo_path, git_root = _containment_check(memo, cwd)
    except ValueError as exc:
        return _err(str(exc))
    except subprocess.TimeoutExpired:
        return _err(f"resolve: containment check timed out for --memo {memo!r}")

    if not memo_path.is_file():
        return _err(f"memo not found: {memo_path}")

    _sid = session_id.strip()
    _at = at.strip()
    _noop_result: list[dict | None] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {memo}")

        pre_dup_count = _count_status_keys(split.fm_text)
        if pre_dup_count >= 2:
            raise MutateAbort(
                f"memo has {pre_dup_count} status: keys — hand-collapse the duplicate before retrying\n"
                f"  (edit the frontmatter to leave exactly one status: line, then retry)"
            )

        status = read_fm_field(split.fm_text, "status")
        picked_up_by = read_fm_field_unquoted(split.fm_text, "picked_up_by")

        if status == "actioned":
            corrected = _handle_already_actioned(split.fm_text, params, "resolve")
            if corrected is None:
                _noop_result[0] = _ok(False, f"{memo} already actioned at target disposition — no-op")
                return old_text
            fm_text = corrected
        else:
            # Live-claim refusal: in_progress held by a DIFFERENT session → refuse, don't steal.
            if status == "in_progress" and picked_up_by != _sid:
                raise MutateAbort(
                    f"memo is already in_progress (held by {picked_up_by or '(empty)'}); "
                    "release it first or use a different session"
                )

            if status not in ("open", "delivered", "in_progress", None):
                raise MutateAbort(
                    f'unexpected current status "{status}" for resolve — expected open or delivered'
                )

            fm_text = _claim_stamp_fields(split.fm_text, _sid, _at)

            fm_text = _apply_action_fields(fm_text, {**params, "at": _at})

        fm_text = _normalize_oversize_summary(fm_text, memo)

        errors = _validate_memo_fm(fm_text)
        if errors:
            details = format_validation_errors(errors)
            raise MutateAbort(f"memo cross-field validation failed: {details}")

        return rebuild(split, fm_text)

    try:
        new_text = locked_rmw(memo_path, _mutate, repo_root=git_root)
    except MutateAbort as exc:
        return _err(str(exc.args[0]) if exc.args else "resolve: unknown mutation error")
    except LockTimeout as exc:
        return _err(str(exc))
    except FileNotFoundError:
        return _err(f"memo not found: {memo_path}")

    if _noop_result[0] is not None:
        resumed_reply = _resume_probe_and_commit(
            memo_path, git_root, "resolve", new_text,
            f"{memo} already resolved at target disposition — resumed a stranded "
            "uncommitted resolve write and committed it",
            attributed_session_id=_sid, params=params,
        )
        if resumed_reply is not None:
            return resumed_reply
        return _noop_result[0]

    written_split = split_frontmatter(new_text)
    if written_split is None or _count_status_keys(written_split.fm_text) != 1:
        return _err(
            f"INTERNAL ERROR — post-write status key count ≠ 1. Inspect {memo} immediately."
        )

    commit_sha, commit_error = _commit_terminal_write(
        memo_path, git_root, "resolve", new_text, attributed_session_id=_sid,
    )
    if commit_error is not None:
        return _attach_surface_advisory(_err(commit_error), params, new_text, git_root)

    return _attach_surface_advisory(
        _ok(True, f"resolved {memo} (picked_up_by {_sid})", commit_sha=commit_sha),
        params, new_text, git_root,
    )


# JSON-RPC handler

@register_op("memo.transition")
async def _handler(
    params: dict, repo_root: Any = None
) -> dict:
    """JSON-RPC 'memo.transition' handler — native atomic memo lifecycle transitions.

    MUTATING: writes to cross-repo memo frontmatter files in-place, and commits that
    write (DR-273) — every verb that lands a real write follow-up-commits it, scoped
    to the memo path only, using the git root it derives from ``params["memo"]``.

    repo_root is received (show_top scope) but intentionally unused — memo location comes
    from params["memo"], not the caller's worktree root (consumer-agnostic design; memos
    may live in any repo's cross-repo/ subtree). The commit this handler now performs
    (DR-273) uses the git root ``_containment_check`` derives from the memo path itself,
    NOT this unused ``repo_root`` — the consumer-agnostic contract is unchanged.

    ``cwd`` (optional, all verbs) — anchors a RELATIVE ``memo`` before resolution
    (state/cross-repo/archive/2026-09-05-claude-klabauter-engine-memo-transition-
    ignores-cwd-and-its-errors-misdirect.md). Previously accepted and silently
    ignored, leaving a relative ``memo`` to resolve against this process's own cwd
    (the engine root when served warm, not the caller's repo). An absolute
    ``memo`` ignores ``cwd`` entirely. See ``_containment_check``.

    Required params:
        verb (str) — one of: claim | action | release | resolve | close | lift.
        memo (str) — path to the target memo file.

    Verb-specific required params:
        claim  : session_id (str, required, non-empty), at (str, ISO timestamp).
                 Accepts a memo at status "open" OR "delivered" (see below).
        action : exactly one of:
                   decision (str: accepted|partial|declined) + optional decision_note, realized_by
                   actioned_note (str)
                 plus optional distill_fate (str: ephemeral|commitment|ratification) and
                 in_repo_capture (str), stamped atomically in the same write (Finding #11, C3);
                 plus optional correct_realization (bool) — see below;
                 plus optional at (str, ISO timestamp) — the closure instant stamped
                 into actioned_at, defaulting to now when absent (_closure_stamp).
        release: (no additional params)
        resolve: session_id (str, required, non-empty), at (str, ISO timestamp), plus the
                 same disposition params as action — atomic open→actioned, no intermediate
                 in_progress write (native-only, no JS mirror — see module docstring; C1 of
                 docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md).
                 Accepts a memo at status "open" OR "delivered" (see below).
        lift   : (no additional params) — draft→open, the one receiver-side move a
                 hand-delivered ``status: draft`` memo has no other way to reach
                 (state/cross-repo/archive/2026-09-02-doe-claude-em-outbound-defects-
                 batch.md § "A hand-delivered draft memo is unclosable by its
                 receiver"). Idempotent no-op when already open.
        close  : at (str, ISO timestamp) — actioned→closed, the previously-unreachable
                 terminal status the schema enum has always permitted. Requires the memo
                 be "actioned" WITH a decision on record; stamps closed_at (+ action_taken_at
                 when absent). Idempotent no-op when already closed.

    correct_realization (bool, action|resolve only): narrow, opt-in re-action of an
        already-``actioned`` memo whose ``decision:`` is UNCHANGED — permits ``realized_by``
        and ``decision_note`` to move (e.g. a cited commit was later reverted). The
        superseded ``realized_by`` is preserved inside ``decision_note`` as a delimited
        correction clause — no new frontmatter key is written. A ``decision:`` CHANGE
        still fails loud with or without this flag; it is not a force/override escape
        hatch. Absent this flag, behaviour is byte-identical to before it existed.

    supersede_note + supersede_realized_by (str, action only, both required together):
        records an APPEND-ONLY reversal of an already-actioned/superseded memo's
        disposition — mutually exclusive with decision/actioned_note/superseded_by
        (it corrects an EXISTING disposition, it does not set a new one). Writes
        disposition_superseded/superseding_note/superseding_realized_by/superseded_at
        (optional supersede_at, defaults to now) anchored right after status; the
        original decision/decision_note/realized_by/actioned_note are left untouched.
        Refused when the memo is not yet actioned/superseded (nothing to supersede),
        and when the memo is already superseded with a DIFFERENT supersede record
        (append-only — one reversal, not a rewrite target).

    "delivered" status (claim/resolve only): treated identically to "open" — a memo
    a sender-side path stamped ``status: delivered`` was previously a lifecycle dead
    end (state/cross-repo/archive/2026-09-02-example-cockpit-repo-em-memo-status-
    delivered-is-a-lifecycle-dead-end-no-verb-accepts.md): no verb accepted it, so it
    could never leave the inbox. claim and resolve now accept it exactly where they
    accept "open".

    Returns:
        {"exit_code": 0, "applied": bool,  "message": str, "commit_sha": str} on success
            (commit_sha additive, DR-273/C13 — present only when a real write landed)
            or no-op (no commit_sha key on a genuine no-op reply).
        {"exit_code": 0, "applied": False, "message": str, "commit_sha": str,
         "resumed": True} on a stranded-write resume (see module docstring's Return
            contract, C5 of docs/plans/2026-08-06-writer-side-commit-ownership-lock-gap.md).
        {"exit_code": 1, "applied": False, "error":   str} on error.

    Exit codes:
        0 — transition applied (applied=True) OR already-at-target no-op (applied=False).
        1 — error (bad params, dup status keys, containment failure, unexpected state,
                   validation failure, I/O error).

    Verb structure: each verb is a sync function dispatched via asyncio.to_thread — the
    blocking git rev-parse (inside _containment_check) must not run on the event loop
    (DR-212 D3).
    """
    verb = (params.get("verb") or "").strip()
    if not verb:
        return _err(
            "memo.transition: 'verb' is required (claim | action | release | resolve | close | lift)"
        )

    memo = (params.get("memo") or "").strip()
    if not memo:
        return _err("memo.transition: 'memo' is required")

    cwd = (params.get("cwd") or "").strip() or None

    if verb == "claim":
        session_id = (params.get("session_id") or "").strip()
        at = (params.get("at") or "").strip()
        return await asyncio.to_thread(_claim, memo, session_id, at, cwd)

    if verb == "action":
        return await asyncio.to_thread(_action, memo, params, cwd)

    if verb == "release":
        return await asyncio.to_thread(_release, memo, cwd)

    if verb == "resolve":
        session_id = (params.get("session_id") or "").strip()
        at = (params.get("at") or "").strip()
        return await asyncio.to_thread(_resolve, memo, session_id, at, params, cwd)

    if verb == "lift":
        return await asyncio.to_thread(_lift, memo, cwd)

    if verb == "close":
        at = (params.get("at") or "").strip()
        return await asyncio.to_thread(_close, memo, at, cwd)

    return _err(
        f"memo.transition: unknown verb {verb!r} — supported: "
        "claim, action, release, resolve, close, lift"
    )
