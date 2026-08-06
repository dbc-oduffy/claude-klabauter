"""
coordinator_core.ops.review_trail_write — per-session review-trail entry writer.

Purpose: writes a JSON review-trail entry (additive-create) under
``<worktree>/state/review-trail/<timestamp>-<session_id_short>.json``.

Port of: coordinator-write-review-trail.sh (example-doctrine-repo 30f4c5fc, 2026-07-19).

JSON record shape (key order is canonical; hand-serialized for byte-parity):
    {"sha_range":"A..B","reviewer":"code-reviewer","scope":"chain","scope_kind":"diff",
     "verdict":"ok","diff_loc":100,"session_id":"abc12345","workstream":null}

    A NINTH, optional key — ``reviewed_paths`` (docs/plans/2026-07-27-review-trail-scope-guard.md
    § C9) — is appended ONLY on ``scope_kind: "diff"`` records, carrying the reviewed-path
    set (e.g. from ``freeze-review-diff.py --paths``) or JSON ``null`` when not supplied.
    ``plan``/``integration`` records OMIT the key entirely; the original eight-key shape is
    otherwise unchanged and every existing by-key-name consumer keeps working.

Filename derivation:
    ``{TIMESTAMP}-{SESSION_ID[:8]}.json``
    TIMESTAMP: ``YYYY-MM-DD-HHMMSS`` (macOS/Windows, second-precision, 17 chars) or
                ``YYYY-MM-DD-HHMMSS{6ns_digits}`` (Linux, nanosecond-truncated, 23 chars).
    SESSION_ID_SHORT: first 8 characters of the resolved session_id.

Session-id resolution (strict precedence — mirrors oracle § Session-id resolution):
    1. ``session_id`` parameter when non-empty (explicit caller override).
    2. ``CLAUDE_SESSION_ID`` env var.
    3. ``CLAUDE_CODE_SESSION_ID`` env var.
    4. Sentinel file: ``{caller_worktree}/.git/coordinator-sessions/.current-session-id``.
    5. Raises ``ValueError`` if not resolved (parity with oracle exit 3).

Workstream resolution (tolerant/nullable — D9 present-as-null discipline):
    1. ``workstream`` parameter when non-empty (explicit caller override).
    2. ``COORDINATOR_REVIEW_WORKSTREAM`` env var.
    3. Scan ``{caller_worktree}/state/handoffs/*.md`` for a handoff whose ``claimed_by``
       names THIS writing session's own resolved session_id, and read its
       ``workstream:`` field (2026-07-27 fix — never an arbitrary peer's handoff).
    4. null — not resolvable (including: no session_id to attribute against).
    Only [A-Za-z0-9_-] chars permitted in slug; any other → reject-to-null.

Write semantics (DR-216 D2(i) SUPERSEDED 2026-07-27 — see incident note below):
    Additive-create, never-clobber: same timestamp+session_id_short is disambiguated with
    a ``-2``, ``-3``, ... suffix (``_reserve_unique_trail_path``) so a same-second
    collision lands in its own file instead of destroying the earlier record. Atomic:
    ``os.open(candidate, O_CREAT | O_EXCL | O_WRONLY)`` claims the target name and the
    full record is written directly into it (never ``os.replace`` onto the final name —
    ``O_EXCL`` fails closed with ``FileExistsError`` on a collision instead of silently
    overwriting, and is atomic create-if-absent on POSIX and Windows alike — CPython
    maps it to ``CREATE_NEW`` on nt). No trailing newline (matches oracle ``printf '%s'``
    write).

    2026-07-27 incident: DR-216 D2(i)'s last-write-wins design rested on the premise
    that same-second same-session collision was "impossible in practice" (DR-215
    serial-by-construction). That premise was falsified live: 9 ``review_trail.write``
    calls in a loop within one wall-clock second produced only 5 surviving files — 4
    records silently destroyed, each call still returning a success ``out_path`` that
    pointed at content a later call had already overwritten. This is an audit-trail
    surface the coverage gate reads to decide whether code was reviewed, so silent loss
    can re-open a coverage hole (or mis-attribute a verdict) with no error ever
    surfacing. The binding constraint is the emit-section reader
    (``coordinator_core.ops.emit.sections._shared._validate_review_trail_file``): it does
    NOT understand JSONL (a multi-line file fails its ``json.loads(fh.read())`` call
    whole-file, which would quarantine a merged record instead of losing it more
    quietly). ``coordinator_core.coverage.build_reviewed_set`` via ``_parse_trail_file``
    already tolerates JSONL as a fallback, but the emit-section reader does not, so
    uniquifying the filename — not switching to JSONL append — is the fix that every
    existing reader already supports unmodified.

MUTATING op: writes ONLY ``state/review-trail/`` (DR-216 D2(iv) noun confinement).
NEVER writes ``state/handoffs/``, ``archive/``, or rag's relational store
(dual-write ban, DR-208 Invariant-1 / tri-plane DD#1).
No git commit from the handler (DR-216 D2(v)).
Blocking FS I/O wrapped in ``asyncio.to_thread`` (DR-216 D3 / DR-213 D3 mandate).

Registered as ``review_trail.write`` in ops/__init__.py (separate EM chunk).
Classified ``OpClass.MUTATING`` in authz/classification.py (separate EM chunk).
``_OP_KEY_SCOPE: common_dir`` — handler receives ``git_common_dir(caller_worktree)`` via
ipc.py; derives worktree via ``main_worktree_root(repo_root)`` before any path construction.

Spec backlink: docs/plans/2026-07-06-strang-10-residual-writer-strangle-command-type.md § C3
DR authority: docs/decisions/DR-216-changelog-completion-reviewtrail-write-carveout.md § D2

Negative-spec:
    - NO yaml.dump / json.dumps with ensure_ascii/separators — hand-serialized JSON for
      byte-parity with the oracle's bash string interpolation (no library normalization).
    - NO trailing newline — oracle writes via ``printf '%s'`` (not ``echo``).
    - NO git commit from the handler (D2(v)).
    - NO rag store write (dual-write ban).
    - NO write to any path outside ``state/review-trail/`` (D2(iv)).
    - NO cwd-based repo resolution; worktree derived from caller's ``repo_root`` param.
    - NO concurrent-session sentinel ambiguity detection (cs_resolve_session_id tier-4
      logic) — simplified sentinel read; daemon context always supplies session_id via env.
"""

from __future__ import annotations
import sys

import asyncio
import datetime
import json
import logging
import os
import platform
import re
import subprocess
import time
from pathlib import Path
from typing import FrozenSet, List, Optional

from coordinator_core import chain_ancestry_waivers, session_attribution
from coordinator_core.ipc import register_op
from coordinator_core.ops._fm_util import extract_frontmatter_scalar
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.session import review_trail_vouch

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Env var override for test isolation: redirect entire trail dir to this root.
# When set, ``{REVIEW_TRAIL_OUTPUT_ROOT}/review-trail/`` is used instead of
# ``{caller_worktree}/state/review-trail/``.
_REVIEW_TRAIL_OUTPUT_ROOT_ENV = "REVIEW_TRAIL_OUTPUT_ROOT"

# Session-id env vars (precedence order 2 and 3).
_CLAUDE_SESSION_ID_ENV = "CLAUDE_SESSION_ID"
_CLAUDE_CODE_SESSION_ID_ENV = "CLAUDE_CODE_SESSION_ID"

# Workstream env var (precedence order 2).
_COORDINATOR_REVIEW_WORKSTREAM_ENV = "COORDINATOR_REVIEW_WORKSTREAM"

# Validated enum values (mirrors oracle validation).
# "wsc-auto-adjudication"/"workstream-close-auto" were the OLD wsc_commit.py's
# _build_effective_review_trail machine-provenance auto-source sentinels
# (module retired 2026-07-29, kill-list op removal) — distinct from human/CI
# reviewer names, but still validated so any already-written auto-sourced
# records round-trip.
_VALID_REVIEWERS = frozenset(
    {
        "code-reviewer",
        "staff-eng",
        "code-reviewer+staff-eng",
        "waived",
        "ubt-compile",
        "wsc-auto-adjudication",
    }
)
_VALID_SCOPES = frozenset({"chain", "session", "workstream-close-auto"})
_VALID_VERDICTS = frozenset({"ok", "warn", "blocked", "waived", "pending"})
_VALID_SCOPE_KINDS = frozenset({"diff", "plan", "integration"})

