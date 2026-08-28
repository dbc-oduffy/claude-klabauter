"""
coordinator_core.ops.review_trail_write — per-session review-trail entry writer.

Purpose: writes a JSON review-trail entry (additive-create) under
``<worktree>/state/review-trail/<timestamp>-<session_id_short>.json``.

Port of: coordinator-write-review-trail.sh (DoE 30f4c5fc, 2026-07-19).

JSON record shape (key order is canonical; hand-serialized for byte-parity):
    {"sha_range":"A..B","reviewer":"code-reviewer","scope":"chain","scope_kind":"diff",
     "verdict":"ok","diff_loc":100,"session_id":"abc12345","workstream":null}

    A NINTH, optional key — ``reviewed_paths`` (docs/plans/2026-07-27-review-trail-scope-guard.md
    § C9) — is appended ONLY on ``scope_kind: "diff"`` records, carrying the reviewed-path
    set (e.g. from ``freeze-review-diff.py --paths``) or JSON ``null`` when not supplied.
    ``plan``/``integration`` records OMIT the key entirely; the original eight-key shape is
    otherwise unchanged and every existing by-key-name consumer keeps working.

    A further optional key — ``execution_basis`` (docs/plans/2026-08-11-review-trail-carries-
    execution-basis.md § C1) — is appended ONLY when supplied — absence omits the key entirely
    and reproduces byte-identical output to a call made before that key existed.

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

    2026-08-15 (P2, docs/plans/2026-08-15-the-ceremony-tail-stops-lying-about-why-it-
    failed.md § C3): the ``-2``/``-3``/... suffix above is now reserved for a genuine
    filename collision AND for a genuine identity divergence (see
    ``_reserve_unique_trail_path``'s "Identity and convergence" section) — record
    identity is ``(session_id, sha_range)``, not the record's serialized bytes. A retry
    whose only difference is a derived field (``execution_basis``) converges on the
    first-written record (a no-op skip, not a new file); a retry that disagrees on
    ``verdict``/``reviewer``/``scope``/``scope_kind``/``reviewed_paths`` for the same
    identity writes a genuinely new record plus a diagnostic naming both paths — it is
    never merged into the first (``reviewed_paths`` compares order-insensitively — a
    list re-derived in a different iteration order is NOT a divergence). This scan is
    session-scoped (the ``*-{session_id_short}*.json`` glob), so a cross-session re-run
    of the "same" review does not converge — see ``_reserve_unique_trail_path`` for why
    that scoping is kept rather than widened.

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
    - NO write to any TRACKED path outside ``state/review-trail/`` (D2(iv)).
      The one exception, added by C1b (docs/plans/2026-08-27-the-reviewed-
      set-is-a-file-not-a-computation.md): a successful write also folds
      this record into the reviewed-set store under
      ``.git/coordinator-review-trail/`` via
      ``coordinator_core.review_trail.backfill.resolve_and_fold`` — the
      same per-clone, gitignored-by-construction, untracked location
      ``session_scope.touch_written_path`` already writes to
      (``.git/coordinator-sessions/``) from this same handler. Never a
      SECOND write to ``state/review-trail/`` itself, and never a git
      commit (D2(v) below is unaffected).
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

MUTATES = ["state/review-trail/*.json"]  # timestamp+session-keyed, data-dependent set of additive-create entries
import sys

import asyncio
import datetime
import json
import logging
import os
import platform
import re
import subprocess
from coordinator_core.win_portability import (
    leaf_spawn_creationflags,
    no_console_creationflags,
)
import time
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional

from coordinator_core import chain_attribution, session_attribution
from coordinator_core.git import repo_root as repo_root_seam
from coordinator_core.session_attribution import GitLogFailed
from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.ipc import CallerFacingValidationError
from coordinator_core.ops._fm_util import extract_frontmatter_scalar
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.ops.session_context import resolve_current_session_id
from coordinator_core.review_trail import backfill as review_trail_backfill
from coordinator_core.session import scope as session_scope

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Env var override for test isolation: redirect entire trail dir to this root.
# When set, ``{REVIEW_TRAIL_OUTPUT_ROOT}/review-trail/`` is used instead of
# ``{caller_worktree}/state/review-trail/``.
_REVIEW_TRAIL_OUTPUT_ROOT_ENV = "REVIEW_TRAIL_OUTPUT_ROOT"

# Negative-spec: this module deliberately holds NO session-id env-var names of
# its own. The one handler that had them read `os.environ` directly and so
# stepped past the warm-serving identity override — see
# `_review_trail_write_handler`'s own session_id comment. Session identity here
# comes from `resolve_current_session_id` only; reintroducing a local env
# constant is how that defect comes back.

# Workstream env var (precedence order 2).
_COORDINATOR_REVIEW_WORKSTREAM_ENV = "COORDINATOR_REVIEW_WORKSTREAM"

# Validated enum values (mirrors oracle validation).
# "wsc-auto-adjudication"/"workstream-close-auto" were the OLD wsc_commit.py's
# _build_effective_review_trail machine-provenance auto-source sentinels
# (module retired 2026-07-29, kill-list op removal) — distinct from human/CI
# reviewer names, but still validated so any already-written auto-sourced
# records round-trip.
#
# "eng-director"/"senior-front-end"/"staff-ux"/"staff-data-sci" are the four
# dispatched-persona reviewers (the Director of Engineering/the Front-End Reviewer/the UX Reviewer/the Data Science Reviewer) the coordinator routing
# table registers alongside "staff-eng" (the Staff Engineer) — roster source is
# DoE-claude's coordinator/routing.md, spelled per each persona's agent-file
# stem (matching the "staff-eng" convention already in use here), never the
# human name. Nothing syncs this frozenset to that routing table automatically
# — a new registered persona needs its own manual addition here.
_VALID_REVIEWERS = frozenset(
    {
        "code-reviewer",
        "staff-eng",
        "code-reviewer+staff-eng",
        "eng-director",
        "senior-front-end",
        "staff-ux",
        "staff-data-sci",
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
#              eng-director, senior-front-end, staff-ux, staff-data-sci,
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
# Chain-ancestry waivers (formerly `state/review-trail/chain-ancestry-waivers/`,
# `coordinator_core.chain_ancestry_waivers`) were NOT this mechanism -- they
# were a separate, per-SHA, gate-minted provenance-not-discharge marker,
# orthogonal to whether a reviewer's OWN verdict is evidenced. Removed
# outright (state/kill-ledger.md K-005, 2026-08-16); this section never
# conflated the two and adds/takes nothing from that now-deleted mechanism.

# SECOND CONSUMER, not obvious from here: `hooks/subagent_review_mark.py ::
# _is_reviewer` imports this set and gates the SubagentStop
# `commit_ledger.store.mark_reviewed` write on membership. Adding a persona
# therefore does two things -- admits it to this op's `reviewer` enum, AND arms
# a durable commit-ledger write for that agent type. Kept as ONE set
# deliberately: `coverage.py` credits on the record's `kind`, never on reviewer
# identity, and `reviewed_by` stores the reviewer's NAME, so a consumer that
# wants to weigh a `staff-ux` pass differently from a `staff-eng` one has the
# data to. Splitting the sets would buy a maintained divergence against a
# consumer that does not exist. Pinned by
# `tests/test_review_trail_write.py :: test_delegate_reviewers_arms_the_commit_ledger_mark`.
# C9 (docs/plans/2026-08-27-the-review-gate-measures-the-whole-session.md): the
# set itself now lives in `ops.reviewer_vocabulary`, a stdlib-only leaf, and is
# re-exported here under its original private name so every by-name consumer
# (including the test named above) keeps working unchanged. Moved because
# `subagent_sandbox.provision_report._provision` — a PreToolUse-Agent hook, cold
# on EVERY agent dispatch — must consult this vocabulary per dispatch, and
# importing THIS module to read it measured 34.4ms marginal. Do not inline the
# frozenset back here: that reintroduces the cost at the reader, not here.
from coordinator_core.reviewer_vocabulary import DELEGATE_REVIEWERS as _DELEGATE_REVIEWERS
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
#: DoE's producer side landed at ``2cb87e464``. Matched case-sensitively --
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
    if stripped.startswith(_READ_ONLY_FALLBACK_TEXT):
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


def normalize_reviewer(reviewer: str) -> str:
    """*reviewer* with its agent-id/subagent-type namespace prefix removed when
    the bare remainder is a member of `_VALID_REVIEWERS`; unchanged otherwise.

    `_VALID_REVIEWERS` holds BARE names, but the value nearest to hand at a
    ceremony seam is the id the EM just dispatched
    (``coordinator:code-reviewer``, ``agent:staff-eng``), which differs from
    the accepted value only by that prefix. The op used to compute the bare
    name solely to say "did you mean 'code-reviewer'?" and then refuse anyway
    — a refusal that names its own remedy is a path the caller should not have
    to walk twice, and every caller of the open-loop record write
    (``coordinator/bin/freeze-review-diff.py :: _open_pending_trail_record``)
    swallows the refusal by design, so the round trip was never taken and the
    record simply went unwritten.

    The vocabulary stays CLOSED: a prefix is stripped only when what remains
    is already legal, so an unknown reviewer is rejected with or without one.
    ``coordinator:staff-eng`` is accepted and stored as ``staff-eng``; the
    on-disk record therefore never carries a namespaced name, and every
    by-value consumer of the ``reviewer`` field keeps working unmodified.

    Mirrors ``hooks.subagent_review_mark``'s own prefix-stripping, which reads
    the same closed vocabulary off this module.
    """
    _prefix, sep, bare = reviewer.rpartition(":")
    if sep and bare in _VALID_REVIEWERS:
        return bare
    return reviewer


def review_enum_errors(
    *, reviewer: str, scope: str, verdict: str, scope_kind: str
) -> list[str]:
    """Every closed-vocabulary violation among the four review-record enum
    fields, one message per offending field, empty when all four are legal.

    THE single authority for these messages. `_validate_review_fields` (the
    apply-time writer gate) and `workstream_complete.directives_commit_tail.
    _raise_on_review_enum_values` (the assemble-time gate that fires before
    any ceremony directive mutates) both call this, so a caller sees
    byte-identical text no matter which seam rejects them — a second,
    hand-copied allow-list in the composer would drift from this one silently.

    Exported (not `_`-private like the frozensets it reads) precisely because
    the vocabularies were unreachable outside this module: the discovery path
    for a caller composing `decisions["review"]` was author-a-guess, run
    apply, get rejected. Spec backlink: cross-repo/inbox/2026-08-18-doe-claude-
    em-review-trail-write-enums-undiscoverable-until-apply.md § 1.
    """
    errors: list[str] = []
    # Normalized, not raw: this function and `write_review_trail_entry` must
    # agree on what is legal, or the assemble-time gate passes a value the
    # apply-time writer then refuses. `normalize_reviewer` is a no-op on
    # anything already legal, so no previously-accepted value changes meaning.
    if normalize_reviewer(reviewer) not in _VALID_REVIEWERS:
        errors.append(
            f"review_trail.write: reviewer {reviewer!r} is invalid; "
            f"allowed: {' | '.join(sorted(_VALID_REVIEWERS))}"
        )
    if scope not in _VALID_SCOPES:
        errors.append(
            f"review_trail.write: scope {scope!r} is invalid; "
            f"allowed: {' | '.join(sorted(_VALID_SCOPES))} "
            "(scope is the record's coverage breadth, not the review's partition scale)"
            f"{_scale_shaped_scope_hint(scope)}"
        )
    if verdict not in _VALID_VERDICTS:
        errors.append(
            f"review_trail.write: verdict {verdict!r} is invalid; "
            f"allowed: {' | '.join(sorted(_VALID_VERDICTS))}"
        )
    if scope_kind not in _VALID_SCOPE_KINDS:
        errors.append(
            f"review_trail.write: scope_kind {scope_kind!r} is invalid; "
            f"allowed: {' | '.join(sorted(_VALID_SCOPE_KINDS))}"
        )
    return errors


def _scale_shaped_scope_hint(scope: str) -> str:
    """`normalize_reviewer`'s sibling for `scope`: `""` unless *scope* is a
    member of `decide_review_scale`'s `scale` vocabulary (`none` |
    `code-reviewer` | `partitioned` | `unresolved`), in which case names the
    axis collision explicitly rather than leaving the caller to notice it
    unaided from the allowed-set alone. `scope` (this module) is the
    record's coverage BREADTH; `scale` (`workstream_complete.
    directives_review.decide_review_scale`) is the review's PARTITION
    STRATEGY — two different axes that happen to share no legal values
    except by coincidence of a caller reading the wrong field name off
    `gates.review_scale`. See cross-repo/inbox/2026-08-15-example-retrieval-repo-em-
    wsc-review-trail-skips-silently.md."""
    if scope in _REVIEW_SCALE_VOCABULARY:
        return (
            f" (got {scope!r}, which is decide_review_scale's partition-strategy "
            "vocabulary, not scope's — scope is this record's coverage breadth, a "
            "different axis)"
        )
    return ""


_VALID_SCOPES = frozenset({"chain", "session", "workstream-close-auto"})
_VALID_VERDICTS = frozenset({"ok", "warn", "blocked", "waived", "pending"})

#: `decide_review_scale`'s (`workstream_complete/directives_review.py`) `scale`
#: vocabulary — a DIFFERENT axis from this module's `scope` (coverage breadth vs.
#: partition strategy). `partitioned` is the natural wrong guess a caller who just read
#: `gates.review_scale` makes when asked for `scope`: cross-repo/inbox/2026-08-15-
#: example-retrieval-repo-em-wsc-review-trail-skips-silently.md. See `_scale_shaped_scope_hint`.
_REVIEW_SCALE_VOCABULARY = frozenset({"none", "code-reviewer", "partitioned", "unresolved"})
_VALID_SCOPE_KINDS = frozenset({"diff", "plan", "integration"})

# Only [A-Za-z0-9_-] permitted in workstream slug (reject-to-null otherwise).
_WORKSTREAM_SLUG_RE = re.compile(r"[^A-Za-z0-9_-]")

# A pure hex token (full or abbreviated SHA) — never needs git to resolve.
# Mirrors coverage.py's _HEX_TOKEN (the read-side counterpart).
_HEX_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")

#: A FULL 40-char SHA, the only shape `git rev-parse` emits for a resolved ref.
#: Stricter than `_HEX_TOKEN_RE` on purpose: this validates git's own OUTPUT in
#: `_batch_resolve_ref_pair`, where an abbreviated or partial line means the
#: batch did not resolve cleanly and the per-token path must run instead.
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


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
            **leaf_spawn_creationflags(),
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


def _has_commit_history(caller_worktree: Path) -> bool:
    """True iff *caller_worktree* sits in a repo with real object storage —
    the question ``git rev-parse --is-inside-work-tree`` used to be spawned to
    answer. WALKS ONLY, never spawns.

    Why not ``repo_root.is_inside_work_tree``: that seam ALWAYS spawns, by its
    own documented design, to preserve a bare-repo distinction this call site
    does not ask about.

    Why not ``repo_root.show_toplevel`` alone: its walk stops at the first
    directory holding a ``.git`` ENTRY of any kind, so a stray or half-created
    ``.git`` directory reads as a repository when git itself refuses it ("not a
    git repository"). That difference is not academic — it is exactly the shape
    this module's own handler tests construct, and taking the walk's word for it
    turned a documented no-op into a hard refusal. So the resolved common dir is
    additionally checked for two of the three markers ``repo_root._looks_like_git_dir``
    uses (``HEAD`` and ``objects``, deliberately not ``refs``) — this call site only
    needs to rule out a stray/half-created ``.git``, not classify a bare repo, so the
    weaker two-marker check is intentional and not full parity with that helper.

    Test-isolation contract, unchanged from the spawn this replaces: a plain tmp
    dir with synthetic SHAs has no history to check a range against, so the
    caller no-ops rather than hard-failing against a non-repo.
    """
    common_dir = repo_root_seam.git_common_dir(str(caller_worktree))
    if not common_dir:
        return False
    resolved = Path(common_dir)
    return (resolved / "HEAD").exists() and (resolved / "objects").is_dir()


def _batch_resolve_ref_pair(
    left: str, right: str, cwd: Path
) -> Optional[tuple[str, str]]:
    """Concretize BOTH endpoints of a symbolic range in one ``git rev-parse``,
    or return None so the caller falls back to the per-token path.

    Why: a range with two symbolic endpoints (``HEAD~1..HEAD``, the shape a
    hand-run review uses) paid one spawn per endpoint to ask git the same
    question twice in a row. Process creation is the cost, not the query, and
    ``git rev-parse`` takes as many revs as it is given.

    ALL-OR-NOTHING by design, and the failure path is the reason. This function
    returns a result only when git resolved both endpoints to full hex SHAs and
    said so unambiguously; anything else — a non-zero exit, a missing line, a
    line that is not a SHA, a spawn error — returns None and lets
    ``_resolve_ref_to_sha`` run per token. That preserves the per-token
    ``ValueError`` naming WHICH ref failed, which a batched call cannot say (git
    reports the first bad rev and stops), and it keeps this fast path unable to
    introduce a refusal shape of its own.

    Both endpoints must be present and symbolic for the batch to be worth
    making: a hex token needs no spawn at all (``_resolve_ref_to_sha``'s own
    fast path), so a mixed range already costs exactly one spawn and batching
    would not lower it.
    """
    if not left or not right:
        return None
    for token in (left, right):
        head = re.split(r"[\^~]", token, maxsplit=1)[0]
        if _HEX_TOKEN_RE.match(token) or (head and _HEX_TOKEN_RE.match(head)):
            return None
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--revs-only", left, right],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            # Same pairing as `_resolve_ref_to_sha`: CREATE_NO_WINDOW alone
            # hangs on Windows when stdin is inherited/invalid.
            stdin=subprocess.DEVNULL,
            **leaf_spawn_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if len(lines) != 2:
        return None
    if not all(_FULL_SHA_RE.match(line) for line in lines):
        return None
    return lines[0], lines[1]


class _RangeWalk:
    """ONE ``git log`` walk over one range, shared by every consumer of a write.

    Purpose: this module asked git the same question about the same range twice
    per write — ``git rev-list --count`` for the emptiness guard, then
    ``git log --format=%H%x1f%P%x1f%(trailers:...)`` for the zero-credit
    diagnostic — and the second walk already enumerates everything the first
    counted. Process creation is the cost, not the query (CLAUDE.md § The
    brightline), so two walks over the same commits is one spawn spent for
    nothing. Measured on this box, 2026-08-22: ``git --version`` — the most
    trivial spawn that exists — is 3.3ms at p50 on macOS, and the same op's
    roster measurement on Windows is ~50x that per process, which is what puts
    `review_trail.write` at a 16.5s max there and 74ms here.

    Memoizes the FAILURE as well as the result: a caller that raises on
    ``GitLogFailed`` and a caller that stays silent on it must see the same
    verdict from the same walk, and a retry would be a second spawn.

    Negative-spec: keyed to the ONE ``(sha_range, caller_worktree)`` it was
    constructed for and never re-parameterized — this is a per-write cache, not
    a process-lifetime one. A window cached across ranges is the over-credit
    shape `chain_attribution`'s own anti-scope forbids, and a window cached
    across writes would serve a stale commit set to a later caller.
    """

    __slots__ = ("_sha_range", "_cwd", "_window", "_failure", "_walked")

    def __init__(self, sha_range: str, caller_worktree: Path) -> None:
        self._sha_range = sha_range
        self._cwd = str(caller_worktree)
        self._window: Optional[Dict[str, "chain_attribution.CommitAttribution"]] = None
        self._failure: Optional[GitLogFailed] = None
        self._walked = False

    def window(self) -> Dict[str, "chain_attribution.CommitAttribution"]:
        """``{sha: CommitAttribution}`` for every commit in the range, merges
        included. Raises ``GitLogFailed`` if git could not resolve the range —
        propagated, never swallowed to an empty map, so a caller can tell
        "unresolvable" from "resolves to nothing"."""
        if not self._walked:
            self._walked = True
            try:
                self._window = chain_attribution.bulk_commit_attribution_map(
                    self._sha_range, self._cwd, _git_runner,
                )
            except GitLogFailed as exc:
                self._failure = exc
        if self._failure is not None:
            raise self._failure
        return self._window or {}


def _reject_empty_sha_range(
    sha_range: str,
    caller_worktree: Optional[Path],
    *,
    batch_context: Optional[dict] = None,
    walk: Optional[_RangeWalk] = None,
) -> None:
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
    # Same test-isolation no-op contract as `_resolve_symbolic_range`: a
    # `caller_worktree` that is not itself inside a git work tree (e.g. a
    # plain tmp dir a unit test passes to exercise write-path logic with
    # synthetic SHAs) has no real commit history to check emptiness
    # against — skip rather than hard-fail on `git rev-list` erroring out
    # against a non-repo.
    # Security invariant (state/subagent-share/60a896a5-0b53-494d-b77a-
    # b4ca00e00f8c/coordinatorcode-reviewer-d8cd8353.md Finding 1): this
    # verdict is deliberately NOT read from `batch_context` — this is a
    # BLOCKING disposition, and `build_batch_attribution_context` never
    # populates such a key. Always re-derived, per call.
    if not _has_commit_history(caller_worktree):
        return
    # Counted off the SHARED range walk, not a `git rev-list --count` of its
    # own: the zero-credit diagnostic below already walks these exact commits,
    # and `len(window)` is the count this guard needs. One spawn, two consumers.
    # `walk` is threaded in by `write_review_trail_entry`; a direct caller that
    # passes none gets its own walk and the same verdict at the same cost this
    # guard has always paid.
    if walk is None:
        walk = _RangeWalk(sha_range, caller_worktree)
    try:
        window = walk.window()
    except GitLogFailed as exc:
        raise ValueError(
            f"review_trail.write: sha_range {sha_range!r} could not be resolved "
            f"by `git log` ({exc}) — refusing to persist a record for an "
            "unresolvable range"
        ) from None
    if not window:
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
    batched = _batch_resolve_ref_pair(left, right, caller_worktree)
    if batched is not None:
        return f"{batched[0]}{sep}{batched[1]}"
    resolved_left = _resolve_ref_to_sha(left, caller_worktree) if left else left
    resolved_right = _resolve_ref_to_sha(right, caller_worktree) if right else right
    return f"{resolved_left}{sep}{resolved_right}"



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
            # inherited/invalid; this is a LIVE git-invocation path, so it
            # must not be left half-fixed.
            stdin=subprocess.DEVNULL,
            **no_console_creationflags(),
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 2, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


# ---------------------------------------------------------------------------
# Write-time zero-chain-terminal-credit diagnostic
#
# state/audits/2026-08-07-wsc-chain-gate-counts-doc-only-commits.md (Q2, Q4,
# Q5); state/lessons/2026-08-06-a-chain-terminal-reviewer-cannot-record-
# 2220489ba97a.yaml.
#
# A self-contained single-commit slice with an honest range boundary is
# defensible for the writing session to record at all. The chain-terminal
# discharge path
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
#       chain-ancestry waiver (that mint is gone, K-005, so this evidence
#       source is now always empty) — the read side's narrowing then
#       empties this record's raw set to `set()` regardless of what
#       chain_dag/chain_code/chain_planning turn out to be later
#       (intersecting the empty set with anything is still empty).
#       `_record_membership_shas`'s `narrow_foreign_shas` leg narrows both
#       `diff` and `plan` records the same way (only `scope_kind in
#       _NON_CODE_SCOPE_KINDS`, i.e. "integration", skips that leg entirely
#       — see shape (b)).
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

