"""
coordinator_core.ops.commit_anchors — JSON-RPC "commit.anchors" COMPUTE_ONLY operation.

Purpose: Derives a git-trailer block from the live read-model and staged diff, returning
it as a text string for the prepare-commit-msg hook to inject into the commit message.
Resolves only what can be determined unambiguously — omits any key that cannot be resolved
with confidence (precision over recall: a wrong anchor permanently embedded in git history
is worse than no anchor at all).

Trailer keys produced (in order, when resolvable):
    Nature:      — classification facet; derived from commit subject prefix taxonomy.
    Plan:        — repo-relative path of the plan file in the staged diff.
    Plan-Id:     — durable plan-grain join key (pln-... from plan frontmatter).
    Deliverable-Id: — durable cross-entity join key (dlv-... from plan frontmatter).
    Resolves:    — completion-grain join key (dlv-... from the SAME staged plan
                   frontmatter as Deliverable-Id:), emitted ONLY at a completion
                   event — i.e. only when an `archive/completed/*.md` completion
                   entry is ALSO present in the staged diff alongside the plan
                   file. This is the missing producer for the `Resolves:`
                   completion-grain convention (coordinator/docs/wiki/
                   resolves-commit-trailer.md); `rollup_derive.py`'s existing
                   `Resolves:`-only join is unchanged (see that module's own
                   negative-spec) — this op is the writer, not the reader.
    Anchor:      — nearest live handoff basename for this session (human-legible only).

Negative-spec — the key is `Deliverable-Id:`, NEVER `Deliverable:`. This op and
`coordinator/bin/coordinator-prepare-commit-msg` are two independent producers of the
SAME deliverable FK, and until 2026-07-28 they spelled it two different ways. git's
`%(trailers:key=X)` filter is an EXACT key match, not a prefix match, so every consumer
in the fleet — claude-klabauter's own `coverage.py` DAG attribution and `close_out_and_stamp`, and
Example-retrieval-repo's `workstate_store` ingest — read `Deliverable-Id` and silently saw nothing
from this op's four `Deliverable:`-stamped commits. Sibling naming is the tell: id-valued
keys carry the `-Id` suffix (`Plan-Id:`, `Session-Id:`); the bare `Plan:` is the path.

Source branches (DD#1 freshness discipline):
    Staged-diff  — Plan/Plan-Id/Deliverable-Id: from `git diff --cached --name-only`, then
                   staged-index frontmatter read via `git show :<path>` (COMPUTE_ONLY-safe
                   read-only subprocess). Self-verifying (file is in the commit) —
                   exempt from the DD#1 freshness gate.
    Live-state   — Anchor: on-disk state/handoffs/*.md scan IS the DD#1 live-state
                   cross-check; no separate re-read gate required (no in-memory
                   fabrication-check index involved).
    Derived      — Nature: COMMIT_EDITMSG subject prefix mapping, or nature param override.

Keying scope: common_dir — handler receives repo_root = git_common_dir (the .git directory).
The main worktree root is derived as repo_root.parent via main_worktree_root() from
coordinator_core/ops/fleet/_common.py (standard-layout assumption, documented there).

Self-registration: importing this module calls register_op("commit.anchors", _handler)
as a side-effect. Add this module to coordinator_core/ops/__init__.py to trigger
registration at start_server() time.

Spec backlink: pln-claude-klabauter-commit-anchor-stamper-q-29b891 § D2 / C1-op

Negative-spec (hard-won):
  - Does NOT write any git object (no git commit, git add, git notes, or
    git interpret-trailers --in-place).
  - Does NOT write any file under state/, archive/, or any other coordinator substrate path.
  - Does NOT stage the trailer via a temp file — returns text only; the mutation (injecting
    into the commit message) is git's own mechanism, executed by the hook (DoE surface, D3).
  - Does NOT emit Session-Id: — the prepare-commit-msg hook already stamps that separately.
  - Does NOT emit any key it cannot resolve unambiguously; precision over recall at every
    key boundary (claude-klabauter-commit-anchor-stamper.md § Anti-scope: a wrong anchor is worse than
    none because cockpit renders it as a confident false edge).
  - Does NOT use ctx.repo_root — it is None in the global service; uses the repo_root arg.
  - Does NOT emit Anchor: when multiple live handoffs match the session — omit on ambiguity.
  - Does NOT apply the DD#1 freshness gate to the staged-diff Plan branch — it is
    self-verifying (the plan file is literally the content being committed).
  - Plan frontmatter is read from the STAGED index (`git show :<path>`), not the working
    tree, so Plan-Id/Deliverable match the version actually committed (not a later local edit).
  - Does NOT emit Resolves: on every commit that carries a Deliverable-Id: — that is
    precisely the workstream-membership/completion conflation this key exists to avoid
    (docs/plans/2026-08-01-baton-spine-information-integrity.md § Anti-scope). Resolves:
    is gated on an additional, independent staged-diff signal: a completion entry
    (`archive/completed/*.md`) in the SAME staged set. A commit with only Deliverable-Id:
    and no staged completion entry never emits Resolves:.
  - Does NOT widen or touch `rollup_derive.py` / `parse_resolves_trailer.py` — this op
    is the producer; the oracle's existing exact-key `Resolves:` join is untouched.
  - Does NOT limit the divergence check to staged docs/plans/*.md files — a routine
    pathspec (one plan file plus the handoffs it spawned) checks `deliverable_id`
    across the WHOLE staged set (`_staged_deliverable_ids_diverge`); two or more
    distinct values omits Plan-Id/Deliverable-Id/Resolves together
    (staff-eng review finding 1, C7B — the confident-wrong-edge gap).
"""