# Only [A-Za-z0-9_-] permitted in workstream slug (reject-to-null otherwise).
_WORKSTREAM_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]")

# A pure hex token (full or abbreviated SHA) — never needs git to resolve.
# Mirrors coverage.py's _HEX_TOKEN (the read-side counterpart).
_HEX_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


# ---------------------------------------------------------------------------
# Write-time symbolic-ref resolution (sha_range false-COVERED defect, write side)
# ---------------------------------------------------------------------------
#
# state/improvement-queue/2026-06-30-review-coverage-gate-false-covered-on-tr.yaml:
# a record persisted with a literal symbolic ref (almost always "HEAD") on either
# side of sha_range — e.g. "0227ea17..HEAD" — re-resolves at READ time, in
# coverage.py's build_reviewed_set, against whatever HEAD happens to be when the
# gate later runs. On a shared branch that means the record's certified width
# silently grows to swallow every commit landed after it was written, with no
# reviewer having opened any of them. Concretizing here, at write time, is the
# half of the fix that stops new bad records; existing on-disk records still
# carrying a literal "HEAD" are handled read-side (coverage.py's Phase 1
# classification excludes them — see build_reviewed_set's module docstring).


def _resolve_ref_to_sha(token: str, cwd: Path) -> str:
    """Resolve one git ref token to its concrete full SHA via ``git rev-parse``.

    A hex token (full or abbreviated SHA, optionally suffixed with ``^``/``~N``
    ops — e.g. ``abc1234^``) is returned unchanged: it is already a concrete,
    non-symbolic anchor and does not re-resolve at read time. Anything else
    (``HEAD``, a branch name, ``origin/main``, a tag, ...) is a symbolic ref
    that MUST be concretized now, or it will silently re-resolve to whatever
    that ref points at when the coverage gate later reads this record back.

    Known narrow gap (Review: code-reviewer — Finding 2, WARN 958054a5): a
    branch name that is ITSELF 4-40 pure hex characters (e.g. a
    ticket-id-shaped branch like ``deadbeef``) matches ``_HEX_TOKEN_RE`` and
    is returned unchanged as if already concrete, even though it is a live,
    movable ref — it is not concretized here and will still re-resolve at
    read time. This mirrors ``coverage.py``'s own ``_HEX_TOKEN`` ambiguity
    (same regex, same trade-off) by deliberate design: a full fix would need
    ``git rev-parse --verify`` / ``git symbolic-ref`` disambiguation on
    every hex-shaped token, which reintroduces exactly the git round-trip
    this hex fast-path exists to skip, for a shape that is legal but
    unobserved in practice. Left as a documented, narrow, low-likelihood
    lane rather than expanded to cover it — a hex-shaped branch name would
    need a dedicated fix on both the read and write side together, not a
    write-side-only patch that would only close half the gap.

    Raises ``ValueError`` if git cannot resolve the token — refusing to persist
    an unresolvable ref is safer than silently writing one through.
    """
    if _HEX_TOKEN_RE.match(token) or (
        token and _HEX_TOKEN_RE.match(re.split(r"[\^~]", token, maxsplit=1)[0])
    ):
        return token
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--verify", "--quiet", token],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            # Review: code-reviewer — Finding 1 (P1): stdin=DEVNULL paired with
            # CREATE_NO_WINDOW, matching coverage.py._run's pairing; CREATE_NO_WINDOW
            # alone hangs on Windows when stdin is inherited/invalid.
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(
            f"review_trail.write: could not resolve ref {token!r} to a concrete "
            f"SHA ({exc}) — refusing to persist an unresolvable/symbolic ref"
        ) from exc
    out = proc.stdout.strip()
    if proc.returncode != 0 or not out:
        raise ValueError(
            f"review_trail.write: could not resolve ref {token!r} to a concrete "
            f"SHA (git rev-parse failed) — refusing to persist an "
            f"unresolvable/symbolic ref"
        )
    return out


def _resolve_symbolic_range(sha_range: str, caller_worktree: Optional[Path]) -> str:
    """Concretize any symbolic ref token(s) in a diff-shaped sha_range.

    Only acts on ranges that actually contain ``..``/``...`` (the diff shape
    resolved read-side by coverage.py's build_reviewed_set) — a bare
    scope_kind=plan/integration token is never a git ref in the first place
    and is left untouched. A ``scope_kind=plan`` record is NOT skipped
    read-side: it is credited against the kind-aware planning-artifact
    subset of its resolved SHAs (C5, docs/plans/2026-08-05-coverage-gate-
    planning-artifact-class.md — see coverage.py's kind-aware crediting
    rule). Only the ref-resolution behaviour here is unaffected by that
    shape, since a plan/integration token was never a resolvable ref to
    begin with.

    ``caller_worktree`` is required to run ``git rev-parse`` against the
    caller's actual repo; when absent (test-isolation callers that skip
    ``caller_worktree`` entirely) this is a no-op — those callers already pass
    concrete SHAs, and there is no repo to resolve against.
    """
    if caller_worktree is None:
        return sha_range
    sep = "..." if "..." in sha_range else (".." if ".." in sha_range else None)
    if sep is None:
        return sha_range
    left, right = sha_range.split(sep, 1)
    resolved_left = _resolve_ref_to_sha(left, caller_worktree) if left else left
    resolved_right = _resolve_ref_to_sha(right, caller_worktree) if right else right
    return f"{resolved_left}{sep}{resolved_right}"


# ---------------------------------------------------------------------------
# Write-side foreign-session scope guard (chokepoint for all five callers)
# ---------------------------------------------------------------------------
#
# docs/plans/2026-07-27-review-trail-scope-guard.md § C2. This is the fix for
# the reported defect: a scope="chain" record whose sha_range spans a peer
# session's commits vouches for code nobody on the writing session reviewed,
# which lets the coverage gate ship a false COVERED verdict. Consumes
# coordinator_core.session_attribution (C1) — the classification algorithm
# itself is not re-implemented here.

#: scoping_method vocabulary — string literals only (mirrors, does not
#: import, the enum wsc_resolve.py/receipt_schema.py declare for the same
#: concept; scoping_method is a ceremony-receipt field, never a review-trail
#: record key, so this module only borrows the vocabulary for its own
#: logger.info call, not the receipt machinery).
_SCOPING_METHOD_TRAILER = "session_id_trailer"
_SCOPING_METHOD_STARTED_AT_RANGE = "started_at_contiguous_range"

#: Cap on the number of offending SHAs named in the case-1 ValueError message.
_FOREIGN_SHA_DISPLAY_CAP = 10

#: Shared tail for both `ForeignSessionRangeRefused` messages (case 1 and
#: case 3) — named once here rather than duplicated at each raise site
#: (a prior incident: an EM read the case-1 message's "peer session's
#: unreviewed commits" phrasing as an established fact rather than what the
#: guard actually tested, wrongly concluded a chain-terminal coverage
#: obligation over its own baton ancestry was someone else's problem, and
#: reported that wrong conclusion up). Both raise sites test one narrow
#: signal — never "this commit is unrelated to your chain" or "this commit
#: is unreviewed" — so this text says exactly that, then names the two
#: readings consistent with the refusal without picking one.
_FOREIGN_SESSION_UNDETERMINED_NOTE = (
    "This does not determine that the named commit(s) are unrelated to "
    "this session's chain, and it does not determine whether they were "
    "reviewed — neither was checked. Two readings are consistent with this "
    "refusal: (i) these are baton-ancestor commits this session is "
    "legitimately obliged to cover as a chain-terminal reviewer, or (ii) "
    "these are genuinely unrelated, concurrent peer work on a shared "
    "branch. Determine which case applies before proceeding. If (i) "
    "applies, do not narrow sha_range to shed chain coverage — partition "
    "the chain and write one per-slice record over this session's own "
    "commits in each slice instead; narrowing is the case-(ii) remedy only."
)