#: Cap on the number of offending SHAs named in the zero-credit diagnostic's
#: detail message.
_FOREIGN_SHA_DISPLAY_CAP = 10

#: The op-result key the write-time zero-chain-terminal-credit diagnostic
#: (`_diagnose_zero_chain_terminal_credit`) is surfaced under -- named here
#: so callers/tests reference one symbol instead of re-typing the literal
#: string. Latent-bug fix: this constant was referenced by
#: `coordinator_core/ops/tests/test_review_trail_write.py` (added
#: 7917c93b9, 2026-08-07) but never actually defined anywhere -- a
#: mid-task session death left the drift-pin half of that commit
#: incomplete, and the three tests that import it have raised NameError
#: ever since. Must stay byte-identical to the literal used at the write
#: site below and in `_build_json_record`'s docstring table.
_ZERO_CREDIT_KEY = "chain_terminal_zero_credit_warning"


# ---------------------------------------------------------------------------
# C1 batching -- one union lookup per `write_review_trail_many` batch
# instead of N per-slice re-derivations
# (docs/plans/2026-08-15-the-review-trail-write-stops-paying-n-wa.md)
#
# `write_review_trail_many` calls `write_review_trail_entry` once per slice,
# each a one-commit `sha_range` (this module's own problem statement: "a
# different one-commit range"). Measured classification of the ~7 per-slice
# git spawns, before changing shape:
#
#   (a) IDENTICAL across every slice of one batch, hoistable outright:
#       - `git rev-parse --is-inside-work-tree` -- same cwd, same answer,
#         every time. Spawned TWICE per slice today (once in
#         `_guard_foreign_session_range`, once again in
#         `_reject_empty_sha_range`), so this alone is 2N spawns collapsing
#         to 1 for the whole batch.
#       - `_own_deliverable_id_for_recovery(own_session_id, caller_worktree)`
#         -- takes no `sha_range` parameter at all; identical for every
#         slice (only reached on the `unplaced_or_foreign` branch, so not
#         every slice pays it today, but every slice that does asks the
#         identical question).
#   (b) range-scoped, but answerable from ONE walk over the union of the
#       slice SHAs -- ONLY the write-time zero-chain-terminal-credit
#       diagnostic's P2 `chain_attribution` walk
#       (`_walk_range_commit_session_trailers`'s
#       `bulk_commit_attribution_map` + `bulk_grep_attributed_shas` pair,
#       2 spawns/slice). Every slice is a single-commit range by
#       construction, so `git log --no-walk <end1> <end2> ... <endN>` lists
#       every slice's own single-commit window entry in ONE call, with no
#       ancestor-graph resolution needed (`--no-walk` never walks parents),
#       replacing 2N spawns with 2 for the whole batch.
#   (c) irreducibly per-slice, NOT touched here: everything inside
#       `_guard_foreign_session_range` whose case-1/2/3 disposition depends
#       on the SPECIFIC narrow range's own commit set (`trailer_foreign_
#       shas`, `_own_session_touched_paths_and_untrailered_flag`,
#       `detect_foreign_commits`, `range_is_contiguous_suffix`, and the
#       ambiguous-single-commit `git rev-list --count` check) -- ~4-5
#       spawns/slice remain O(N). The guard's refusal STRENGTH (Anti-scope
#       constraint 3) depends on evaluating each slice's own range on its
#       own terms; collapsing those into one shared answer risks exactly
#       the isolation/strength regression the plan's Anti-scope forbids
#       ("Do not batch the per-slice op calls into one... Speed must not be
#       bought with the isolation property"). Left per-slice, and NOT
#       claimed as O(1)-in-N by this change — see the executor report for
#       the measured before/after split.
#
# (b) is deliberately scoped to the write-time zero-chain-terminal-credit
# DIAGNOSTIC only -- purely advisory (never raises, never blocks, never
# changes `verdict`; see that section's own module comment above) -- never
# the foreign-session guard itself, so Anti-scope constraint 3 (guard
# refusal strength) is untouched by this section. The fast path below can
# only ever UNDER-count a range that turns out NOT to be single-commit (it
# inspects only the range's own endpoint): per this module's own documented
# risk tolerance ("today's harmless false positive... [never]... a false
# NEGATIVE" -- `_walk_range_commit_session_trailers`'s pre-existing
# docstring, echoing the plan's own Anti-scope constraint 1), under-counting
# can only make this advisory flag fire in the SAME direction already
# tolerated (an extra, harmless warning on a record that in fact still
# credits something) -- it can never SILENCE a genuinely zero-credit write
# the unbatched walk would have caught, because a batched positive requires
# the endpoint itself to be foreign; a non-single-commit range's OTHER
# (unexamined) commits could only ADD more-foreign evidence, never subtract
# from what the endpoint alone already showed. A slice whose endpoint sha is
# not present in the precomputed window (batch prep skipped a non-hex/
# unparseable range, or `caller_worktree` was `None`) transparently falls
# back to the original, unbatched, fully-general per-range walk --
# correctness for that shape is unchanged.
# ---------------------------------------------------------------------------