from __future__ import annotations
import sys

import logging
import re
import subprocess
from coordinator_core.win_portability import no_console_creationflags
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from coordinator_core.claim_state import resolve_claim_state
from coordinator_core.dag import _parse_frontmatter, _read_meta
from coordinator_core.ipc import register_op
from coordinator_core.ops.fleet._common import main_worktree_root

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Nature prefix taxonomy
# Spec backlink: pln-claude-klabauter-commit-anchor-stamper-q-29b891 § D1
# ---------------------------------------------------------------------------

#: Closed enum of valid Nature values (slice-1, additive-only per cockpit co-design).
_NATURE_ENUM = frozenset({
    "bugfix",
    "infra",
    "roadmap",
    "refactor",
    "docs",
    "chore",
    "session-op",
})

#: Maps commit subject prefix (lowercase) to a Nature enum value.
#: Covers the standard coordinator subject-prefix taxonomy (D1 table).
_PREFIX_TO_NATURE: Dict[str, str] = {
    "fix":          "bugfix",
    "execute":      "roadmap",
    "execute-plan": "roadmap",
    "memo":         "session-op",
    "session-init": "session-op",
    "pickup":       "session-op",
    "refactor":     "refactor",
    "docs":         "docs",
    "chore":        "chore",
}

#: Conventional-commit subject prefix pattern: "prefix:" or "prefix(scope):".
_PREFIX_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9_-]*)(?:\([^)]*\))?:")

#: Handoff statuses that mark a handoff as no longer live (not a continuity candidate).
#: SSOT alias of HANDOFF_ANCHOR_EXCLUDED_STATUSES — that export carries defensive
#: non-schema tokens (e.g. "archived") and is a standalone SSOT export by design
#: (EM axis-check decision, DR-084 C3).
from coordinator_core.lifecycle_constants import HANDOFF_ANCHOR_EXCLUDED_STATUSES

_TERMINAL_STATUSES = HANDOFF_ANCHOR_EXCLUDED_STATUSES


# ---------------------------------------------------------------------------
# Nature derivation helpers
# ---------------------------------------------------------------------------


def _derive_nature_from_subject(subject: str) -> Optional[str]:
    """Map a commit subject line to a Nature enum value.

    Purpose: extracts the conventional-commit prefix from the subject and maps it to
    the coordinator Nature taxonomy. Returns the mapped value for known prefixes,
    "chore" for subjects with an unrecognised prefix (the subject has prefix shape but
    the prefix is unmapped), and None for subjects with no recognisable prefix pattern
    (empty, no colon) — callers treat None as unresolvable and omit the key.
    """
    subject = subject.strip()
    m = _PREFIX_RE.match(subject)
    if not m:
        # No "prefix:" pattern: Nature is not derivable from this subject.
        return None
    prefix = m.group(1).lower()
    return _PREFIX_TO_NATURE.get(prefix, "chore")


def _read_commit_subject(common_dir: Path) -> str:
    """Read the current commit subject from COMMIT_EDITMSG in the git common dir.

    Purpose: COMMIT_EDITMSG is written by git before the prepare-commit-msg hook fires;
    it contains the in-progress commit message. For non-hook callers the file may hold
    the previous commit's message.

    Returns the first non-empty, non-comment line from COMMIT_EDITMSG, or '' on any
    I/O error or when the file contains only comments / blank lines.
    """
    commit_msg_path = common_dir / "COMMIT_EDITMSG"
    try:
        content = commit_msg_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print(f"skip: _read_commit_subject: content = commit_msg_path.read_text(encoding=\"utf-8\", errors=\"replace\" failed: {sys.exc_info()[1]}", file=sys.stderr)
        return ""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped
    return ""


# ---------------------------------------------------------------------------
# Staged-index frontmatter reader (COMPUTE_ONLY-safe — read-only subprocess)
# ---------------------------------------------------------------------------