class ForeignSessionRangeRefused(ValueError):
    """Raised when ``_guard_foreign_session_range`` refuses a caller-supplied
    ``sha_range`` that spans commits not attributable to the writing session
    (docs/plans/2026-07-27-review-trail-scope-guard.md § AC9).

    A ``ValueError`` subclass, deliberately -- every existing caller/test that
    catches the parent ``ValueError`` keeps working unchanged (this is
    additive typing, not a contract change). It exists so a caller that DOES
    care (e.g. ``ceremony.tail_ops.write_review_trail``) can distinguish a
    genuine scope-guard refusal from an ordinary validation ``ValueError`` and
    route it to a ``failed_critical[]``-shaped signal, untruncated, instead of
    losing it in a generic truncated ``failed[]`` message.

    Raised from case 1 (a commit trailer-attributed to a different session)
    and case 3 (a genuinely ambiguous range) of ``_guard_foreign_session_range``
    -- never from case 2 (the safe, provably-in-scope case, which returns
    normally).
    """


def _git_runner(args: list[str], cwd: Optional[str]) -> tuple[int, str, str]:
    """Never-raises git-invocation helper conforming to session_attribution.GitRunner.

    ``args`` already includes the leading ``"git"`` token (the contract
    ``session_attribution.trailer_foreign_shas`` calls its injected ``run``
    with) — this helper does not prepend it again.

    Windows-safe: suppresses the console window a bare subprocess.run would
    otherwise flash on Windows (CREATE_NO_WINDOW) AND pins stdin=DEVNULL —
    CREATE_NO_WINDOW alone hangs on Windows when stdin is inherited/invalid
    (see coverage.py._run's pairing), matching this module's other subprocess
    call sites.
    """
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
            # Review: code-reviewer — Finding 1 (P1): stdin=DEVNULL paired with
            # CREATE_NO_WINDOW, matching coverage.py._run and the two sibling
            # chokepoints (session_attribution._git_run, wsc_commit's
            # _close_ceremony_git_runner) this same diff already fixed —
            # CREATE_NO_WINDOW alone hangs on Windows when stdin is
            # inherited/invalid; this is the LIVE path _guard_foreign_session_range
            # runs through, so it must not be left half-fixed.
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 2, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _own_session_touched_paths_and_untrailered_flag(
    sha_range: str,
    own_session_id: str,
    worktree_root: Path,
) -> tuple[frozenset[str], bool]:
    """Scan sha_range once for this write's own known-scope-path input.

    Returns (paths, saw_untrailered):
      paths — the union of file paths touched by commits in sha_range whose
        own Session-Id trailer names own_session_id. This is the write-side
        known-scope-path set handed to
        ``session_attribution.detect_foreign_commits`` — a trailerless
        commit is classified in-scope only if it touches a path this
        session's own commits, within this same range, already touched.
      saw_untrailered — True iff at least one commit in sha_range carries no
        Session-Id trailer at all (used only to choose which scoping_method
        string to log — never persisted).

    Returns (frozenset(), False) on any git failure — never silently widens
    scope on an unreadable git state; a git failure here is not distinguishable
    from "no commits found", the same graceful-empty contract
    session_attribution.detect_foreign_commits documents for its own
    git-failure case.
    """
    rc, out, _err = _git_runner(
        [
            "git", "log", "--no-merges", "--name-only",
            "--format=%x02%H%x1f%(trailers:key=Session-Id,valueonly)",
            sha_range,
        ],
        str(worktree_root),
    )
    if rc != 0:
        return frozenset(), False

    paths: set[str] = set()
    saw_untrailered = False
    for block in out.split("\x02"):
        block = block.strip("\n")
        if not block:
            continue
        lines = block.splitlines()
        header = lines[0]
        if "\x1f" not in header:
            continue
        _sha, _sep, trailer = header.partition("\x1f")
        trailer = trailer.strip()
        if not trailer:
            saw_untrailered = True
            continue
        if trailer != own_session_id:
            continue
        for line in lines[1:]:
            line = line.strip()
            if line:
                paths.add(line)
    return frozenset(paths), saw_untrailered


#: state/review-trail/ subdirectory holding permanent, idempotent per-SHA
#: PM-vouch waivers — see `_record_pm_vouch_waivers` and
#: `coordinator_core.coverage._narrow_foreign_session_scope` (the read-side
#: consumer that subtracts this set from the commits it strips for
#: foreign-session attribution).
_PM_VOUCH_WAIVER_DIRNAME = "pm-vouches"


def _record_pm_vouch_waivers(
    caller_worktree: Path,
    shas: FrozenSet[str],
    writing_session_id: str,
    vouch_record: Optional[dict],
) -> None:
    """Persist an idempotent, PERMANENT per-SHA waiver under
    ``state/review-trail/pm-vouches/<sha>.json`` for each sha in ``shas`` —
    the durable, read-side-effective half of the Case-1 PM-vouch relaxation
    (see ``_guard_foreign_session_range``'s docstring for the write-time
    half).

    Why a SEPARATE, non-liveness-gated file rather than re-consulting
    ``review_trail_vouch.check_review_trail_vouch`` at read time: the
    coverage gate that reads a review-trail record back
    (``coordinator_core.coverage._narrow_foreign_session_scope``) commonly
    runs in a LATER session — often the very NEXT thing a chain-terminal
    ``/workstream-complete`` does after this write, by which point the
    writing session may already be closing. A liveness-gated re-check at
    read time would make the relaxation evaporate exactly when it is needed
    most. This file is instead a durable historical fact: the write-time
    liveness check already ran (in ``_guard_foreign_session_range`` via
    ``review_trail_vouch.check_review_trail_vouch``) and gated CREATING
    this waiver — once created, it is never re-validated, exactly as the
    review-trail record itself is never re-validated against the session
    that wrote it.

    Idempotent-create only (``O_CREAT | O_EXCL``): a waiver, once written
    for a given sha, is never overwritten or re-derived by a later call —
    first grant to name a SHA wins, and the file's content is purely
    informational/audit (the read side only checks FILE PRESENCE, not
    content — see coverage.py). Still within ``state/review-trail/`` (D2(iv)
    noun confinement — this module's negative-spec: "NO write to any path
    outside state/review-trail/").

    Best-effort: an ``OSError`` creating the directory or a waiver file is
    logged and swallowed, never raised — a waiver-persistence failure must
    not turn an otherwise-successful, PM-authorized write into a hard
    failure; it degrades to "the read side will strip this commit's credit
    after all," which is the safe (fail-closed on crediting) direction, not
    a silent authorization bypass.

    Spec backlink: archive/specs/2026-07/2026-07-27-review-trail-scope-guard.md § C7 (amended)
    """
    waiver_dir = caller_worktree / "state" / "review-trail" / _PM_VOUCH_WAIVER_DIRNAME
    try:
        waiver_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "review_trail.write: could not create pm-vouches waiver dir %s (%s) — "
            "this write proceeds, but the coverage read side will NOT credit the "
            "vouched-for commit(s) without a waiver file",
            waiver_dir, exc,
        )
        return
    written_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    for sha in sorted(shas):
        target = waiver_dir / f"{sha}.json"
        if target.exists():
            continue
        waiver_record = {
            "schema_version": 1,
            "sha": sha,
            "vouched_by_session": writing_session_id,
            "granted_by": (vouch_record or {}).get("granted_by"),
            "granted_at": (vouch_record or {}).get("granted_at"),
            "note": (vouch_record or {}).get("note"),
            "waiver_written_at": written_at,
        }
        try:
            fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(waiver_record, fh, indent=2)
                fh.write("\n")
        except FileExistsError:
            continue
        except OSError as exc:
            logger.warning(
                "review_trail.write: could not write pm-vouches waiver %s (%s) — "
                "this write proceeds, but the coverage read side will NOT credit "
                "this commit without a waiver file",
                target, exc,
            )