def _range_end_sha(sha_range: str) -> Optional[str]:
    """Extract the right-hand (tip) token of a `<start>[..|...]<end>` range,
    or `None` if `sha_range` carries no `..`/`...` separator, or the token is
    not a concrete SHA-shaped hex string. Callers only ever hand this a
    `sha_range` that has already been through `_resolve_symbolic_range`, so
    a real symbolic ref (``HEAD``, a branch name) is not expected here — this
    is a defensive parse, not a resolution step.
    """
    sep = "..." if "..." in sha_range else (".." if ".." in sha_range else None)
    if sep is None:
        return None
    _left, _sep, end = sha_range.partition(sep)
    end = end.strip()
    if not end or not _HEX_TOKEN_RE.match(end):
        return None
    return end


def _sha_range_is_single_commit_shaped(
    sha_range: str, end_sha: str, *, is_merge: Optional[bool] = None,
) -> bool:
    """True iff *sha_range* is genuinely a single-commit range whose sole
    member is *end_sha* — i.e. TEXTUALLY shaped ``<end_sha>^..<end_sha>``
    (the only form `write_review_trail_many`'s callers construct per-slice;
    see `workstream_complete.__init__.py` line ~2965 and
    `directives_review.py`'s own docstring for the two spellings, ``^..`` and
    the equivalent-but-unused-here bare ``..`` single-parent form) AND
    genuinely single-commit in git's own range semantics — i.e. *end_sha* is
    not a merge commit.

    P2 fix (Finding 2, state/subagent-share/60a896a5-0b53-494d-b77a-
    b4ca00e00f8c/coordinatorcode-reviewer-d8cd8353.md): `_range_end_sha`
    alone only extracts the right-hand token — it says nothing about whether
    the LEFT-hand token actually names *end_sha*'s sole parent. A genuine
    multi-commit range (e.g. ``abc123..def456`` spanning several commits)
    whose endpoint is foreign but whose earlier commits are the writing
    session's own would, without this check, take the batched fast path in
    `_walk_range_commit_session_trailers` and be reported as "every commit
    is foreign" from the endpoint alone — a false
    `chain_terminal_zero_credit_warning`. Refusing the fast path for
    anything but this exact shape routes such a range to the original,
    fully-general per-slice `chain_attribution.bulk_commit_attribution_map`
    walk instead, which correctly examines every commit in the range.

    Merge-endpoint fix (Finding 1, state/subagent-share/20a161c3-3734-4e01-
    98db-6256978147dc/chain-review-lens1-attribution.md): the textual-shape
    check alone is INSUFFICIENT — for a merge commit ``M``, ``M^..M`` is not
    single-commit in git's actual range semantics. ``M^`` resolves to ``M``'s
    FIRST parent only, so the range ``M^..M`` walks every commit reachable
    from ``M`` but not from that first parent: the entire second-parent-side
    lineage the merge folded in. The batched fast path only ever computes
    attribution for ``M`` itself, so taking it for a merge endpoint would
    silently substitute ``M``'s own attribution for that whole folded-in
    lineage's attribution — not merely under-counting (this module's
    documented safe direction), but answering a different question, which
    can err either direction. *is_merge* is the caller's already-computed
    parent-count signal for *end_sha* (sourced from the same `git log`
    record the batched path already parsed — no extra git spawn); ``None``
    (parent count not determined) refuses the fast path, the same safe
    default as a genuine merge.
    """
    left, sep, right = sha_range.partition("^..")
    if not sep:
        return False
    if left != end_sha or right != end_sha:
        return False
    return is_merge is False