def _read_meta_from_staged(worktree_root: Path, plan_rel_path: str) -> dict:
    """Read and parse YAML frontmatter from the STAGED version of a plan file.

    Purpose: uses `git show :<repo-rel-path>` (read-only subprocess, COMPUTE_ONLY-safe)
    so that Plan-Id/Deliverable reflect the version actually committed, not a later local
    working-tree edit that was not re-staged.

    Review: code-reviewer — F2: read from staged index, not working tree, so the durable
    join keys (Plan-Id/Deliverable) match the version that lands in the commit.

    Returns {} on any I/O or parse error.
    """
    try:
        result = subprocess.run(
            ["git", "show", f":{plan_rel_path}"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(worktree_root),
            **no_console_creationflags(),
        )
    except (OSError, ValueError):
        print(f"skip: _read_meta_from_staged: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return {}
    if result.returncode != 0:
        return {}
    return _parse_frontmatter(result.stdout)


# ---------------------------------------------------------------------------
# Staged-diff Plan resolver (staged-diff branch — DD#1 exempt)
# ---------------------------------------------------------------------------


def _normalize_scope_paths(scope_paths: Optional[Sequence[str]]) -> Optional[frozenset]:
    """Canonicalize a caller-supplied commit pathspec to the forward-slash,
    repo-relative shape `git diff --cached --name-only` emits, or ``None``
    when the caller supplied no scope at all.

    ``None`` (no scope) and an EMPTY scope are deliberately distinct: no
    scope means "read the whole index" (the pre-scoping behaviour every
    other caller still relies on), while an empty scope would mean "this
    commit touches nothing" and is normalized to ``None`` rather than
    silently suppressing every anchor — a caller that passes `[]` has not
    told us anything about scope.
    """
    if not scope_paths:
        return None
    normalized = {
        str(p).replace("\\", "/").strip().lstrip("./")
        for p in scope_paths
        if str(p).strip()
    }
    return frozenset(normalized) or None


def _staged_files(
    worktree_root: Path, scope_paths: Optional[Sequence[str]] = None
) -> Optional[list]:
    """The staged path set, narrowed to this commit's own pathspec.

    Shared by the `Plan:` resolver and the `Resolves:` completion-entry
    gate so the two cannot drift on what "the staged set" means.

    Why the narrowing is load-bearing, not a refinement: on a SHARED
    worktree (this box's norm — see CLAUDE.md § Load norm) the index holds
    every concurrent session's staged work, so a bare `git diff --cached
    --name-only` answers "what is staged in the repo", never "what is in
    THIS commit". A ship commit scoped to four of its own files was stamped
    `Plan:`/`Plan-Id:`/`Deliverable-Id:` off a PEER's staged plan because
    that peer's plan happened to be the only `docs/plans/*.md` in the
    shared index — the ambiguity guard below cannot fire on a set of one,
    however foreign that one is. Measured 2026-08-18 on ship commit
    582c7b510, which carried `dlv-fl-core-03` while committing
    `dlv-chain-terminal-review-trail-discharge-d7b568`'s own artifacts.

    Returns ``None`` (never `[]`) on any git failure, matching the
    omit-rather-than-guess posture both callers already apply.
    """
    try:
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(worktree_root),
            **no_console_creationflags(),
        )
    except (OSError, ValueError):
        print(
            f"skip: _staged_files: subprocess.run failed: {sys.exc_info()[1]}",
            file=sys.stderr,
        )
        return None

    if result.returncode != 0:
        return None

    staged = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
    scope = _normalize_scope_paths(scope_paths)
    if scope is None:
        return staged
    return [f for f in staged if f.replace("\\", "/") in scope]


def _resolve_plan_from_diff(
    worktree_root: Path, scope_paths: Optional[Sequence[str]] = None
) -> Optional[Dict[str, Optional[str]]]:
    """Find a plan file in the staged diff and extract its frontmatter identity.

    Purpose: reads the staged set via `_staged_files` (READ-ONLY), narrowed to
    `scope_paths` when the caller supplied this commit's own pathspec, and
    looks for a single docs/plans/*.md file in it. Reads that file's
    frontmatter for plan_id (pln-...) and deliverable_id (dlv-...).

    Returns a dict {path, plan_id, deliverable_id} on success, or None when:
    - No docs/plans/*.md file is in the staged set.
    - Multiple plan files are staged (ambiguous — precision over recall → omit).
    - The git command fails (offline, not a repo).

    Staged-diff branch is self-verifying (the plan file is in the commit) — exempt
    from the DD#1 freshness gate (claude-klabauter-commit-anchor-stamper.md § D2).
    """
    staged_files = _staged_files(worktree_root, scope_paths)
    if staged_files is None:
        return None

    # Match docs/plans/<filename>.md — single path component only (no nested subdirs).
    plan_files = [f for f in staged_files if re.match(r"^docs/plans/[^/]+\.md$", f)]

    if len(plan_files) != 1:
        # Zero → no plan in this commit. Multiple → ambiguous. Both: omit.
        return None

    plan_rel_path = plan_files[0]
    # Read frontmatter from the STAGED index — guarantees Plan-Id/Deliverable match the
    # version actually committed, not a working-tree edit made after staging.
    # Review: code-reviewer — F2: use staged blob, not working-tree path.
    fm = _read_meta_from_staged(worktree_root, plan_rel_path)

    plan_id: Optional[str] = fm.get("plan_id")
    deliverable_id: Optional[str] = fm.get("deliverable_id")

    # Only emit Plan-Id for valid pln-... ids; Deliverable for valid dlv-... ids.
    if not (isinstance(plan_id, str) and plan_id.startswith("pln-")):
        plan_id = None
    if not (isinstance(deliverable_id, str) and deliverable_id.startswith("dlv-")):
        deliverable_id = None

    return {
        "path": plan_rel_path,
        "plan_id": plan_id,
        "deliverable_id": deliverable_id,
    }


def _staged_deliverable_ids_diverge(
    worktree_root: Path, scope_paths: Optional[Sequence[str]] = None
) -> bool:
    """True when the staged set (this commit's own pathspec) carries TWO OR
    MORE distinct `deliverable_id` values across ALL staged
    artifacts, not just staged docs/plans/*.md files.

    Staff-eng review finding 1 (critical): `_resolve_plan_from_diff` only
    detects ambiguity among staged docs/plans/*.md files -- blind to
    divergence among the OTHER staged artifacts (the PM's headline case:
    eleven staged handoffs), so a routine pathspec (one plan file plus the
    handoffs it spawned) let commit_anchors stamp that single plan's
    `Deliverable-Id:` onto a commit whose contents actually belong to
    several deliverables -- a CONFIDENT WRONG edge, the precision-cost side
    of this module's own stated asymmetry (module docstring, Negative-spec).
    `Resolves:` rides the same resolver and is gated on the same check.

    Reuses `commit_trailers._read_deliverable_id_from_frontmatter` for
    extraction -- the SAME leaf-read
    `commit_trailers._resolve_deliverable_id_from_paths` (the sibling
    producer already brought to this posture) uses for exactly this class
    of check, not a second copy of the resolution logic.

    Returns False (never ambiguous) when the staged set is empty/unreadable
    or carries zero/one distinct deliverable_id -- omit-rather-than-guess
    only fires on a genuine two-or-more-value collision.
    """
    from coordinator_core.git.commit_trailers import _read_deliverable_id_from_frontmatter

    staged = _staged_files(worktree_root, scope_paths)
    if not staged:
        return False

    found: Dict[str, str] = {}
    for rel_path in staged:
        deliverable_id = _read_deliverable_id_from_frontmatter(worktree_root / rel_path)
        if deliverable_id:
            found[rel_path] = deliverable_id

    return len(set(found.values())) > 1


def _resolve_plan_from_governing_slug(
    worktree_root: Path, governing_plan_slug: str
) -> Optional[Dict[str, Optional[str]]]:
    """Resolve Plan/Plan-Id/Deliverable-Id from the CALLER-SUPPLIED governing
    plan slug, reading `docs/plans/<governing_plan_slug>.md` directly from the
    WORKING TREE (not the staged diff / index) rather than `git show :path`.

    Purpose: an explicit `governing_plan_slug` names the workstream this
    commit closes independently of what happens to be staged in the shared
    index at commit time — the plan file itself is very often NOT part of
    this commit's own diff at all (already landed in an earlier commit this
    session, or not yet staged), so `_resolve_plan_from_diff`'s staged-diff
    scan cannot see it. Reading straight off disk is safe here specifically
    because the caller already told us which plan this is: nothing is being
    guessed from shared, foreign-session state. Residual risk: the read is
    a single working-tree snapshot with no retry, so a concurrent in-place
    edit to THIS SAME plan file (a frontmatter stamp mid-write, by this
    session or a collaborating one, on a shared tree) can land as a torn
    read. `_parse_frontmatter` never raises — a torn read parses to `{}`
    or a partial dict, same shape as a plan stub mid-enrichment — so it
    fails closed into the "resolved, no usable ids, logged loudly, no
    staged-diff fallback" branch below, never into stamping a transitional
    id.

    Fixes `2026-08-18-wsc-tail-commit-trailers-name-a-foreign-deliverable-
    3f7ac1d20e94.yaml`: `_resolve_plan_from_diff`'s staged-diff scan
    (correctly scoped to THIS commit's own paths) found zero plan files
    in-scope and returned `None`, so `_handler` fell through Plan/Plan-Id/
    Deliverable-Id entirely -- except `_staged_files` still degrades to a
    whole-shared-index read whenever its `scope` normalizes to `None` (see
    that function's own incident history), at which point the single
    `docs/plans/*.md` a concurrent peer happened to have staged wins by
    default, however foreign. `governing_plan_slug`, when supplied, is
    authoritative over that guess and never subject to it.

    Returns `None` ONLY when the plan file does not exist or is unreadable
    (nothing to be authoritative WITH — `_handler` falls back to the
    staged-diff scan in this case, unchanged).

    Returns a dict (never `None`) whenever the plan file exists and was
    read, EVEN WHEN neither `plan_id` nor `deliverable_id` resolves to a
    valid id (a plan stub mid-enrichment, or a torn read) — `plan_id`/
    `deliverable_id` may then both be `None`. This is deliberate: a
    caller-named governing plan that resolves but carries no usable ids
    must not let `_handler` silently fall through to the staged-diff
    guess, which is exactly the foreign-peer-plan class this function
    exists to stop. `_handler` logs this case loudly and does not emit
    Plan-Id:/Deliverable-Id: from any other source.
    """
    slug = governing_plan_slug.strip()
    if not slug:
        return None

    plan_rel_path = f"docs/plans/{slug}.md"
    plan_file = worktree_root / "docs" / "plans" / f"{slug}.md"
    try:
        text = plan_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    fm = _parse_frontmatter(text)
    plan_id = fm.get("plan_id")
    deliverable_id = fm.get("deliverable_id")

    if not (isinstance(plan_id, str) and plan_id.startswith("pln-")):
        plan_id = None
    if not (isinstance(deliverable_id, str) and deliverable_id.startswith("dlv-")):
        deliverable_id = None

    return {
        "path": plan_rel_path,
        "plan_id": plan_id,
        "deliverable_id": deliverable_id,
    }


#: Matches a staged completion-entry path written by
#: `coordinator_core.ops.coordinator_complete_entry._write_entry`
#: (`archive/completed/<yyyy-mm>/<slug>.md`) — the workstream-complete /
#: ship-handoff ceremony's own completion-event marker.
_COMPLETION_ENTRY_RE = re.compile(r"^archive/completed/[^/]+/[^/]+\.md$")


def _completion_entry_deliverable_id(worktree_root: Path, entry_rel_path: str) -> Optional[str]:
    """Resolve the `deliverable_id` a staged completion entry actually names.

    Purpose: `coordinator_complete_entry._write_entry` stamps a `chain:` frontmatter
    field naming the GOVERNING PLAN SLUG (not a deliverable_id directly) into the
    completion entry. Cross-reference that plan's own `deliverable_id:` frontmatter
    (docs/plans/<chain>.md) to recover the deliverable the completion entry
    actually completes.

    Review: code-reviewer — F1: `_has_staged_completion_entry` used to return True
    for ANY staged completion entry, so a commit staging plan A's frontmatter
    alongside an unrelated completion entry for plan B stamped `Resolves: dlv-A`
    falsely. This resolves the entry's OWN deliverable so the caller can require
    an exact match before stamping.

    Reads the STAGED blob for the completion entry (git show :path — the entry
    may be freshly staged, not yet committed) and the on-disk plan file (not
    necessarily part of this commit's staged diff at all — an already-committed
    plan for an unrelated chain). Returns None on any unresolvable hop — fail
    closed: the caller must treat None as "cannot verify" and OMIT `Resolves:`,
    never stamp against an unverified target.
    """
    fm = _read_meta_from_staged(worktree_root, entry_rel_path)
    chain_slug = fm.get("chain")
    if not (isinstance(chain_slug, str) and chain_slug.strip()):
        return None
    chain_slug = chain_slug.strip()

    plan_file = worktree_root / "docs" / "plans" / f"{chain_slug}.md"
    try:
        text = plan_file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        print(f"skip: _completion_entry_deliverable_id: plan_file.read_text( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None

    plan_fm = _parse_frontmatter(text)
    dlv_id = plan_fm.get("deliverable_id")
    if isinstance(dlv_id, str) and dlv_id.startswith("dlv-"):
        return dlv_id
    return None


def _has_staged_completion_entry(
    worktree_root: Path,
    deliverable_id: Optional[str],
    scope_paths: Optional[Sequence[str]] = None,
) -> bool:
    """True iff the staged diff includes an `archive/completed/*.md` entry that
    itself resolves to `deliverable_id` — the completion-event signal `Resolves:`
    is gated on.

    Purpose: `coordinator_complete_entry.py` scaffolds a completion entry only on
    the workstream-complete / ship-handoff ceremony path; an ordinary mid-flight
    commit (that merely carries `Deliverable-Id:` from workstream-membership)
    never stages one. Read-only `git diff --cached --name-only` — no write.

    Review: code-reviewer — F1: scoped to the deliverable actually being
    resolved. A staged completion entry that resolves to a DIFFERENT deliverable
    (or to none at all) no longer satisfies this gate — fail closed rather than
    stamping a false `Resolves:` on an unrelated deliverable.

    Spec backlink: DoE-claude:pln-baton-spine-information-integr-d3e1d7
    § A1 (b) — stamp Resolves: at the completion event, not on every commit.

    Read-only comparison — the trailer this gates on is always stamped with
    the caller's RAW `deliverable_id` (see `_handler`'s `Resolves:` trailer).
    """
    if not deliverable_id:
        return False
    staged = _staged_files(worktree_root, scope_paths)
    if staged is None:
        return False
    for path in staged:
        if path and _COMPLETION_ENTRY_RE.match(path):
            entry_dlv_id = _completion_entry_deliverable_id(worktree_root, path)
            if entry_dlv_id is not None and entry_dlv_id == deliverable_id:
                return True
    return False


# ---------------------------------------------------------------------------
# Anchor resolver (live-state branch — IS the DD#1 cross-check)
# ---------------------------------------------------------------------------


def _resolve_anchor(worktree_root: Path, session_id: str) -> Optional[str]:
    """Find the nearest live handoff basename for this session.

    Purpose: scans state/handoffs/*.md on-disk — the disk read IS the DD#1 live-state
    cross-check (not an in-memory fabrication-check index). No separate re-read gate
    required.

    Returns 'handoff/<stem>' when exactly one live, non-terminal handoff names
    session_id in its picked_up_by or claimed_by (consumed_by fallback, DR-084
    transitional — restored 2026-07-23) field.
    Returns None when:
    - Zero matching handoffs (session not yet associated with a handoff).
    - Multiple matching handoffs (ambiguous — precision over recall → omit).
    - state/handoffs/ does not exist or is unreadable.

    Anchor: is a human-legible breadcrumb only — NOT a durable join key
    (claude-klabauter-commit-anchor-stamper.md § D1 co-design resolution: Anchor demoted from
    durable join to display breadcrumb; durable graph uses Plan-Id/Deliverable/Session-Id).

    Claim-holder matching is ledger-first via `claim_state.resolve_claim_state` (C1,
    coordinator_core/claim_state.py) — NOT a raw read of the frontmatter mirror's
    `claimed_by`/`consumed_by` fields. On a desynced baton (mirror reverted by a
    branch switch while the branch-independent claim ledger still holds the claim —
    the incident `claim_state.py`'s module docstring names) the raw-mirror read used
    to come back empty and the Anchor: trailer was permanently lost — there is no
    later pass that reconstructs it once the commit is made. `resolve_claim_state`
    surfaces the ledger holder in that case; a dead ledger holder degrades to
    `source == "mirror"`/`"none"`, never falsely reported as `"ledger"`.
    `picked_up_by` has no ledger counterpart and is still read from the mirror
    directly — only the claimed_by/consumed_by hop is migrated.
    """
    if not session_id:
        return None

    handoff_dir = worktree_root / "state" / "handoffs"
    if not handoff_dir.is_dir():
        return None

    try:
        # Review: code-reviewer — F10: sorted() removed; return is a single-element match
        # or discarded, so ordering is irrelevant and the sort was behaviorally inert.
        candidates = list(handoff_dir.glob("*.md"))
    except OSError:
        print(f"skip: _resolve_anchor: candidates = list(handoff_dir.glob(\"*.md\")) failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None

    matches: List[str] = []
    for path in candidates:
        fm = _read_meta(str(path))
        if not fm:
            continue

        status = fm.get("status") or ""
        if status in _TERMINAL_STATUSES:
            continue  # Terminal handoff — not a live continuity anchor.

        picked_up_by = fm.get("picked_up_by")

        # Review: code-reviewer — F3: use equality (not substring) so session IDs do not
        # false-match on prefix/suffix variants. List form (YAML sequence) uses exact
        # membership — the `in` operator on a list is element equality, not substring.
        session_in_picked = (
            (isinstance(picked_up_by, str) and picked_up_by.strip() == session_id)
            or (isinstance(picked_up_by, list) and session_id in picked_up_by)
        )

        claim_state = resolve_claim_state(path, repo_root=worktree_root)
        session_in_claim = claim_state.holder == session_id

        if session_in_picked or session_in_claim:
            matches.append(path.stem)  # filename without .md extension

    if len(matches) == 1:
        return f"handoff/{matches[0]}"

    # Zero or multiple → omit (precision over recall).
    return None


# ---------------------------------------------------------------------------
# Op handler
# ---------------------------------------------------------------------------


@register_op("commit.anchors")
def _handler(
    params: dict,
    repo_root: Optional[Path] = None,
) -> dict:
    """JSON-RPC "commit.anchors" handler — COMPUTE_ONLY, sync.

    Purpose: Derives a git-trailer block from the staged diff and live read-model and
    returns it as text. The prepare-commit-msg hook (DoE surface, D3) injects the block
    into the commit message. Returns {"trailers": ""} when nothing is resolvable.

    Sync (not async): all I/O in this handler and its full call tree is synchronous
    (subprocess.run + disk reads). ipc.py offloads sync handlers via asyncio.to_thread,
    keeping the event loop live for other partitions and making DISPATCH_TIMEOUT_SECS
    effective. Review: code-reviewer — F1: AC-3 Gap-3 fix; converting to sync is the
    correct pattern when no `await` appears anywhere in the call tree.

    Params:
        session_id  (str)      — composing session identity, used for Anchor resolution.
        nature      (str|null) — EM-supplied Nature override. When non-null and a valid
                                 _NATURE_ENUM member, used verbatim. When null, Nature is
                                 derived from the COMMIT_EDITMSG subject prefix taxonomy.
        governing_plan_slug (str|null) — caller-supplied plan slug this commit closes
                                 against (`docs/plans/<slug>.md`). AUTHORITATIVE over the
                                 staged-diff Plan/Plan-Id/Deliverable-Id scan whenever its
                                 own plan file exists at all (see
                                 `_resolve_plan_from_governing_slug`) — the staged-diff scan
                                 degrades to a whole-shared-index read on some scope shapes
                                 and a peer's unrelated staged plan can otherwise win by
                                 default. A staged-diff match whose PATH or deliverable_id
                                 DISAGREES with the resolved governing plan is logged loudly
                                 (never silently preferred) and then discarded in favor of
                                 the governing slug's own values. When the governing plan
                                 file exists but carries no valid plan_id/deliverable_id
                                 (a stub mid-enrichment, or a torn read), that is ALSO
                                 logged loudly and the staged-diff result is discarded
                                 rather than substituted — only Plan: (no ids) is emitted.

    Returns:
        {"trailers": "<newline-joined Key: value lines, or '' when nothing resolvable>"}

    COMPUTE_ONLY affirmation (five questions, authz/classification.py discipline):
        1. No git write executed (no commit / add / notes / interpret-trailers --in-place).
        2. No file written under state/, archive/, or any coordinator substrate path.
        3. No temp file written that a caller reads back.
        4. Returns computed text only; mutation is git's own mechanism via the hook.
        5. Read-only git subprocesses (git diff --cached, git show :path) are permitted
           (ipc.py:44 carve-out).

    Keying scope: common_dir — repo_root is the .git directory (git_common_dir result).
    """
    session_id: str = params.get("session_id") or ""
    # This commit's own pathspec, when the caller knows it. Absent, the
    # staged-diff readers below fall back to the whole index — correct for a
    # sole-occupant tree, WRONG on a shared one, where the index carries
    # every concurrent session's staged work. See `_staged_files`.
    raw_paths = params.get("paths")
    scope_paths: Optional[Sequence[str]] = (
        [str(p) for p in raw_paths] if isinstance(raw_paths, (list, tuple)) else None
    )
    nature_override: Optional[str] = params.get("nature")
    governing_plan_slug: str = str(params.get("governing_plan_slug") or "")

    trailers: List[str] = []

    # ------------------------------------------------------------------
    # 1. Nature: — param override takes priority; fall back to subject derivation.
    # Cardinality 1 when derivable; omit only when truly unresolvable.
    # ------------------------------------------------------------------
    nature_value: Optional[str] = None

    if nature_override is not None and isinstance(nature_override, str):
        # EM-supplied override: validate against the closed enum.
        if nature_override in _NATURE_ENUM:
            nature_value = nature_override
        else:
            # Invalid override token — treat as "chore" (caller supplied a nature,
            # just an unknown one; still better than falling back to subject derivation).
            # Review: code-reviewer — F11: log the coercion so it's observable in daemon
            # logs; silent mislabeling would produce a wrong Nature: in permanent git history.
            logger.warning(
                "commit.anchors: unrecognized nature_override %r; falling back to 'chore'",
                nature_override,
            )
            nature_value = "chore"

    if nature_value is None and repo_root is not None:
        # No valid override: derive from COMMIT_EDITMSG subject prefix.
        # repo_root IS the .git common dir — COMMIT_EDITMSG lives there directly.
        subject = _read_commit_subject(repo_root)
        if subject:
            nature_value = _derive_nature_from_subject(subject)
        # If subject is empty or unresolvable → nature_value stays None → key omitted.

    if nature_value is not None:
        trailers.append(f"Nature: {nature_value}")

    # ------------------------------------------------------------------
    # 2. Plan: / Plan-Id: / Deliverable-Id: — staged-diff branch (DD#1 exempt).
    # Reads docs/plans/*.md from the staged index; self-verifying.
    # ------------------------------------------------------------------
    if repo_root is not None:
        worktree_root = main_worktree_root(repo_root)

        plan_info = _resolve_plan_from_diff(worktree_root, scope_paths)
        governing_info = (
            _resolve_plan_from_governing_slug(worktree_root, governing_plan_slug)
            if governing_plan_slug
            else None
        )
        if governing_info is not None:
            if governing_info.get("plan_id") is None and governing_info.get("deliverable_id") is None:
                # The governing plan file resolved but carries no valid pln-/dlv-
                # id (a plan stub mid-enrichment, or a torn read). The staged-diff
                # result must NOT be allowed to supply Plan/Plan-Id/Deliverable-Id
                # here -- that is precisely the foreign-peer-plan class this
                # fix exists to stop. See `_resolve_plan_from_governing_slug`'s
                # own docstring for the incident this guards against.
                logger.warning(
                    "commit.anchors: governing_plan_slug %r resolved to %s but "
                    "carries no valid plan_id/deliverable_id -- staged-diff "
                    "result (if any) discarded rather than substituted",
                    governing_plan_slug, governing_info.get("path"),
                )
            elif (
                plan_info is not None
                and (
                    plan_info.get("path") != governing_info.get("path")
                    or (
                        plan_info.get("deliverable_id")
                        and plan_info.get("deliverable_id") != governing_info.get("deliverable_id")
                    )
                )
            ):
                # Never silently prefer the staged-diff guess -- see
                # `_resolve_plan_from_governing_slug`'s own docstring for the
                # incident this disagreement guards against. Widened beyond a
                # deliverable_id mismatch: a staged-diff match on a DIFFERENT
                # plan path whose deliverable_id is null or coincidentally
                # equal still disagrees and must still be logged.
                logger.warning(
                    "commit.anchors: staged-diff plan scan resolved %s "
                    "(deliverable_id=%s), which DISAGREES with the supplied "
                    "governing_plan_slug %r (path=%s, deliverable_id=%s) -- the "
                    "governing slug wins, staged-diff result discarded",
                    plan_info.get("path"), plan_info.get("deliverable_id"),
                    governing_plan_slug, governing_info.get("path"), governing_info.get("deliverable_id"),
                )
            plan_info = governing_info
        if plan_info is not None:
            trailers.append(f"Plan: {plan_info['path']}")

            # Confident-wrong-edge guard (staff-eng review finding 1): the
            # staged set may carry more than one deliverable across the
            # OTHER staged artifacts (handoffs, etc.), not just staged
            # docs/plans/*.md files -- see `_staged_deliverable_ids_diverge`.
            # Plan-Id/Deliverable-Id/Resolves all ride the SAME resolved
            # deliverable_id and are omitted together on divergence; Plan:
            # (the path, not a join key) is unaffected.
            staged_ids_diverge = _staged_deliverable_ids_diverge(worktree_root, scope_paths)
            if staged_ids_diverge:
                logger.warning(
                    "commit.anchors: staged set carries divergent deliverable_id "
                    "values across staged artifacts -- omitting Plan-Id/"
                    "Deliverable-Id/Resolves rather than guessing (plan=%s)",
                    plan_info.get("path"),
                )

            if plan_info["plan_id"] and not staged_ids_diverge:
                trailers.append(f"Plan-Id: {plan_info['plan_id']}")
            if plan_info["deliverable_id"] and not staged_ids_diverge:
                trailers.append(f"Deliverable-Id: {plan_info['deliverable_id']}")

                # --------------------------------------------------------
                # 2b. Resolves: — completion-grain join key (missing producer,
                # docs/plans/2026-08-01-baton-spine-information-integrity.md
                # § A1). Reuses the SAME deliverable_id already resolved for
                # Deliverable-Id: above (same staged plan frontmatter) — this
                # is NOT a second independent resolution path. Gated on an
                # additional completion-event signal so an ordinary mid-flight
                # commit (Deliverable-Id: only) never emits Resolves:.
                # --------------------------------------------------------
                if _has_staged_completion_entry(
                    worktree_root, plan_info["deliverable_id"], scope_paths
                ):
                    trailers.append(f"Resolves: {plan_info['deliverable_id']}")

        # ------------------------------------------------------------------
        # 3. Anchor: — nearest live handoff (on-disk scan, DD#1-compliant breadcrumb).
        # Omit on ambiguity (zero or multiple matches).
        # ------------------------------------------------------------------
        if session_id:
            anchor = _resolve_anchor(worktree_root, session_id)
            if anchor is not None:
                trailers.append(f"Anchor: {anchor}")

    return {"trailers": "\n".join(trailers)}