def _guard_foreign_session_range(
    sha_range: str,
    own_session_id: str,
    caller_worktree: Path,
) -> FrozenSet[str]:
    """Refuse, or force affirmative disambiguation of, a diff-shaped sha_range
    that spans commits not attributable to the writing session.

    Three-way disposition (docs/plans/2026-07-27-review-trail-scope-guard.md § C2),
    AMENDED 2026-07-28 for Case 1's narrow PM-vouch relaxation (see below —
    archive/specs/2026-07/2026-07-27-review-trail-scope-guard.md § C7 amendment,
    coordinator_core/session/review_trail_vouch.py):

    Case 1 — any commit whose OWN Session-Id trailer names a DIFFERENT
    session: hard refusal (``ForeignSessionRangeRefused``), UNLESS the
    offending SHA is covered by one of TWO evidence sources — the writing
    session holds a LIVE, per-session PM-vouch grant
    (``coordinator_core.session.review_trail_vouch.check_review_trail_vouch``)
    naming it, OR it carries a chain-ancestry waiver minted for THIS chain
    (``chain_ancestry_waivers.chain_ancestry_waived_shas`` — see below).
    Refusal strength is otherwise UNCHANGED: still refuse unless every
    offending SHA is covered by one of the two. OLD semantics (through
    2026-07-27): genuinely no override existed at all — see the module this
    function's docstring superseded, archive/specs/2026-07/2026-07-27-review-
    trail-scope-guard.md § AC3 ("No `--force`, no env override, no
    warn-and-write fallback for case 1"). NEW semantics (2026-07-28,
    amended 2026-07-31): that "no override" claim is narrowed, not
    withdrawn — there is still no `--force` flag, no env var, and no
    blanket per-session exemption. The FIRST relaxation is a PM-vouch grant
    that (a) requires a recorded PM utterance to create (never
    self-grantable — an agent cannot write its own grant file any more than
    it can forge a Tier-U grant, see review_trail_vouch.py's negative-spec),
    (b) is liveness-gated at WRITE time (a grant from a dead or different
    session authorizes nothing), and (c) names the SPECIFIC offending
    SHA(s) — a grant naming SHA X never authorizes a range containing a
    DIFFERENT foreign SHA Y. The SECOND relaxation
    (docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md § C3),
    added 2026-07-31, is a per-SHA, per-chain waiver minted by the coverage
    GATE at HALT (``coordinator_core.ops.coverage_gate``, C2) — NOT
    granted by a human, and NOT this guard's own doing; this guard only
    OBSERVES it via ``chain_ancestry_waivers.chain_ancestry_waived_shas``,
    scoped to ``own_session_id`` (the writing session's own id, which IS
    the closing chain's identity when this session is that chain's
    terminal ``/workstream-complete`` — see that module's own docstring for
    why no new derivation is needed to resolve it here). Both sources are a
    per-SHA set; a range whose foreign-attributed SHAs are only PARTIALLY
    covered by the UNION of the two still refuses, naming only the
    uncovered remainder. This closes the chain-terminal
    ``/workstream-complete`` deadlock (coverage gate HALTs on an uncovered
    predecessor commit; the writer forbade recording a review of it) without
    reopening the hazard C7 closed — see this function's own body and
    ``_record_pm_vouch_waivers`` for how the PM-vouch relaxation is made
    effective on the READ side without re-checking liveness after the
    session that used it has closed; the chain-ancestry waiver needs no
    equivalent write-side persistence step here — it was already minted,
    permanently, by the gate at HALT (C2/C1). Trailerlessness alone is
    never Case 1 (SC-DR-008 sanctions trailerless commits by design).

    Case 2 — no foreign-trailer commit, AND every untrailered commit in
    range touches at least one path this session's own trailer-attributed
    commits (within this same range) already touched, AND that in-scope set
    is contiguous with the session's own commits: the write proceeds. Which
    scoping strategy established safety is logged via ``logger.info`` — it is
    NOT persisted to the record (scoping_method has no honest home in the
    review-trail record schema; C9 governs the one sanctioned additive key,
    ``reviewed_paths``, separately).

    Case 3 — anything else (an untrailered commit the touched-path signal
    cannot place in scope, or an in-scope set interleaved rather than
    contiguous with the writing session's own commits): genuinely ambiguous.
    Neither silently written nor blanket-refused — force an affirmative
    caller-supplied narrower range instead (the X-node / DR-502 J-node shape:
    docs/wiki/claude-klabauter-ceremony-lifecycle-machinery.md § DR-502 — "a
    heuristic-suggested default never silently self-resolves an ambiguous
    judgment node").

    Applies regardless of the record's ``scope`` value, INCLUDING
    ``scope="chain"`` — scope-blindness is deliberate (the reported defect
    was a chain-scoped record).

    Cases 1 and 3 raise ``ForeignSessionRangeRefused`` (not a bare
    ``ValueError``) so a caller that cares — e.g.
    ``ceremony.tail_ops.write_review_trail`` — can route the refusal to a
    distinguishable, untruncated ``failed_critical[]`` entry instead of an
    ordinary truncated ``failed[]`` message (AC9,
    docs/plans/2026-07-27-review-trail-scope-guard.md § AC9). It remains a
    ``ValueError`` subclass, so every pre-existing ``except ValueError``
    caller/test keeps working unchanged.

    ``caller_worktree`` that is not itself inside a git work tree is the same
    test-isolation no-op contract ``_resolve_symbolic_range`` documents for a
    ``None`` caller_worktree, extended to "a directory was supplied but it is
    not a git repo at all" — there is no real commit history here to reason
    about, so the guard is a no-op rather than a hard git-subprocess failure.
    """
    is_work_tree_rc, _out, is_work_tree_err = _git_runner(
        ["git", "rev-parse", "--is-inside-work-tree"], str(caller_worktree),
    )
    if is_work_tree_rc != 0:
        # Review: code-reviewer — Finding 3 (P1): distinguish "confirmed not a
        # git work tree" (git itself ran and reported the failure) from "the
        # git invocation itself failed" (binary missing, timeout, permission
        # error on a REAL repo) — only the former is the documented no-op
        # carve-out (_resolve_symbolic_range's "no real repo to reason about"
        # contract). `_git_runner` returns rc=2 ONLY from its own except-clause
        # on OSError/TimeoutExpired (never as a real git exit code for this
        # check), so rc==2 here is unambiguously "git invocation failed", not
        # "not a repo" — fail CLOSED on that rather than silently disabling
        # the whole foreign-session guard. Either way, log so a silent bypass
        # is never invisible to an operator debugging a false COVERED verdict.
        if is_work_tree_rc == 2:
            logger.warning(
                "review_trail.write: git invocation failed while checking "
                "whether %r is a git work tree (%s) — refusing to silently "
                "skip the foreign-session scope guard",
                str(caller_worktree),
                is_work_tree_err,
            )
            raise ValueError(
                "review_trail.write: could not verify caller_worktree is a "
                f"git work tree ({is_work_tree_err!r}) — refusing to write "
                "without running the foreign-session scope guard"
            )
        logger.info(
            "review_trail.write: caller_worktree %r is not a git work tree "
            "(rc=%s) — foreign-session guard is a no-op (test-isolation "
            "contract)",
            str(caller_worktree),
            is_work_tree_rc,
        )
        return frozenset()

    trailer_cache: dict[tuple[str, Optional[str]], frozenset[str]] = {}
    foreign_trailer_shas = session_attribution.trailer_foreign_shas(
        sha_range, own_session_id, str(caller_worktree), trailer_cache, _git_runner,
    )
    if foreign_trailer_shas:
        # Per-session, PM-granted relaxation (2026-07-28) — see this
        # function's docstring and coordinator_core/session/
        # review_trail_vouch.py. `check_review_trail_vouch` returns ONLY the
        # subset of `foreign_trailer_shas` that a LIVE grant for THIS
        # session (own_session_id) explicitly names; a grant naming SHA X
        # never covers a different foreign SHA Y (AC3), and a dead/foreign
        # session's grant covers nothing (AC4).
        #
        # Review: code-reviewer — Finding 5 (nit): every foreign-trailer hit
        # now also resolves this session's grant file (a `meta.json` read
        # via `core.session_dir` + `liveness.session_live`) before refusing
        # — extra I/O on what is already the fail path, not the success
        # path, so genuinely low-stakes; noted for completeness only.
        pm_vouched, vouch_record = review_trail_vouch.check_review_trail_vouch(
            foreign_trailer_shas, cwd=str(caller_worktree), session_id=own_session_id,
        )
        # Second evidence source (2026-07-31, C3,
        # docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md):
        # a per-SHA waiver the coverage gate already minted for THIS chain at
        # HALT (C2/C1). `own_session_id` IS the closing chain's identity at
        # this call site (see this function's own docstring) — no signature
        # change, no new parameter, no new derivation. This guard only
        # OBSERVES the waiver; it never mints one.
        chain_waived = chain_ancestry_waivers.chain_ancestry_waived_shas(
            str(caller_worktree), own_session_id,
        ) & foreign_trailer_shas
        waived = pm_vouched | chain_waived
        unvouched = foreign_trailer_shas - waived
        if not unvouched:
            # Fully covered: proceed. Persist a PERMANENT, idempotent
            # per-SHA PM-vouch waiver (state/review-trail/pm-vouches/<sha>.json)
            # for the PM-vouched subset only — this, not review_trail_vouch's
            # liveness-gated grant, is what the coverage READ side
            # (coordinator_core.coverage._narrow_foreign_session_scope)
            # consults, so the relaxation stays effective after the
            # writing/granting session closes (AC5 — see
            # _record_pm_vouch_waivers' own docstring for why). A
            # chain-ancestry waiver needs no equivalent step here: it was
            # already minted, permanently, by the gate at HALT (C2/C1), and
            # this guard never re-derives or re-mints it.
            if pm_vouched:
                _record_pm_vouch_waivers(caller_worktree, pm_vouched, own_session_id, vouch_record)
                logger.warning(
                    "review_trail.write: sha_range %r contains foreign-attributed "
                    "commit(s) %s written under a live, per-session PM-vouch grant "
                    "(note: %r) — persisted a permanent waiver under "
                    "state/review-trail/pm-vouches/ so the coverage read side "
                    "credits it after this session ends",
                    sha_range,
                    ", ".join(sorted(pm_vouched)),
                    (vouch_record or {}).get("note"),
                )
            if chain_waived:
                logger.info(
                    "review_trail.write: sha_range %r contains foreign-attributed "
                    "commit(s) %s covered by a gate-minted chain-ancestry waiver "
                    "for chain %s — no new waiver written here; the gate already "
                    "minted it permanently at HALT",
                    sha_range,
                    ", ".join(sorted(chain_waived)),
                    own_session_id,
                )
            return waived
        offending = sorted(unvouched)
        shown = offending[:_FOREIGN_SHA_DISPLAY_CAP]
        remainder = len(offending) - len(shown)
        remainder_note = f" (+{remainder} more)" if remainder else ""
        vouched_note = (
            f" ({len(waived)} of {len(foreign_trailer_shas)} offending SHA(s) ARE "
            "covered by a live PM-vouch grant or a gate-minted chain-ancestry "
            "waiver for this session, but not all — "
            "see `python3 coordinator/bin/review-trail-vouch-cli` to name "
            "the remainder explicitly)"
            if waived
            else ""
        )
        raise ForeignSessionRangeRefused(
            "review_trail.write: sha_range "
            f"{sha_range!r} contains commit(s) whose own Session-Id git "
            f"trailer names a different session: {', '.join(shown)}"
            f"{remainder_note}{vouched_note} — refusing to write a record "
            f"for them on that basis alone. {_FOREIGN_SESSION_UNDETERMINED_NOTE} "
            "Write per-slice records over this session's own commits instead, "
            "narrow sha_range to exclude the foreign commit(s) named above, "
            "or obtain a PM-vouch grant naming them explicitly "
            "(python3 coordinator/bin/review-trail-vouch-cli grant pm "
            "\"<verbatim PM utterance>\" --sha <sha> ...)."
        )

    known_scope_paths, saw_untrailered = _own_session_touched_paths_and_untrailered_flag(
        sha_range, own_session_id, caller_worktree,
    )
    unplaced_or_foreign = session_attribution.detect_foreign_commits(
        caller_worktree, own_session_id, sha_range, known_scope_paths,
    )
    contiguous = session_attribution.range_is_contiguous_suffix(
        caller_worktree, sha_range, unplaced_or_foreign,
    )
    if not unplaced_or_foreign and contiguous:
        scoping_method = (
            _SCOPING_METHOD_STARTED_AT_RANGE if saw_untrailered else _SCOPING_METHOD_TRAILER
        )
        logger.info(
            "review_trail.write: sha_range %r provably scoped to session %s "
            "via scoping_method=%s",
            sha_range,
            own_session_id,
            scoping_method,
        )
        return frozenset()

    raise ForeignSessionRangeRefused(
        "review_trail.write: sha_range "
        f"{sha_range!r} is genuinely ambiguous — it contains an untrailered "
        "commit the touched-path signal cannot place in this session's "
        "scope, or an in-scope commit set that is interleaved rather than "
        f"contiguous with this session's own commits. {_FOREIGN_SESSION_UNDETERMINED_NOTE} "
        "Supply an affirmatively-scoped, narrower sha_range for this "
        "session's own commits rather than the wide window."
    )