def _bulk_commit_attribution_map_no_walk(
    shas: List[str], cwd: str, run,
) -> Dict[str, "chain_attribution.CommitAttribution"]:
    """`--no-walk` sibling of `chain_attribution.bulk_commit_attribution_map`:
    resolves a bare LIST of concrete commit SHAs in ONE `git log --no-walk`
    invocation instead of one `git log <range>` walk per SHA. `--no-walk`
    lists exactly the named commits, with no ancestor-graph resolution, so N
    single-commit slice endpoints resolve in one call.

    Reuses `chain_attribution`'s own record format/parser (`_LOG_FORMAT` /
    `_parse_log_records`) so parsing semantics can never diverge from the P2
    primitive this module is required to use exclusively (module docstring;
    plan Anti-scope constraint 1). `chain_attribution.py` is outside this
    chunk's edit scope, so the small per-record `CommitAttribution`
    construction is duplicated here rather than factored out there — see
    `chain_attribution.bulk_commit_attribution_map`'s own body for the
    canonical version this mirrors field-for-field.

    Raises `GitLogFailed` on a non-zero `git log` returncode — same
    fail-closed contract as the primitive it mirrors.

    A hex-shaped token that does not resolve to a real object (e.g. a
    plausible-looking but nonexistent SHA) fails the WHOLE call: `git log
    --no-walk` does not resolve what it can and stay silent about the rest —
    it exits non-zero (`fatal: bad object <token>`) and this function raises
    `GitLogFailed` for the whole batch, per the contract above. Verified live
    (2026-08-15): `git log --no-walk --format=%H <real-sha> <bogus-hex-sha>`
    returns rc 128 with no stdout, not a partial result. In practice this is
    unreachable here anyway: the sole caller
    (`build_batch_attribution_context`) pre-filters `shas` through
    `_HEX_TOKEN_RE` before calling in, so a non-hex token never reaches this
    function — but a hex-shaped, non-resolving token would still hit the
    `GitLogFailed` branch above, not a silent per-token drop.
    """
    if not shas:
        return {}
    rc, out, err = run(
        ["git", "log", "--no-walk", f"--format={chain_attribution._LOG_FORMAT}", *shas],
        cwd,
    )
    if rc != 0:
        raise GitLogFailed(
            "git log --no-walk failed while batch-resolving commit "
            f"attribution for {len(shas)} sha(s): {err.strip() or 'unknown error'}"
        )
    result: Dict[str, "chain_attribution.CommitAttribution"] = {}
    for sha, parents, trailer in chain_attribution._parse_log_records(out):
        parent_shas = [p for p in parents.split(" ") if p]
        is_merge = len(parent_shas) > 1
        trailer_values = [v for v in trailer.split("\n") if v.strip()]
        if not trailer_values:
            trailer_session_id: Optional[str] = None
            ambiguous = False
        elif len(trailer_values) == 1:
            trailer_session_id = trailer_values[0].strip()
            ambiguous = False
        else:
            trailer_session_id = trailer_values[0].strip()
            ambiguous = True
        result[sha] = chain_attribution.CommitAttribution(
            sha=sha, trailer_session_id=trailer_session_id,
            is_merge=is_merge, trailer_ambiguous=ambiguous,
        )
    return result


