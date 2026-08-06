"""
coordinator_core.ops.memo_transition — memo lifecycle transition op (memo.transition op).

Purpose: Native Python port of example-doctrine-repo coordinator/bin/memo-transition.js — atomic
cross-repo-memo lifecycle frontmatter transitions. Implements ``claim``, ``action``, and
``release`` verbs that mutate memo state using the coordinator_core frontmatter primitives,
byte-faithful to the node oracle. Also implements ``resolve``, a native-only verb with no
JS mirror (see Parity note below). No subprocess / node reach-back.

Parity oracle: example-doctrine-repo coordinator/bin/memo-transition.js — covers claim/action/release
only. ``resolve`` is a native-only composition introduced by C1 of
docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md; claude-klabauter owns
cross-repo-memo tooling outright post-strangler-cut (DR-210), so this verb has no JS-side
oracle to stay byte-faithful to and none is expected. Do NOT read the module-level
"byte-faithful to the node oracle" claim above as covering ``resolve`` — it covers only the
three JS-mirrored verbs.
Spec backlink: docs/plans/2026-07-06-memo-transition-native-python-port.md (claim/action/release)
Spec backlink: docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md (resolve, C1)

Verb contracts (mirrored from the JS spec, plus the native-only addition):
  claim   — open → in_progress; writes picked_up_at + picked_up_by.
  action  — in_progress → actioned; writes decision/decision_note/realized_by
              OR actioned_note (consult/fyi shape). Preserves picked_up_by/at.
              An already-actioned memo re-actioned with ``correct_realization``
              truthy AND an UNCHANGED ``decision:`` may move ``realized_by``/
              ``decision_note`` only (evidence correction, e.g. a cited commit
              was later reverted) — a verdict change still fails loud
              regardless of the flag. See ``_handle_already_actioned``.
  release — in_progress → open; removes picked_up_by + picked_up_at entirely.
  resolve — open → actioned in ONE locked_rmw closure (native-only, no JS mirror).
              Collapses claim+action into a single atomic write — no intermediate
              in_progress state is ever visible on disk. This is the same two-step
              ceremony (archive-stamp-cli claim-memo-stamp, then action-memo) that
              state/lessons/2026-07-24-memo-terminal-flip-is-a-two-step-transit-147cc531ae68.yaml
              documents as already-established convention, collapsed into one call.

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

Negative-spec:
  - Does NOT subprocess / shell out. No node, no cli_path, no fallback escape hatch.
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
    format_validation_errors,
    validate_memo_cross_fields,
)
from coordinator_core.ipc import register_op
from coordinator_core.locked_write import LockTimeout, MutateAbort, locked_rmw
from coordinator_core.ops.ceremony import git_native
from coordinator_core.ops.fleet._memo_summary import _SUMMARY_MAX_CHARS


# ---------------------------------------------------------------------------
# Containment gate (SECURITY)
#
# UDS-only + MUTATING op; caller is a local same-user coordinator session with
# direct FS access; containment is defense-in-depth, not an escalation boundary.
# ---------------------------------------------------------------------------

_ALLOWED_SUBTREES = ("cross-repo", "state")
# Review: code-reviewer (F1) — bound the containment-check subprocess the same way the
# sibling workday_complete_step2_5_dirty_tree.py's _run_git helper does: timeout so a
# hung git process can't wedge the asyncio.to_thread pool indefinitely, stdin=DEVNULL so
# an interactive prompt never blocks on the daemon's inherited stdin, and CREATE_NO_WINDOW
# so Windows callers don't flash a console per memo transition.
_GIT_TIMEOUT_SECS = 30
_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _containment_check(memo: str) -> Path:
    """Raise if the resolved memo path is not under a git repo's cross-repo/ or state/ subtree.

    Returns the resolved git repository root on success — callers pass this as
    ``repo_root`` to ``locked_rmw`` so the ``git_common_dir`` lru_cache is keyed
    on the repo root (not per-memo-directory, which would cause N subprocess calls
    per distinct memo directory instead of 1 for the process lifetime).

    Steps:
        1. Resolve the absolute path of the memo.
        2. Find the git toplevel of its parent directory.
        3. Accept iff the resolved path is relative to <git-root>/cross-repo or <git-root>/state.

    Raises:
        ValueError — if the path fails any step of containment.

    Returns:
        Path — the resolved git repository root (for use as locked_rmw repo_root).
    """
    # Review: code-reviewer (F6) — return git_root so callers use it as repo_root
    # in locked_rmw, keying lru_cache on the stable repo root rather than the per-call
    # memo_path.parent, which would spawn a fresh git rev-parse per unique memo directory.
    m = Path(memo).resolve()

    result = subprocess.run(
        ["git", "-C", str(m.parent), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=_GIT_TIMEOUT_SECS,
        creationflags=_CREATIONFLAGS,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise ValueError(
            f"memo.transition: --memo outside containment (must be under a git repo "
            f"cross-repo/ or state/ subtree): {memo!r} — could not determine git root"
        )

    # Review: code-reviewer (F7) — .resolve() makes symlink handling explicit and
    # platform-consistent; git on macOS returns /private/tmp/... for /tmp/... repos.
    git_root = Path(result.stdout.strip()).resolve()
    for subtree in _ALLOWED_SUBTREES:
        if m.is_relative_to(git_root / subtree):
            return git_root

    raise ValueError(
        f"memo.transition: --memo outside containment (must be under a git repo "
        f"cross-repo/ or state/ subtree): resolved {m} is not under "
        f"{git_root}/cross-repo or {git_root}/state"
    )


# ---------------------------------------------------------------------------
# Reply helpers (identical shape to handoff_transition)
# ---------------------------------------------------------------------------

def _ok(applied: bool, message: str, commit_sha: str | None = None) -> dict:
    """Return exit_code=0 reply.

    ``commit_sha`` (additive, C13/DR-273) — the SHA of the follow-up commit
    ``_commit_terminal_write`` made for this verb's write, when one landed.
    Omitted from the envelope entirely when ``None`` (an idempotent no-op
    reply never carries a ``commit_sha`` key at all) — an existing consumer
    reading only ``exit_code``/``applied``/``message`` is unaffected.
    """
    reply = {"exit_code": 0, "applied": applied, "message": message}
    if commit_sha is not None:
        reply["commit_sha"] = commit_sha
    return reply


def _err(message: str) -> dict:
    """Return exit_code=1 reply.

    Ordinarily "error; no write performed" — the pre-write MutateAbort/validation
    paths never touch disk. The ONE exception is ``_commit_terminal_write``'s
    caller: a follow-up-commit failure after a successful frontmatter write
    still returns this shape, but the frontmatter mutation IS already on disk
    (uncommitted). Callers of that specific failure text should not assume "no
    write performed" — read the message.
    """
    return {"exit_code": 1, "applied": False, "error": message}


# ---------------------------------------------------------------------------
# Commit ownership (DR-273) — the terminal write's own follow-up commit.
#
# memo.transition takes commit ownership of the frontmatter mutation it just
# wrote, using the git root `_containment_check` already resolves (the
# consumer-agnostic contract stays intact — the caller's `repo_root` remains
# unused; this derives its own root from `params["memo"]`, not the caller's
# worktree). See docs/decisions/DR-273-memo-transition-commit-ownership.md.
#
# Routed through `git_native.commit_authored_content` (DR-272 § 3, the
# hash-object-populated private-index commit form C2 of
# docs/plans/2026-08-06-writer-side-commit-ownership-lock-gap.md adds) rather
# than `commit_scoped` — `commit_scoped`'s AGREE branch reads `path` off the
# WORKTREE to decide what to stage, which is exactly the "commit whatever
# happens to be on disk, not what this invocation authored" vector that plan
# closes (Defect 1). `commit_authored_content` takes the bytes THIS
# invocation's own `locked_rmw` call produced (or, on the resume branch
# below, the SAME lock-held-read bytes already validated against the verb's
# expected terminal state) as an explicit `content` parameter and never
# re-reads `memo_path` off the worktree at all.
# ---------------------------------------------------------------------------

def _commit_terminal_write(
    memo_path: Path, git_root: Path, verb: str, content: str
) -> tuple[str | None, str | None]:
    """Commit ``content`` (the exact bytes this call authored/validated) as the
    memo's on-disk mutation, in one single-path follow-up commit.

    Called AFTER ``locked_rmw`` has already written ``content`` to disk (the real-write
    path) OR, on the stranded-write resume branch (``_resume_probe_and_commit``), after
    the verb's own idempotency comparison has already validated ``content`` against the
    verb's expected terminal state — this never commits unvalidated worktree content.

    ``content`` is passed straight through to ``git_native.commit_authored_content`` —
    no worktree read on ``memo_path`` happens anywhere in this call (DR-272 § 3.3 bound 2).

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
        pathspec = str(memo_path.resolve().relative_to(git_root))
    except ValueError:
        # Unreachable in practice — _containment_check already proved memo_path
        # resolves under git_root/cross-repo or git_root/state before any write
        # was attempted. Defensive fallback only.
        pathspec = str(memo_path.resolve())

    message = f"memo.transition {verb}: {pathspec}\n"
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as fh:
        fh.write(message)
        msg_path = fh.name

    try:
        commit_result = git_native.commit_authored_content(pathspec, content, msg_path, git_root)
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

    return commit_result.stdout.strip(), None