# ---------------------------------------------------------------------------
# Timestamp helpers (platform-aware — byte-parity with oracle)
# ---------------------------------------------------------------------------


def _compute_timestamp(_now_ns: Optional[int] = None) -> str:
    """Compute the filename timestamp component, replicating oracle platform logic.

    Oracle behavior:
        - Tries ``date -u +%Y-%m-%d-%H%M%S%N`` (26 chars on Linux; literal ``%N`` on macOS).
        - If the result contains literal ``%N`` OR is shorter than 20 chars → second-precision
          fallback: ``YYYY-MM-DD-HHMMSS`` (17 chars).
        - Otherwise (Linux nanosecond) → truncate to 23 chars:
          ``YYYY-MM-DD-HHMMSS`` (17) + 6 nanosecond digits = 23 chars.

    Python port:
        - Linux: ``time.time_ns()`` gives combined ns since epoch; derive seconds and ns
          component atomically from the same call.
        - Non-Linux: second-precision via ``datetime.utcnow()`` → 17-char string.

    ``_now_ns``: injectable for test isolation (pin nanosecond timestamp).
    """
    if platform.system() == "Linux":
        now_ns = _now_ns if _now_ns is not None else time.time_ns()
        seconds = now_ns // 1_000_000_000
        ns_remainder = now_ns % 1_000_000_000
        dt = datetime.datetime.fromtimestamp(seconds, datetime.timezone.utc)  # Review: code-reviewer — utcfromtimestamp deprecated Python 3.12+
        base = dt.strftime("%Y-%m-%d-%H%M%S")          # 17 chars
        ns_str = str(ns_remainder).zfill(9)[:6]         # 6-digit ns prefix
        return base + ns_str                             # 23 chars total
    # macOS / Windows: second-precision (oracle falls back when %N is literal).
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d-%H%M%S")  # 17 chars


# ---------------------------------------------------------------------------
# Session-id resolution
# ---------------------------------------------------------------------------


def _resolve_session_id(caller_worktree: Optional[Path] = None) -> Optional[str]:
    """Resolve session_id from env vars or sentinel file.

    Thin compatibility shim — delegates to
    ``coordinator_core.ops.session_context.resolve_current_session_id``.

    The three-tier chain (CLAUDE_SESSION_ID → CLAUDE_CODE_SESSION_ID → .current-session-id
    sentinel) is defined in ``session_context.py`` (C3 extraction) so it is shared with
    ``handoff.author_fork`` and any future op that needs session identity at call time.
    Callers of this module-local function continue to receive identical behavior.

    Extraction rationale: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C3
    """
    return resolve_current_session_id(caller_worktree)


# ---------------------------------------------------------------------------
# Workstream resolution
# ---------------------------------------------------------------------------


