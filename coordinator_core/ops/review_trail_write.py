"""
coordinator_core.ops.review_trail_write — per-session review-trail entry writer.

Purpose: writes a JSON review-trail entry (additive-create) under
``<worktree>/state/review-trail/<timestamp>-<session_id_short>.json``.

Port of: coordinator-write-review-trail.sh (coordinator-claude 30f4c5fc, 2026-07-19).

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
    4. Raises ``ValueError`` if not resolved (parity with oracle exit 3).

    (KS-2, 2026-08-07: the former tier-4 sentinel file read —
    ``{caller_worktree}/.git/coordinator-sessions/.current-session-id`` — was removed.
    It was documented last-writer-wins under this fleet's ~18 concurrent sessions on
    one shared worktree, and its sole writer, ``session-init.py``, was deleted
    2026-07-15. Do not restore it; see ``session_context.py`` for the full rationale.)

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

Spec backlink: pln-strang-10-residual-writer-clus-b67ff8 § C3
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
      logic) — daemon context always supplies session_id via env. (The sentinel-file
      tier itself was removed KS-2 2026-08-07 — see module docstring.)
    - NO persistence of the write-time zero-chain-terminal-credit diagnostic
      (``chain_terminal_zero_credit_warning``) into the on-disk JSON record —
      advisory-only, returned in the op result, never a ninth/tenth record key
      (see the "Write-time zero-chain-terminal-credit diagnostic" section).
    - NO turning an accepted write into a failure on a predicted zero-credit
      shape — the diagnostic never raises and never blocks; a record written
      for the human paper trail stays legitimate even when it provably
      discharges nothing at the chain-terminal path.
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
from coordinator_core.win_portability import no_console_creationflags
import time
from pathlib import Path
from typing import FrozenSet, List, Optional

from coordinator_core import chain_ancestry_waivers, chain_attribution, session_attribution
from coordinator_core.session_attribution import GitLogFailed
from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.ipc import register_op
from coordinator_core.ops._fm_util import extract_frontmatter_scalar
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.session_context import resolve_current_session_id

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
        "em-verified",
    }
)

# ---------------------------------------------------------------------------
# reviewer_evidence — refuse an unevidenced reviewer verdict
# ---------------------------------------------------------------------------
#
# state/bug-backlog/2026-08-10-coordinator-write-review-trail-accepts-a-295d3cd80d13.yaml.
# Prior to this, --reviewer/--verdict were free parameters: nothing correlated
# them with any artifact showing a review occurred. Demonstrated against the
# EM itself on 2026-08-10 (see that bug-backlog record's `evidence` field): two
# verdict-"ok" records were written for commits no reviewer had seen, and the
# CLI accepted both. This section makes the wrong record unwritable rather
# than merely forbidden in prose (the workstream-complete skill's existing,
# unenforced rule).
#
# Reviewer values split into three evidence classes:
#
#   DELEGATE  ({code-reviewer, staff-eng, code-reviewer+staff-eng,
#              ubt-compile}) -- a real reviewer actually ran. Evidence MUST be
#              one of:
#                (a) a sidecar path that exists on disk at write time
#                    (state/subagent-share/... or state/plan-sidecars/...,
#                    resolved relative to caller_worktree), or
#                (b) a dispatch id resolvable in THIS session's own
#                    `.git/coordinator-sessions/<sid>/dispatched-agents.txt`.
#              Both are artifacts a reviewer dispatch actually produces --
#              typing a string proves nothing; a resolvable path/id does.
#
#   WAIVED    ({waived}) -- no reviewer is coming, by deliberate decision.
#              Evidence is a free-text justification, held to a floor
#              (`_MIN_JUSTIFICATION_CHARS`) high enough that a one-word
#              placeholder ("later", "n/a") fails it -- this is the "explicit
#              waived form carrying its own justification" the bug-backlog
#              record calls for. This is deliberately CHEAPER than resolving a
#              real sidecar/dispatch id (typing a sentence vs. actually
#              dispatching a reviewer), which is the correct ordering: waiving
#              review is a real, lighter-weight decision than performing one.
#              It is NOT free, though -- an empty or trivial value refuses --
#              so it does not become the road every session takes at 22:40
#              purely because refusing costs more than typing a placeholder.
#              (Design question (b) in the bug-backlog record: whatever it
#              costs to use, it must cost more than the current zero. A
#              justification floor is the cheapest defensible instrument
#              available without inventing a second-party approval mechanism
#              this repo has nowhere to source from -- see that record's
#              design-question (b) for the fuller reasoning this trades off.)
#
#   EM-VERIFIED ({em-verified}) -- design question (a) in the bug-backlog
#              record: an EM that reads a diff, runs the code, and satisfies
#              itself has done something real, but it is not a delegate
#              review (forcing it into `code-reviewer` is the exact
#              falsification this whole section exists to close) and it
#              overstates nothing to also call it `waived` (a waiver asserts
#              NO verification happened; this asserts real, if self-graded,
#              verification did). It gets its OWN reviewer value and its own
#              evidence floor -- concrete checks performed, held to the same
#              justification floor as `waived` -- rather than being folded
#              into either existing bucket. Downstream consumers that weight
#              trust by `reviewer` can and should treat `em-verified` as
#              weaker than a delegate reviewer without having to first infer
#              that from a `waived` record's absence of any real verification.
#
#   MACHINE-PROVENANCE ({wsc-auto-adjudication}) -- pre-existing, narrower
#              carve-out. Not a human typing --reviewer on a CLI: it is the
#              old wsc_commit.py machine auto-source sentinel this module's
#              own docstring already documents as distinct from a human/CI
#              reviewer name (module docstring, `_VALID_REVIEWERS` comment).
#              Left EXEMPT from the evidence requirement -- accepted, narrow,
#              named gap: nothing here stops a caller from typing
#              `--reviewer wsc-auto-adjudication` by hand to dodge the
#              evidence gate. Closing that fully needs a caller-identity
#              signal this op has no way to source (there is no
#              machine-vs-human provenance channel on this call), so it is
#              named rather than silently left implicit -- see this comment
#              instead of assuming it was overlooked.
#
# Existing on-disk records are UNCHANGED and keep validating: this check runs
# only at WRITE time, on the write path, and never re-validates or rejects a
# record already on disk (no on-disk schema change, no new required key on
# read).
#
# Chain-ancestry waivers (`state/review-trail/chain-ancestry-waivers/`,
# `coordinator_core.chain_ancestry_waivers`) are NOT this mechanism -- they
# are a separate, per-SHA, gate-minted provenance-not-discharge marker
# consumed by `_guard_foreign_session_range` above, orthogonal to whether a
# reviewer's OWN verdict is evidenced. Design question (c) in the bug-backlog
# record: do not conflate the two; this section adds nothing to, and takes
# nothing from, that mechanism.

_DELEGATE_REVIEWERS = frozenset(
    {"code-reviewer", "staff-eng", "code-reviewer+staff-eng", "ubt-compile"}
)
_JUSTIFICATION_REVIEWERS = frozenset({"waived", "em-verified"})
_EVIDENCE_EXEMPT_REVIEWERS = frozenset({"wsc-auto-adjudication"})

#: Floor for a `waived`/`em-verified` free-text justification -- high enough
#: that "n/a", "later", "skip" (all comfortably under this) fail, without
#: policing content this op has no authority to grade for substance.
_MIN_JUSTIFICATION_CHARS = 20

#: Sidecar-shaped path prefixes a delegate-reviewer evidence path must fall
#: under -- mirrors the two locations this repo's own subagent dispatch
#: machinery actually writes to (§ Run-Report Sidecar / plan-sidecar
#: conventions), so an arbitrary existing file elsewhere in the tree cannot
#: be pointed at as if it were review evidence.
_SIDECAR_EVIDENCE_PREFIXES = ("state/subagent-share/", "state/plan-sidecars/")

#: Opt-in enforcement switch (default OFF — advisory only). See
#: `_verify_reviewer_evidence`'s Negative-spec block for why the default is
#: advisory and what flips it.
_REVIEW_TRAIL_EVIDENCE_ENFORCE_ENV = "COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE"

_TRUTHY = frozenset({"1", "true", "yes"})


def _evidence_enforcement_enabled() -> bool:
    """True iff `COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE` is set to a
    truthy value (case-insensitive `1`/`true`/`yes`). Default (unset, or any
    other value) is False — advisory-only. See `_verify_reviewer_evidence`'s
    Negative-spec block.
    """
    return os.environ.get(_REVIEW_TRAIL_EVIDENCE_ENFORCE_ENV, "").strip().lower() in _TRUTHY


def _dispatched_agents_file(caller_worktree: Path, session_id: str) -> Path:
    """Path to THIS session's own dispatch ledger — never a peer session's."""
    return (
        caller_worktree
        / ".git"
        / "coordinator-sessions"
        / session_id
        / "dispatched-agents.txt"
    )


def _dispatch_id_resolvable(
    dispatch_id: str, caller_worktree: Path, session_id: str
) -> bool:
    """True iff *dispatch_id* exactly matches column 1 (the ``agent_id``
    dedup key) of some row in this session's own ``dispatched-agents.txt``
    ledger. Fails safe (False) on any read error — an unreadable ledger
    proves nothing, so it must never be read as evidence.

    Negative-spec: this is a field-exact match against column 1, NOT a
    substring test against the raw line (``track_dispatched_agents.py``'s
    row shape is tab-delimited ``<agentId>\\t<model>\\t<subagent_type>\\t
    <unix-epoch>``, with column 1 the sole dedup key — see that module's
    ``_process_dispatched_sync`` docstring). A substring test would let a
    short or generic evidence value resolve by incidentally appearing
    inside an unrelated row's model name, subagent_type, or timestamp
    column, or inside a longer agent id it is merely a prefix/infix of —
    exactly the false-positive gap Finding P2 (state/subagent-share/
    db6e6193-1773-4bbc-a13d-444606ccbfc2/coordinatorcode-reviewer-5086cf69.md)
    named against the module's own stated design goal ("typing a string
    proves nothing; a resolvable path/id does").
    """
    ledger = _dispatched_agents_file(caller_worktree, session_id)
    try:
        text = ledger.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    dispatch_id = dispatch_id.strip()
    if not dispatch_id:
        return False
    for line in text.splitlines():
        agent_id_col = line.split("\t", 1)[0]
        if agent_id_col == dispatch_id:
            return True
    return False


def _resolve_sidecar_evidence_path(evidence: str, caller_worktree: Path) -> Optional[Path]:
    """Return the on-disk sidecar path *evidence* names, or ``None`` if it
    does not resolve. Shared resolution logic behind ``_sidecar_evidence_exists``
    (existence check only) and ``_derive_execution_basis_from_sidecar`` (C2 —
    also needs the resolved path to read its content). Rejects any path
    outside ``_SIDECAR_EVIDENCE_PREFIXES``, and any absolute/``..``-escaping
    path, before ever touching the filesystem.
    """
    normalized = evidence.strip().replace("\\", "/").lstrip("/")
    if not normalized.startswith(_SIDECAR_EVIDENCE_PREFIXES):
        return None
    if ".." in Path(normalized).parts:
        return None
    candidate = caller_worktree / normalized
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


def _sidecar_evidence_exists(evidence: str, caller_worktree: Path) -> bool:
    """True iff *evidence* names a sidecar-shaped path (see
    ``_SIDECAR_EVIDENCE_PREFIXES``) that exists on disk under
    ``caller_worktree``. Rejects any path outside those prefixes, and any
    absolute/``..``-escaping path, before ever touching the filesystem —
    evidence must live where this repo's own dispatch machinery actually
    writes it, not merely be a path that happens to resolve.
    """
    return _resolve_sidecar_evidence_path(evidence, caller_worktree) is not None


# ---------------------------------------------------------------------------
# execution_basis derivation from the reviewer's own sidecar (C2,
# docs/plans/2026-08-11-review-trail-carries-execution-basis.md § C2)
# ---------------------------------------------------------------------------
#
# C1 alone would accept ``execution_basis="executed"`` from anyone typing it
# on the CLI -- the exact shape of falsification the "reviewer_evidence"
# section above already closes for the ``reviewer`` field. This section
# closes it for ``execution_basis`` by deriving the value from the DELEGATE
# reviewer's OWN sidecar (the artifact ``reviewer_evidence`` already proves
# exists) instead of trusting a caller-typed flag, wherever that sidecar
# carries an answer. Follows the same DELEGATE/WAIVED/EM-VERIFIED evidence-
# class shape as ``_verify_reviewer_evidence`` -- this only ever fires for a
# DELEGATE reviewer whose ``reviewer_evidence`` resolves to a sidecar file.

#: Section heading the sidecar templates in
#: ``coordinator_core.subagent_sandbox.provision_report`` scaffold
#: (``## Execution capability``), and the literal read-only fallback string
#: coordinator-claude's producer side landed at ``2cb87e464``. Matched case-sensitively --
#: this is a fixed contract string, not free prose to fuzzy-match.
_EXECUTION_CAPABILITY_HEADING_RE = re.compile(
    r"^##\s+Execution capability\s*$", re.MULTILINE,
)
_NEXT_HEADING_RE = re.compile(r"^##\s+\S", re.MULTILINE)
_READ_ONLY_FALLBACK_TEXT = "none — this verdict rests on reading only"

#: Strips an HTML comment block (the scaffolded instructional placeholder,
#: e.g. ``<!-- Name what you actually ran ... -->``) so an untouched
#: template section reads as empty rather than as "content".
_HTML_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)


def _derive_execution_basis_from_sidecar_text(text: str) -> Optional[str]:
    """Parse a sidecar's ``## Execution capability`` section and return
    ``"executed"`` | ``"read-only"`` | ``None`` (section absent, or present
    but carrying no real content once the scaffolded HTML-comment
    placeholder is stripped -- i.e. unparseable/not yet filled in).

    Rule 4 (§ C2): absent-or-unparseable is deliberately NOT a refusal --
    the caller of this function treats ``None`` as "no derivation available"
    and this chunk's own asymmetry rule (see the write-path call site)
    decides what happens next. This is expected to be the OVERWHELMINGLY
    common case against today's sidecar corpus (state/audits/2026-08-11-
    review-trail-execution-basis-derivability.md's 0% derivable rate is
    entirely explained by the section not existing in this repo's templates
    until C6 landed it) -- not a defect, just the current corpus shape.
    """
    heading_match = _EXECUTION_CAPABILITY_HEADING_RE.search(text)
    if heading_match is None:
        return None
    section_start = heading_match.end()
    next_heading_match = _NEXT_HEADING_RE.search(text, section_start)
    section_end = next_heading_match.start() if next_heading_match else len(text)
    section_text = text[section_start:section_end]
    stripped = _HTML_COMMENT_RE.sub("", section_text).strip()
    if not stripped:
        return None
    if stripped == _READ_ONLY_FALLBACK_TEXT:
        return "read-only"
    return "executed"


class _SidecarUndetermined:
    """Sentinel type for Rule 4 (§ C2 correction pass): a reviewer sidecar
    DID resolve to a file on disk, but its ``## Execution capability``
    section is absent, empty (scaffold-comment-only), or otherwise
    unparseable. Distinct from plain ``None`` (Rule 3: no sidecar resolved
    at all -- not applicable) precisely so
    ``write_review_trail_entry`` can tell the two apart and apply the
    correct rule: Rule 3 leaves the caller's ``execution_basis`` value
    standing; Rule 4 forces it to be OMITTED from the written record.

    Chosen over a bare string sentinel (e.g. ``"undetermined"``) because a
    string could collide with a real, currently-unenumerated
    ``execution_basis`` value in the future; a module-private singleton
    instance cannot. Chosen over a ``(state, value)`` tuple because the
    three-way return stays a single value callers can `is`-compare, which
    reads closer to this module's existing ``Optional[...]`` idioms than
    unpacking a tuple at every call site would.
    """


#: The one instance of `_SidecarUndetermined` ever constructed -- callers
#: compare against this object with `is`, never construct their own.
_SIDECAR_UNDETERMINED = _SidecarUndetermined()


def _derive_execution_basis_from_sidecar(
    reviewer: str, reviewer_evidence: Optional[str], caller_worktree: Optional[Path]
) -> "Optional[str] | _SidecarUndetermined":
    """Entry point for C2 derivation. Three-way return (§ C2 correction pass):

      - ``None`` -- Rule 3, NOT APPLICABLE: *reviewer* is not a DELEGATE
        reviewer, or *reviewer_evidence*/``caller_worktree`` is missing, or
        *reviewer_evidence* does not resolve to an existing sidecar file on
        disk. There is nothing to derive FROM.
      - `_SIDECAR_UNDETERMINED` -- Rule 4, UNDETERMINED: a sidecar file DID
        resolve, but it became unreadable between resolution and read (a
        narrow race with the file existing at `is_file()` time and vanishing
        or erroring on `read_text`), or its ``## Execution capability``
        section is absent/empty/unparseable per
        `_derive_execution_basis_from_sidecar_text`. Either way a sidecar
        exists and attests nothing -- distinct from "no sidecar" (Rule 3).
      - ``"executed"`` / ``"read-only"`` -- Rule 1/2, DERIVED: a real value
        was read from the section.

    A dispatch-id-shaped evidence value (not a sidecar path) always returns
    `None` (Rule 3) -- there is no sidecar file to read a section out of.
    """
    if reviewer not in _DELEGATE_REVIEWERS:
        return None
    if not reviewer_evidence or caller_worktree is None:
        return None
    sidecar_path = _resolve_sidecar_evidence_path(reviewer_evidence, caller_worktree)
    if sidecar_path is None:
        return None
    try:
        text = sidecar_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return _SIDECAR_UNDETERMINED
    derived = _derive_execution_basis_from_sidecar_text(text)
    if derived is None:
        return _SIDECAR_UNDETERMINED
    return derived


def _compose_reviewer_evidence_message(
    reviewer: str,
    verdict: str,
    reviewer_evidence: Optional[str],
    caller_worktree: Optional[Path],
    resolved_session_id: str,
) -> Optional[str]:
    """Return the diagnostic message for an unevidenced/unresolvable
    ``reviewer`` claim, or ``None`` if the claim is adequately evidenced (or
    exempt/not-yet-applicable).

    Single message-composition site shared by the enforcing (raise) and
    advisory (warn-to-stderr) paths in `_verify_reviewer_evidence` — so a
    future edit to the wording only has one place to land, not two that can
    drift apart.
    """
    if reviewer in _EVIDENCE_EXEMPT_REVIEWERS:
        return None
    if verdict == "pending" and reviewer not in _JUSTIFICATION_REVIEWERS:
        return None

    evidence = (reviewer_evidence or "").strip()

    if reviewer in _JUSTIFICATION_REVIEWERS:
        if len(evidence) < _MIN_JUSTIFICATION_CHARS:
            return (
                f"review_trail.write: reviewer={reviewer!r} requires "
                "--reviewer-evidence carrying a real justification "
                f"(at least {_MIN_JUSTIFICATION_CHARS} characters after "
                f"trimming; got {len(evidence)}) — a typed verdict with no "
                "correlating artifact is the exact falsification this gate "
                "exists to refuse. State concretely why no delegate "
                "reviewer is needed (waived) or what was actually checked "
                "(em-verified)."
            )
        return None

    if reviewer in _DELEGATE_REVIEWERS:
        if not evidence:
            return (
                f"review_trail.write: reviewer={reviewer!r} requires "
                "--reviewer-evidence naming either an existing sidecar path "
                "(state/subagent-share/... or state/plan-sidecars/...) or a "
                "dispatch id resolvable in this session's own "
                "dispatched-agents.txt — nothing here correlates a typed "
                "reviewer name with an artifact showing a review occurred "
                "otherwise."
            )
        if caller_worktree is None:
            return (
                f"review_trail.write: reviewer={reviewer!r} evidence "
                f"{evidence!r} cannot be verified — no caller_worktree to "
                "resolve it against."
            )
        if _sidecar_evidence_exists(evidence, caller_worktree):
            return None
        if _dispatch_id_resolvable(evidence, caller_worktree, resolved_session_id):
            return None
        return (
            f"review_trail.write: reviewer={reviewer!r} evidence "
            f"{evidence!r} does not resolve — it is neither an existing "
            "sidecar path under state/subagent-share/ or "
            "state/plan-sidecars/, nor a dispatch id present in this "
            "session's own .git/coordinator-sessions/"
            f"{resolved_session_id}/dispatched-agents.txt. Refusing to "
            "persist a reviewer claim nothing correlates to an actual "
            "review."
        )

    # Unreachable given `_VALID_REVIEWERS` gates `reviewer` before this runs
    # (defensive — a new enum value added without updating the three sets
    # above must fail loud, not silently pass unevidenced).
    return (
        f"review_trail.write: reviewer={reviewer!r} has no evidence "
        "classification (delegate / justification / exempt) — refusing to "
        "write rather than silently accepting an unclassified reviewer."
    )


def _verify_reviewer_evidence(
    reviewer: str,
    verdict: str,
    reviewer_evidence: Optional[str],
    caller_worktree: Optional[Path],
    resolved_session_id: str,
) -> None:
    """Refuse to write a record whose ``reviewer`` claim is unevidenced —
    unless enforcement is off (the default), in which case log an advisory
    and let the write proceed.

    See the module-level "reviewer_evidence" comment block above for the
    full design (evidence classes, why each is shaped as it is, and the
    named machine-provenance carve-out). When enforcing, raises
    ``ValueError`` on any unevidenced/unresolvable claim; never mutates
    ``reviewer`` or ``verdict``, and never persists ``reviewer_evidence``
    itself to the on-disk record (a write-time gate only, not a new schema
    key).

    ``verdict == "pending"`` is EXEMPT from the delegate-reviewer evidence
    check (found live during this change: ``freeze-review-diff.py`` writes
    an open-loop ``reviewer="code-reviewer", verdict="pending"`` record at
    review-freeze time, before any reviewer has run — the whole point of
    that record is to OPEN a review loop, not assert one already closed, so
    there is no artifact to require yet. A ``pending`` record makes no
    completed-review claim for anything to falsify; only a terminal verdict
    (ok/warn/blocked) or an explicit ``waived`` does, and both stay fully
    gated below.

    Negative-spec: this gate is advisory-by-default, NOT enforcing-by-default,
    controlled by ``COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE`` (unset/falsy
    → advisory; truthy `1`/`true`/`yes` → enforcing). Shipping this
    enforcing-by-default broke the entire fleet on 2026-08-10 for ~10
    minutes: `coordinator/bin/wsc-coverage-gate-runner.py`'s `write-trail`
    subcommand had no `--reviewer-evidence` flag at all, so every peer
    session's `/workstream-complete` that wrote a trail record failed at
    once, and the EM reverted the change to a scratchpad patch. Do not flip
    the env var's default to truthy until (a) `wsc-coverage-gate-runner.py
    write-trail` forwards `--reviewer-evidence` at every call site that needs
    it, and (b) the full test suite is green with enforcement on. Until
    then, an operator who wants to test enforcement locally sets
    `COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE=1`.
    """
    message = _compose_reviewer_evidence_message(
        reviewer, verdict, reviewer_evidence, caller_worktree, resolved_session_id
    )
    if message is None:
        return
    if _evidence_enforcement_enabled():
        raise ValueError(message)
    logger.warning("review_trail.write advisory (would refuse if enforcing): %s", message)


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
            **no_console_creationflags(),
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


def _reject_empty_sha_range(sha_range: str, caller_worktree: Optional[Path]) -> None:
    """Refuse to write a record whose diff-shaped ``sha_range`` resolves to
    ZERO commits (state/bug-backlog/2026-08-08-cmd-exe-shim-eats-the-caret-
    in-a-git-rev-6679bf76eb8a.yaml).

    Only acts on a range that actually contains ``..``/``...`` (the same
    diff shape ``_resolve_symbolic_range`` targets) — a bare scope_kind=plan/
    integration token is never a range and is left untouched. A no-op
    without a real ``caller_worktree`` (test-isolation contract, matching
    every other git-backed check in this module): there is no repo to run
    ``git rev-list`` against.

    Raises ``ValueError`` on an empty (zero-commit) range — refusing to
    persist a record that discharges nothing is the point of this check.
    Raises ``ValueError`` on a range git cannot resolve at all too (a
    DIFFERENT failure than "empty" — e.g. a genuinely bogus/unknown SHA —
    but equally unsafe to persist silently); the two cases get distinct
    messages so an operator isn't misled about which happened.

    CALL-SITE ORDERING (restored 2026-08-10, docs/plans/2026-08-10-caret-
    fix-on-the-wrong-launcher.md § C2 — read this before moving the call):
    ``c4a8e5e86`` originally called this function immediately after
    ``_resolve_symbolic_range``, AHEAD of ``_guard_foreign_session_range``.
    That ordering made this function's own ``git rev-list`` failure the
    FIRST thing an unresolvable range hit, so it raised a bare ``ValueError``
    before the foreign-session guard ever ran — pre-empting that guard's own
    ``GitLogFailed``-derived exception contract for the identical input, and
    turning a passing test red. ``052996621`` reverted the whole function
    for that reason. The fix is not "don't restore it" — it is call-site
    ordering: ``write_review_trail_entry`` now invokes this function AFTER
    ``_guard_foreign_session_range`` has already run (and not raised), so
    the guard's exception contract still fires first for any range that
    FAILS TO RESOLVE, and this backstop is only reached for a range that
    RESOLVES and resolves to zero commits — a strictly narrower condition
    than "the guard didn't raise." Do not move this call back ahead of the
    guard.
    """
    if caller_worktree is None:
        return
    sep = "..." if "..." in sha_range else (".." if ".." in sha_range else None)
    if sep is None:
        return
    # Same test-isolation no-op contract as `_resolve_symbolic_range` /
    # `_guard_foreign_session_range`: a `caller_worktree` that is not itself
    # inside a git work tree (e.g. a plain tmp dir a unit test passes to
    # exercise write-path logic with synthetic SHAs) has no real commit
    # history to check emptiness against — skip rather than hard-fail on
    # `git rev-list` erroring out against a non-repo.
    is_work_tree_rc, _out, _err = _git_runner(
        ["git", "rev-parse", "--is-inside-work-tree"], str(caller_worktree),
    )
    if is_work_tree_rc != 0:
        return
    rc, out, err = _git_runner(
        ["git", "rev-list", "--count", sha_range], str(caller_worktree),
    )
    if rc != 0:
        raise ValueError(
            f"review_trail.write: sha_range {sha_range!r} could not be resolved "
            f"by `git rev-list` (rc={rc}: {err.strip()!r}) — refusing to persist "
            "a record for an unresolvable range"
        )
    try:
        count = int(out.strip())
    except ValueError:
        raise ValueError(
            f"review_trail.write: sha_range {sha_range!r} — `git rev-list --count` "
            f"returned a non-integer result ({out.strip()!r}) — refusing to persist "
            "a record for an unresolvable range"
        ) from None
    if count == 0:
        raise ValueError(
            f"review_trail.write: sha_range {sha_range!r} resolves to ZERO commits "
            "— refusing to persist a record that discharges nothing. This is the "
            "exact shape a caret-eating shell/shim produces from a legitimate "
            "per-commit '<sha>^..<sha>' request (e.g. the pre-fix cmd.exe launcher "
            "defect, or any other path that mangles a range the same way): the "
            "record would still look like a successful write while covering no "
            "commits at all. Verify the range was constructed correctly."
        )


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
    "commits in each slice instead; narrowing is the case-(ii) remedy only. "
    "Per-slice records are a list-shaped `decisions[\"review\"]`, one entry "
    "per slice."
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
            **no_console_creationflags(),
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


def _guard_foreign_session_range(
    sha_range: str,
    own_session_id: str,
    caller_worktree: Path,
) -> FrozenSet[str]:
    """Refuse, or force affirmative disambiguation of, a diff-shaped sha_range
    that spans commits not attributable to the writing session.

    Three-way disposition (docs/plans/2026-07-27-review-trail-scope-guard.md § C2).
    2026-08-08 (docs/plans/2026-08-08-vouch-free-review-coverage-gates.md § C2):
    the PM-vouch relaxation this docstring formerly described (grant CLI +
    liveness-gated per-session check, `coordinator_core/session/
    review_trail_vouch.py`) is deleted outright — it never discharged the case
    it was built for (see that plan's ## Problem). Refusal STRENGTH is
    UNCHANGED: only the vouch-shaped escape disappears.

    Case 1 — any commit whose OWN Session-Id trailer names a DIFFERENT
    session: hard refusal (``ForeignSessionRangeRefused``), UNLESS the
    offending SHA carries a chain-ancestry waiver minted for THIS chain
    (``chain_ancestry_waivers.chain_ancestry_waived_shas`` — see below).
    Refusal strength is otherwise UNCHANGED: still refuse unless every
    offending SHA is covered by that one remaining evidence source. The
    waiver (docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md
    § C3), added 2026-07-31, is a per-SHA, per-chain waiver minted by the
    coverage GATE (``coordinator_core.ops.coverage_gate``, C2) — NOT granted
    by a human, and NOT this guard's own doing; this guard only OBSERVES it
    via ``chain_ancestry_waivers.chain_ancestry_waived_shas``, scoped to
    ``own_session_id`` (the writing session's own id, which IS the closing
    chain's identity when this session is that chain's terminal
    ``/workstream-complete`` — see that module's own docstring for why no
    new derivation is needed to resolve it here). The mint fires whenever a
    caller passes ``mint_chain_waivers=True`` and ``from_handoff`` and the
    gate's ``result.uncovered_shas`` is non-empty — NOT gated on
    ``result.verdict`` (the verdict leg was deliberately dropped 2026-08-07,
    state/audits/2026-08-07-review-gate-scoping-predecessor-and-planning-artifacts.md,
    since a chain whose only uncovered commits are planning artifacts can
    still net verdict COVERED while genuinely owing the narrowing). In
    practice this means: a chain-terminal session that runs its own close
    gate against the handoff it picked up, before attempting this write,
    mints waivers covering its predecessor's commits — the constraint this
    guard's refusal exists under is ORDERING (gate before write), not
    impossibility — **for a picked-up handoff whose chain walk reaches at
    least one node other than the closing handoff itself.**

    NEGATIVE SPEC — the shape that ordering does NOT rescue
    (state/audits/2026-08-10-chain-ancestry-waiver-mint-attribution-gap.md,
    measured in-process): predecessor commits reach ``uncovered_shas`` only
    through a WALKED ANCESTOR node, because
    ``coverage._derive_dag_chain_set``'s Step-3 segment attribution takes its
    trailer-derived ``else`` branch only for non-closing nodes. For the
    ``--from-handoff`` node ITSELF it substitutes the RUNNING session's id
    (D3 case 3), so that node never contributes its AUTHOR's commits. When
    the walk collapses to a single node — a picked-up handoff with no walked
    predecessor edge, the ``predecessor: none``-on-a-continuation-handoff
    shape — there is no ancestor node to supply them, the mint has nothing
    to mint over, and running the gate first changes nothing. For that shape
    the refusal IS impossibility, not ordering, and no amount of re-ordering
    clears it; the range must be narrowed instead. Do not read the paragraph
    above as an unconditional remedy.

    NEGATIVE SPEC 2 — the covered-in-chain-foreign shape, and why it is NOT a
    defect (ruled 2026-08-11; state/bug-backlog/2026-08-11-a-covered-in-chain-
    commit-owned-by-another.yaml, closed by that ruling). The gate mints over
    ``result.uncovered_shas`` only, so a commit that is simultaneously in the
    chain walk, COVERED, and trailer-owned by another session receives no
    waiver and is refused permanently. This is correct, not a mechanism gap:
    ``coverage.run_coverage_gate`` derives COVERED as ``sha in reviewed_set``
    (i.e. a covering trail record already exists) or as ledger-only
    bookkeeping. In the first case the write this guard refuses would be a
    SECOND record over another session's commit that its owner already
    recorded — it credits nothing not already credited, discharges no
    obligation, and blocks no close (DR-245's 2026-08-08 correction: the C13
    PARTITION-MANDATORY halt for the ancestor/foreign-only case is gone, so no
    session is stopped on account of this alone). In the second, the refused
    write is a stranger session recording a review of another session's
    ledger churn, which this guard should refuse on its own terms. Neither
    relaxing the guard to accept coverage-credit as provenance (coverage
    credit includes review-free bookkeeping — it is not an attestation) nor
    widening the mint to covered-in-chain commits buys any crediting the
    system does not already have. The reviewer's findings sidecar is the
    durable evidence for this shape.

    A range whose foreign-attributed SHAs are only PARTIALLY
    covered by the waiver set still refuses, naming only the uncovered
    remainder — this closes the chain-terminal ``/workstream-complete``
    deadlock only for the commits a waiver actually names; the waiver needs
    no write-side persistence step here — it was already minted,
    permanently, by the gate.
    Trailerlessness alone is never Case 1 (SC-DR-008 sanctions trailerless
    commits by design).

    A range naming a foreign, unwaived commit is refused AS-IS — narrowing
    sha_range to exclude it (case-(ii) below), or writing a per-slice record
    over this session's own commits instead, are the sanctioned paths that
    make the WRITE itself proceed. For case (i) (a chain-terminal reviewer
    obliged to cover baton-ancestor commits), there IS a remedy, but it is
    upstream of this guard, not a parameter to it: running the ceremony
    close coverage gate (``wsc-coverage-gate-runner.py coverage-gate`` /
    ``brightline-gate``, ``--from-handoff``) against the picked-up handoff
    BEFORE this write mints a chain-ancestry waiver keyed to THIS session,
    which this guard then observes on retry. The binding constraint is
    ordering — gate before write — not an absence of any remedy, SUBJECT TO
    the walked-ancestor precondition in the negative spec above: on a
    single-node walk that gate mints nothing and the remedy does not exist
    for this range at all.

    Separately, a refusal here is a verdict on THIS CALL SITE, not on the
    range in the abstract. The open-loop freeze-time record
    (``freeze-review-diff.py``) and the close-side write
    (``coordinator-write-review-trail`` with an explicit verdict) are
    distinct callers; once waivers exist, the close-side write can succeed
    for the very range the freeze-time one refused
    (cross-repo/inbox/2026-08-10-example-retrieval-repo-em-correction-chain-terminal-
    trail-write-does-work.md — a peer read a freeze-time refusal as a
    verdict on the range and filed a memo about a bug that was not there).

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
        # Evidence source (2026-07-31, C3,
        # docs/plans/2026-07-31-review-trail-chain-ancestry-discriminator.md):
        # a per-SHA waiver the coverage gate already minted for THIS chain
        # (coverage_gate.py's `mint_chain_waivers and from_handoff and
        # result.uncovered_shas` condition — not gated on `result.verdict`,
        # dropped 2026-08-07, see
        # state/audits/2026-08-07-review-gate-scoping-predecessor-and-planning-artifacts.md).
        # `own_session_id` IS the closing chain's
        # identity at this call site (see this function's own docstring) —
        # no signature change, no new parameter, no new derivation. This
        # guard only OBSERVES the waiver; it never mints one. The PM-vouch
        # evidence source formerly consulted here is gone
        # (docs/plans/2026-08-08-vouch-free-review-coverage-gates.md § C2) —
        # this is the ONLY remaining evidence source.
        chain_waived = chain_ancestry_waivers.chain_ancestry_waived_shas(
            str(caller_worktree), own_session_id,
        ) & foreign_trailer_shas
        waived = chain_waived
        unvouched = foreign_trailer_shas - waived
        if not unvouched:
            # Fully covered: proceed. No write-side persistence step is
            # needed for a chain-ancestry waiver — it was already minted,
            # permanently, by the gate at HALT (C2/C1), and this guard never
            # re-derives or re-mints it.
            logger.info(
                "review_trail.write: sha_range %r contains foreign-attributed "
                "commit(s) %s covered by a gate-minted chain-ancestry waiver "
                "for chain %s — no new waiver written here; the gate already "
                "minted it permanently",
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
            "covered by a gate-minted chain-ancestry waiver for this session, "
            "but not all)"
            if waived
            else ""
        )
        raise ForeignSessionRangeRefused(
            "review_trail.write: sha_range "
            f"{sha_range!r} contains commit(s) whose own Session-Id git "
            f"trailer names a different session: {', '.join(shown)}"
            f"{remainder_note}{vouched_note} — refusing to write a record "
            f"for them on that basis alone. {_FOREIGN_SESSION_UNDETERMINED_NOTE} "
            "This session cannot record a range naming another session's "
            "commits as-is. Two paths remain: narrow sha_range to exclude "
            "the foreign commit(s) named above (case (ii) only — see above), "
            "or, if this is a chain-terminal close reviewing a picked-up "
            "handoff (case (i)), the coverage gate is the remedy — but "
            "first answer this before running anything: does the "
            "--from-handoff handoff have a walked predecessor edge, or "
            "does it carry predecessor: none (every spinoff handoff does, "
            "by construction — schema rule C2-4)? The gate attributes the "
            "--from-handoff node to the RUNNING session, not to the session "
            "that AUTHORED it, so predecessor: none collapses the chain "
            "walk to one node — the gate mints nothing, this remedy does "
            "not exist for this range (DR-294 — this is a ruled limit, not a gap), and "
            "the reviewer's findings sidecar is the terminal evidence; do "
            "not run the gate expecting it to "
            "clear this refusal. If instead the handoff DOES have a walked "
            "predecessor edge: run the ceremony close coverage gate against "
            "that handoff BEFORE this write — wsc-coverage-gate-runner.py "
            "coverage-gate/brightline-gate with --from-handoff (minting is "
            "that runner's DEFAULT; it takes no --mint-chain-waivers flag "
            "and rejects one — --no-mint is its opt-OUT. "
            "--mint-chain-waivers belongs to review-coverage-gate.py, the "
            "child the runner invokes, and is only needed when calling "
            "that child directly) mints a per-SHA chain-ancestry waiver "
            "keyed to THIS session's own id for every uncovered predecessor "
            "commit, and this guard observes that waiver on retry. The "
            "constraint is then ordering, not impossibility: a session "
            "that reviews-then-writes before reaching its own close gate "
            "hits this refusal; running the gate first and retrying the "
            "write clears it. If this refusal is unchanged after already "
            "running the gate with --from-handoff, do not re-run it a "
            "third time — narrow sha_range instead. Separately — "
            "the gate mints only for commits it counts UNCOVERED, so an "
            "in-chain foreign commit the gate counts COVERED never "
            "receives a waiver and this refusal is permanent for it. That "
            "is not a gap to work around: COVERED means a covering "
            "review-trail record already exists for that commit (or it is "
            "ledger-only bookkeeping), so the record you are attempting "
            "would duplicate another session's record over another "
            "session's commit, crediting nothing that is not already "
            "credited. Keep the reviewer's findings sidecar as the "
            "evidence and do not re-run the gate. Also note this refusal "
            "is a verdict on THIS call site, not on the range: the "
            "close-side write (coordinator-write-review-trail with an "
            "explicit verdict) may succeed for this very range once "
            "waivers exist, even where the freeze-time open-loop record "
            "refused it. Caveat: gate admission is not discharge — "
            "crediting is range-based and narrowed again downstream, so "
            "an admitted record can still credit zero commits at the "
            "chain-terminal path (see this module's own "
            "zero-chain-terminal-credit diagnostic, "
            "_diagnose_zero_chain_terminal_credit / "
            "_ALWAYS_ZERO_CREDIT_SCOPE_KINDS, for when that applies)."
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

    # DEFECT 2 fix (2026-08-07 coordinator-claude-em memos: case3-remedy-is-not-
    # performable / review-trail-guard-remedy-unreachable, CONFIRMED-LIVE per
    # state/audits/2026-08-12-inbox-blitz-dominant-verify-wave-b.md items
    # 11/12): when sha_range is ALREADY a single commit, "supply a narrower
    # sha_range" names an action that does not exist — there is no narrower
    # range than one commit. Detect that shape and name a performable remedy
    # instead (re-commit through the trailer-emitting path, e.g.
    # ceremony.scoped_git_commit, so the commit carries a Session-Id trailer,
    # then retry with the new SHA) rather than repeating advice the sender
    # already proved unreachable by construction.
    commit_count_rc, commit_count_out, _commit_count_err = _git_runner(
        ["git", "rev-list", "--count", sha_range], str(caller_worktree),
    )
    is_single_commit = (
        commit_count_rc == 0 and commit_count_out.strip() == "1"
    )

    if is_single_commit:
        raise ForeignSessionRangeRefused(
            "review_trail.write: sha_range "
            f"{sha_range!r} is already a single commit and is genuinely "
            "ambiguous — it is untrailered (or its own Session-Id trailer is "
            "absent) and the touched-path signal cannot place it in this "
            f"session's scope. {_FOREIGN_SESSION_UNDETERMINED_NOTE} "
            "There is no narrower range than one commit, so narrowing "
            "further is not a performable remedy here. Two remedies "
            "actually resolve this: (1) re-commit the same change through "
            "the trailer-emitting path (ceremony.scoped_git_commit / the "
            "normal commit machinery, not a raw git-commit or commit-tree "
            "invocation) so the commit carries this session's own "
            "Session-Id trailer, then retry the write against the new SHA; "
            "or (2) if this is a chain-terminal close reviewing a picked-up "
            "handoff, run the ceremony close coverage gate against that "
            "handoff BEFORE this write (wsc-coverage-gate-runner.py "
            "coverage-gate/brightline-gate --from-handoff) — it mints a "
            "per-SHA chain-ancestry waiver keyed to this session, though "
            "that path exists only for baton-ancestor commits with a "
            "foreign Session-Id trailer (case 1), not for a genuinely "
            "trailerless one. A PM vouch does NOT apply here — Case 3 never "
            "consults it (trailerlessness alone is never Case 1, by "
            "design)."
        )

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
# Write-time zero-chain-terminal-credit diagnostic
#
# state/audits/2026-08-07-wsc-chain-gate-counts-doc-only-commits.md (Q2, Q4,
# Q5); state/lessons/2026-08-06-a-chain-terminal-reviewer-cannot-record-
# 2220489ba97a.yaml.
#
# `_guard_foreign_session_range` above answers "is this range DEFENSIBLE for
# the writing session to record at all" — a self-contained single-commit
# slice with an honest range boundary passes it (case 2), or is admitted
# under a PM vouch / chain-ancestry waiver (case 1's relaxation). The
# chain-terminal discharge path
# (`coordinator_core.workstream_complete.directives_review.
# _record_membership_shas`, consumed by `_collect_discharging_range_shas`)
# asks a DIFFERENT question at READ time: it narrows a session/chain-scoped
# record's raw commit set by SUBTRACTING every commit whose own Session-Id
# trailer does not name the WRITING session (vouched/waived shas excluded
# from the subtraction) — a predicate the write-side guard never runs. A
# record can therefore pass the guard, land on disk with verdict "ok", and
# still credit ZERO commits at that discharge path — sharpest on a
# single-commit range naming a predecessor session's own commit, which is
# 100% foreign-and-unvouched by construction and narrows to `set()`. The
# writer sees a clean accepted write and has no way to learn, at write time,
# that the record they just wrote discharges nothing — this is exactly the
# "silently accept, silently credit nothing" shape this repo's own doctrine
# calls out as the failure mode to avoid.
#
# `_diagnose_zero_chain_terminal_credit` predicts the two shapes that are
# PROVABLY zero-credit using only write-time-available information (no
# chain DAG, no chain_code_shas/chain_planning_shas — those exist only
# after the CLOSING session's own chain traversal, which has not happened
# yet when an earlier session in the chain writes its own record):
#
#   (a) every commit named by a `scope_kind` "diff" or "plan" sha_range is
#       foreign to the writing session AND not covered by a gate-minted
#       chain-ancestry waiver — the read side's narrowing then empties this
#       record's raw set to `set()` regardless of what
#       chain_dag/chain_code/chain_planning turn out to be later
#       (intersecting the empty set with anything is still empty). This
#       function resolves the same evidence source
#       `_guard_foreign_session_range` consults for its own Case-1
#       relaxation (`chain_ancestry_waivers.chain_ancestry_waived_shas`)
#       INDEPENDENTLY,
#       rather than trusting that guard to have already run: for both
#       `scope_kind="diff"` and `scope_kind="plan"` the guard already
#       refuses an unvouched foreign range outright (2026-08-07 fix — see
#       `write_review_trail_entry`'s call site,
#       `if scope_kind in ("diff", "plan") and caller_worktree is not None:`
#       — both shapes are therefore live-unreachable here today, verified
#       directly against `_guard_foreign_session_range`, not merely
#       inferred). This diagnostic's independent re-resolution is retained
#       for both scope_kinds anyway, as a standing check that is not
#       coupled to the guard's own gating — a future change to the guard's
#       call-site condition must not silently regress this diagnostic's own
#       coverage of either shape. `_record_membership_shas`'s
#       `narrow_foreign_shas` leg narrows both `diff` and `plan` records the
#       same way (only `scope_kind in _NON_CODE_SCOPE_KINDS`, i.e.
#       "integration", skips that leg entirely — see shape (b)).
#   (b) `scope_kind == "integration"` — rejected OUTRIGHT by the discharge
#       path's `_NON_CODE_SCOPE_KINDS` filter before any range resolution
#       runs, credits zero unconditionally, regardless of sha_range or
#       scope.
#
# `scope_kind == "plan"` is NOT flagged for reason (b) (2026-08-07
# correction, commit 1b710512e, `directives_review._NON_CODE_SCOPE_KINDS`):
# a plan record now credits the commits its range shares with the
# caller-supplied PLANNING subset of chain_code_shas, a chain-scoped fact
# this write-time diagnostic cannot resolve — so shape (b) stays silent on
# every plan record rather than either over- or under-claiming about its
# eventual credit; shape (a) still applies to it, independently of (b).
#
# Purely advisory: never raises, never changes `verdict`, `scope`,
# `scope_kind`, or any crediting rule, and never blocks, retries, or slows
# the write. A record written for the human paper trail is still a
# legitimate record even when it provably discharges nothing here — see
# `write_review_trail_entry`'s Returns docstring for the additive result
# key this surfaces on.
# ---------------------------------------------------------------------------

#: mirrors `directives_review._NON_CODE_SCOPE_KINDS` as of the 2026-08-07
#: correction (commit 1b710512e) — "plan" was removed from that set,
#: "integration" was not. Duplicated, not imported: this ops-layer module
#: does not import `coordinator_core.workstream_complete` (layering), and
#: this set is small and has changed exactly once in this module's history.
#: `test_review_trail_write.py` pins that this stays in sync with the real
#: constant.
_ALWAYS_ZERO_CREDIT_SCOPE_KINDS = frozenset({"integration"})

#: mirrors `coverage._FOREIGN_STRIPPED_SCOPES` — as of writing, identical to
#: this module's own `_VALID_SCOPES` (every scope this module accepts is
#: subject to the chain-terminal discharge path's foreign-session
#: narrowing). Kept as an explicit local mirror rather than a cross-layer
#: import so a future divergence between the two fails this diagnostic
#: CLOSED (it silently stops warning) rather than reaching across the
#: ops/workstream_complete layering boundary.
_FOREIGN_NARROWED_SCOPES = frozenset({"chain", "session", "workstream-close-auto"})

_ZERO_CREDIT_REASON_FOREIGN_SESSION = "foreign_session_narrowing"
_ZERO_CREDIT_REASON_NON_CODE_SCOPE_KIND = "non_code_scope_kind"


def _walk_range_commit_session_trailers(
    sha_range: str, own_session_id: str, caller_worktree: Path,
) -> Optional[dict[str, bool]]:
    """Return ``{sha: is_foreign}`` for every commit git resolves in
    ``sha_range``, or ``None`` if the range fails to resolve (unparseable,
    no matching repo, git failure) — the caller treats ``None`` as "cannot
    predict, stay silent" rather than guessing.

    Adopts P2 (`coordinator_core.chain_attribution`, A1) — the whole-window
    trailer walk (`bulk_commit_attribution_map`, WITH merges) PLUS the grep
    leg this function never had (`bulk_grep_attributed_shas`), combined via
    `foreign_shas_from_window`'s three-way merge/ambiguous/trailer/grep
    logic — instead of this module's own single-`git log`-with-trailers-only
    walk, which (a) parsed line-by-line, silently dropping continuation
    lines of a multi-valued Session-Id trailer, and (b) had no grep leg, so
    every untrailered commit was unconditionally treated as foreign even
    when it was this session's own trailerless work (SC-DR-008).

    This is now DEMONSTRATED, not theoretical (docs/plans/2026-08-07-n-plus-
    one-git-spawn-class-and-amplification-gate.md, task A4):
    fork-adjudication.md § 11.1 replayed all on-disk review-trail records
    (1,566 parsed, 1,562 qualifying) against history pinned at
    0515db0626bc542752f0c3302a2a4bf7fcc07cf3 and found 5 real records — all
    from one session (4524bf7d), one workstream (handoff-write-cas),
    2026-07-28 — that the OLD walk here would have flagged as a false
    POSITIVE zero-credit prediction it never actually reached (this
    diagnostic shipped 2026-08-07, ten days after those records).

    Do NOT collapse this onto `session_attribution.bulk_trailer_session_map`
    (P1) "for consistency" — P1 is `--no-merges` and drops untrailered
    commits, the LOOSER semantics. At this write-side diagnostic, adopting
    P1 instead of P2 would convert today's harmless false positive into a
    false NEGATIVE — silence on a genuinely zero-credit write, the direction
    that actually matters (see the governing plan's § Anti-scope 5). Use P2
    (`chain_attribution`) exclusively.

    Mirrors `wsc-coverage-gate-runner.py`'s `_resolve_foreign_session_shas` /
    A1's `chain_attribution.unattributed_foreign_shas` walk (git log WITH
    merges, same trailer atom, plus the grep leg) so a write-time "every
    commit is foreign" verdict here is never contradicted later by the read
    side resolving a different commit set for the same range.

    ``caller_worktree`` is the EXPLICITLY-PASSED root threaded down from
    `write_review_trail_entry`'s own `caller_worktree` parameter (itself
    derived once via `main_worktree_root(repo_root)` in the JSON-RPC
    handler) — this function never independently rediscovers a repo root.
    fork-adjudication.md § 11.2 found this diagnostic resolves its root
    against `caller_worktree` while the read side resolves via
    `_resolve_repo_root()`, and confirmed that divergence is REACHABLE
    (Claude Code 2.1.x auto-creates per-dispatch worktrees under
    `.claude/worktrees/` that bypass the hard `block_worktree_creation`
    guard). Resolving strictly against the caller-supplied root here (never
    re-deriving one) is this chunk's fix for that half of the gap; the
    separate `state/`-resolution question § 11.2 left open is NOT addressed
    here — out of this chunk's scope.

    `GitLogFailed` from the P2 bulk primitives is caught here (not
    propagated further) so this whole diagnostic keeps its documented
    "never raises" contract (see the module-level "Write-time
    zero-chain-terminal-credit diagnostic" comment above) — this is a
    boundary decision at THIS advisory diagnostic's own edge, not the
    primitives themselves swallowing the error (`bulk_commit_attribution_map`
    and `bulk_grep_attributed_shas` still raise/never-fail-open internally;
    see their own docstrings and § Anti-scope 13 of the governing plan).
    """
    try:
        window = chain_attribution.bulk_commit_attribution_map(
            sha_range, str(caller_worktree), _git_runner,
        )
    except GitLogFailed:
        return None
    if not window:
        return None
    grep_attributed = chain_attribution.bulk_grep_attributed_shas(
        sha_range, own_session_id, str(caller_worktree), _git_runner,
    )
    foreign = chain_attribution.foreign_shas_from_window(
        window.keys(), own_session_id, window, grep_attributed,
    )
    return {sha: (sha in foreign) for sha in window}


def _resolve_write_time_vouched_shas(
    candidate_shas: FrozenSet[str], own_session_id: str, caller_worktree: Path,
) -> FrozenSet[str]:
    """The same evidence source `_guard_foreign_session_range`'s Case-1
    relaxation consults (module docstring above), resolved independently
    against an arbitrary ``candidate_shas`` set — used here so this
    diagnostic stays accurate for ``scope_kind="plan"``, which never runs
    through that guard at all. Fail-safe toward "not vouched": a raising
    lookup is treated as an empty result, matching
    ``_guard_foreign_session_range``'s own fail-safe posture for the same
    call.
    """
    try:
        chain_waived = chain_ancestry_waivers.chain_ancestry_waived_shas(
            str(caller_worktree), own_session_id,
        ) & candidate_shas
    except Exception:  # noqa: BLE001 - a broken waiver lookup must narrow, never crash
        chain_waived = frozenset()
    return frozenset(chain_waived)


def _diagnose_zero_chain_terminal_credit(
    sha_range: str,
    scope: str,
    scope_kind: str,
    own_session_id: str,
    caller_worktree: Optional[Path],
) -> Optional[dict]:
    """Predict, from write-time-available information only, whether this
    record is a PROVABLE zero-credit write at the chain-terminal discharge
    path (`directives_review._record_membership_shas`). See this section's
    module-level comment for the two shapes detected. Returns `None`
    whenever neither shape is provably present — including every case this
    function cannot resolve (no `caller_worktree`, an unparseable range, a
    git failure) — never a false positive on an ordinary write.
    """
    if scope_kind in _ALWAYS_ZERO_CREDIT_SCOPE_KINDS:
        return {
            "reason": _ZERO_CREDIT_REASON_NON_CODE_SCOPE_KIND,
            "shas": [],
            "detail": (
                f"scope_kind={scope_kind!r} is rejected outright by the "
                "chain-terminal discharge path's _NON_CODE_SCOPE_KINDS filter, "
                "before any sha_range resolution runs — this record credits "
                "zero commits there regardless of sha_range or scope."
            ),
            "alternatives": [
                "This record is still a legitimate paper-trail entry — no "
                "action is required if that is its only purpose.",
                "If this record was meant to discharge a chain code-coverage "
                "obligation, write a scope_kind='diff' record (or, for a "
                "planning-artifact commit, 'plan') over the relevant "
                "commit(s) instead.",
            ],
        }
    if scope_kind not in ("diff", "plan") or scope not in _FOREIGN_NARROWED_SCOPES:
        return None
    if caller_worktree is None:
        return None
    foreign_map = _walk_range_commit_session_trailers(sha_range, own_session_id, caller_worktree)
    if not foreign_map:
        return None
    foreign = frozenset(sha for sha, is_foreign in foreign_map.items() if is_foreign)
    if not foreign or len(foreign) != len(foreign_map):
        return None  # at least one commit is this session's own — not provably zero.
    vouched = _resolve_write_time_vouched_shas(foreign, own_session_id, caller_worktree)
    if vouched >= foreign:
        return None  # every foreign commit is vouched/waived — will still credit.
    shas = sorted(foreign_map)
    shown = shas[:_FOREIGN_SHA_DISPLAY_CAP]
    remainder = len(shas) - len(shown)
    remainder_note = f" (+{remainder} more)" if remainder else ""
    return {
        "reason": _ZERO_CREDIT_REASON_FOREIGN_SESSION,
        "shas": shas,
        "detail": (
            f"every commit named by sha_range {sha_range!r} carries a "
            f"Session-Id trailer other than this writing session's own "
            f"({own_session_id!r}), or none at all, and none is covered by "
            "a gate-minted chain-ancestry waiver — the "
            "chain-terminal discharge path's foreign-session narrowing "
            f"subtracts all of them, emptying this record's contribution "
            f"to the empty set: {', '.join(shown)}{remainder_note}."
        ),
        "alternatives": [
            "This record is still a legitimate paper-trail entry — no "
            "action is required if that is its only purpose.",
            "If this record was meant to discharge a chain-terminal "
            "coverage obligation over a predecessor session's commit(s), "
            "this session cannot record a range naming another session's "
            "commits — the sanctioned remedy is a gate-minted "
            "chain-ancestry waiver (minted automatically by the coverage "
            "gate at HALT, not something this session can request) or "
            "writing per-slice records over this session's own commits. "
            "Narrowing sha_range further does not help here, since it is "
            "already narrowed to a range containing none of this "
            "session's own commits.",
        ],
    }


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
    """Resolve session_id from env vars.

    Thin compatibility shim — delegates to
    ``coordinator_core.ops.session_context.resolve_current_session_id``.

    The two-tier chain (CLAUDE_SESSION_ID → CLAUDE_CODE_SESSION_ID) is defined in
    ``session_context.py`` (C3 extraction, sentinel tier removed KS-2 2026-08-07) so it
    is shared with ``handoff.author_fork`` and any future op that needs session identity
    at call time. Callers of this module-local function continue to receive identical
    behavior.

    Extraction rationale: docs/plans/2026-07-07-claude-klabauter-fork-provenance-creation-path-tooling.md § C3
    """
    return resolve_current_session_id(caller_worktree)


# ---------------------------------------------------------------------------
# Workstream resolution
# ---------------------------------------------------------------------------


def _scan_workstream(
    handoffs_dir: Path,
    own_session_id: Optional[str],
    caller_worktree: Optional[Path] = None,
) -> Optional[str]:
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

    2026-08-07 fix (docs/plans/2026-08-07-claim-state-ledger-first-authoritative-read.md
    § C6a): attribution now resolves each candidate handoff's claim
    ledger-first via ``claim_state.resolve_claim_state`` rather than reading
    the frontmatter mirror's ``claimed_by`` directly. On a desynced baton
    (mirror reverted to ``status: open`` by a branch switch while the
    branch-independent ledger still holds the claim) the mirror-only read
    misattributes the handoff — this record would then be written
    unpartitioned, which lands but in the wrong place. The ledger-first
    accessor's resolved ``holder`` is compared against ``own_session_id``
    instead.

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
            claim_state = resolve_claim_state(hfile, repo_root=caller_worktree)
        except Exception as exc:
            logger.warning(
                "review_trail.write: claim_state resolution failed for %s — %s; skipping",
                hfile,
                exc,
            )
            continue
        if claim_state.holder != own_session_id:
            continue
        try:
            text = hfile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            print(f"skip: _scan_workstream: text = hfile.read_text(encoding=\"utf-8\", errors=\"replace\") failed: {sys.exc_info()[1]}", file=sys.stderr)
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
        return _scan_workstream(handoffs_dir, own_session_id, caller_worktree)
    return None


# ---------------------------------------------------------------------------
# JSON record serialization (hand-built — byte-parity with oracle bash interpolation)
# ---------------------------------------------------------------------------


#: Valid values for ``execution_basis`` (docs/plans/2026-08-11-review-trail-
#: carries-execution-basis.md § C1). There is NO ``unknown`` value — absence
#: of the key already means unknown (AC5 / § Anti-scope); a third explicit
#: value would be a state that must be kept byte-distinguished from absence
#: for no named consumer.
_VALID_EXECUTION_BASES = frozenset({"executed", "read-only"})


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
    execution_basis: Optional[str] = None,
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

    ``execution_basis`` (docs/plans/2026-08-11-review-trail-carries-execution-basis.md
    § C1) is a further optional key, appended AFTER the ``reviewed_paths`` block (when
    present) — ONLY when ``execution_basis`` is not ``None``. Unlike ``reviewed_paths``,
    this key is NOT conditioned on ``scope_kind`` — a plan-scoped or integration-scoped
    review has an execution basis just as a diff-scoped one does. When ``execution_basis``
    is ``None`` (not supplied), the key is omitted entirely and the produced bytes are
    byte-identical to what this function produced before this key existed (AC5).

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
    if execution_basis is not None:
        record = record[:-1] + f',"execution_basis":"{execution_basis}"}}'
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
    (2026-07-27 fix for the DR-216 D2(i) last-write-wins clobber defect). A record
    whose bytes exactly match one already on disk for this session is a REPLAY, not a
    collision — see "Replay convergence" below — and converging onto its existing
    path (rather than uniquifying past it) is what makes a re-run of a failed
    ``workstream_complete`` apply pass idempotent.

    On the first attempt, tries ``base_filename`` verbatim — this keeps the common
    (no-collision) case's filename byte-identical to the pre-fix format, so every
    existing caller/test that depends on the bare ``{ts}-{sid}.json`` shape is
    unaffected. Only on an actual same-timestamp+session_id_short collision does this
    fall back to ``{stem}-2.json``, ``{stem}-3.json``, ... — appended after the
    session_id segment, which ``_shared._validate_review_trail_file``'s
    ``_TIME_SEG_RE`` regex already tolerates (it only anchors on the leading digit run
    for the reviewed_at timestamp; anything after the first ``-`` is opaque to it).

    Replay convergence (fix for the workstream_complete PARTIAL_MUTATION duplicate-
    record defect): before reserving anything, this scans existing records for one
    whose bytes are EXACTLY equal to *record_bytes*. A byte-identical record is not a
    collision to uniquify around — it is the SAME write happening again, because an
    apply pass that failed on a LATER directive got re-run and this trail-write
    directive fired a second (or third, or Nth) time. ``reviewed_at`` lives only in
    the filename, never in the record body, so a slow retry across a second boundary
    produces a byte-identical record under a DIFFERENT filename — a same-second
    collision alone would miss that case, which is why this is a content scan, not a
    filename check. Converging on the existing path makes a re-run of a failed apply
    pass idempotent: the trail directive's second firing writes nothing new and
    returns the same path it already wrote. This is safe because the serialized
    record bytes already ARE the identity key — a real re-review after fixes
    necessarily changes ``verdict`` or ``sha_range`` (and thus the bytes), so it is
    never swallowed by this check; only a call that would have produced literally the
    same record converges. The residual: two independent reviews in the same session
    that happen to produce byte-identical records (same sha_range, reviewer, scope,
    verdict, diff_loc, workstream, ...) now collapse to one record. That is intended,
    not a bug — byte-identical means the same verdict over the same range by the same
    reviewer, so nothing is lost by recording it once.

    The scan is bounded to THIS session, not a full-directory scan: *base_filename*
    has the shape ``{ts}-{session_id_short}.json``, so the session_id_short segment is
    extracted from it and only ``*-{session_id_short}*.json`` under *trail_dir* is
    globbed. A replay is always same-session (the directive re-runs inside the same
    apply-pass retry), so this is correct as well as cheap: measured, a session holds
    at most ~40 trail records even when the directory holds thousands, so this is a
    bounded ~40-entry glob-and-compare, never an unbounded scan of the whole
    directory.

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

    # Replay convergence pre-check — see docstring. Bounded to this session's
    # records only (never a full-directory scan): base_filename is
    # "{ts}-{session_id_short}.json", and ts itself is dash-delimited
    # ("YYYY-MM-DD-HHMMSS[ns]"), so rpartition on the LAST "-" (not the
    # first) to recover the session_id_short suffix and glob only that
    # session's files.
    _, _, session_id_short = stem.rpartition("-")
    if session_id_short:
        for existing in trail_dir.glob(f"*-{session_id_short}*.json"):
            try:
                if existing.read_bytes() == record_bytes:
                    return existing
            except OSError:
                continue

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
    reviewer_evidence: Optional[str] = None,
    execution_basis: Optional[str] = None,
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
        session_id  — explicit session_id override; falls back to env when empty/None.
        workstream  — explicit workstream slug; falls back to env/scan/null when None.
                      Pass empty string to suppress scanning and emit null explicitly.
        reviewed_paths — optional list of paths this record's review actually covered (e.g.
                      from ``freeze-review-diff.py --paths``). Persisted ONLY when
                      ``scope_kind == "diff"`` (docs/plans/2026-07-27-review-trail-scope-guard.md
                      § C9); ignored/omitted for ``plan``/``integration`` records.
        reviewer_evidence — evidence correlating ``reviewer`` with an artifact showing a
                      review occurred. See the module-level "reviewer_evidence" comment
                      block for the full evidence-class design. Enforced (raises
                      ``ValueError``) only when ``COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE``
                      is truthy; advisory (logs, does not raise) by default — see
                      ``_verify_reviewer_evidence``'s Negative-spec block. Never persisted
                      to the on-disk record.
        execution_basis — one of ``executed`` | ``read-only``, or ``None`` (default).
                      Persisted as a further optional key (see ``_build_json_record``),
                      appended after ``reviewed_paths`` and NOT conditioned on
                      ``scope_kind``. Absence (``None``) means unknown — absence is
                      NEVER equivalent to ``read-only``; a record written without this
                      parameter is byte-identical to what the same call produces today
                      (AC5, docs/plans/2026-08-11-review-trail-carries-execution-basis.md).
        caller_worktree — the caller's repo worktree root (from main_worktree_root(repo_root)).
        _timestamp  — injectable timestamp string for test isolation (bypasses _compute_timestamp).

    Returns:
        {"out_path": str, "sha_range": str, "reviewer": str, "scope": str,
         "scope_kind": str, "verdict": str, "diff_loc": int,
         "session_id": str, "workstream": str | None,
         "reviewed_paths": list[str] | None,  # key present only when scope_kind == "diff"
         "chain_terminal_zero_credit_warning": dict}  # key present ONLY when
        `_diagnose_zero_chain_terminal_credit` provably determines this record
        discharges zero commits at the chain-terminal read path — see that
        function's docstring. Absent on every ordinary write; never changes
        `verdict` or blocks the write.

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

    if execution_basis is not None and execution_basis not in _VALID_EXECUTION_BASES:
        raise ValueError(
            f"review_trail.write: execution_basis {execution_basis!r} is invalid; "
            f"allowed: {' | '.join(sorted(_VALID_EXECUTION_BASES))}"
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

    # Refuse (or, advisory-mode, warn on) an unevidenced reviewer claim
    # (state/bug-backlog/2026-08-10-coordinator-write-review-trail-accepts-a-
    # 295d3cd80d13.yaml) — see the module-level "reviewer_evidence" comment
    # block for the full design. Runs after session_id resolution (evidence
    # for a delegate reviewer is verified against THIS session's own dispatch
    # ledger) and before any git-backed range work, so an enforced-mode
    # unevidenced claim never reaches the foreign-session guard or the
    # filesystem write at all.
    _verify_reviewer_evidence(
        reviewer, verdict, reviewer_evidence, caller_worktree, resolved_session_id
    )

    # Derive execution_basis from the reviewer's own sidecar instead of
    # trusting whatever the caller typed (C2, docs/plans/2026-08-11-review-
    # trail-carries-execution-basis.md § C2). See the module-level
    # "execution_basis derivation" comment block above for the full design.
    derived_execution_basis = _derive_execution_basis_from_sidecar(
        reviewer, reviewer_evidence, caller_worktree
    )
    if derived_execution_basis is _SIDECAR_UNDETERMINED:
        # Rule 4 (§ C2 correction pass): a sidecar DID resolve, but its
        # "## Execution capability" section is absent, empty, or
        # unparseable -- distinct from Rule 3 below (no sidecar resolves at
        # all). Deliberately NOT a refusal: the write still SUCCEEDS, but
        # `execution_basis` is forced to `None` so the key is OMITTED from
        # the written record rather than persisting whatever the caller
        # typed as though it were evidenced. This is the DOMINANT case
        # against today's sidecar corpus (state/audits/2026-08-11-review-
        # trail-execution-basis-derivability.md: ~92% of records resolve a
        # sidecar; essentially none yet carry the section), so a discarded
        # caller value is logged -- not silent -- naming the sidecar so an
        # EM isn't left wondering why their flag vanished.
        if execution_basis is not None:
            logger.warning(
                "review_trail.write: discarding caller-supplied "
                "execution_basis %r -- reviewer's sidecar (%r) resolved but "
                "its '## Execution capability' section attests nothing "
                "(absent, empty, or unparseable); execution_basis is "
                "omitted from the written record rather than persisting an "
                "unevidenced caller value.",
                execution_basis,
                reviewer_evidence,
            )
        execution_basis = None
    elif isinstance(derived_execution_basis, str):
        # Rule 1/2: a real value was read from the sidecar's own section. A
        # caller-supplied value that CONTRADICTS it is refused -- but under
        # the SAME env gate as `_verify_reviewer_evidence`
        # (advisory/non-blocking unless COORDINATOR_REVIEW_TRAIL_EVIDENCE_ENFORCE
        # is truthy), not a stricter one. The sidecar-derived value always
        # wins over a contradicting caller value -- that is the entire point
        # of this chunk's title ("derive... instead of trusting whatever the
        # caller typed") -- the env gate controls only whether the
        # contradiction RAISES (enforcing) or merely WARNS (advisory,
        # default) before it is overridden.
        if execution_basis is not None and execution_basis != derived_execution_basis:
            contradiction_message = (
                f"review_trail.write: caller-supplied execution_basis "
                f"{execution_basis!r} contradicts the reviewer's own sidecar "
                f"({reviewer_evidence!r}), which derives {derived_execution_basis!r} "
                "-- the sidecar-derived value is authoritative."
            )
            if _evidence_enforcement_enabled():
                raise ValueError(contradiction_message)
            logger.warning(
                "review_trail.write advisory (would refuse if enforcing): %s",
                contradiction_message,
            )
        execution_basis = derived_execution_basis
    # else (Rule 3): no sidecar resolves at all (waived/em-verified/machine-
    # provenance reviewers, or a DELEGATE reviewer with no/unresolvable
    # evidence) -- there is nothing to derive FROM, so the caller's value
    # stands on the existing justification floor, same as before C2. This
    # is the REAL asymmetry with Rule 4 above: Rule 3 has no sidecar to
    # doubt the caller against, so the caller's typed value is the best
    # information available; Rule 4 has a sidecar that was consulted and
    # came back empty, so persisting the caller's value would misrepresent
    # silence as evidence.

    # Concretize any symbolic ref (HEAD, a branch, ...) in sha_range to its
    # current concrete SHA — persisting a literal "HEAD" lets the record's
    # certified range silently grow at coverage-gate READ time as new commits
    # land (see _resolve_symbolic_range docstring).
    sha_range = _resolve_symbolic_range(sha_range, caller_worktree)

    # Foreign-session scope guard — runs for scope_kind "diff" AND "plan"
    # (2026-08-07 fix): the guard body (_guard_foreign_session_range) has no
    # diff-specific logic — it walks sha_range via `git log` and reasons
    # about trailer attribution regardless of shape. Gating this call to
    # "diff" alone meant a scope_kind="plan" record with a foreign-session
    # range was accepted (no guard ran) even though the read side
    # (workstream_complete.directives_review._record_membership_shas)
    # narrows plan records for foreign sessions exactly as it does diff
    # records — an asymmetric guard/credit pair. "integration" is
    # deliberately excluded — _NON_CODE_SCOPE_KINDS rejects it outright
    # downstream as non-code, so no guard machinery should ever engage for
    # it. Only meaningful when a real repo exists to check against — no
    # caller_worktree is the same test-isolation no-op contract
    # _resolve_symbolic_range documents above.
    if scope_kind in ("diff", "plan") and caller_worktree is not None:
        _guard_foreign_session_range(sha_range, resolved_session_id, caller_worktree)

    # Empty-range rejection (state/bug-backlog/2026-08-08-cmd-exe-shim-eats-
    # the-caret-in-a-git-rev-6679bf76eb8a.yaml): a caller-side mangler (the
    # cmd.exe launcher caret defect fixed alongside this check, or ANY other
    # future path that mangles a range the same way) can turn a legitimate
    # per-commit `<sha>^..<sha>` request into `<sha>..<sha>` — a range git
    # itself resolves to ZERO commits. Left unchecked, that record is still
    # written, the CLI still exits 0, and it discharges nothing while
    # looking exactly like a successful, meaningful write — the silent
    # failure this check exists to close, independent of and in addition to
    # the caller-side caret fix, so any OTHER path that mangles a range
    # reproduces the identical silent hole and is still caught here.
    #
    # DELIBERATELY AFTER the foreign-session guard above, not before
    # (see _reject_empty_sha_range's own docstring, "CALL-SITE ORDERING"):
    # placing this ahead of the guard is what made 052996621 revert it —
    # this function's own `git rev-list` ValueError on an unresolvable
    # range pre-empted `_guard_foreign_session_range`'s GitLogFailed-derived
    # exception contract for the identical input. Running it after means
    # the guard's contract still fires first for a range that fails to
    # resolve; this backstop only fires for a range that resolves and
    # resolves to zero commits.
    _reject_empty_sha_range(sha_range, caller_worktree)

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
        execution_basis=execution_basis,
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
    if execution_basis is not None:
        result["execution_basis"] = execution_basis

    # Advisory-only, additive: never persisted to the on-disk JSON record
    # (see the "Write-time zero-chain-terminal-credit diagnostic" section
    # above) — present in the result ONLY when a zero-credit shape is
    # provable, so an ordinary write's result/log stays unchanged.
    zero_credit_diagnostic = _diagnose_zero_chain_terminal_credit(
        sha_range, scope, scope_kind, resolved_session_id, caller_worktree,
    )
    if zero_credit_diagnostic is not None:
        logger.warning(
            "review_trail.write: this record is a provable zero-credit write "
            "at the chain-terminal discharge path (reason=%s) — %s",
            zero_credit_diagnostic["reason"],
            zero_credit_diagnostic["detail"],
        )
        result["chain_terminal_zero_credit_warning"] = zero_credit_diagnostic
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
        reviewer_evidence (str) — evidence correlating ``reviewer`` with an artifact
                      showing a review occurred; see ``write_review_trail_entry``'s
                      docstring and the module-level "reviewer_evidence" comment block.
        execution_basis (str) — one of ``executed`` | ``read-only``; see
                      ``write_review_trail_entry``'s docstring. Absence means unknown.

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
        reviewer_evidence=params.get("reviewer_evidence"),
        execution_basis=params.get("execution_basis"),
        caller_worktree=caller_worktree,
    )
    return result