# ---------------------------------------------------------------------------
# Stranded-write resume (Defect 2, C5) — a memo path whose per-verb idempotency
# comparison finds the on-disk frontmatter already at the verb's expected
# terminal state is EITHER a genuine no-op (the memo was already at rest,
# untouched by this call) OR a resume of a prior invocation that wrote the
# terminal state and crashed/died before its own follow-up commit landed
# (the write is stranded, uncommitted, on disk). The two are distinguished by
# whether the memo path is DIRTY in git — a genuine no-op memo (never written
# by this call in this lock acquisition) is clean; a stranded write is not.
# ---------------------------------------------------------------------------

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
    memo_path: Path, git_root: Path, verb: str, content: str, resumed_message: str
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
    """
    try:
        relpath = str(memo_path.resolve().relative_to(git_root))
    except ValueError:
        relpath = str(memo_path.resolve())

    if not _memo_path_dirty(git_root, relpath):
        return None

    commit_sha, commit_error = _commit_terminal_write(memo_path, git_root, verb, content)
    if commit_error is not None:
        return _err(commit_error)

    reply = _ok(False, resumed_message, commit_sha=commit_sha)
    reply["resumed"] = True
    return reply


# ---------------------------------------------------------------------------
# Dup-key guard (C5)
#
# Port of countStatusKeys from example-doctrine-repo coordinator/bin/memo-transition.js:136-143.
# Boundary lookahead /^status:(?=[ \t]|\r?$)/mg — prevents status:open (no space) from
# being counted, per the node oracle fix (code-reviewer slice A — F2).
# Operates on fm_text ONLY (never on the whole document body).
#
# The `\r?` half (2026-07-28, matching frontmatter/primitives.py's key-resolution
# rule) is what makes the guard CRLF-safe: without it a present-but-empty
# `status:\r\n` in a Windows-authored memo is invisible to the counter, so the
# duplicate-key guard silently UNDER-COUNTS — failing open on exactly the
# corruption it exists to catch. No upstream LF-only normalization of memo text
# exists to lean on.
# ---------------------------------------------------------------------------

_STATUS_KEY_RE = re.compile(r'^status:(?=[ \t]|\r?$)', re.MULTILINE)


def _count_status_keys(fm_text: str) -> int:
    """Count occurrences of ``status:`` lines in frontmatter text.

    Port of countStatusKeys from example-doctrine-repo coordinator/bin/memo-transition.js:136-143.
    Uses the boundary lookahead ``(?=[ \\t]|\\r?$)`` so ``status:open`` (no space)
    is not counted — aligning with ``read_fm_field``'s contract, ``\\r?`` half
    included (a CRLF-authored empty ``status:`` must count, or the guard fails open).

    Negative-spec: operates on fm_text ONLY, never on the whole document. A ``status:``
    in the body must not be counted.
    """
    return len(_STATUS_KEY_RE.findall(fm_text))


# ---------------------------------------------------------------------------
# Post-mutation validation seam
#
# Port of validateMemoFrontmatter from memo-transition.js:170-188.
# Node ordering: single-status-key postcondition FIRST (before cross-field rules),
# then cross-field rules. This matches memo-transition.js:175-187.
# ---------------------------------------------------------------------------

def _validate_memo_fm(fm_text: str) -> list[dict]:
    """Validate post-mutation frontmatter text.

    Node ordering (memo-transition.js:175-187):
    1. Single-status-key postcondition (dup-key post-mutation check).
    2. Cross-field rules via validate_memo_cross_fields.

    Returns a (possibly empty) list of error dicts. Empty → valid.
    Catches YAML parse errors and surfaces them as a synthetic error entry.

    Negative-spec: this is the POST-MUTATION check, not the pre-mutation dup-key guard.
    The pre-mutation guard (≥2 keys → fail-loud) runs in each verb before mutations.
    """
    # Step 1: single-status-key postcondition (seam requirement, memo-transition.js:175-178).
    key_count = _count_status_keys(fm_text)
    if key_count != 1:
        return [{
            'field': 'status',
            'error': f'post-mutation frontmatter has {key_count} status: key(s) (expected exactly 1) — fix the frontmatter manually',
            'hint': '',
        }]

    # Step 2: parse and run cross-field rules.
    try:
        fm_dict = yaml.safe_load(fm_text) or {}
    except Exception as exc:  # noqa: BLE001
        return [{"field": "(parse)", "error": f"YAML parse error in frontmatter: {exc}", "hint": ""}]

    return validate_memo_cross_fields(fm_dict)


# ---------------------------------------------------------------------------
# Summary-cap normalization (truncate-and-warn, not hard-fail)
#
# PM ruling 2026-07-22 (cross-repo ask 2): an over-cap summary: is cosmetic and
# must not strand an otherwise-good memo at _validate_memo_fm's hard-fail. This
# normalizes ahead of the gate; the gate (schema_validate._memo_cf_summary_length_cap)
# stays strict — see module-level negative-spec below.
# ---------------------------------------------------------------------------

_BLOCK_SCALAR_INDICATOR_RE = re.compile(r'^[|>][+\-0-9]*$')
# Matches the bare block-scalar indicator token read_fm_field returns for a
# `summary: |` / `summary: >` line (optionally with chomping `+`/`-` and/or an
# explicit indentation-indicator digit, e.g. `|-`, `>+`, `|2`, `|2-`) — never a
# real single-line value, which cannot itself be `|` or `>` alone without
# quoting (serialize_yaml_scalar quotes on the `|`/`>` structural chars).


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
    """Truncate an over-cap block-scalar ``summary: |`` / ``summary: >`` field.

    ``replace_fm_field``/``remove_fm_field`` both refuse (block-scalar guard, the Staff Engineer F1)
    to touch a block scalar — correctly, since a single-line ``.*$`` substitution would
    orphan the indented continuation lines. This helper instead decodes the full value
    via ``yaml.safe_load`` (the frontmatter text is a valid flow mapping; no hand-rolled
    YAML parsing), flattens embedded newlines to spaces (a block scalar's multi-line
    shape cannot be preserved once demoted to the single-line quoted form every sender
    path emits), truncates using the same ``value[:CAP - 1] + "…"`` shape as the
    plain-scalar branch, and splices the ENTIRE key-line-plus-continuation-block span
    (located via ``_find_block_scalar_span``, not a single-line regex) with one
    ``summary: "…"`` line.

    Length gate mirrors ``schema_validate._memo_cf_summary_length_cap`` exactly — that
    validator measures ``len(str(summary))`` on the yaml.safe_load-decoded value
    (embedded newlines counted as 1 char each, not flattened), so this helper gates on
    the same decoded length before deciding to act, not on the flattened/truncated length.

    Idempotent: an at-or-under-cap block scalar (decoded length) is left byte-identical
    (still a block scalar on disk — valid, since the cross-field cap check operates on
    decoded length, not on-disk shape), no warning emitted.

    Negative-spec: does NOT run when ``yaml.safe_load`` fails to parse ``fm_text`` — an
    unparseable frontmatter is left untouched; ``_validate_memo_fm``'s own YAML-parse-error
    path (not this helper) is the correct place to surface that failure.
    """
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
    """Replace a block-scalar ``key:`` line PLUS all its indented continuation lines.

    Locates the span from the ``key: |``/``key: >`` line through the last contiguous
    line that is either blank or indented (YAML requires block-scalar continuation
    lines to be indented relative to the key, which at frontmatter top level is column
    0 — so "indented or blank" is exactly the continuation-line test). Returns ``None``
    if the span cannot be located (defensive — should not happen given the caller
    already confirmed ``read_fm_field`` saw a block-scalar indicator for this key).
    """
    text = fm_text if fm_text.endswith('\n') else fm_text + '\n'
    # `\r?\n` at both line-terminator positions (2026-07-28): with a bare `\n`
    # the span never matched a CRLF-authored memo, so the locator returned None
    # and the caller silently left an OVER-CAP summary on disk — a fail-open on
    # the cap this path exists to enforce. The blank-continuation-line branch
    # needs it too: `.*` absorbs a `\r` on a content line, but a blank `\r\n`
    # line has no `.*` to absorb it.
    pattern = re.compile(
        r'^' + re.escape(key) + r':(?=[ \t]|\r?$)[ \t]*[|>][+\-0-9]*[ \t]*\r?\n'
        r'(?:(?:[ \t]+.*)?\r?\n)*',
        re.MULTILINE,
    )
    m = pattern.search(text)
    if not m:
        return None
    # The replacement line adopts the span's own terminator, so splicing into a
    # CRLF document cannot leave it with mixed line endings.
    if m.group(0).partition('\n')[0].endswith('\r') and not new_line.endswith('\r\n'):
        new_line = new_line[:-1] + '\r\n' if new_line.endswith('\n') else new_line + '\r\n'
    return text[: m.start()] + new_line + text[m.end():]


# ---------------------------------------------------------------------------
# YAML scalar unquote helper
# ---------------------------------------------------------------------------

# Review: code-reviewer (F2) — _validate_action_disposition deleted; it was a dead function
# never called by _action, with is-not-None semantics that differed from _action's live truthy
# guard and raising semantics that violated the AC6 _err() return contract. Tests retargeted
# to _action directly in test_memo_transition_unit.py.
#
# Review: code-reviewer (F5) — _unquote_yaml_scalar extracted from the nested _unq closure
# inside _action's if-status-actioned block. Module-level placement made it testable in
# isolation and eliminated per-call function recreation.
#
# 2026-07-21: the local _unquote_yaml_scalar copy is retired in favour of
# frontmatter.primitives.unquote_yaml_scalar — the shared inverse of serialize_yaml_scalar,
# which also covers the double-quoted form this local copy passed through unchanged.


# ---------------------------------------------------------------------------
# Shared claim-field-write helper (C1) — the ONE place status->in_progress plus
# picked_up_at/picked_up_by stamping is composed. _claim's mutate closure and
# resolve's mutate closure (C1) both call this rather than each carrying its
# own copy of the field-write algorithm.
# ---------------------------------------------------------------------------

def _claim_stamp_fields(fm_text: str, session_id: str, at: str) -> str:
    """Stamp status→in_progress plus picked_up_at/picked_up_by, as ``_claim`` does.

    Port of the field-write half of claim() from memo-transition.js:230-307,
    extracted so ``resolve`` (C1) can reuse the exact same algorithm inside its
    own single ``locked_rmw`` closure instead of calling ``_claim()`` (a
    complete lock cycle on its own).

    status is inserted (anchored after title) if absent, else replaced —
    callers that already validated the pre-mutation status (open/None/
    in_progress-by-self) pass it through unconditionally; this helper does not
    re-validate. picked_up_at/picked_up_by are inserted only if absent, so a
    caller re-stamping an already-in_progress-by-self memo leaves the original
    claim timestamps untouched.

    numeric_quoting=True on every write: node's serializeYamlScalar has no
    opt-in flag — it unconditionally quotes all-digit values on every field.
    """
    if read_fm_field(fm_text, "status") is None:
        fm_text = insert_fm_field(fm_text, "status", "in_progress", "title", numeric_quoting=True)
    else:
        fm_text = replace_fm_field(fm_text, "status", "in_progress", numeric_quoting=True)

    if read_fm_field(fm_text, "picked_up_at") is None:
        fm_text = insert_fm_field(fm_text, "picked_up_at", at, "status", numeric_quoting=True)

    if read_fm_field(fm_text, "picked_up_by") is None:
        fm_text = insert_fm_field(fm_text, "picked_up_by", session_id, "picked_up_at", numeric_quoting=True)

    return fm_text


# ---------------------------------------------------------------------------
# claim verb (sync — dispatched via asyncio.to_thread)
#
# Port of claim() from example-doctrine-repo coordinator/bin/memo-transition.js:230-307.
# ---------------------------------------------------------------------------

def _claim(memo: str, session_id: str, at: str) -> dict:
    """Apply claim transition: open → in_progress, write picked_up_at + picked_up_by.

    Byte-faithful port of claim() from memo-transition.js:230-307.

    Idempotency: no-op when already in_progress with the SAME session.
    Collision: any other in_progress → fail-loud (held by different session).
    Pre-mutation dup-key guard: ≥2 status: keys → fail-loud, no write (raises MutateAbort
    from inside mutate so locked_rmw skips the write).
    Post-write self-verify: exactly 1 status: key must remain.
    """
    # Fail-loud on empty session_id — never write picked_up_by: empty.
    if not session_id or not session_id.strip():
        return _err(
            "claim requires a non-empty --session-id (empty picked_up_by would corrupt the claim gate)"
        )
    if not at or not at.strip():
        return _err("claim requires --at <ISO timestamp>")

    # Containment gate MUST fire before any frontmatter-primitive call (lesson: externally-triggered-ops-must-contain).
    # Review: code-reviewer (F1) — wrap in try/except so containment ValueError returns _err()
    # (AC6 {exit_code:1} contract) instead of propagating through asyncio.to_thread to the IPC
    # BaseException handler which would emit a -32603 INTERNAL_ERROR with no result.exit_code.
    # Review: code-reviewer (F6) — capture git_root for use as locked_rmw repo_root to avoid
    # lru_cache thrash (memo_path.parent varies per call; git_root is stable for the repo lifetime).
    try:
        git_root = _containment_check(memo)
    except ValueError as exc:
        return _err(str(exc))
    except subprocess.TimeoutExpired:
        # Review: code-reviewer (F1) — surface a timed-out containment-check git
        # subprocess as the AC6 {exit_code:1} contract, same as ValueError above,
        # instead of letting it escape unhandled through asyncio.to_thread.
        return _err(f"claim: containment check timed out for --memo {memo!r}")

    memo_path = Path(memo)
    if not memo_path.is_file():
        return _err(f"memo not found: {memo}")

    _sid = session_id.strip()
    _at = at.strip()
    # Mutable container so the closure can signal an idempotent no-op without raising.
    _noop_result: list[dict | None] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {memo}")

        # Pre-mutation dup-key guard (C5, memo-transition.js:244-250).
        # Raises MutateAbort so locked_rmw releases the lock without writing.
        pre_dup_count = _count_status_keys(split.fm_text)
        if pre_dup_count >= 2:
            raise MutateAbort(
                f"memo has {pre_dup_count} status: keys — hand-collapse the duplicate before retrying\n"
                f"  (edit the frontmatter to leave exactly one status: line, then retry)"
            )

        status = read_fm_field(split.fm_text, "status")
        # Unquoted read: picked_up_by is written with numeric_quoting=True, so a
        # session id that is all-digit, scientific-notation-shaped, or contains a
        # structural character lands single-quoted and would never compare equal
        # to the bare _sid — silently defeating the claim idempotency no-op.
        picked_up_by = read_fm_field_unquoted(split.fm_text, "picked_up_by")

        # Idempotency: no-op when already in_progress with the SAME session.
        # Return old_text unchanged; locked_rmw detects byte-identity and skips the write.
        if status == "in_progress" and picked_up_by == _sid:
            _noop_result[0] = _ok(False, f"{memo} already in_progress (picked_up_by {_sid}) — no-op")
            return old_text

        # Collision: any other in_progress held by a different session.
        # Raises MutateAbort so locked_rmw releases the lock without writing.
        if status == "in_progress":
            raise MutateAbort(
                f"memo is already in_progress (held by {picked_up_by or '(empty)'}); "
                "release it first or use a different session"
            )

        # Unexpected terminal or unknown state.
        # Raises MutateAbort so locked_rmw releases the lock without writing.
        if status not in ("open", None):
            raise MutateAbort(f'unexpected current status "{status}" for claim — expected open')

        fm_text = split.fm_text

        # status → in_progress + picked_up_at/picked_up_by (shared with resolve, C1).
        fm_text = _claim_stamp_fields(fm_text, _sid, _at)

        # Truncate-and-warn an over-cap summary: BEFORE the validation gate (Ask 2) —
        # the cap is cosmetic; it must not hard-fail the transition.
        fm_text = _normalize_oversize_summary(fm_text, memo)

        # Post-mutation validation gate (before write).
        # Raises MutateAbort so locked_rmw releases the lock without writing.
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
        # Review: code-reviewer (F1) — memo deleted between is_file() check and lock
        # acquisition (TOCTOU window); locked_rmw raises FileNotFoundError. Without this
        # clause it escapes through asyncio.to_thread to the IPC dispatcher → -32603
        # INTERNAL_ERROR with no exit_code field (AC6/AC10 contract violation).
        return _err(f"memo not found: {memo}")

    # Idempotent no-op: mutate returned old_text unchanged; locked_rmw skipped the write.
    if _noop_result[0] is not None:
        resumed_reply = _resume_probe_and_commit(
            memo_path, git_root, "claim", new_text,
            f"{memo} already in_progress (picked_up_by {_sid}) — resumed a stranded "
            "uncommitted write and committed it",
        )
        if resumed_reply is not None:
            return resumed_reply
        return _noop_result[0]

    # Post-write self-verify (memo-transition.js:299-303).
    # Use the text returned by locked_rmw (what was written) rather than re-reading from disk.
    written_split = split_frontmatter(new_text)
    if written_split is None or _count_status_keys(written_split.fm_text) != 1:
        return _err(
            f"INTERNAL ERROR — post-write status key count ≠ 1. Inspect {memo} immediately."
        )

    commit_sha, commit_error = _commit_terminal_write(memo_path, git_root, "claim", new_text)
    if commit_error is not None:
        return _err(commit_error)

    return _ok(True, f"claimed {memo} (picked_up_by {_sid})", commit_sha=commit_sha)


# ---------------------------------------------------------------------------
# Shared action-disposition validation + field-write helpers (C1) — extracted
# so _action and resolve both call these instead of each carrying a parallel
# copy of the disposition-validation / field-write / idempotency-match logic.
# ---------------------------------------------------------------------------

def _validate_action_disposition(params: dict, verb: str = "action") -> dict | None:
    """Validate action-shape disposition params. Returns an ``_err()`` dict, or ``None`` if valid.

    Port of the param-validation half of action() from memo-transition.js:311-438,
    extracted so ``resolve`` (C1) can run the SAME check — before its lock is even
    acquired — instead of a parallel copy. ``verb`` parameterizes the error text
    (``"action"`` or ``"resolve"``) so callers get an accurate message.

    Mutually exclusive: decision XOR actioned_note. Exactly one is required.
    realized_by is required when decision is accepted|partial (not for declined).

    Fail-loud (no write) when ``decision_note`` or ``actioned_note`` contains an
    embedded \\n or \\r — serialize_yaml_scalar's own docstring documents it does
    not handle multi-line values; a raw newline would break out of the intended
    single-line ``decision_note:``/``actioned_note:`` value onto its own YAML
    line, truncating the frontmatter from that point on (mirrors
    handoff_transition.py's ``_unclaim`` fail-loud on ``park_note`` for the same
    ``serialize_yaml_scalar`` negative-spec). This check runs BEFORE the
    realized_by-required check below so a caller who both omits --realized-by
    AND passes a multi-line note is pointed at the note, not misdirected toward
    realized_by by the cross-field validator's misleading downstream error.
    """
    decision = params.get("decision")
    actioned_note = params.get("actioned_note")
    realized_by = params.get("realized_by")
    decision_note = params.get("decision_note")

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
        # realized_by is required for accepted|partial (shape validated by cross-field seam).
        if decision in ("accepted", "partial") and not realized_by:
            return _err(f"{verb}: --realized-by is required when --decision is {decision}")

    return None


def _disposition_matches(fm_text: str, params: dict) -> bool:
    """True iff an already-actioned memo's on-disk disposition matches ``params`` exactly.

    Port of the already_at_target comparison from action() (memo-transition.js:360-361).
    Shared by ``_action``'s idempotency check and ``resolve``'s (C1) — the comparison
    algorithm exists exactly once.
    """
    decision = params.get("decision")
    decision_note = params.get("decision_note")
    realized_by = params.get("realized_by")
    actioned_note = params.get("actioned_note")

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


# ---------------------------------------------------------------------------
# realized_by correction (--correct-realization) — the ONE place the narrow,
# opt-in re-action-with-unchanged-verdict correction path is composed. Both
# ``_action`` and ``_resolve`` call ``_handle_already_actioned`` from their
# "status == actioned" branch instead of each carrying a parallel copy of the
# guard/correction logic.
#
# Spec: fixes the fail-loud in memo_transition.py that had no legitimate
# escape hatch for correcting a stale ``realized_by`` (e.g. a commit later
# reverted) on a memo whose verdict (``decision:``) has NOT changed. A verdict
# CHANGE still fails loud unconditionally — this path only ever touches
# ``realized_by``/``decision_note``.
#
# Boundary constraint (load-bearing): does NOT add a new frontmatter key. The
# superseded ``realized_by`` is carried forward inside the existing free-text
# ``decision_note`` field as a clearly-delimited correction clause — the memo
# frontmatter SHAPE is coordinator-claude's contract to own, not claude-klabauter's
# (tri-plane ownership boundary, see repo CLAUDE.md § Project Overview).
# ---------------------------------------------------------------------------

def _apply_realization_correction(fm_text: str, params: dict) -> str:
    """Apply a ``--correct-realization`` correction: move ``realized_by``/``decision_note``
    ONLY, preserving the superseded ``realized_by`` value inside ``decision_note``.

    Preconditions (caller's responsibility — see ``_handle_already_actioned``):
    the memo is already ``actioned``, decision-shape (not ``actioned_note``-shape),
    with the SAME ``decision:`` value as ``params`` requests, and a DIFFERENT
    disposition overall (else the idempotent no-op branch would already have fired).

    Audit trail: whatever ``decision_note`` this write ends up carrying (the
    caller-supplied one, or the pre-existing one if the caller didn't supply a
    new one) has a ``[correction ...]`` clause appended naming the superseded
    ``realized_by`` value and a UTC timestamp — the superseded SHA is never
    silently dropped. No new frontmatter key is introduced.
    """
    cur_realized_by = unquote_yaml_scalar(read_fm_field(fm_text, "realized_by"))
    new_realized_by = params.get("realized_by")
    base_note = params.get("decision_note")
    if base_note is None:
        base_note = unquote_yaml_scalar(read_fm_field(fm_text, "decision_note")) or ""

    ts = datetime.now(timezone.utc).isoformat()
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


def _handle_already_actioned(fm_text: str, params: dict, verb: str) -> str | None:
    """Decide the outcome when the memo is already ``actioned`` and ``verb`` is
    called again requesting a (possibly identical) disposition.

    Shared by ``_action`` and ``_resolve`` — the guard/correction logic exists
    exactly once, both verbs call this from their "status == actioned" branch.

    Returns:
        None — idempotent no-op; caller returns ``old_text`` unchanged (no write).
        str  — corrected ``fm_text`` (the ``--correct-realization`` path);
               caller continues through the normal validate + write path.

    Raises:
        MutateAbort — a verdict change (``decision:`` differs from the on-disk
        value), WITH OR WITHOUT ``--correct-realization`` — this flag never
        unlocks a verdict change, only evidence correction under an unchanged
        verdict. Also raised when ``--correct-realization`` is requested
        without ``--decision`` (there is no ``realized_by`` to correct on an
        ``actioned_note``-shape memo), or when the disposition otherwise
        differs and no correction was requested at all (the pre-existing
        fail-loud, unchanged).
    """
    if _disposition_matches(fm_text, params):
        return None  # idempotent no-op

    if not params.get("correct_realization"):
        raise MutateAbort("memo is already actioned with a different disposition — cannot re-action")

    new_decision = params.get("decision")
    if not new_decision:
        raise MutateAbort(
            f"{verb}: --correct-realization requires --decision matching the on-disk "
            "decision value (decision-shape memos only — there is no realized_by to "
            "correct on an actioned_note-shape memo)"
        )

    cur_decision = read_fm_field_unquoted(fm_text, "decision")
    if cur_decision != new_decision:
        # Verdict change — --correct-realization does NOT unlock this.
        raise MutateAbort("memo is already actioned with a different disposition — cannot re-action")

    return _apply_realization_correction(fm_text, params)


def _apply_action_fields(fm_text: str, params: dict) -> str:
    """Apply status→actioned plus the disposition field writes, as ``_action`` does.

    Port of the field-write half of action() from memo-transition.js:311-438 (decision
    mode: decision + optional decision_note/realized_by; note mode: actioned_note only;
    plus the optional distill_fate/in_repo_capture Finding-#11 stamp fields), extracted
    so ``resolve`` (C1) can reuse the exact same algorithm inside its own single
    ``locked_rmw`` closure instead of calling ``_action()`` (a complete lock cycle on
    its own).

    Precondition (caller's responsibility, not re-checked here): status is already
    "in_progress" in ``fm_text`` — ``_action`` enforces this before calling; ``resolve``
    (C1) enforces it via its own step 1-2 (collision/idempotency checks + claim stamp)
    within the SAME closure before calling this.

    picked_up_by/picked_up_at are NOT touched — both callers preserve them as the
    claim-of-record.
    """
    decision = params.get("decision")
    actioned_note = params.get("actioned_note")
    decision_note = params.get("decision_note")
    realized_by = params.get("realized_by")
    distill_fate = params.get("distill_fate")
    in_repo_capture = params.get("in_repo_capture")

    # status → actioned
    # numeric_quoting=True on every field write below: node's serializeYamlScalar
    # (schema.js) has no opt-in flag — it unconditionally quotes all-digit values on
    # every field it serializes. Python must match on every write, not just realized_by.
    fm_text = replace_fm_field(fm_text, "status", "actioned", numeric_quoting=True)

    if decision:
        # decision field: replace if present, insert after status if absent.
        if read_fm_field(fm_text, "decision") is None:
            fm_text = insert_fm_field(fm_text, "decision", decision, "status", numeric_quoting=True)
        else:
            fm_text = replace_fm_field(fm_text, "decision", decision, numeric_quoting=True)

        # decision_note (optional): replace or insert after decision.
        if decision_note:
            if read_fm_field(fm_text, "decision_note") is None:
                fm_text = insert_fm_field(
                    fm_text, "decision_note", decision_note, "decision", numeric_quoting=True
                )
            else:
                fm_text = replace_fm_field(
                    fm_text, "decision_note", decision_note, numeric_quoting=True
                )

        # realized_by (required for accepted|partial, absent for declined): replace or insert.
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
        # Consult/fyi shape: write actioned_note only.
        if read_fm_field(fm_text, "actioned_note") is None:
            fm_text = insert_fm_field(
                fm_text, "actioned_note", actioned_note, "status", numeric_quoting=True
            )
        else:
            fm_text = replace_fm_field(fm_text, "actioned_note", actioned_note, numeric_quoting=True)

    # distill_fate / in_repo_capture — Finding #11 stamp-at-source fields, written in
    # the SAME atomic write as status/decision/realized_by above (port of
    # memo-transition.js:423-453). Anchored after whatever field ended up last in the
    # block above (realized_by/decision_note/decision for the decision shape,
    # actioned_note for the consult/fyi shape) so field order on disk reads as a
    # coherent append, not an interleave.
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


# ---------------------------------------------------------------------------
# action verb (sync — dispatched via asyncio.to_thread)
#
# Port of action() from example-doctrine-repo coordinator/bin/memo-transition.js:311-438.
# ---------------------------------------------------------------------------

def _action(memo: str, params: dict) -> dict:
    """Apply action transition: in_progress → actioned, write disposition fields.

    Byte-faithful port of action() from memo-transition.js:311-492.

    Decision mode: writes decision + optional decision_note/realized_by.
    Note mode: writes actioned_note only.
    Preserves picked_up_by/at — they are the claim-of-record for the archived memo.

    distill_fate / in_repo_capture (Finding #11, C3): stamped in the SAME atomic
    write as status/decision/realized_by — port of memo-transition.js:423-453.
    Cross-field shape (ratification requires in_repo_capture; a ~/.claude path
    fails validation) is enforced by _validate_memo_fm → validate_memo_cross_fields,
    same as every other field this op writes.
    Spec backlink: docs/plans/2026-07-12-distill-rebuild-claude-klabauter-reliant.md § C3

    Already-actioned idempotency: no-op ONLY when status==actioned AND full disposition matches.
    Re-action with different disposition → fail-loud, UNLESS ``params["correct_realization"]``
    is truthy AND ``decision:`` is unchanged — in which case only ``realized_by``/
    ``decision_note`` move, with the superseded ``realized_by`` preserved inside
    ``decision_note`` (see ``_handle_already_actioned`` / ``_apply_realization_correction``).
    A verdict (``decision:``) CHANGE still fails loud even with the flag set.
    """
    distill_fate = params.get("distill_fate")
    in_repo_capture = params.get("in_repo_capture")

    disposition_error = _validate_action_disposition(params, verb="action")
    if disposition_error is not None:
        return disposition_error

    # Containment gate MUST fire before any frontmatter-primitive call.
    # Review: code-reviewer (F1) — containment ValueError → _err() (AC6 contract).
    # Review: code-reviewer (F6) — capture git_root for locked_rmw repo_root stability.
    try:
        git_root = _containment_check(memo)
    except ValueError as exc:
        return _err(str(exc))
    except subprocess.TimeoutExpired:
        # Review: code-reviewer (F1) — same AC6 {exit_code:1} contract as ValueError.
        return _err(f"action: containment check timed out for --memo {memo!r}")

    memo_path = Path(memo)
    if not memo_path.is_file():
        return _err(f"memo not found: {memo}")

    # Mutable container so the closure can signal an idempotent no-op without raising.
    _noop_result: list[dict | None] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {memo}")

        # Pre-mutation dup-key guard (C5).
        # Raises MutateAbort so locked_rmw releases the lock without writing.
        pre_dup_count = _count_status_keys(split.fm_text)
        if pre_dup_count >= 2:
            raise MutateAbort(
                f"memo has {pre_dup_count} status: keys — hand-collapse the duplicate before retrying\n"
                f"  (edit the frontmatter to leave exactly one status: line, then retry)"
            )

        status = read_fm_field(split.fm_text, "status")

        # Idempotency / correction / fail-loud: no-op when already at the exact
        # target disposition; otherwise --correct-realization (unchanged decision:
        # only) applies a narrow evidence correction, or the pre-existing
        # re-action guard fires. Shared with resolve via _handle_already_actioned.
        # Return old_text unchanged on no-op; locked_rmw detects byte-identity
        # and skips the write.
        if status == "actioned":
            corrected = _handle_already_actioned(split.fm_text, params, "action")
            if corrected is None:
                _noop_result[0] = _ok(False, f"{memo} already actioned at target disposition — no-op")
                return old_text
            fm_text = corrected
        else:
            # Unexpected status → fail-loud, no write.
            if status != "in_progress":
                raise MutateAbort(
                    f'unexpected current status "{status or "(missing)"}" for action — expected in_progress'
                )

            # PRESERVE picked_up_by and picked_up_at — claim-of-record for the archived memo.
            fm_text = _apply_action_fields(split.fm_text, params)

        # Truncate-and-warn an over-cap summary: BEFORE the validation gate (Ask 2) —
        # the cap is cosmetic; it must not hard-fail the transition.
        fm_text = _normalize_oversize_summary(fm_text, memo)

        # Post-mutation validation gate (before write).
        # Raises MutateAbort so locked_rmw releases the lock without writing.
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
        # Review: code-reviewer (F1) — TOCTOU: memo deleted between is_file() and lock
        # acquire; locked_rmw raises FileNotFoundError → would escape as -32603 INTERNAL_ERROR.
        return _err(f"memo not found: {memo}")

    # Idempotent no-op: mutate returned old_text unchanged; locked_rmw skipped the write.
    if _noop_result[0] is not None:
        resumed_reply = _resume_probe_and_commit(
            memo_path, git_root, "action", new_text,
            f"{memo} already actioned at target disposition — resumed a stranded "
            "uncommitted write and committed it",
        )
        if resumed_reply is not None:
            return resumed_reply
        return _noop_result[0]

    # Post-write self-verify (replace-not-append discipline): confirm exactly one
    # status: key survived AND, when the caller supplied distill_fate/in_repo_capture,
    # confirm exactly one of each of THOSE keys survived too — guards the same
    # duplicate-key corruption class the status self-verify already guards, extended
    # to the two C3 fields (port of memo-transition.js:470-488).
    # Use the text returned by locked_rmw (what was written) rather than re-reading from disk.
    written_split = split_frontmatter(new_text)
    if written_split is None or _count_status_keys(written_split.fm_text) != 1:
        return _err(
            f"INTERNAL ERROR — post-write status key count ≠ 1. Inspect {memo} immediately."
        )
    if distill_fate:
        # `\r?$` for the same reason as _STATUS_KEY_RE: a CRLF present-but-empty
        # key must be counted, or this self-verify fails open.
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

    commit_sha, commit_error = _commit_terminal_write(memo_path, git_root, "action", new_text)
    if commit_error is not None:
        return _err(commit_error)

    return _ok(True, f"actioned {memo}", commit_sha=commit_sha)


# ---------------------------------------------------------------------------
# release verb (sync — dispatched via asyncio.to_thread)
#
# Port of release() from example-doctrine-repo coordinator/bin/memo-transition.js:442-496.
# ---------------------------------------------------------------------------

def _release(memo: str) -> dict:
    """Apply release transition: in_progress → open, remove picked_up_by + picked_up_at.

    Byte-faithful port of release() from memo-transition.js:442-496.

    Idempotency: no-op when already open.
    Negative-spec: do NOT preserve picked_up_by/at (contrast with action, which preserves them).
    """
    # Containment gate MUST fire before any frontmatter-primitive call.
    # Review: code-reviewer (F1) — containment ValueError → _err() (AC6 contract).
    # Review: code-reviewer (F6) — capture git_root for locked_rmw repo_root stability.
    try:
        git_root = _containment_check(memo)
    except ValueError as exc:
        return _err(str(exc))
    except subprocess.TimeoutExpired:
        # Review: code-reviewer (F1) — same AC6 {exit_code:1} contract as ValueError.
        return _err(f"release: containment check timed out for --memo {memo!r}")

    memo_path = Path(memo)
    if not memo_path.is_file():
        return _err(f"memo not found: {memo}")

    # Mutable container so the closure can signal an idempotent no-op without raising.
    _noop_result: list[dict | None] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {memo}")

        # Pre-mutation dup-key guard (C5).
        # Raises MutateAbort so locked_rmw releases the lock without writing.
        pre_dup_count = _count_status_keys(split.fm_text)
        if pre_dup_count >= 2:
            raise MutateAbort(
                f"memo has {pre_dup_count} status: keys — hand-collapse the duplicate before retrying\n"
                f"  (edit the frontmatter to leave exactly one status: line, then retry)"
            )

        status = read_fm_field(split.fm_text, "status")

        # Idempotency: no-op when already open.
        # Return old_text unchanged; locked_rmw detects byte-identity and skips the write.
        if status == "open":
            _noop_result[0] = _ok(False, f"{memo} already open — no-op")
            return old_text

        # Unexpected status → fail-loud, no write.
        if status != "in_progress":
            raise MutateAbort(
                f'unexpected current status "{status or "(missing)"}" for release — expected in_progress'
            )

        fm_text = split.fm_text

        # status → open
        # numeric_quoting=True: node's serializeYamlScalar (schema.js) has no opt-in flag —
        # it unconditionally quotes all-digit values on every field it serializes. Python
        # must match on every write, not just realized_by.
        fm_text = replace_fm_field(fm_text, "status", "open", numeric_quoting=True)

        # CLEAR picked_up_by and picked_up_at entirely — release reverts the claim.
        fm_text = remove_fm_field(fm_text, "picked_up_by")
        fm_text = remove_fm_field(fm_text, "picked_up_at")

        # Truncate-and-warn an over-cap summary: BEFORE the validation gate (Ask 2) —
        # the cap is cosmetic; it must not hard-fail the transition.
        fm_text = _normalize_oversize_summary(fm_text, memo)

        # Post-mutation validation gate (before write).
        # Raises MutateAbort so locked_rmw releases the lock without writing.
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
        # Review: code-reviewer (F1) — TOCTOU: memo deleted between is_file() and lock
        # acquire; locked_rmw raises FileNotFoundError → would escape as -32603 INTERNAL_ERROR.
        return _err(f"memo not found: {memo}")

    # Idempotent no-op: mutate returned old_text unchanged; locked_rmw skipped the write.
    if _noop_result[0] is not None:
        resumed_reply = _resume_probe_and_commit(
            memo_path, git_root, "release", new_text,
            f"{memo} already open — resumed a stranded uncommitted write and committed it",
        )
        if resumed_reply is not None:
            return resumed_reply
        return _noop_result[0]

    # Post-write self-verify.
    # Use the text returned by locked_rmw (what was written) rather than re-reading from disk.
    written_split = split_frontmatter(new_text)
    if written_split is None or _count_status_keys(written_split.fm_text) != 1:
        return _err(
            f"INTERNAL ERROR — post-write status key count ≠ 1. Inspect {memo} immediately."
        )

    commit_sha, commit_error = _commit_terminal_write(memo_path, git_root, "release", new_text)
    if commit_error is not None:
        return _err(commit_error)

    return _ok(True, f"released {memo} (status reset to open, claim cleared)", commit_sha=commit_sha)


# ---------------------------------------------------------------------------
# resolve verb (sync — dispatched via asyncio.to_thread)
#
# Native-only — no JS-side oracle (see module docstring's Parity note). C1 of
# docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md.
# ---------------------------------------------------------------------------

def _resolve(memo: str, session_id: str, at: str, params: dict) -> dict:
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

    # Fail-loud on empty session_id — never write picked_up_by: empty.
    if not session_id or not session_id.strip():
        return _err(
            "resolve requires a non-empty --session-id (empty picked_up_by would corrupt the claim gate)"
        )
    if not at or not at.strip():
        return _err("resolve requires --at <ISO timestamp>")

    # Containment gate MUST fire before any frontmatter-primitive call.
    try:
        git_root = _containment_check(memo)
    except ValueError as exc:
        return _err(str(exc))
    except subprocess.TimeoutExpired:
        return _err(f"resolve: containment check timed out for --memo {memo!r}")

    memo_path = Path(memo)
    if not memo_path.is_file():
        return _err(f"memo not found: {memo}")

    _sid = session_id.strip()
    _at = at.strip()
    # Mutable container so the closure can signal an idempotent no-op without raising.
    _noop_result: list[dict | None] = [None]

    def _mutate(old_text: str) -> str:
        split = split_frontmatter(old_text)
        if split is None:
            raise MutateAbort(f"no parseable YAML frontmatter in {memo}")

        # Pre-mutation dup-key guard (C5).
        # Raises MutateAbort so locked_rmw releases the lock without writing.
        pre_dup_count = _count_status_keys(split.fm_text)
        if pre_dup_count >= 2:
            raise MutateAbort(
                f"memo has {pre_dup_count} status: keys — hand-collapse the duplicate before retrying\n"
                f"  (edit the frontmatter to leave exactly one status: line, then retry)"
            )

        status = read_fm_field(split.fm_text, "status")
        # Unquoted read: picked_up_by is written with numeric_quoting=True (see
        # _claim's identical comment) — comparisons must go through the unquoted form.
        picked_up_by = read_fm_field_unquoted(split.fm_text, "picked_up_by")

        # Step 1: _claim's collision/idempotency checks, against THIS lock acquisition —
        # already-actioned re-action guard, correction, or fail-loud (shared with
        # _action via _handle_already_actioned).
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

            # open, None, or in_progress-held-by-us are the only remaining legal states.
            if status not in ("open", "in_progress", None):
                raise MutateAbort(f'unexpected current status "{status}" for resolve — expected open')

            # Step 2: stamp picked_up_at/picked_up_by as _claim does. This is an in-memory
            # transition only — status briefly reads "in_progress" in fm_text here, but that
            # value is never returned/written on its own; step 3 overwrites it to "actioned"
            # within this SAME closure before the single rebuild() below.
            fm_text = _claim_stamp_fields(split.fm_text, _sid, _at)

            # Step 3: apply _action's disposition field writes. Its "requires in_progress"
            # precondition is satisfied by step 2 above (status is now "in_progress" in
            # fm_text) — checked within this SAME closure, never a second read.
            fm_text = _apply_action_fields(fm_text, params)

        # Truncate-and-warn an over-cap summary: BEFORE the validation gate (Ask 2) —
        # the cap is cosmetic; it must not hard-fail the transition.
        fm_text = _normalize_oversize_summary(fm_text, memo)

        # Step 4: the same _validate_memo_fm gate _claim/_action already run, once,
        # before the single write. Raises MutateAbort so locked_rmw releases the lock
        # without writing.
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
        # TOCTOU: memo deleted between is_file() and lock acquire.
        return _err(f"memo not found: {memo}")

    # Idempotent no-op: mutate returned old_text unchanged; locked_rmw skipped the write.
    if _noop_result[0] is not None:
        resumed_reply = _resume_probe_and_commit(
            memo_path, git_root, "resolve", new_text,
            # Review: code-reviewer (P2) — resolve's resumed-reply names its own verb,
            # matching _claim/_action/_release's per-verb-distinct wording; previously
            # identical to _action's message, so a resumed resolve read as an action
            # resume in logs/commit messages.
            f"{memo} already resolved at target disposition — resumed a stranded "
            "uncommitted resolve write and committed it",
        )
        if resumed_reply is not None:
            return resumed_reply
        return _noop_result[0]

    # Post-write self-verify (AC1): exactly one status: key must remain, and it must
    # read "actioned" — proves no intermediate in_progress state was written.
    written_split = split_frontmatter(new_text)
    if written_split is None or _count_status_keys(written_split.fm_text) != 1:
        return _err(
            f"INTERNAL ERROR — post-write status key count ≠ 1. Inspect {memo} immediately."
        )

    commit_sha, commit_error = _commit_terminal_write(memo_path, git_root, "resolve", new_text)
    if commit_error is not None:
        return _err(commit_error)

    return _ok(True, f"resolved {memo} (picked_up_by {_sid})", commit_sha=commit_sha)


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------

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

    Required params:
        verb (str) — one of: claim | action | release | resolve.
        memo (str) — path to the target memo file.

    Verb-specific required params:
        claim  : session_id (str, required, non-empty), at (str, ISO timestamp)
        action : exactly one of:
                   decision (str: accepted|partial|declined) + optional decision_note, realized_by
                   actioned_note (str)
                 plus optional distill_fate (str: ephemeral|commitment|ratification) and
                 in_repo_capture (str), stamped atomically in the same write (Finding #11, C3);
                 plus optional correct_realization (bool) — see below.
        release: (no additional params)
        resolve: session_id (str, required, non-empty), at (str, ISO timestamp), plus the
                 same disposition params as action — atomic open→actioned, no intermediate
                 in_progress write (native-only, no JS mirror — see module docstring; C1 of
                 docs/plans/2026-07-26-memo-disposition-flip-op-and-hand-edit-hole.md).

    correct_realization (bool, action|resolve only): narrow, opt-in re-action of an
        already-``actioned`` memo whose ``decision:`` is UNCHANGED — permits ``realized_by``
        and ``decision_note`` to move (e.g. a cited commit was later reverted). The
        superseded ``realized_by`` is preserved inside ``decision_note`` as a delimited
        correction clause — no new frontmatter key is written. A ``decision:`` CHANGE
        still fails loud with or without this flag; it is not a force/override escape
        hatch. Absent this flag, behaviour is byte-identical to before it existed.

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
        return _err("memo.transition: 'verb' is required (claim | action | release | resolve)")

    memo = (params.get("memo") or "").strip()
    if not memo:
        return _err("memo.transition: 'memo' is required")

    if verb == "claim":
        session_id = (params.get("session_id") or "").strip()
        at = (params.get("at") or "").strip()
        # asyncio.to_thread: blocking containment check + file I/O must not run on the event loop.
        return await asyncio.to_thread(_claim, memo, session_id, at)

    if verb == "action":
        # asyncio.to_thread for DR-212 D3 async-loop mandate.
        return await asyncio.to_thread(_action, memo, params)

    if verb == "release":
        # asyncio.to_thread for DR-212 D3 async-loop mandate.
        return await asyncio.to_thread(_release, memo)

    if verb == "resolve":
        session_id = (params.get("session_id") or "").strip()
        at = (params.get("at") or "").strip()
        # asyncio.to_thread: blocking containment check + file I/O must not run on the event loop.
        return await asyncio.to_thread(_resolve, memo, session_id, at, params)

    return _err(
        f"memo.transition: unknown verb {verb!r} — supported: claim, action, release, resolve"
    )