def _scan_workstream(handoffs_dir: Path, own_session_id: Optional[str]) -> Optional[str]:
    """Scan state/handoffs/*.md for a handoff attributable to THIS writing session
    and return its workstream field.

    2026-07-27 fix (docs/plans/2026-07-27-review-trail-scope-guard.md § C4): the
    prior contract picked the first handoff (in sorted filename order) whose
    status was not ``claimed``/``consumed``/``superseded`` — an unclaimed,
    ownerless handoff. On a shared branch with concurrent sessions that is an
    arbitrary peer's slug, not this session's, and it poisons the very "is this
    record mine?" check the workstream field exists to support. Attribution now
    requires a handoff whose ``claimed_by`` frontmatter names this session's own
    id. Without an ``own_session_id`` to compare against, no handoff can be
    attributed, so this returns None immediately — an honest null beats a
    confident wrong slug.

    Rejects slugs containing chars outside [A-Za-z0-9_-] → null (D9 reject-to-null).
    """
    if not handoffs_dir.is_dir():
        return None
    # NOTE: uses iterdir(), NOT glob("*.md") — Path.glob()'s selector silently
    # swallows PermissionError while walking (an unreadable handoffs_dir yields an
    # empty iterator, no exception), which would make "workstream: null" indistinguishable
    # from a genuinely-empty/no-active-handoff dir. iterdir() raises OSError as expected,
    # letting a permission-denied scan log distinctly from the no-active-handoff case.
    # This check runs regardless of own_session_id so an unreadable-dir warning still
    # fires even when there is nothing to attribute against.
    try:
        entries = list(handoffs_dir.iterdir())
    except OSError as exc:
        logger.warning(
            "review_trail.write: cannot scan handoffs dir %s for workstream resolution "
            "— %s; emitting null (distinct from a genuinely-empty handoffs dir)",
            handoffs_dir,
            exc,
        )
        return None
    if not own_session_id:
        return None
    md_files = sorted(p for p in entries if p.suffix == ".md" and p.is_file())
    for hfile in md_files:
        try:
            text = hfile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            print(f"skip: _scan_workstream: text = hfile.read_text(encoding=\"utf-8\", errors=\"replace\") failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue
        claimed_by = extract_frontmatter_scalar(text, "claimed_by")
        if claimed_by != own_session_id:
            continue
        ws = extract_frontmatter_scalar(text, "workstream")
        if ws:
            if _WORKSTREAM_SLUG_RE.search(ws):
                logger.warning(
                    "review_trail.write: workstream slug %r in %s contains invalid chars "
                    "(only [A-Za-z0-9_-] permitted); emitting null",
                    ws,
                    hfile,
                )
                continue
            return ws
    return None


def _resolve_workstream(
    workstream_param: Optional[str],
    caller_worktree: Optional[Path],
    own_session_id: Optional[str] = None,
) -> Optional[str]:
    """Resolve workstream slug for JSON record.

    Resolution order (mirrors oracle workstream resolution):
        1. ``workstream`` parameter (explicit override).
        2. ``COORDINATOR_REVIEW_WORKSTREAM`` env var.
        3. Scan ``{caller_worktree}/state/handoffs/*.md`` for a handoff whose
           ``claimed_by`` names ``own_session_id`` (see ``_scan_workstream`` —
           2026-07-27 fix, never a peer's handoff).
        4. null (None) — not resolvable.
    """
    # Review: code-reviewer — Finding 2 (P1): the explicit-override path reached
    # _build_json_record's unescaped f'"{workstream}"' interpolation with zero
    # validation, unlike the env-var and handoff-scan paths below — apply the
    # same _WORKSTREAM_SLUG_RE discipline here so all three resolution sources
    # share one validation path before reaching the JSON builder.
    if workstream_param is not None:
        if not workstream_param:
            return None
        if _WORKSTREAM_SLUG_RE.search(workstream_param):
            logger.warning(
                "review_trail.write: workstream param %r contains invalid chars "
                "(only [A-Za-z0-9_-] permitted); emitting null",
                workstream_param,
            )
            return None
        return workstream_param
    env_ws = os.environ.get(_COORDINATOR_REVIEW_WORKSTREAM_ENV, "").strip()
    if env_ws:
        if _WORKSTREAM_SLUG_RE.search(env_ws):
            logger.warning(
                "review_trail.write: COORDINATOR_REVIEW_WORKSTREAM %r contains invalid chars; "
                "emitting null",
                env_ws,
            )
            return None
        return env_ws
    if caller_worktree is not None:
        handoffs_dir = caller_worktree / "state" / "handoffs"
        return _scan_workstream(handoffs_dir, own_session_id)
    return None


# ---------------------------------------------------------------------------
# JSON record serialization (hand-built — byte-parity with oracle bash interpolation)
# ---------------------------------------------------------------------------


def _build_json_record(
    sha_range: str,
    reviewer: str,
    scope: str,
    scope_kind: str,
    verdict: str,
    diff_loc: int,
    session_id: str,
    workstream: Optional[str],
    reviewed_paths: Optional[List[str]] = None,
) -> str:
    """Hand-build the JSON record string matching oracle bash string interpolation exactly.

    Key order: sha_range, reviewer, scope, scope_kind, verdict, diff_loc, session_id, workstream.
    This ORDER is canonical and must not change (consumers parse by key name, but byte-parity
    requires identical serialization for the parity harness).

    ``reviewed_paths`` (docs/plans/2026-07-27-review-trail-scope-guard.md § C9) is a NINTH,
    optional key, appended ONLY when ``scope_kind == "diff"`` (a reviewed-path set is only
    meaningful against a diff-shaped record) — a ``scope_kind`` of ``plan``/``integration``
    OMITS the key entirely, regardless of what ``reviewed_paths`` was passed. When
    ``scope_kind == "diff"`` and ``reviewed_paths`` is ``None`` (not supplied), the key is
    still emitted, as JSON ``null`` — the same present-as-null discipline the ``workstream``
    key already uses.

    Oracle: ``printf '%s'`` writes without trailing newline. This function also omits the newline.

    Negative-spec: do NOT use json.dumps — it normalises separators and may differ from the
    oracle's hand-interpolated output (e.g. whitespace, unicode escaping, key ordering).
    """
    workstream_json = "null" if workstream is None else f'"{workstream}"'
    record = (
        f'{{"sha_range":"{sha_range}",'
        f'"reviewer":"{reviewer}",'
        f'"scope":"{scope}",'
        f'"scope_kind":"{scope_kind}",'
        f'"verdict":"{verdict}",'
        f'"diff_loc":{diff_loc},'
        f'"session_id":"{session_id}",'
        f'"workstream":{workstream_json}}}'
    )
    if scope_kind == "diff":
        if reviewed_paths is None:
            reviewed_paths_json = "null"
        else:
            reviewed_paths_json = "[" + ",".join(f'"{p}"' for p in reviewed_paths) + "]"
        record = record[:-1] + f',"reviewed_paths":{reviewed_paths_json}}}'
    elif reviewed_paths is not None:
        # Review: code-reviewer — Finding 6 (nit): a caller passing
        # reviewed_paths alongside a non-diff scope_kind gets it silently
        # dropped by design (C9) — log so a caller debugging "why isn't my
        # reviewed_paths showing up" gets a signal instead of nothing.
        logger.debug(
            "review_trail.write: reviewed_paths supplied but scope_kind=%r != "
            "'diff' — value dropped by design (reviewed_paths is diff-only, C9)",
            scope_kind,
        )
    return record


# ---------------------------------------------------------------------------
# Output path resolution
# ---------------------------------------------------------------------------


def _trail_dir(caller_worktree: Optional[Path]) -> Path:
    """Resolve the target review-trail directory.

    Precedence:
        1. ``REVIEW_TRAIL_OUTPUT_ROOT`` env var (test isolation) →
           ``{REVIEW_TRAIL_OUTPUT_ROOT}/review-trail/``.
        2. ``{caller_worktree}/state/review-trail/``.

    Raises ``RuntimeError`` when neither resolves (no env override and no caller_worktree).
    """
    override_root = os.environ.get(_REVIEW_TRAIL_OUTPUT_ROOT_ENV, "").strip()
    if override_root:
        return Path(override_root) / "review-trail"
    if caller_worktree is not None:
        return caller_worktree / "state" / "review-trail"
    raise RuntimeError(
        "review_trail.write: cannot resolve trail directory — "
        "caller_worktree not provided and REVIEW_TRAIL_OUTPUT_ROOT env var not set"
    )


# Hard cap on same-second-same-session collision retries (2026-07-27 fix). This is not
# a realistic ceiling — the live incident that motivated this fix was 9 collisions in
# one second — it exists only so a genuine pathology (e.g. a runaway caller loop) fails
# loud with a clear message instead of spinning forever.
_MAX_UNIQUE_SUFFIX_ATTEMPTS = 10_000


def _reserve_unique_trail_path(trail_dir: Path, base_filename: str, record_bytes: bytes) -> Path:
    """Atomically claim a not-yet-existing path for *base_filename* under *trail_dir*
    and write *record_bytes* into it in full — never overwriting an existing file
    (2026-07-27 fix for the DR-216 D2(i) last-write-wins clobber defect).

    On the first attempt, tries ``base_filename`` verbatim — this keeps the common
    (no-collision) case's filename byte-identical to the pre-fix format, so every
    existing caller/test that depends on the bare ``{ts}-{sid}.json`` shape is
    unaffected. Only on an actual same-timestamp+session_id_short collision does this
    fall back to ``{stem}-2.json``, ``{stem}-3.json``, ... — appended after the
    session_id segment, which ``_shared._validate_review_trail_file``'s
    ``_TIME_SEG_RE`` regex already tolerates (it only anchors on the leading digit run
    for the reviewed_at timestamp; anything after the first ``-`` is opaque to it).

    Uses ``os.open`` with ``O_CREAT | O_EXCL | O_WRONLY``, not ``os.replace``: ``O_EXCL``
    fails closed with ``FileExistsError`` when the target already exists, so a race
    between two writers landing the same candidate name is detected rather than one
    silently destroying the other's content — the exact failure mode this fix exists to
    close. Unlike a hardlink-based reservation, this needs no filesystem hardlink support
    (some SMB/network shares, exFAT/FAT32, OneDrive-synced Windows checkouts, and some
    FUSE/Docker bind mounts do not support ``os.link``, which would otherwise turn every
    write into a hard failure on those filesystems) and needs no separate tempfile at
    all — the target file is opened, written, and closed in one step. ``O_EXCL`` is
    atomic create-if-absent on POSIX and Windows alike (CPython maps it to ``CREATE_NEW``
    via ``_wsopen_s`` on nt).

    If the write itself fails after the exclusive create succeeds, the partially-written
    file is removed so a later retry (or a future write reusing this name after a
    process restart) never finds a corrupt half-written record.

    Raises ``RuntimeError`` if ``_MAX_UNIQUE_SUFFIX_ATTEMPTS`` candidates are all taken
    (see cap's docstring — a defensive ceiling, not an expected path).
    """
    assert base_filename.endswith(".json")
    stem = base_filename[: -len(".json")]

    candidate = trail_dir / base_filename
    attempt = 1
    while True:
        try:
            fd = os.open(str(candidate), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            attempt += 1
            if attempt > _MAX_UNIQUE_SUFFIX_ATTEMPTS:
                raise RuntimeError(
                    f"review_trail.write: could not reserve a unique path for "
                    f"{base_filename!r} after {_MAX_UNIQUE_SUFFIX_ATTEMPTS} attempts "
                    f"under {trail_dir} — this should never happen; investigate a "
                    f"runaway caller loop or a directory listing anomaly"
                )
            candidate = trail_dir / f"{stem}-{attempt}.json"
            continue

        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(record_bytes)
        except Exception:
            try:
                os.unlink(str(candidate))
            except OSError:
                print(
                    f"skip: _reserve_unique_trail_path: cleanup unlink of {candidate} "
                    f"failed after write error: {sys.exc_info()[1]}",
                    file=sys.stderr,
                )
            raise
        return candidate


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate(
    sha_range: str,
    reviewer: str,
    scope: str,
    verdict: str,
    diff_loc: int,
    scope_kind: str,
) -> None:
    """Validate required fields and enum values (mirrors oracle validation section).

    Raises ``ValueError`` with an oracle-parity message on any invalid input.
    """
    if not sha_range:
        raise ValueError("review_trail.write: --sha-range is required")
    # Review: code-reviewer — reject sha_range containing JSON-unsafe characters; direct
    #   interpolation into the hand-built JSON record would produce malformed output.
    if any(c in sha_range for c in ('"', "\\")):
        raise ValueError(
            f"review_trail.write: sha_range contains unsafe JSON character "
            f"(no '\"' or '\\\\' allowed): {sha_range!r}"
        )
    if not reviewer:
        raise ValueError("review_trail.write: --reviewer is required")
    if not scope:
        raise ValueError("review_trail.write: --scope is required")
    if not verdict:
        raise ValueError("review_trail.write: --verdict is required")
    if diff_loc < 0:
        raise ValueError(
            f"review_trail.write: diff_loc must be a non-negative integer, got {diff_loc}"
        )

    if reviewer not in _VALID_REVIEWERS:
        raise ValueError(
            f"review_trail.write: reviewer {reviewer!r} is invalid; "
            f"allowed: {' | '.join(sorted(_VALID_REVIEWERS))}"
        )
    if scope not in _VALID_SCOPES:
        raise ValueError(
            f"review_trail.write: scope {scope!r} is invalid; "
            f"allowed: {' | '.join(sorted(_VALID_SCOPES))}"
        )
    if verdict not in _VALID_VERDICTS:
        raise ValueError(
            f"review_trail.write: verdict {verdict!r} is invalid; "
            f"allowed: {' | '.join(sorted(_VALID_VERDICTS))}"
        )
    if scope_kind not in _VALID_SCOPE_KINDS:
        raise ValueError(
            f"review_trail.write: scope_kind {scope_kind!r} is invalid; "
            f"allowed: {' | '.join(sorted(_VALID_SCOPE_KINDS))}"
        )
    # scope_kind=diff requires sha_range to contain ".." (writer/consumer symmetry).
    if scope_kind == "diff" and ".." not in sha_range:
        raise ValueError(
            f"review_trail.write: scope_kind 'diff' requires sha_range to contain '..', "
            f"got: {sha_range!r}"
        )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_review_trail_entry(
    sha_range: str,
    reviewer: str,
    scope: str,
    verdict: str,
    diff_loc: int,
    *,
    scope_kind: str = "diff",
    session_id: Optional[str] = None,
    workstream: Optional[str] = None,
    reviewed_paths: Optional[List[str]] = None,
    caller_worktree: Optional[Path] = None,
    _timestamp: Optional[str] = None,
) -> dict:
    """Write one JSON entry to state/review-trail/.

    Byte-parity port of the bash oracle's write path.

    Parameters mirror oracle CLI flags (underscored); ``caller_worktree`` replaces
    oracle's cwd-based ``coordinator_state_root`` call (never daemon cwd).

    Parameters:
        sha_range   — e.g. ``abc1234..def5678``; required for scope_kind ``diff``.
        reviewer    — one of: code-reviewer, staff-eng, code-reviewer+staff-eng, waived,
                      ubt-compile, wsc-auto-adjudication.
        scope       — one of: chain, session, workstream-close-auto.
        verdict     — one of: ok, warn, blocked, waived, pending.
        diff_loc    — non-negative integer LOC count.
        scope_kind  — one of: diff, plan, integration (default: diff).
        session_id  — explicit session_id override; falls back to env/sentinel when empty/None.
        workstream  — explicit workstream slug; falls back to env/scan/null when None.
                      Pass empty string to suppress scanning and emit null explicitly.
        reviewed_paths — optional list of paths this record's review actually covered (e.g.
                      from ``freeze-review-diff.py --paths``). Persisted ONLY when
                      ``scope_kind == "diff"`` (docs/plans/2026-07-27-review-trail-scope-guard.md
                      § C9); ignored/omitted for ``plan``/``integration`` records.
        caller_worktree — the caller's repo worktree root (from main_worktree_root(repo_root)).
        _timestamp  — injectable timestamp string for test isolation (bypasses _compute_timestamp).

    Returns:
        {"out_path": str, "sha_range": str, "reviewer": str, "scope": str,
         "scope_kind": str, "verdict": str, "diff_loc": int,
         "session_id": str, "workstream": str | None,
         "reviewed_paths": list[str] | None}  (key present only when scope_kind == "diff")

    Raises:
        ValueError  — invalid/missing required field or enum value.
        RuntimeError — trail directory unresolvable (no caller_worktree and no env override).
    """
    # Validate inputs.
    _validate(sha_range, reviewer, scope, verdict, diff_loc, scope_kind)
    # Review: code-reviewer — Finding 5 (P2): validate entry type before the char
    # check (a non-string entry previously raised an unclear TypeError instead
    # of this module's usual clean ValueError), and screen control characters
    # too — an embedded newline/tab is a legal git filename character but still
    # breaks the hand-built JSON just as much as an unescaped quote/backslash.
    if reviewed_paths is not None:
        for idx, p in enumerate(reviewed_paths):
            if not isinstance(p, str):
                raise ValueError(
                    f"review_trail.write: reviewed_paths[{idx}] must be a string, "
                    f"got {type(p).__name__}: {p!r}"
                )
            if any(c in p for c in ('"', "\\")) or any(ord(c) < 0x20 for c in p):
                raise ValueError(
                    f"review_trail.write: reviewed_paths[{idx}] contains unsafe JSON "
                    f"character (no '\"', '\\\\', or control characters allowed): {p!r}"
                )

    # Resolve session_id (before sha_range concretization — an unresolvable
    # session_id is the more fundamental failure and should surface first,
    # rather than being masked by a git-resolution error on a caller_worktree
    # that happens not to be a git repo, e.g. in isolated unit tests).
    resolved_session_id: Optional[str] = None
    if session_id:
        resolved_session_id = session_id
    else:
        resolved_session_id = _resolve_session_id(caller_worktree)
    if not resolved_session_id:
        raise ValueError(
            "review_trail.write: could not resolve session_id — "
            "set CLAUDE_CODE_SESSION_ID or pass session_id explicitly"
        )
    # Review: code-reviewer — reject session_id containing JSON-unsafe characters; same
    #   injection risk as sha_range (both are interpolated directly into the hand-built record).
    if any(c in resolved_session_id for c in ('"', "\\")):
        raise ValueError(
            f"review_trail.write: session_id contains unsafe JSON character "
            f"(no '\"' or '\\\\' allowed): {resolved_session_id!r}"
        )

    # Concretize any symbolic ref (HEAD, a branch, ...) in sha_range to its
    # current concrete SHA — persisting a literal "HEAD" lets the record's
    # certified range silently grow at coverage-gate READ time as new commits
    # land (see _resolve_symbolic_range docstring).
    sha_range = _resolve_symbolic_range(sha_range, caller_worktree)

    # Foreign-session scope guard (only meaningful for a diff-shaped git
    # range, and only when a real repo exists to check against — no
    # caller_worktree is the same test-isolation no-op contract
    # _resolve_symbolic_range documents above).
    if scope_kind == "diff" and caller_worktree is not None:
        _guard_foreign_session_range(sha_range, resolved_session_id, caller_worktree)

    # Resolve workstream.
    resolved_workstream = _resolve_workstream(workstream, caller_worktree, resolved_session_id)

    # Compute timestamp + filename.
    ts = _timestamp if _timestamp is not None else _compute_timestamp()
    session_id_short = resolved_session_id[:8]
    filename = f"{ts}-{session_id_short}.json"

    # Resolve trail dir and ensure it exists.
    trail_dir = _trail_dir(caller_worktree)
    trail_dir.mkdir(parents=True, exist_ok=True)

    # Build JSON record (hand-serialized — no json.dumps, byte-parity with oracle).
    json_record = _build_json_record(
        sha_range=sha_range,
        reviewer=reviewer,
        scope=scope,
        scope_kind=scope_kind,
        verdict=verdict,
        diff_loc=diff_loc,
        session_id=resolved_session_id,
        workstream=resolved_workstream,
        reviewed_paths=reviewed_paths,
    )
    # Encode to bytes (ASCII — all values are validated ASCII-safe).
    record_bytes = json_record.encode("utf-8")

    # Atomic, never-clobbering write: O_CREAT|O_EXCL|O_WRONLY claims a reserved unique
    # name and writes the full record directly into it (DR-213 D3 additive-create
    # discipline; DR-216 D2(i)'s os.replace-overwrites last-write-wins design SUPERSEDED
    # 2026-07-27 — see the module docstring's incident note for why).
    # No trailing newline — oracle uses printf '%s' (not echo).
    out_path = _reserve_unique_trail_path(trail_dir, filename, record_bytes)

    result = {
        "out_path": str(out_path),
        "sha_range": sha_range,
        "reviewer": reviewer,
        "scope": scope,
        "scope_kind": scope_kind,
        "verdict": verdict,
        "diff_loc": diff_loc,
        "session_id": resolved_session_id,
        "workstream": resolved_workstream,
    }
    if scope_kind == "diff":
        result["reviewed_paths"] = reviewed_paths
    return result


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


@register_op("review_trail.write")
async def _review_trail_write_handler(
    params: dict, repo_root: Optional[Path] = None
) -> dict:
    """JSON-RPC ``review_trail.write`` handler — write a review-trail JSON entry.

    MUTATING (writes one JSON record to ``state/review-trail/``; DR-216 carve-out).
    Delegates blocking FS I/O to ``asyncio.to_thread`` (DR-216 D3 async-loop mandate).

    ``repo_root`` receives ``git_common_dir(caller_worktree)`` via
    ``_OP_KEY_SCOPE: common_dir`` (ipc.py). Handler calls ``main_worktree_root(repo_root)``
    to derive the caller's worktree root before any path construction.

    Required params:
        sha_range  (str)  — e.g. ``abc1234..def5678``.
        reviewer   (str)  — one of: code-reviewer, staff-eng, code-reviewer+staff-eng, waived,
                      ubt-compile, wsc-auto-adjudication.
        scope      (str)  — one of: chain, session, workstream-close-auto.
        verdict    (str)  — one of: ok, warn, blocked, waived, pending.
        diff_loc   (int)  — non-negative LOC count (also accepted as str, cast to int).

    Optional params:
        scope_kind (str)  — one of: diff, plan, integration (default: diff).
        workstream (str)  — workstream slug override; null → scan/env/fallback.
        reviewed_paths (list[str]) — reviewed-path set (only persisted when scope_kind
                      is ``diff``; see ``write_review_trail_entry``'s docstring).

    Returns:
        {"out_path": str, "sha_range": str, "reviewer": str, "scope": str,
         "scope_kind": str, "verdict": str, "diff_loc": int,
         "session_id": str, "workstream": str | None,
         "reviewed_paths": list[str] | None}  (key present only when scope_kind == "diff")

    On unresolvable session_id: logs ERROR and raises ``ValueError``
    (callers must not invoke this handler without an active session).
    """
    # Derive caller's worktree root from the socket-authoritative common_dir.
    caller_worktree: Optional[Path] = None
    if repo_root is not None:
        caller_worktree = main_worktree_root(repo_root)

    # Resolve session_id in daemon context (env is the primary source).
    session_id = (
        os.environ.get(_CLAUDE_SESSION_ID_ENV, "").strip()
        or os.environ.get(_CLAUDE_CODE_SESSION_ID_ENV, "").strip()
    )

    # Cast diff_loc to int (params may carry it as a string from CLI serialization).
    raw_diff_loc = params.get("diff_loc", 0)
    try:
        diff_loc = int(raw_diff_loc)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"review_trail.write: diff_loc must be a non-negative integer, got {raw_diff_loc!r}"
        ) from exc

    raw_reviewed_paths = params.get("reviewed_paths")
    if raw_reviewed_paths is not None and not isinstance(raw_reviewed_paths, list):
        raise ValueError(
            "review_trail.write: reviewed_paths must be a list of strings when provided, "
            f"got {raw_reviewed_paths!r}"
        )

    result = await asyncio.to_thread(
        write_review_trail_entry,
        sha_range=params.get("sha_range", ""),
        reviewer=params.get("reviewer", ""),
        scope=params.get("scope", ""),
        verdict=params.get("verdict", ""),
        diff_loc=diff_loc,
        scope_kind=params.get("scope_kind", "diff"),
        session_id=session_id if session_id else None,
        workstream=params.get("workstream"),
        reviewed_paths=raw_reviewed_paths,
        caller_worktree=caller_worktree,
    )
    return result