def _bulk_grep_attributed_shas_no_walk(
    shas: List[str], session_id: Optional[str], cwd: str, run,
) -> FrozenSet[str]:
    """`--no-walk` sibling of `chain_attribution.bulk_grep_attributed_shas` --
    see `_bulk_commit_attribution_map_no_walk`'s docstring for the shape and
    rationale. Same validation/failure contract as the singular primitive:
    empty frozenset on a malformed `session_id` or any git failure, never
    fail-open.
    """
    if not shas or not session_id or not chain_attribution._UUID_RE.match(session_id):
        return frozenset()
    rc, out, err = run(
        [
            "git", "log", "--no-walk", "--no-merges",
            f"--grep=^Session-Id: {session_id}$",
            "--format=%H",
            *shas,
        ],
        cwd,
    )
    if rc != 0:
        return frozenset()
    return frozenset(line.strip() for line in out.splitlines() if line.strip())


# The complete set of keys `build_batch_attribution_context` may populate —
# every one of them advisory-only per that function's own "SECURITY
# INVARIANT" docstring paragraph. This is the structural half of that
# paragraph's "do not add a new key ... without first checking every
# consumer treats it as advisory-only" guidance: the docstring alone is a
# comment stating an invariant, not an enforcement of it, and the chain that
# introduced this comment (a76c9fa50) is the same chain whose own P1 was
# exactly that gap (`is_work_tree_rc` / `deliverable_id` — a comment, not a
# guard). `_ADVISORY_ONLY_BATCH_CONTEXT_KEYS` plus the assertion at the
# bottom of `build_batch_attribution_context` turn "check every consumer"
# into "the set changing at all fails loudly at construction," so a future
# edit that reintroduces a blocking-reachable key surfaces here first,
# before it ever reaches a consumer.
_ADVISORY_ONLY_BATCH_CONTEXT_KEYS = frozenset(
    {"own_session_id", "attribution_window", "grep_attributed"}
)


def build_batch_attribution_context(
    caller_worktree: Optional[Path],
    sha_ranges: List[str],
) -> dict:
    """Precompute, ONCE for a whole `write_review_trail_many` batch, the
    batchable ADVISORY-ONLY work this module's write-side guards do not
    depend on: `own_session_id`, and the P2 attribution window/grep answer
    for every slice's single-commit endpoint (feeds
    `_diagnose_zero_chain_terminal_credit` only).

    SECURITY INVARIANT (state/subagent-share/60a896a5-0b53-494d-b77a-
    b4ca00e00f8c/coordinatorcode-reviewer-d8cd8353.md Finding 1): nothing
    reachable from this context's return value may influence a BLOCKING
    disposition (`_guard_foreign_session_range`, `_reject_empty_sha_range`)
    — it may only pre-compute data for the advisory zero-credit diagnostic,
    which never raises and never blocks a write. This context reaches every
    JSON-RPC caller unfiltered as `_batch_context` (`ipc.py` does not strip
    unknown params keys), so any key here that a guard reads to short-
    circuit a blocking verdict is forgeable over the wire. The
    `is_work_tree_rc` and `deliverable_id` keys this function formerly
    populated were removed for exactly that reason — see the two guards'
    own comments at their (now-always-live) git re-derivation call sites.
    Do not add a new key here without first checking every consumer treats
    it as advisory-only.

    Returns `{}` when `caller_worktree` is `None` (the same test-isolation
    no-op contract every other git-backed check in this module honors) or
    `sha_ranges` is empty. An empty/partial context is always a safe,
    correctness-preserving input to every consumer below — each one falls
    back to its original, fully-general per-slice computation for whatever
    a partial context does not cover. This function never raises.
    """
    if caller_worktree is None or not sha_ranges:
        return {}

    # Security invariant (state/subagent-share/60a896a5-0b53-494d-b77a-
    # b4ca00e00f8c/coordinatorcode-reviewer-d8cd8353.md Finding 1): nothing
    # reachable from `_batch_context` may influence a blocking disposition
    # in `_guard_foreign_session_range` / `_reject_empty_sha_range` — it may
    # only pre-compute ADVISORY data (the zero-chain-terminal-credit
    # diagnostic). `is_work_tree_rc` and `deliverable_id` were removed from
    # this context because both were read by those two guards to short-
    # circuit a blocking verdict, and `ipc.py` does not strip unknown
    # top-level params keys — any JSON-RPC caller could forge
    # `_batch_context` over the wire (`review_trail_write.py`'s own
    # `_review_trail_write_handler` does `params.get("_batch_context")`
    # unconditionally) and pick a value for either key that flips a guard's
    # verdict, since `write_review_trail_many`'s in-process caller and a
    # wire caller dispatch through the exact same `params` dict. Do NOT
    # re-add either key to this context, or re-thread either key into a
    # guard's blocking path, without re-deriving this analysis: only
    # `own_session_id`, `attribution_window`, and `grep_attributed` are safe
    # here, because `_diagnose_zero_chain_terminal_credit` (the sole
    # consumer of the latter two) never raises and never blocks a write —
    # a forged value there can only make an advisory warning wrong.
    own_session_id = _resolve_session_id(caller_worktree)
    context: dict = {}
    if own_session_id:
        context["own_session_id"] = own_session_id

    end_shas = sorted({s for s in (_range_end_sha(r) for r in sha_ranges) if s})
    if not end_shas:
        return context

    try:
        context["attribution_window"] = _bulk_commit_attribution_map_no_walk(
            end_shas, str(caller_worktree), _git_runner,
        )
    except GitLogFailed:
        return context

    if own_session_id:
        context["grep_attributed"] = _bulk_grep_attributed_shas_no_walk(
            end_shas, own_session_id, str(caller_worktree), _git_runner,
        )
    assert context.keys() <= _ADVISORY_ONLY_BATCH_CONTEXT_KEYS, (
        f"build_batch_attribution_context: populated key(s) outside the "
        f"advisory-only allow-list: {sorted(context.keys() - _ADVISORY_ONLY_BATCH_CONTEXT_KEYS)} "
        "— re-read the SECURITY INVARIANT paragraph above before adding a "
        "new key here."
    )
    return context


def _walk_range_commit_session_trailers(
    sha_range: str, own_session_id: str, caller_worktree: Path,
    *, batch_context: Optional[dict] = None, walk: Optional[_RangeWalk] = None,
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
    # C1 fast path: a batch-precomputed window covering this range's own
    # single-commit endpoint answers this call from memory, ZERO additional
    # git spawns. See the "C1 batching" module comment above
    # `build_batch_attribution_context` for the full argument (including why
    # this can only ever under-count, never over-credit, a non-single-commit
    # range — the tolerated direction). Any other shape (no batch_context, a
    # session-id mismatch, or an endpoint the batch prep did not cover) falls
    # through unchanged to the original per-range walk below.
    if batch_context and batch_context.get("own_session_id") == own_session_id:
        window_all = batch_context.get("attribution_window")
        if window_all is not None:
            end_sha = _range_end_sha(sha_range)
            if (
                end_sha is not None
                and end_sha in window_all
                and _sha_range_is_single_commit_shaped(
                    sha_range, end_sha, is_merge=window_all[end_sha].is_merge,
                )
            ):
                grep_attributed = batch_context.get("grep_attributed") or frozenset()
                foreign = chain_attribution.foreign_shas_from_window(
                    [end_sha], own_session_id, window_all, grep_attributed,
                )
                return {end_sha: (end_sha in foreign)}

    # The SHARED walk `write_review_trail_entry` already paid for on this
    # range — `_reject_empty_sha_range` counted its commits off the same
    # window. A caller that passes none (a direct/test caller) gets its own.
    if walk is None:
        walk = _RangeWalk(sha_range, caller_worktree)
    try:
        window = walk.window()
    except GitLogFailed:
        return None
    if not window:
        return None
    # The grep leg is a SECOND `git log` over the same range, and it is read
    # only for a commit that is untrailered and not a merge
    # (`foreign_shas_from_window`'s last branch). When the window holds no such
    # commit its result cannot change this function's answer, so the spawn buys
    # nothing and is not made. `prepare-commit-msg` trailers every commit it
    # sees, which makes the elided case the ordinary one here rather than a
    # tuned-for edge.
    if chain_attribution.window_needs_grep_signal(window):
        grep_attributed = chain_attribution.bulk_grep_attributed_shas(
            sha_range, own_session_id, str(caller_worktree), _git_runner,
        )
    else:
        grep_attributed = frozenset()
    foreign = chain_attribution.foreign_shas_from_window(
        window.keys(), own_session_id, window, grep_attributed,
    )
    return {sha: (sha in foreign) for sha in window}


def _diagnose_zero_chain_terminal_credit(
    sha_range: str,
    scope: str,
    scope_kind: str,
    own_session_id: str,
    caller_worktree: Optional[Path],
    *,
    batch_context: Optional[dict] = None,
    walk: Optional[_RangeWalk] = None,
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
    foreign_map = _walk_range_commit_session_trailers(
        sha_range, own_session_id, caller_worktree,
        batch_context=batch_context, walk=walk,
    )
    if not foreign_map:
        return None
    foreign = frozenset(sha for sha, is_foreign in foreign_map.items() if is_foreign)
    if not foreign or len(foreign) != len(foreign_map):
        return None  # at least one commit is this session's own — not provably zero.
    # No waiver source remains (state/kill-ledger.md K-005, 2026-08-16 —
    # "waiver system dies"): every commit in `foreign` is therefore
    # provably zero-credit at the chain-terminal discharge path — there is
    # no vouched/waived/attested set left to subtract from it.
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
            f"({own_session_id!r}), or none at all — the "
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
            "commits — write per-slice records over this session's own "
            "commits instead. Narrowing sha_range further does not help "
            "here, since it is already narrowed to a range containing "
            "none of this session's own commits.",
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
    # Windows is this repo's first-class platform (CLAUDE.md) and, since
    # write_review_trail_many's per-slice writes fire concurrently
    # (asyncio.gather -> asyncio.to_thread, not a serialized loop), most or
    # all slices of one batch now routinely compute an identical base
    # filename candidate here — collision on the second is the expected case
    # for a batch, not an edge case. `_reserve_unique_trail_path`'s O_EXCL
    # retry loop is what carries every record to a distinct file; see
    # `TestAtomicWrite.test_concurrent_writers_same_candidate_all_survive`.
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


#: The load-bearing fields AC6 names explicitly: a second write sharing this
#: write's ``(session_id, sha_range)`` identity that disagrees on ANY of
#: these must produce a SECOND on-disk record, never a silent merge — this
#: is precisely the set a downstream consumer trusts to answer "was this
#: code reviewed, by whom, at what breadth, and with what verdict". Every
#: other field (``diff_loc``, ``workstream``, ``execution_basis``, ...) is
#: NOT load-bearing for identity purposes: a derived/incidental difference
#: there converges on the first writer (see ``_reserve_unique_trail_path``'s
#: "Identity and convergence" section).
#: Anti-scope: do not widen this set to "everything that drifted" — that
#: is the exact upsert this plan's AC6 forbids.
#:
#: ``scope_kind`` IS included (2026-08-15, review slice 2, C3 finding #2):
#: it selects which review-target TYPE (``diff``/``plan``/``integration``) a
#: record describes, and it gates whether ``reviewed_paths`` is even
#: serialized (omitted entirely for non-``diff`` scope_kinds — see
#: ``_build_json_record``) — so a ``scope_kind="diff"`` record written with
#: ``reviewed_paths=None`` and a ``scope_kind="plan"``/``"integration"``
#: record both read ``reviewed_paths -> None`` via ``.get`` and would
#: otherwise be indistinguishable on that field alone. Nothing in
#: ``_validate`` prevents a ``plan``/``integration`` ``sha_range`` from
#: coincidentally containing ``".."`` (only ``scope_kind == "diff"`` is
#: constrained to require it), so a same-session collision across scope
#: kinds is reachable, not merely theoretical. Adding ``scope_kind`` here
#: only makes convergence STRICTER (a scope_kind mismatch that previously
#: converged now diverges into a second record) — the safe direction under
#: AC6, which forbids two disagreeing records silently collapsing into one;
#: it cannot cause an unsafe collapse, and a genuine same-scope_kind replay
#: is unaffected (scope_kind is always identical on a true replay).
_LOAD_BEARING_IDENTITY_FIELDS = (
    "verdict", "reviewer", "scope", "scope_kind", "reviewed_paths",
)


def _load_bearing_fields_diverge(existing_record: dict, new_record: dict) -> bool:
    """True iff *existing_record* and *new_record* disagree on any of
    ``_LOAD_BEARING_IDENTITY_FIELDS`` — see that constant's docstring.

    ``reviewed_paths`` compares order-insensitively (sorted) — it is a
    caller-supplied path SET, not an ordered sequence, and nothing upstream
    guarantees iteration order is stable across a retry that re-derives the
    same set (e.g. from a different filesystem walk order). Comparing it
    order-sensitively would defeat convergence for two logically identical
    records — exactly the spurious-duplicate failure mode this identity
    check exists to remove — so normalize for THIS COMPARISON ONLY. The
    on-disk bytes (``_build_json_record``) are never touched: an emission
    envelope other repos read consumes ``reviewed_paths`` in caller-supplied
    order, and this normalization must not change what gets written.
    """
    for field in _LOAD_BEARING_IDENTITY_FIELDS:
        existing_value = existing_record.get(field)
        new_value = new_record.get(field)
        if field == "reviewed_paths":
            if isinstance(existing_value, list):
                existing_value = sorted(existing_value)
            if isinstance(new_value, list):
                new_value = sorted(new_value)
        if existing_value != new_value:
            return True
    return False


def _reserve_unique_trail_path(
    trail_dir: Path,
    base_filename: str,
    record_bytes: bytes,
    *,
    session_id: str,
    sha_range: str,
) -> Path:
    """Atomically claim a not-yet-existing path for *base_filename* under *trail_dir*
    and write *record_bytes* into it in full — never overwriting an existing file
    (2026-07-27 fix for the DR-216 D2(i) last-write-wins clobber defect).

    On the first attempt, tries ``base_filename`` verbatim — this keeps the common
    (no-collision) case's filename byte-identical to the pre-fix format, so every
    existing caller/test that depends on the bare ``{ts}-{sid}.json`` shape is
    unaffected. Only on an actual same-timestamp+session_id_short collision, or on a
    genuine identity divergence (see below), does this fall back to ``{stem}-2.json``,
    ``{stem}-3.json``, ... — appended after the session_id segment, which
    ``_shared._validate_review_trail_file``'s ``_TIME_SEG_RE`` regex already tolerates
    (it only anchors on the leading digit run for the reviewed_at timestamp; anything
    after the first ``-`` is opaque to it). Never a ``-integration.json``-shaped suffix
    (or any other hyphen-tailed suffix): ``example-retrieval-repo-ue-addon/bin/validate-artifact-
    shapes.py``'s suffix glob outranks its broad ``*.json`` and would validate such a
    file against the wrong schema — see the module docstring's filename-derivation
    section; this function introduces no new suffix shape.

    Identity and convergence (P2, docs/plans/2026-08-15-the-ceremony-tail-stops-lying-
    about-why-it-failed.md § C3 — supersedes the prior whole-record-bytes identity):
    record identity is ``(session_id, sha_range)``, not the serialized bytes. The
    prior byte-identity check converged only when EVERY field matched, and
    ``execution_basis`` is DERIVED (``_derive_execution_basis_from_sidecar`` omits it
    entirely on ``_SidecarUndetermined``) — a retry whose sidecar resolved differently
    the second time produced two records for what was logically one write. Before
    reserving anything, this scans existing records sharing this write's
    ``(session_id, sha_range)`` and applies one of two rules:

      - AGREE on every field in ``_LOAD_BEARING_IDENTITY_FIELDS`` (verdict, reviewer,
        scope, scope_kind, reviewed_paths) — CONVERGE-ON-FIRST-WRITER: return the existing path,
        write nothing new. This covers both the byte-identical replay case (an
        ``apply`` pass re-firing the same directive after a later step failed) and a
        derived-field-only difference (``execution_basis`` present on one write,
        absent on the other) — either way this is the SAME logical write, and the
        second call is a dedup-by-key no-op skip. The first record's bytes are NEVER
        rewritten to prefer whichever write happens to carry ``execution_basis``: that
        would be in-place mutation of an already-written file, which DR-216 D2's
        additive-create bound (affirmed by ``authz/classification.py``'s
        ``review_trail.write`` ``OP_CLASSIFICATION`` block; DR-213 §D4 does NOT govern
        this op — it is scoped to ``queue.*`` handlers over seven named ``state/``
        subdirs that do not include ``state/review-trail/``) forbids. A caller needing
        a basis a first-writer record lacks derives one from a later record's
        re-resolution, or reports absence — it does not force this write into a
        rewrite.
      - DISAGREE on any of those fields — DIVERGE: this is two records that disagree
        about the answer (most commonly ``verdict``), which is strictly worse to
        silently collapse than to duplicate. A diagnostic naming both paths is logged
        (never raised — a same-session re-review after fixes, with a corrected verdict
        over the same range, is a legitimate path this must not block), and the write
        proceeds to create a genuinely new file below. This never overwrites the
        divergent existing record.

    A field OUTSIDE ``_LOAD_BEARING_IDENTITY_FIELDS`` (``diff_loc``, ``workstream``,
    ``execution_basis``) differing alone does not block convergence —
    widening identity to "everything that drifted" is the exact upsert this plan's
    anti-scope forbids (it would make a legitimate re-review with a corrected verdict
    indistinguishable from noise on an unrelated field).

    SESSION SCOPING — deliberately kept session-scoped, not widened to a
    cross-session scan (see below the constant this function inherits from the
    caller). The scan is bounded to THIS session: *base_filename* has the shape
    ``{ts}-{session_id_short}.json``, so the session_id_short segment is extracted
    from it and only ``*-{session_id_short}*.json`` under *trail_dir* is globbed.
    Reasoned choice, not an oversight: (a) `session_id` is already half of the new
    identity key, so a cross-session scan would still need to filter by session_id
    after widening the glob — it buys nothing the identity check doesn't already
    reject; (b) a cross-session glob over the full directory is the unbounded scan
    this function's session-scoped design deliberately avoids (measured: a session
    holds ~40 trail records even when the directory holds thousands); (c) two
    DIFFERENT sessions never produce a record sharing this writer's exact
    ``(session_id, sha_range)`` key, by construction — ``session_id`` here is
    always this writer's own resolved session_id, never a peer's, so the key
    itself rules out a cross-session match rather than any runtime check
    doing so. A same-``sha_range``
    cross-session REPLAY (the same reviewing session re-invoked under a
    different session_id, e.g. after a coordinator restart) is NOT converged by
    this function and produces a new record — that is an intentional, narrower
    gap than "cross-session replay never converges" (P2's own writeup already
    flags the glob as session-scoped): the module's docstring at the top no
    longer implies replay-idempotence across a session boundary; only within one.

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
    (see cap's docstring — a defensive ceiling, not an expected path). Never raises on
    a load-bearing-field divergence (AC6) — that path logs and creates a second record.
    """
    assert base_filename.endswith(".json")
    stem = base_filename[: -len(".json")]

    try:
        new_record = json.loads(record_bytes.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        # Defensive only — record_bytes is produced by this module's own
        # _build_json_record and is always valid JSON in practice. A future
        # change to that function that breaks this invariant should not
        # crash the write; it just disables identity-based convergence for
        # this call (falls through to the plain collision-suffix path below).
        new_record = None

    # Identity-based convergence pre-check — see "Identity and convergence"
    # above. Bounded to this session's records only (never a full-directory
    # scan): base_filename is "{ts}-{session_id_short}.json", and ts itself
    # is dash-delimited ("YYYY-MM-DD-HHMMSS[ns]"), so rpartition on the LAST
    # "-" (not the first) to recover the session_id_short suffix and glob
    # only that session's files.
    divergent_existing: Optional[Path] = None
    _, _, session_id_short = stem.rpartition("-")
    if session_id_short and new_record is not None:
        for existing in trail_dir.glob(f"*-{session_id_short}*.json"):
            try:
                existing_bytes = existing.read_bytes()
            except OSError:
                continue
            try:
                existing_record = json.loads(existing_bytes.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if (
                existing_record.get("session_id") != session_id
                or existing_record.get("sha_range") != sha_range
            ):
                continue
            if not _load_bearing_fields_diverge(existing_record, new_record):
                # Converge-on-first-writer: same identity, no load-bearing
                # disagreement (byte-identical replay, or a derived-field-
                # only difference like execution_basis). No-op skip.
                return existing
            # AC6: divergent load-bearing field(s) on the same identity —
            # never merge, never raise. Remember it (first one found) so the
            # diagnostic below can name both paths against the ACTUAL final
            # path this write reserves, not a guess at base_filename.
            if divergent_existing is None:
                divergent_existing = existing

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
        if divergent_existing is not None:
            # AC6 diagnostic — logged only once the real final path is known
            # (candidate may have been suffixed past base_filename above on
            # an unrelated same-second collision).
            logger.warning(
                "review_trail.write: a second record for session_id=%r "
                "sha_range=%r disagrees with an existing record on a "
                "load-bearing field (one of %s) — wrote a SECOND record "
                "rather than silently discarding the disagreement. "
                "existing=%s new=%s",
                session_id,
                sha_range,
                _LOAD_BEARING_IDENTITY_FIELDS,
                divergent_existing,
                candidate,
            )
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

    enum_errors = review_enum_errors(
        reviewer=reviewer, scope=scope, verdict=verdict, scope_kind=scope_kind
    )
    # ALL invalid enum fields in one raise, not first-wins: a caller fixing a
    # closed-vocabulary mistake one field per invocation pays a round trip per
    # field, and every one of those round trips happens at a ceremony seam.
    # Each message is byte-identical to its former standalone form, so a
    # single-invalid-field call reads exactly as before.
    #
    # B (cross-repo/inbox/2026-08-15-example-retrieval-repo-em-wsc-review-trail-skips-
    # silently.md): `CallerFacingValidationError`, not a bare `ValueError` --
    # these messages already name their own legal value set, and a direct
    # (non-IPC) caller sees no change since it still subclasses `ValueError`.
    # Across the IPC boundary this now reaches the caller as `-32602 <this
    # message>` instead of the generic `-32603 Internal error: ValueError`
    # that discarded it before.
    if enum_errors:
        raise CallerFacingValidationError(" | ".join(enum_errors))
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
    _batch_context: Optional[dict] = None,
) -> dict:
    """Write one JSON entry to state/review-trail/.

    Byte-parity port of the bash oracle's write path.

    Parameters mirror oracle CLI flags (underscored); ``caller_worktree`` replaces
    oracle's cwd-based ``coordinator_state_root`` call (never daemon cwd).

    Parameters:
        sha_range   — e.g. ``abc1234..def5678``; required for scope_kind ``diff``.
        reviewer    — one of: code-reviewer, staff-eng, code-reviewer+staff-eng, eng-director,
                      senior-front-end, staff-ux, staff-data-sci, waived, ubt-compile,
                      wsc-auto-adjudication, em-verified.
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
        _batch_context — intended for internal in-process passthrough only
                      (a `build_batch_attribution_context(...)` result
                      threaded down by `ceremony.tail_ops.
                      write_review_trail_many`, C1, docs/plans/2026-08-15-
                      the-review-trail-write-stops-paying-n-wa.md), but
                      `ipc.py` does NOT strip unknown params keys, so this
                      key is reachable from any JSON-RPC caller over the
                      wire — treat it as attacker-controlled, not as proof
                      of an in-process caller. This is why every consumer
                      of this dict is restricted to ADVISORY-only data
                      (`own_session_id` for the zero-chain-terminal-credit
                      diagnostic's own-session comparison, plus the P2
                      attribution window/grep answers that diagnostic
                      alone reads) — see `build_batch_attribution_context`'s
                      own docstring "SECURITY INVARIANT" paragraph. `None`
                      (default) preserves every existing call site's
                      behavior exactly.

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
    # Before validation, so every downstream reader — the evidence check, the
    # sidecar-derived execution_basis, the commit-ledger mark, and the on-disk
    # record itself — sees the bare name and never a namespaced one.
    reviewer = normalize_reviewer(reviewer)

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
    # ONE walk over this range for the whole write: the emptiness guard below
    # counts its commits, and the zero-credit diagnostic at the tail classifies
    # the same ones. Constructed after `_resolve_symbolic_range` so it is keyed
    # to the CONCRETE range that actually gets persisted, never to a symbolic
    # spelling of it.
    range_walk = (
        _RangeWalk(sha_range, caller_worktree) if caller_worktree is not None else None
    )
    _reject_empty_sha_range(
        sha_range, caller_worktree, batch_context=_batch_context, walk=range_walk,
    )

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
    out_path = _reserve_unique_trail_path(
        trail_dir,
        filename,
        record_bytes,
        session_id=resolved_session_id,
        sha_range=sha_range,
    )

    # --- declare the write (after success, never speculatively) ---
    # Mirrors coordinator_core/dispatch/provision.py's touch_written_path
    # call sites (and coordinator_core/ops/ceremony/receipt_emit.py's C2
    # sibling): declare the RAW resolved_session_id, never a re-resolved
    # current session and never agent_id (see session_scope.
    # touch_written_path's own docstring for why). Only possible when we
    # know caller_worktree — the path the out_path is relative to — which
    # test-isolation callers routing through REVIEW_TRAIL_OUTPUT_ROOT
    # without a real caller_worktree do not supply, so declaration is
    # skipped rather than guessed. touch_written_path's own phantom-live-
    # peer guard additionally no-ops silently for a foreign/absent session
    # dir — not re-checked here, per that function's own docstring.
    if caller_worktree is not None:
        try:
            rel_out_path = out_path.relative_to(caller_worktree)
        except ValueError:
            pass
        else:
            session_scope.touch_written_path(
                resolved_session_id, str(rel_out_path).replace(os.sep, "/"), str(caller_worktree)
            )
            # --- write-time reviewed-set resolution (C1b) ---
            # Ordering is load-bearing (C1b brief): the record file above is
            # ALREADY durably created (O_EXCL succeeded) before this runs —
            # never the reverse. Applies coverage.py's five preserved credit
            # rules and folds the result into the reviewed_set store
            # (coordinator_core.review_trail.reviewed_set) keyed by this
            # record's own relative path — the SAME record id
            # `review_trail.backfill.run_backfill` derives for this file, so
            # a write-time-folded record is never re-folded by a later
            # backfill pass. Best-effort: a resolution failure here (a
            # transient git error, an unresolvable endpoint) must never fail
            # the write itself — it leaves the record UNRESOLVED, which the
            # backfill path (coordinator_core.review_trail.backfill) heals
            # on its next run, exactly like a crash between the two writes.
            # Assumes `caller_worktree` here is the SAME path `run_backfill`
            # is later invoked with for this file — both derive record_id as
            # the file's path relative to that root, POSIX-separated. Unstated
            # by either module; if a future backfill invocation runs against a
            # different root (e.g. a superproject) than writes used, ids
            # diverge and a record can double-credit.
            record_id = str(rel_out_path).replace(os.sep, "/")
            try:
                review_trail_backfill.resolve_and_fold(
                    str(caller_worktree),
                    [
                        (
                            record_id,
                            {
                                "sha_range": sha_range,
                                "verdict": verdict,
                                "scope_kind": scope_kind,
                                "scope": scope,
                                "session_id": resolved_session_id,
                            },
                        )
                    ],
                )
            except Exception:
                logger.warning(
                    "review_trail.write: write-time reviewed-set resolution "
                    "failed for %s — will be healed by the next "
                    "review_trail.backfill.run_backfill pass",
                    record_id,
                    exc_info=True,
                )

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
    #
    # NEGATIVE SPEC — this diagnostic is the one surface still reachable from
    # `_batch_context`, whose keys a JSON-RPC caller can forge (`ipc.py`
    # strips no unknown params). That is safe ONLY because the warning is
    # non-persisted and non-gating: a forger can fabricate or suppress a log
    # line, nothing more. Elevating this warning to gate a write, or
    # persisting it into the record, re-opens the a76c9fa bypass class.
    zero_credit_diagnostic = _diagnose_zero_chain_terminal_credit(
        sha_range, scope, scope_kind, resolved_session_id, caller_worktree,
        batch_context=_batch_context, walk=range_walk,
    )
    if zero_credit_diagnostic is not None:
        logger.warning(
            "review_trail.write: this record is a provable zero-credit write "
            "at the chain-terminal discharge path (reason=%s) — %s",
            zero_credit_diagnostic["reason"],
            zero_credit_diagnostic["detail"],
        )
        result[_ZERO_CREDIT_KEY] = zero_credit_diagnostic
    return result


# ---------------------------------------------------------------------------
# JSON-RPC handler
# ---------------------------------------------------------------------------


