"""
coordinator_core.ops.draft_plan_aging — mechanized draft-plan staleness/liveness detector.

Purpose: faithful clone of handoff_gate_aging's structure (same frontmatter-scalar
reader, same single-file-or-directory scan, same 0/1/2 exit contract), adapted to
the draft-plan liveness predicate. A stale, orphaned draft plan must not sit
silently in docs/plans/ forever — this module is the detector; the caller
(workday-start/workweek-start) drives the surfacing + decision prompt.

Predicate — a draft plan is STALE iff ALL hold:
  1. status: draft (frontmatter) — cheap short-circuit.
  2. now - created: >= 14 days.
  3. Liveness discriminator — NO recent real-work commit AND NO active baton:
     3a. Resolve scope: frontmatter paths, prefix-normalized (strip a leading
         "plugins/coordinator-claude/" so both the published-repo convention
         and this repo's bare convention resolve against this tree).
     3b. No commit within the aging window on a normalized scope path whose
         subject is NOT on the mechanical denylist (pickup:, reclaim(docs),
         session-init, frontmatter mutation). A no-scope plan fails 3b
         definitionally (toward surfacing) — no plan-doc-level git-log
         fallback.
     3c. No non-claimed handoff in state/handoffs/*.md references the plan's
         on-disk path in its body. An active baton suppresses STALE.

Checker-sidecar exclusion: docs/plans/ also holds report artifacts emitted by
prior-art-checker / reviewer / docs-checker / plan-coverage-checker
(*.prior-art-check.md, *.review.md, *.docs-check.md, *.plan-coverage-check.md).
These carry status:/created: frontmatter of their own but are NOT plans — both
the directory scan and single-file mode exclude/short-circuit them.

Exit codes (parity-critical — both callers branch on these):
  0 — no stale draft plans found
  1 — one or more stale draft plans found (fail loud)
  2 — internal error (missing path, bad frontmatter, unparseable date,
      git-log failure, or unresolvable handoff-read failure)
  3 — DoE trampoline transport failure (engine-root resolution / import
      failure) — NEVER returned by this module itself; allocated by
      coordinator/bin/draft-plan-aging.sh (DoE) as a dedicated code so
      transport failure can't collide with this module's own 0/1/2 business
      codes above, mirroring verify_dist_publish_repo_sync's identical
      resolution.

Port of: draft-plan-aging.sh (DoE b5a4192c, 2026-07-20)
Spec backlink: DoE-claude:pln-continuity-artifact-staleness--bec61c § Design Fix #2, § Chunks C1
Port backlink: docs/plans/2026-07-15-bash-to-naked-python-engine-migration.md

Negative-spec:
    - Does NOT resolve GIT_ROOT — both the `git log` scope-path check and the
      `state/handoffs` scan run against the invoking process's ambient cwd,
      exactly like the bash oracle (which never `cd`s or resolves a repo
      root either). Callers must invoke this from within the target repo.
    - Does NOT parse full YAML for the `scope:` block extraction — reads a
      fixed indentation shape (`  - <path>` lines), byte-identical to the
      awk idiom in the bash oracle and to dirty_tree_gate's sibling reader.
    - Directory scan is one level only (`*.md`, no recursion), sorted for
      deterministic output — the bash oracle's `find -maxdepth 1` order is
      filesystem-arbitrary, but STALE detection is per-file-independent so
      this ordering refinement changes presentation only, not correctness.
    - Precedence quirk inherited verbatim from the bash oracle (and from
      handoff_gate_aging before it): when a directory scan mixes a parse
      error (file A) with genuine staleness (file B), STALE wins and the
      scan exits 1, not 2 — the parse-error diagnostic is still printed to
      stderr. This is intentional-inherited, not an oversight.

Sibling op — `plan.list_stale_executing` (JSON-RPC, EXTEND wave 1 C1g):

Purpose: a second, simpler predicate over the same docs/plans/ corpus —
`status: executing` (not `status: draft`) plans whose most recent real git
commit against the plan file itself is older than a caller-given threshold.
Unlike the STALE-draft predicate above (which needs the 3-part liveness
discriminator because an inert draft is the NORMAL long-lived state for a
plan not yet picked up), an `executing` plan with no recent commit activity
is a stronger, simpler signal — it was claimed and then apparently
abandoned mid-flight — so this predicate is a direct frontmatter-status +
git-log-mtime age check, no scope-path / handoff-baton cross-reference.

Port source: commands/workday-start.md:353 (fence: "Loop docs/plans/*.md,
awk frontmatter for status:executing, compute age via git log mtime, filter
>3 days").

Wire params:
    threshold_days (int, required) — age-in-days cutoff; a plan's age is
                    computed from its own file's most recent commit
                    timestamp (`git log -1 --format=%ct -- <path>`), NOT a
                    `created:` frontmatter field. A plan is reported iff
                    age_days >= threshold_days.

Reply:
    {"stale": [{"path": <repo-relative str>, "age_days": <int>}, ...]}
    Empty list, never an error, when no plan is currently `status:
    executing` or none exceeds the threshold — advisory-only, never blocks
    (oracle rationale: "never blocks" — see distinct-ops-new.tsv).

Scope: common_dir (per op-classification.tsv) — this op scans the
CALLING repo's OWN docs/plans/, not claude-klabauter's. `repo_root` (injected by
ipc.dispatch_message as the request's git_common_dir) is REQUIRED; the
handler fails loud when it is None rather than silently falling back to
this repo's own docs/plans/ (the exact double-fail-open the manifest's
scope-verdict column warns against). The main-worktree root is derived via
coordinator_core.ops.fleet._common.main_worktree_root(repo_root) — same
convention as goal.append / artifact.emit — and docs/plans is a plain join
off it (DEC-1: no resolver exists for this join and none is warranted).

Idempotency (AC7): trivially satisfied — a pure read-only scan (git log
queries + frontmatter reads only, zero writes) is a safe no-op on any
re-invocation; the oracle rates both idempotency- and platform-hazard
"none" for this row, so no DEC-7 docstring note is required.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from coordinator_core.frontmatter.primitives import split_frontmatter
from coordinator_core.ipc import register_op
from coordinator_core.lifecycle_constants import PLAN_ORPHAN_TERMINAL_STATUS
from coordinator_core.ops._fm_util import extract_frontmatter_scalar
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.win_portability import no_console_creationflags


_CREATIONFLAGS = no_console_creationflags()

AGING_THRESHOLD_DAYS = 14

EXECUTING_STATUS = "executing"
DRAFT_STATUS = "draft"
REVIEWED_STATUS = "reviewed"
APPROVED_STATUS = "approved"
LANDED_STATUS = "landed"

# Checker-sidecar suffix denylist (report artifacts, not plans — see module
# docstring). Kept as a tuple so it's greppable and extensible.
SIDECAR_SUFFIXES = (".prior-art-check.md", ".review.md", ".docs-check.md", ".plan-coverage-check.md")

# Mechanical commit-subject denylist prefixes (anchored — always prefixes in
# this repo's commit-message convention).
_MECHANICAL_PREFIXES = ("pickup:", "reclaim(docs)", "session-init")
# "frontmatter mutation" appears as a SUFFIX ("pickup: X — frontmatter
# mutation") and must stay an unanchored substring check.
_MECHANICAL_SUBSTRING = "frontmatter mutation"

_PROG = "draft-plan-aging.sh"  # literal program-name prefix — mirrors bash oracle stderr text
_GIT_LOG_TIMEOUT_SECS = 15


def _is_sidecar_file(path: str) -> bool:
    """True iff the basename ends in a checker-sidecar suffix (report artifact, not a plan)."""
    base = os.path.basename(path)
    return any(base.endswith(suffix) for suffix in SIDECAR_SUFFIXES)


def _is_census_local_sidecar(file_path: Path, directory: Path) -> bool:
    """Census-local sidecar predicate — used ONLY by `list_orphaned`'s
    population walk, additive to (never a replacement for) `_is_sidecar_file`
    above. Does NOT touch `SIDECAR_SUFFIXES`, `_is_sidecar_file`, `scan()`, or
    `check_one()` — AC10 parity depends on those staying untouched.

    A file is a sidecar of some parent plan iff its name has the shape
    `<stem>.<token>.md` where `<stem>.md` exists as a real file in
    *directory*. Concretely: split the stem (name minus the trailing `.md`)
    on `.`, left to right; if ANY resulting prefix + `.md` names a real file
    in *directory* (other than the file itself), this file is that plan's
    sidecar.

    This catches sidecar-naming variants `_is_sidecar_file`'s fixed
    four-suffix denylist cannot (`.review-the Director of Engineering.md`, `.node-map.md`,
    `.phase0.md`, a timestamped `.plan-coverage-check.<ts>.md`, etc.)
    without guessing at more literal suffixes — see module "Sibling op:
    plan.list_orphaned" docstring section, "Population" paragraph, for the
    corpus measurement that motivated this.

    A plan filename with no dot in its stem (the overwhelming common case,
    including hyphen-dated names like `2026-07-05-strang-03.md`) never
    matches — there is no prefix to test. A dotted plan name whose prefix is
    NOT itself a real file on disk also does not match, by construction —
    the anti-over-exclusion guard: this predicate never removes a plan that
    has no real parent to be a sidecar of.

    Residual limitation (documented, not fixed): a filename that legitimately
    contains a dot AND whose prefix coincidentally matches an unrelated real
    plan file on disk (e.g. a hypothetical `v1.2-notes.md` sitting alongside
    an unrelated real `v1.md`) is misclassified as that unrelated file's
    sidecar, with no actual parent/sidecar relationship between them. Left
    unnarrowed deliberately — requiring a marker-token-shaped suffix would
    reintroduce the suffix-guessing fragility this structural predicate
    replaced, and measurement across this repo's live corpus found zero
    false positives of this shape.
    """
    name = file_path.name
    if not name.endswith(".md"):
        return False
    stem = name[: -len(".md")]
    parts = stem.split(".")
    for i in range(1, len(parts)):
        prefix = ".".join(parts[:i])
        candidate = directory / f"{prefix}.md"
        if candidate.is_file():
            return True
    return False


def _read_scope_paths(text: str) -> List[str]:
    """Extract `scope:` block paths from YAML frontmatter.

    Mirrors the bash oracle's awk idiom:
        awk '/^scope:/{found=1; next}
             found && /^  - /{print substr($0, 5)}
             found && /^---/{exit}
             found && /^[a-z]/{exit}'
    """
    paths: List[str] = []
    found = False
    for line in text.splitlines():
        if not found:
            if line.startswith("scope:"):
                found = True
            continue
        if line.startswith("  - "):
            paths.append(line[4:])
            continue
        if line.startswith("---"):
            break
        if re.match(r"^[a-z]", line):
            break
    return paths


def _normalize_prefix(path: str) -> str:
    """Strip a leading "plugins/coordinator-claude/" prefix (condition 3a).

    So both the published-repo convention and this repo's bare convention
    resolve against this tree — the DoE-vs-published prefix trap.
    """
    prefix = "plugins/coordinator-claude/"
    if path.startswith(prefix):
        return path[len(prefix):]
    return path


_COMPLETED_ARCHIVE_DIR = os.path.join("archive", "completed")


# Review: overengineering-reviewer — dropped the `root` param; no caller ever
# passed it, and it was a config axis R2 was written to close off.
def _load_completed_deliverable_ids() -> frozenset:
    """Read every `archive/completed/**/*.md` completion entry's frontmatter
    `deliverable_id:` scalar ONCE, returning the set of ids that have a
    shipped completion entry.

    Join-key choice (defect 1 fix): `deliverable_id` equality, not `chain:`
    slug or `commits:` list membership. This is the SAME join key
    `resolve_plan_owner`/`list_orphaned` already use to link a plan to its
    owning handoff elsewhere in this module, and R2 (2026-08-04, see
    `resolve_plan_owner`'s docstring) rules out a second join key that can
    disagree with it — `chain:` is a human-authored slug with no uniqueness
    guarantee across unrelated workstreams, and `commits:` is a short-SHA
    list that only proves activity, not identity. A completion entry's own
    `deliverable_id:` frontmatter scalar (present on entries authored after
    the carry-observability fix, `_CARRY_OBSERVABILITY_FIX_LANDED_ON`) is
    the same value the plan itself carries, so equality is a direct,
    unambiguous join with no secondary-key disagreement risk.

    Called at most once per `scan()`/CLI invocation (see `scan()`'s
    threading of the result into `check_one`) — never once per plan, to stay
    inside the 200ms-per-process / no-second-full-corpus-scan brightline
    budget named in the dispatch brief. Missing directory or unreadable file
    degrades to an empty/partial set, never an exception (same degrade-not-
    crash idiom as the rest of this module).
    """
    ids = set()
    if not os.path.isdir(_COMPLETED_ARCHIVE_DIR):
        return frozenset()
    for dirpath, _dirnames, filenames in os.walk(_COMPLETED_ARCHIVE_DIR):
        # Review: overengineering-reviewer — unsorted; the result feeds a
        # set, so ordering is unobservable, and this is a budgeted path.
        for name in filenames:
            if not name.endswith(".md"):
                continue
            path = os.path.join(dirpath, name)
            try:
                entry_text = Path(path).read_text(encoding="utf-8")
            # Review: code-reviewer (Finding 1) — UnicodeDecodeError is a
            # ValueError subclass, not an OSError; without it, one malformed
            # file in the 657-entry externally-authored corpus crashed the
            # whole scan(), taking staleness detection down for every plan.
            except (OSError, UnicodeDecodeError):
                continue
            entry_deliverable_id = extract_frontmatter_scalar(entry_text, "deliverable_id")
            if entry_deliverable_id:
                ids.add(entry_deliverable_id)
    return frozenset(ids)


# Review: overengineering-reviewer — required, not Optional[...]=None; the
# self-load branch had no production consumer and was where a future
# in-loop caller would silently re-pay the full-corpus read.
# Review: code-reviewer (Finding 2) — renamed from
# `_has_recent_real_work_commit`: the completion-entry arm below has no time
# bound (deliberate — delivered work isn't stale regardless of age), so
# "recent" no longer describes half of what this function returns. Only the
# git-log arm stays windowed; see docstring.
def _has_real_work_or_completion_match(
    file: str, completed_deliverable_ids: frozenset
) -> Tuple[Optional[bool], Optional[str]]:
    """Condition 3b: does ANY normalized scope path have a real-work commit
    (non-mechanical subject) within the aging window, OR does this plan's own
    `deliverable_id` match a shipped `archive/completed/` completion entry?

    The completion-entry arm closes defect 1: a plan delivered entirely
    through commits that never touch one of its own `scope:`-cited paths
    (the common case) previously read as having no real work at all. Both
    arms are ORed — the completion-entry match is additive, not a
    replacement for the pre-existing scope-path git-log arm.

    Only the git-log arm is windowed (`AGING_THRESHOLD_DAYS`, 14 days). The
    completion-entry arm is deliberately unbounded — a plan shipped years
    ago that still matches a completion entry is real, delivered work
    regardless of age, so it is not subject to the aging window at all.

    *completed_deliverable_ids* is the pre-loaded
    `_load_completed_deliverable_ids()` result — required, not optional:
    callers in a per-plan loop (`scan()`) MUST pass this through rather than
    letting each call rebuild it, to avoid a second full-corpus read per
    plan. There is no self-loading default; direct callers/tests pass the
    result of a single `_load_completed_deliverable_ids()` call (or an empty
    `frozenset()`) explicitly.

    Returns (True, None) if a real-work commit or a completion-entry match
    was found (NOT stale-toward on this condition), (False, None) if neither
    was found (including the no-scope-block case, which is definitionally
    False on the scope-path arm alone — no plan-doc-level git-log fallback),
    or (None, diagnostic) on a git-log invocation failure (distinct from a
    valid empty result).
    """
    try:
        text = Path(file).read_text(encoding="utf-8")
    except OSError:
        # Mirrors the bash oracle's awk `2>/dev/null` silent-degrade: an
        # unreadable file yields an empty scope-path list, not a crash.
        text = ""

    plan_deliverable_id = extract_frontmatter_scalar(text, "deliverable_id")
    if plan_deliverable_id and plan_deliverable_id in completed_deliverable_ids:
        return True, None

    raw_scope_paths = _read_scope_paths(text)
    scope_paths = [_normalize_prefix(p) for p in raw_scope_paths if p]

    if not scope_paths:
        # No-scope-block case: no scope path to prove a real-work commit
        # against. Condition 3b is definitionally False — fails toward
        # surfacing. Do NOT fall back to a plan-doc-level git-log check.
        return False, None

    try:
        result = subprocess.run(
            ["git", "log", f"--since={AGING_THRESHOLD_DAYS} days ago", "--format=%s", "--", *scope_paths],
            capture_output=True,
            text=True,
            timeout=_GIT_LOG_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"skip: _has_real_work_or_completion_match: result = subprocess.run( failed: {exc}", file=sys.stderr)
        return None, (
            f"{_PROG}: {file}: git log failed on scope paths ({' '.join(scope_paths)}): {exc}"
        )

    if result.returncode != 0:
        combined = (result.stdout or "") + (result.stderr or "")
        return None, (
            f"{_PROG}: {file}: git log failed on scope paths ({' '.join(scope_paths)}): {combined}"
        )

    log_output = result.stdout
    if not log_output.strip():
        # Valid empty result: no commits in window at all — not an error, not real work.
        return False, None

    for subject in log_output.splitlines():
        if not subject:
            continue
        lc_subject = subject.lower()
        if (
            not lc_subject.startswith(_MECHANICAL_PREFIXES)
            and _MECHANICAL_SUBSTRING not in lc_subject
        ):
            return True, None

    # Every in-window commit was mechanical.
    return False, None


def _has_active_baton(file: str) -> Tuple[Optional[bool], Optional[str]]:
    """Condition 3c: does any non-'claimed' handoff in state/handoffs/*.md
    reference this plan (its own on-disk path, as given) in its body?

    Also matches a plan referenced ONLY in a baton's structured `scope:`
    frontmatter list, never in narrative prose (defect 2 fix) — the
    pre-existing `plan_path in hf_text` scan is a raw substring match over
    the full handoff body and is blind to that case. Reuses this module's
    existing `_read_scope_paths` reader (the same one used for a plan's own
    scope block) rather than adding a third YAML-block reader, and
    `_normalize_prefix`-normalizes both the handoff's scope entries and the
    plan's own path before comparing, since a scope entry may carry the
    `plugins/coordinator-claude/` prefix variant.

    Returns (True, None) if an active baton is found (suppresses STALE),
    (False, None) otherwise. On a state/handoffs/ read failure, returns
    (None, diagnostic).
    """
    handoffs_dir = "state/handoffs"

    if not os.path.isdir(handoffs_dir):
        # No handoffs directory at all: nothing can reference the plan.
        return False, None

    try:
        entries = sorted(os.listdir(handoffs_dir))
    except OSError as exc:
        print(f"skip: _has_active_baton: entries = sorted(os.listdir(handoffs_dir)) failed: {exc}", file=sys.stderr)
        return None, f"{_PROG}: {file}: failed to read {handoffs_dir}: {exc}"

    # Match target: the plan's own on-disk path, as given on the CLI — no
    # prefix-variant construction here (the plan's own path is never
    # prefix-ambiguous under this repo's convention; only scope-cited paths
    # inside the plan body are, and that's condition 3a's job, not 3c's).
    plan_path = file

    for name in entries:
        if not name.endswith(".md"):
            continue
        hf = os.path.join(handoffs_dir, name)
        if not os.path.isfile(hf):
            continue
        try:
            hf_text = Path(hf).read_text(encoding="utf-8")
        except OSError:
            print(f"skip: _has_active_baton: hf_text = Path(hf).read_text(encoding=\"utf-8\") failed: {sys.exc_info()[1]}", file=sys.stderr)
            continue

        status = extract_frontmatter_scalar(hf_text, "status")
        if status in ("claimed", "consumed"):
            continue

        if plan_path in hf_text:
            return True, None

        hf_scope_paths = {_normalize_prefix(p) for p in _read_scope_paths(hf_text) if p}
        if _normalize_prefix(plan_path) in hf_scope_paths:
            return True, None

    return False, None


# Review: overengineering-reviewer — completed_deliverable_ids is keyword-only
# and required, not Optional[...]=None: every production caller already
# passes it, and an optional self-loading default is exactly the shape a
# future in-loop caller silently reacquires the per-plan corpus through.
def check_one(
    file: str,
    today: Optional[date] = None,
    *,
    completed_deliverable_ids: frozenset,
) -> Tuple[str, int]:
    """Evaluate the draft-plan staleness predicate for one plan file.

    *completed_deliverable_ids*: pre-loaded `_load_completed_deliverable_ids()`
    result, threaded through to `_has_real_work_or_completion_match`. `scan()`
    loads this once per invocation and passes it to every `check_one` call in
    its loop — never rebuilt per plan (brightline: no second full-corpus scan
    per plan).

    Returns (stdout_line_or_empty, rc). rc is 0 (not stale / no error) or 2
    (parse error / internal error). A non-empty stdout_line signals STALE.
    """
    if today is None:
        today = date.today()

    try:
        text = Path(file).read_text(encoding="utf-8")
    except OSError:
        # Mirrors the bash oracle's awk `2>/dev/null` silent-degrade: an
        # unreadable file reads as having no frontmatter fields at all.
        text = ""

    status = extract_frontmatter_scalar(text, "status")
    # Condition 1: status must be draft — cheap short-circuit.
    if status != "draft":
        return "", 0

    created = extract_frontmatter_scalar(text, "created")
    if not created:
        print(
            f"{_PROG}: {file}: draft plan missing 'created:' field — cannot evaluate aging",
            file=sys.stderr,
        )
        return "", 2

    try:
        created_date = date.fromisoformat(created.strip())
    except (ValueError, AttributeError):
        print(f"{_PROG}: {file}: unparseable created date '{created}'", file=sys.stderr)
        return "", 2

    age_days = (today - created_date).days
    # Condition 2: age >= 14 days.
    if age_days < AGING_THRESHOLD_DAYS:
        return "", 0

    # Condition 3b: recent real-work commit check.
    has_recent_work, err = _has_real_work_or_completion_match(file, completed_deliverable_ids)
    if err is not None:
        print(err, file=sys.stderr)
        return "", 2
    if has_recent_work:
        # Recent real-work commit found -> not stale.
        return "", 0

    # Condition 3c: active baton check.
    has_baton, err = _has_active_baton(file)
    if err is not None:
        print(err, file=sys.stderr)
        return "", 2
    if has_baton:
        # Active baton found -> not stale.
        return "", 0

    return (
        f"STALE: {file} (created {age_days}d ago, no real-work commit, no owning baton)",
        0,
    )


def scan(target: str, today: Optional[date] = None) -> Tuple[List[str], int]:
    """Top-level driver — mirrors the bash file's FILES=(...) collection + loop.

    target: a single plan file path, or a directory (scanned one level for
    *.md files, checker-sidecars excluded, sorted for deterministic output).

    Returns (stale_lines, rc) with rc arbitration: STALE wins over
    PARSE_ERROR-with-zero-stale (inherited verbatim from the bash oracle —
    see module negative-spec).
    """
    if os.path.isdir(target):
        files = sorted(
            os.path.join(target, name)
            for name in os.listdir(target)
            if name.endswith(".md") and not _is_sidecar_file(os.path.join(target, name))
        )
    else:
        if _is_sidecar_file(target):
            # Single-file mode on a checker sidecar: not a plan, never stale.
            return [], 0
        files = [target]

    completed_deliverable_ids = _load_completed_deliverable_ids()

    lines: List[str] = []
    stale_count = 0
    parse_error = False
    for f in files:
        line, rc = check_one(f, today, completed_deliverable_ids=completed_deliverable_ids)
        if rc == 2:
            parse_error = True
        if line:
            lines.append(line)
            stale_count += 1

    if parse_error and stale_count == 0:
        return lines, 2
    if stale_count > 0:
        return lines, 1
    return lines, 0


# ---------------------------------------------------------------------------
# Field-aware ownership resolver (AC1/AC2/AC14) — plan.list_orphaned's C1
# building block. Additive: does NOT touch _has_active_baton/check_one/scan/
# main above (AC10 — before/after parity depends on that separation).
#
# Ownership predicate (spec of record: DoE-claude
# coordinator/docs/wiki/coordinator-tripwires.md § PLAN-ORPHAN-OWNERSHIP):
#   handoff.deliverable_id == plan.deliverable_id, raw-string equality.
#   A plan carrying no `deliverable_id` at all resolves to NO owner — retired
#   2026-08-04 (docs/plans/2026-08-04-terminal-state-propagation-join-keys.md
#   § C12, PM ruling R2): this resolver previously fell back to an anchored
#   path-pointer secondary key (a `**Plan:**` body line, or the now-retired
#   `plan:` frontmatter scalar) naming the plan by its on-disk path. R2 rules
#   out any second join key that can disagree with `deliverable_id` — the
#   secondary key was exactly that, so it is gone, not narrowed.
#   `origin_plan_id`, `scope:` membership, `workstream` equality, and raw
#   substring match confer NOTHING (AC1, AC2) — this resolver never reads
#   those fields at all, by construction.
#   Handoff `status` of both `open` AND `claimed` confers ownership;
#   `claimed` is the strongest signal in the enum, not a skip case.
# ---------------------------------------------------------------------------


def resolve_plan_owner(
    plan_path: Path, repo_root: Path, plan_text: Optional[str] = None
) -> Optional[str]:
    """AC1/AC2 ownership resolver: does an `open`/`claimed` handoff under
    `<repo_root>/state/handoffs/*.md` own *plan_path*?

    Args:
        plan_path: path to the plan file (its own frontmatter is read for
            `deliverable_id`; its path relative to repo_root is the secondary
            key's comparison target).
        repo_root: explicit repo root (AC14) — the handoff corpus is read
            from `<repo_root>/state/handoffs/*.md` only. No ambient-cwd
            fallback.
        plan_text: optional pre-read contents of *plan_path*. When given, the
            plan file is NOT re-read from disk — callers that already hold
            the plan's text (e.g. `list_orphaned`'s population walk) should
            pass it through to avoid a duplicate read. When omitted (the
            default), this function reads the file itself, preserving the
            original standalone-call signature/behaviour.

    Returns:
        The repo-relative path (str, forward-slash separated) of the OWNING
        handoff, or None if the plan resolves to no owner.

    Raises:
        ValueError: if repo_root is None (AC14 — fail loud, matching
            `_plan_list_stale_executing`'s posture).
    """
    if repo_root is None:
        raise ValueError(
            "resolve_plan_owner requires an explicit repo_root; no "
            "ambient-cwd state/handoffs fallback (AC14)."
        )

    repo_root = Path(repo_root)
    plan_path = Path(plan_path)

    if plan_text is None:
        try:
            plan_text = plan_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            plan_text = ""
    plan_deliverable_id = extract_frontmatter_scalar(plan_text, "deliverable_id")
    if not plan_deliverable_id:
        # No deliverable_id, no join key — retired 2026-08-04 (C12/R2): a
        # plan with no deliverable_id at all now resolves to no owner rather
        # than falling to the retired path-pointer secondary key.
        return None
    handoffs_dir = repo_root / "state" / "handoffs"
    if not handoffs_dir.is_dir():
        return None

    try:
        names = sorted(os.listdir(handoffs_dir))
    except OSError:
        return None

    for name in names:
        if not name.endswith(".md"):
            continue
        hf_path = handoffs_dir / name
        if not hf_path.is_file():
            continue
        try:
            hf_text = hf_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        status = extract_frontmatter_scalar(hf_text, "status")
        if status not in ("open", "claimed"):
            continue

        hf_deliverable_id = extract_frontmatter_scalar(hf_text, "deliverable_id")
        if hf_deliverable_id and hf_deliverable_id == plan_deliverable_id:
            return "/".join(("state", "handoffs", name))

    return None


# ---------------------------------------------------------------------------
# Sibling op: plan.list_stale_executing (JSON-RPC) — see module docstring
# "Sibling op" section for the full contract.
# ---------------------------------------------------------------------------

def _git_commit_epoch(repo_root: Path, rel_path: str) -> Optional[int]:
    """Return the committer-timestamp (`%ct`, unix epoch seconds) of the most
    recent commit touching *rel_path* under *repo_root*, or None on any
    subprocess failure or empty result (no commit history for this path).

    Read-only git query only — mirrors orphan_branch_sweep._git's `git log -1
    --format=%ct` idiom (portable, no `date +%s` bash dependency).

    Single-path form, retained for direct callers/tests. `list_stale_executing`
    below does NOT call this per plan — see `_batch_git_commit_epochs` for the
    corpus-wide N+1 fix (C14).
    """
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--format=%ct", "--", rel_path],
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=_GIT_LOG_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    raw = result.stdout.strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# Header-line marker for `_batch_git_commit_epochs`' `--format=` string —
# printable ASCII (a NUL byte in argv raises ValueError on Windows'
# subprocess/CreateProcess), chosen to be a string no real docs/plans/*.md
# repo-relative path line could plausibly equal. Distinguishes a commit-
# header line from the `--name-only` file-path lines that follow it in the
# same stdout stream.
_BATCH_EPOCH_HEADER_PREFIX = "\x01GIT-COMMIT-EPOCH\x01"


def _batch_git_commit_epochs(repo_root: Path, rel_paths: List[str]) -> Dict[str, int]:
    """One `git log --name-only` walk over *rel_paths* as pathspecs, returning
    `{rel_path: most-recent-commit-epoch}` for every path this walk actually
    finds a touching commit for.

    Shape: multi-pathspec / object-membership, NOT range batching. `git log
    --format=... -- pathA pathB ...` asks "every commit that touches ANY of
    these paths" (union/OR semantics) and walks history newest-first; it is
    not `git rev-list A..B C..D`, which merges N independent ranges into a
    single `reachable(positives) \\ reachable(negatives)` set expression and
    silently drops all-but-the-tip on a linear chain (the forbidden shape —
    see `coverage.py`'s hard correctness constraints and the closed backlog
    entry this module's own C14 chunk cites). No reachability/ancestry
    arithmetic happens here at all: each commit header is read once, and the
    first (= most recent, since the walk is newest-first) commit touching a
    given path wins that path's slot — a plain membership scan, structurally
    the same shape as `emit/sections/handoffs.py`'s `_resolve_shipped_in_dates`
    (prefix-match + a `matched` set), adapted from SHA-prefix matching to
    path membership.

    A *rel_paths* entry ABSENT from the returned dict means the walk found NO
    commit touching that path (untracked, brand-new, or unreadable-by-git) —
    callers MUST treat absence as "no resolved timestamp", never silently
    defaulting it to "now" or epoch zero (§ Anti-scope 25). On any subprocess
    failure/timeout or non-zero exit, returns `{}` (every requested path
    reads as absent) — same fail-open posture as the single-path
    `_git_commit_epoch` above.
    """
    if not rel_paths:
        return {}
    try:
        result = subprocess.run(
            [
                "git", "log",
                f"--format={_BATCH_EPOCH_HEADER_PREFIX}%ct",
                "--name-only",
                # Merge commits print NO file-list line under plain
                # --name-only (git suppresses it by default for merges,
                # even ones that survive history simplification under a
                # pathspec because they are non-TREESAME to every parent).
                # Without this, a path whose most recent touch was a
                # conflict-resolution merge silently attributes to the
                # next, older commit that does print a name line — a
                # stale timestamp with no error signal. A kept merge under
                # default simplification is always non-TREESAME to its
                # first parent (implied by non-TREESAME-to-every-parent),
                # so first-parent diffing always emits its file list.
                "--diff-merges=first-parent",
                "--",
                *rel_paths,
            ],
            capture_output=True,
            text=True,
            cwd=repo_root,
            timeout=_GIT_LOG_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired):
        return {}
    if result.returncode != 0:
        return {}

    wanted = {p.replace("\\", "/") for p in rel_paths}
    resolved: Dict[str, int] = {}
    current_epoch: Optional[int] = None
    for line in result.stdout.splitlines():
        if line.startswith(_BATCH_EPOCH_HEADER_PREFIX):
            raw = line[len(_BATCH_EPOCH_HEADER_PREFIX):].strip()
            try:
                current_epoch = int(raw)
            except ValueError:
                current_epoch = None
            continue
        path = line.strip()
        if not path or current_epoch is None:
            continue
        # git's --name-only paths are POSIX-forward-slash regardless of
        # platform; rel_paths built via os.path.join above may carry
        # os.sep on Windows, so normalize both sides for the membership test.
        normalized = path.replace("\\", "/")
        if normalized in wanted and normalized not in resolved:
            resolved[normalized] = current_epoch

    return resolved


def list_stale_executing(
    repo_root: Path,
    threshold_days: int,
    today: Optional[date] = None,
) -> List[Dict[str, Any]]:
    """Scan `<repo_root>/docs/plans/*.md` (one level, checker-sidecars
    excluded — same exclusion as `scan()`) for `status: executing` plans
    whose most recent commit is >= *threshold_days* old.

    age_days is computed from the plan file's own most recent commit
    timestamp (`git log -1 --format=%ct`), NOT a `created:` frontmatter
    field — this predicate is deliberately simpler than the STALE-draft one
    above (see module docstring "Sibling op" section for why).

    A file with no git history for its path (never committed, or
    unreadable) is treated as NOT stale — reported skip is preferable to a
    false-positive fail-loud on an advisory-only, never-blocking op.

    Returns a list of {"path": <repo-relative str>, "age_days": <int>}
    dicts, sorted by path for deterministic output. Empty list if the
    docs/plans directory does not exist, holds no matching plans, or none
    exceeds the threshold.
    """
    if today is None:
        today = date.today()

    plans_dir = repo_root / "docs" / "plans"
    if not plans_dir.is_dir():
        return []

    # First pass: collect every `status: executing` candidate's repo-relative
    # path (forward-slash, matching `_batch_git_commit_epochs`'/`list_orphaned`'s
    # convention) — no git call here at all.
    candidate_paths: List[str] = []
    for name in sorted(os.listdir(plans_dir)):
        if not name.endswith(".md"):
            continue
        file_path = plans_dir / name
        if _is_sidecar_file(str(file_path)) or not file_path.is_file():
            continue

        try:
            text = file_path.read_text(encoding="utf-8")
        except OSError:
            continue

        status = extract_frontmatter_scalar(text, "status")
        if status != EXECUTING_STATUS:
            continue

        candidate_paths.append(os.path.join("docs", "plans", name).replace(os.sep, "/"))

    # Second pass: ONE git-log walk resolves every candidate's most-recent-
    # commit epoch (C14 — was one `_git_commit_epoch` spawn per candidate).
    # A candidate absent from the returned map has no commit touching it at
    # all — reconciled explicitly below as "not stale" (same posture the
    # per-path form used: no git history -> not stale, never a fail-loud).
    epochs = _batch_git_commit_epochs(repo_root, candidate_paths)

    entries: List[Dict[str, Any]] = []
    for rel_path in candidate_paths:
        epoch = epochs.get(rel_path)
        if epoch is None:
            # Absence reconciled explicitly: no touching commit found for
            # this candidate (untracked / brand-new / unreadable-by-git) —
            # never defaulted to "now" or epoch zero (§ Anti-scope 25).
            continue

        commit_date = datetime.fromtimestamp(epoch, tz=timezone.utc).date()
        age_days = (today - commit_date).days
        if age_days >= threshold_days:
            entries.append({"path": rel_path, "age_days": age_days})

    return entries


@register_op("plan.list_stale_executing")
def _plan_list_stale_executing(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'plan.list_stale_executing' handler.

    See module docstring "Sibling op" section for the full wire contract.
    Fails loud when repo_root is None (common_dir scope requires
    _origin_worktree in the envelope) rather than silently scanning this
    repo's own docs/plans/ instead of the caller's — same posture as
    goal.append (AC5).
    """
    if repo_root is None:
        raise ValueError(
            "plan.list_stale_executing requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None — op scope must be "
            "'common_dir' and _origin_worktree must be present in the "
            "JSON-RPC envelope. No silent fallback to this repo's own "
            "docs/plans/."
        )

    raw_threshold = params.get("threshold_days")
    if raw_threshold is None or isinstance(raw_threshold, bool):
        raise ValueError("plan.list_stale_executing requires an integer threshold_days param")
    try:
        threshold_days = int(raw_threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"plan.list_stale_executing requires an integer threshold_days param: {exc}"
        ) from exc

    derived_root = main_worktree_root(repo_root)
    stale = list_stale_executing(derived_root, threshold_days)
    return {"stale": stale}


# ---------------------------------------------------------------------------
# Reverse direction (C3, AC7) — baton -> plan dangling-pointer detector.
#
# Same join C1's resolve_plan_owner performs (deliverable_id, the sole join
# key since 2026-08-04's C12/R2 retirement of the path-pointer secondary
# key), read in the OPPOSITE direction: instead of "does a handoff own this
# plan", "does this handoff's plan reference resolve to something real". Uses
# the SAME single walk of docs/plans that list_orphaned already performs to
# build the deliverable_id index below (no second corpus walk over
# docs/plans); state/handoffs/ is walked exactly once here (not once per
# plan, unlike resolve_plan_owner's per-plan walk in C1/C2).
#
# Per handoff:
#   `deliverable_id` present (frontmatter) -> resolves iff SOME plan file
#   (any status — a shipped/implemented plan is still a real file) carries
#   that same deliverable_id. This is C1's primary-key equality test,
#   direction-flipped. Absent that: nothing to check — not a finding (no
#   reference made). The retired path-pointer secondary key (a `**Plan:**`
#   body line, or the now-retired `plan:` frontmatter scalar) is no longer
#   consulted here either — R2 rules out any second join key that can
#   disagree with `deliverable_id`, in either join direction.
#
# Report only (AC7): never deletes the reference, never rewrites the baton,
# never creates the missing plan. Surfaced under `plan.list_orphaned`'s own
# envelope as `dangling_baton_references` rather than a third op — one
# census, two directions.
# ---------------------------------------------------------------------------


def _list_dangling_baton_plan_references(
    repo_root: Path,
    known_plan_deliverable_ids: Dict[str, List[str]],
) -> List[Dict[str, str]]:
    """One walk of `<repo_root>/state/handoffs/*.md`, reporting every plan
    reference that does not resolve (AC7). See section docstring above for
    the precedence rule and join direction.

    `known_plan_deliverable_ids` is the deliverable_id -> repo-relative-paths
    index built by list_orphaned's own single walk of docs/plans (ALL plan
    files, any status) — passed in so this function never re-walks
    docs/plans itself.
    """
    findings: List[Dict[str, str]] = []

    handoffs_dir = repo_root / "state" / "handoffs"
    if not handoffs_dir.is_dir():
        return findings

    try:
        names = sorted(os.listdir(handoffs_dir))
    except OSError:
        return findings

    for name in names:
        if not name.endswith(".md"):
            continue
        hf_path = handoffs_dir / name
        if not hf_path.is_file():
            continue
        try:
            hf_text = hf_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        rel_handoff = "/".join(("state", "handoffs", name))

        deliverable_id = extract_frontmatter_scalar(hf_text, "deliverable_id")
        if not deliverable_id:
            # No deliverable_id, no plan reference made — not a finding. The
            # retired path-pointer secondary key is no longer consulted here.
            continue

        if deliverable_id not in known_plan_deliverable_ids:
            findings.append(
                {
                    "handoff": rel_handoff,
                    "reference": deliverable_id,
                    "reference_kind": "deliverable_id",
                }
            )

    return findings


# ---------------------------------------------------------------------------
# Sibling op: plan.list_orphaned (JSON-RPC, C2) — the tiered orphan census
# built on C1's resolve_plan_owner. Spec of record: DoE-claude
# coordinator/docs/wiki/coordinator-tripwires.md § PLAN-ORPHAN-OWNERSHIP,
# implementation plan docs/plans/2026-07-31-plan-orphan-ownership-resolver.md
# § C2. Advisory only — never blocks (same posture as list_stale_executing).
#
# Population (AC3): every *.md under <repo_root>/docs/plans/, sidecars
# excluded (BOTH the fixed `_is_sidecar_file` four-suffix denylist AND the
# census-local `_is_census_local_sidecar` structural predicate — a file
# excluded by either is excluded; see that function's docstring for why the
# fixed denylist alone under-excludes the live corpus), whose status is NOT
# in PLAN_ORPHAN_TERMINAL_STATUS. The status enum is OPEN: any status
# outside PLAN_ORPHAN_TERMINAL_STATUS is non-terminal
# by construction (fails toward surfacing) and IS included in the population;
# a status that is furthermore not one of the two known non-terminal values
# ("draft", "executing") is ALSO recorded in the `unrecognized_status`
# diagnostic bucket, alongside inclusion in the population — both, not
# either/or (AC3, AC16).
#
# Non-plan exclusion (cross-repo memo
# cross-repo/inbox/2026-08-03-doe-claude-em-two-rulings-plan-orphan-population-and-dr088-antiscope.md
# § 1, DoE-claude coordinator/docs/wiki/coordinator-tripwires.md §
# PLAN-ORPHAN-OWNERSHIP): a file under docs/plans/ that survives the sidecar
# exclusions above but carries NO YAML frontmatter block at all (structural
# presence, via `coordinator_core.frontmatter.primitives.split_frontmatter`
# returning None — never a probe for a particular key such as `status:`, so
# a real plan merely missing `status:` still counts as a plan and still
# reaches `unrecognized_status`) is not a plan — `docs/plans/INDEX.md` and
# `README.md` are the motivating cases. It is excluded from the population
# and counted in `non_plan_excluded_count`, the same counted-never-alarmed
# shape as `parked_below_threshold_count` / `legacy_unjoinable_count`.
# Deliberately NOT a filename denylist — that rots as new non-plan files
# land in the directory; frontmatter presence is cheap and unambiguous.
#
# Accounting invariant (deviation from the memo's literal two-term form,
# documented here per that memo's instruction): the memo states
# `scanned_count == population_count + non_plan_excluded_count`. That is not
# satisfiable as written against this code, because a frontmatter-bearing
# plan whose status is in PLAN_ORPHAN_TERMINAL_STATUS is scanned but
# deliberately excluded from population_count — under the literal two-term
# form such a plan would be an uncounted drop. This module implements the
# invariant's INTENT (nothing silently dropped) as a strict superset:
#
#     scanned_count == population_count + non_plan_excluded_count + terminal_count
#
# `scanned_count` counts every *.md file that reached the frontmatter read
# (i.e. passed the sidecar exclusions and was successfully read from disk).
# `terminal_count` counts frontmatter-bearing plans skipped for terminal
# status. Files skipped BEFORE the frontmatter read — non-`.md` files,
# sidecars via `_is_sidecar_file` / `_is_census_local_sidecar`, or files that
# raised on read (`OSError`/`UnicodeDecodeError`) — are outside
# `scanned_count` entirely, same boundary `scan()`/`list_stale_executing`
# already draw for their own file-loop skips.
#
# Tiering (AC4/AC5/AC5a/AC6), applied only to UNOWNED, non-legacy plans:
#   P1 authorized_orphan — execution_authorized_by carries a real value
#     (branch on the field-STATE per extract_frontmatter_scalar's three-way
#     return: None/""/the literal string "null" all mean "no authorization" —
#     see _has_execution_authorization below). Loud, one line per plan, no
#     age gate.
#   P2 chain_gap — PARKED. No validated stub_id->deliverable_id join exists
#     in this fleet (see plan § C2 body); this tier NAME is kept in the
#     vocabulary as a placeholder and is NEVER populated. Every plan that
#     would otherwise be P2 falls to P3.
#   P3 parked — no authorization. Count only, gated on threshold_days (the
#     existing AGING_THRESHOLD_DAYS default): a plan younger than the
#     threshold is not yet ALARMED (too fresh to flag, same grace period as
#     the STALE-draft predicate above) but IS still counted, in the separate
#     `parked_below_threshold_count` bucket — the age gate governs whether P3
#     is surfaced/alarmed, never whether it exists in the census (AC5: the
#     partition is total). Suppressed means COUNTED, never dropped; never
#     expanded per-plan in the default render (AC6).
#
# `legacy_unjoinable` bucket (DoE ruling 95bf1cc4d, AC1a rescoping): a
# non-terminal, UNOWNED plan authored before the carry-observability fix
# (C0b) landed is structurally unjoinable — its deliverable_id, and any
# handoff's, was minted per-artifact rather than per-deliverable pre-fix, so
# no read-side resolver can recover the join; asserting such a plan is P1/P3
# would overstate confidence in a signal the fix itself proves was broken at
# mint time. Counted, never rendered as an alarm line, and excluded from
# BOTH the P1/P3 tiers and the unowned share AC1a's ~25% gate evaluates.
#
# Landing-cutoff caveat (documented, not silently guessed — see the module's
# spec backlink plan § C2 body "legacy-unjoinable bucket" note): C0b's fix
# has no git-commit-SHA landing marker to look up (nothing on disk stamps
# it), so the cutoff below is pinned to the calendar DATE the fix lands in
# THIS repo (2026-07-31), not a finer-grained commit timestamp. A future
# implementer with a real landing-commit reference should replace this
# constant with a git-log lookup; until then this is the best-available,
# explicitly-named approximation. A plan with no parseable `created:` field
# cannot be reliably dated against this cutoff at all — per this op's
# "fails toward surfacing, never silently guessed into a suppressed bucket"
# convention (AC3), such a plan is treated as post-carry (NOT legacy) rather
# than silently absorbed into the quiet legacy_unjoinable count.
_CARRY_OBSERVABILITY_FIX_LANDED_ON = date(2026, 7, 31)

# SSOT: coordinator_core/frontmatter/schemas/plan.schema.json
# (properties.status.enum) lists the full plan-status vocabulary:
# ["draft","reviewed","approved","executing","landed","implemented",
# "deferred","abandoned","superseded"]. This module does not parse that
# JSON at runtime (no JSON-read in this hot per-plan loop) — the frozenset
# below is a hand-kept literal that must track the schema enum's
# NON-terminal members. Terminal members are governed separately by
# PLAN_ORPHAN_TERMINAL_STATUS in lifecycle_constants.py, which is only
# PARTIALLY reconciled with the schema enum: it carries
# "shipped"/"complete"/"executed", absent from the enum entirely and retained
# as documented defensive tolerance. Its "landed" member was struck
# 2026-08-06 (DoE ruling 80b0b29fb) because the schema documents that value
# as explicitly non-terminal; "landed" therefore joins the frozenset below,
# and MUST stay in it — a status that is neither terminal nor listed here
# lands in the `unrecognized_status` diagnostic bucket, which is the same
# false-report class that made every "approved" plan read as unrecognized.
# Any future non-terminal status added to the schema enum must be added
# below by hand.
_KNOWN_NON_TERMINAL_PLAN_STATUSES = frozenset(
    {DRAFT_STATUS, EXECUTING_STATUS, REVIEWED_STATUS, APPROVED_STATUS, LANDED_STATUS}
)


def _has_execution_authorization(text: str) -> Optional[str]:
    """AC5a field-state branch for `execution_authorized_by`.

    `extract_frontmatter_scalar` returns None for an absent key, "" for a
    valueless key, and the truthy STRING "null" for an explicit
    `execution_authorized_by: null` — all three mean "no authorization."
    Returns the real value (non-null, non-empty) iff one is present,
    otherwise None. Callers MUST branch on this return, not on
    `is not None` or bare truthiness against the raw extractor result.
    """
    value = extract_frontmatter_scalar(text, "execution_authorized_by")
    if value is None or value == "" or value == "null":
        return None
    return value


def _plan_created_date(text: str) -> Optional[date]:
    """Parse the `created:` frontmatter scalar as a date, or None if absent
    or unparseable — mirrors check_one's own created-date parsing.
    """
    created = extract_frontmatter_scalar(text, "created")
    if not created:
        return None
    try:
        return date.fromisoformat(created.strip())
    except (ValueError, AttributeError):
        return None


def list_orphaned(
    repo_root: Path,
    threshold_days: int,
    today: Optional[date] = None,
) -> Dict[str, Any]:
    """Compute core for `plan.list_orphaned` — see module "Sibling op" section
    above for the full tier/bucket contract.

    Raises:
        ValueError: if repo_root is None (AC14 — same fail-loud posture as
            resolve_plan_owner / _plan_list_stale_executing).
    """
    if repo_root is None:
        raise ValueError(
            "list_orphaned requires an explicit repo_root; no ambient-cwd "
            "docs/plans fallback (AC14)."
        )

    if today is None:
        today = date.today()

    repo_root = Path(repo_root)
    plans_dir = repo_root / "docs" / "plans"

    authorized_orphan: List[Dict[str, Any]] = []
    unrecognized_status: List[Dict[str, Any]] = []
    parked_count = 0
    parked_below_threshold_count = 0
    legacy_unjoinable_count = 0
    population_count = 0
    owned_count = 0
    # non_plan_excluded_count / scanned_count / terminal_count — see the
    # "Non-plan exclusion" / "Accounting invariant" module docstring
    # paragraphs above this function for the invariant these three satisfy
    # and why it deviates from the memo's literal two-term form.
    non_plan_excluded_count = 0
    scanned_count = 0
    terminal_count = 0
    # Reverse-direction (C3, AC7) index: deliverable_id -> repo-relative
    # paths, populated from EVERY plan file this walk visits regardless of
    # status — a shipped/implemented plan is still a real file a handoff may
    # validly reference. Built from this SAME walk so the reverse check
    # never re-walks docs/plans on its own (see
    # _list_dangling_baton_plan_references).
    plan_deliverable_ids: Dict[str, List[str]] = {}

    if not plans_dir.is_dir():
        return {
            "authorized_orphan": authorized_orphan,
            "chain_gap": [],
            "parked_count": parked_count,
            "parked_below_threshold_count": parked_below_threshold_count,
            "legacy_unjoinable_count": legacy_unjoinable_count,
            "unrecognized_status": unrecognized_status,
            "population_count": population_count,
            "owned_count": owned_count,
            "non_plan_excluded_count": non_plan_excluded_count,
            "scanned_count": scanned_count,
            "terminal_count": terminal_count,
            "dangling_baton_references": _list_dangling_baton_plan_references(
                repo_root, plan_deliverable_ids
            ),
        }

    for name in sorted(os.listdir(plans_dir)):
        if not name.endswith(".md"):
            continue
        file_path = plans_dir / name
        if (
            _is_sidecar_file(str(file_path))
            or _is_census_local_sidecar(file_path, plans_dir)
            or not file_path.is_file()
        ):
            continue

        try:
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        scanned_count += 1

        if split_frontmatter(text) is None:
            # Structural discriminator (memo § 1): no YAML frontmatter block
            # at all -> not a plan (INDEX.md, README.md). Never a probe for
            # a particular key — a real plan missing status: still has a
            # frontmatter block and falls through to the status handling
            # below, still reaching unrecognized_status.
            non_plan_excluded_count += 1
            continue

        rel_path = os.path.join("docs", "plans", name).replace(os.sep, "/")
        plan_deliverable_id = extract_frontmatter_scalar(text, "deliverable_id")
        if plan_deliverable_id:
            plan_deliverable_ids.setdefault(plan_deliverable_id, []).append(rel_path)

        status = extract_frontmatter_scalar(text, "status")
        if status in PLAN_ORPHAN_TERMINAL_STATUS:
            terminal_count += 1
            continue

        population_count += 1

        if status not in _KNOWN_NON_TERMINAL_PLAN_STATUSES:
            unrecognized_status.append({"path": rel_path, "status": status})

        owner = resolve_plan_owner(file_path, repo_root, plan_text=text)
        if owner is not None:
            owned_count += 1
            continue

        # Unowned from here on. Legacy check BEFORE the P1/P3 authorization
        # branch — a pre-fix plan's ownership signal itself is unreliable, so
        # it must not be asserted into a tier that presumes the signal holds.
        created_date = _plan_created_date(text)
        if created_date is not None and created_date < _CARRY_OBSERVABILITY_FIX_LANDED_ON:
            legacy_unjoinable_count += 1
            continue

        # Precedence (AC5a): branch on execution_authorized_by's field-state
        # first — P1 (authorized) and not-P1 are mutually exclusive states of
        # the SAME field, never two independent booleans.
        authorized_by = _has_execution_authorization(text)
        if authorized_by is not None:
            authorized_orphan.append({"path": rel_path, "execution_authorized_by": authorized_by})
            continue

        # Not-P1: P2 (chain_gap) is parked/unreachable (no chain-membership
        # mechanism exists — see module docstring), so every remaining plan
        # falls to P3. The threshold_days age gate governs whether P3 is
        # SURFACED as the alarmed `parked_count` tier (AC4) — it does not
        # govern whether the plan EXISTS in the census at all (AC5/AC6: the
        # partition must be total, never dropping a plan). A plan under the
        # threshold still counts, just in the separate `parked_below_threshold_count`
        # bucket — counted, never alarmed, same "suppressed means counted"
        # spirit as `legacy_unjoinable_count`.
        # No parseable created: on a non-legacy plan: fails toward the
        # alarmed tier (age_days pinned to threshold_days), never silently
        # routed to parked_below_threshold_count.
        age_days = (today - created_date).days if created_date is not None else threshold_days
        if age_days >= threshold_days:
            parked_count += 1
        else:
            parked_below_threshold_count += 1

    return {
        "authorized_orphan": authorized_orphan,
        "chain_gap": [],
        "parked_count": parked_count,
        "parked_below_threshold_count": parked_below_threshold_count,
        "legacy_unjoinable_count": legacy_unjoinable_count,
        "unrecognized_status": unrecognized_status,
        "population_count": population_count,
        "owned_count": owned_count,
        "non_plan_excluded_count": non_plan_excluded_count,
        "scanned_count": scanned_count,
        "terminal_count": terminal_count,
        "dangling_baton_references": _list_dangling_baton_plan_references(
            repo_root, plan_deliverable_ids
        ),
    }


@register_op("plan.list_orphaned")
def _plan_list_orphaned(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'plan.list_orphaned' handler.

    See module "Sibling op: plan.list_orphaned" docstring section for the
    full wire contract. Fails loud when repo_root is None — same posture as
    plan.list_stale_executing (common_dir scope requires _origin_worktree in
    the envelope); no silent fallback to this repo's own docs/plans/.
    """
    if repo_root is None:
        raise ValueError(
            "plan.list_orphaned requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None — op scope must be "
            "'common_dir' and _origin_worktree must be present in the "
            "JSON-RPC envelope. No silent fallback to this repo's own "
            "docs/plans/."
        )

    raw_threshold = params.get("threshold_days")
    if raw_threshold is None or isinstance(raw_threshold, bool):
        raise ValueError("plan.list_orphaned requires an integer threshold_days param")
    try:
        threshold_days = int(raw_threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"plan.list_orphaned requires an integer threshold_days param: {exc}"
        ) from exc

    derived_root = main_worktree_root(repo_root)
    return list_orphaned(derived_root, threshold_days)


def main(argv: List[str]) -> int:
    """CLI entry: arg validation, scan, print, return rc."""
    if not argv:
        print(f"{_PROG}: missing argument: <plan-path-or-directory>", file=sys.stderr)
        return 2

    target = argv[0]
    if not os.path.exists(target):
        print(f"{_PROG}: path not found: {target}", file=sys.stderr)
        return 2

    lines, rc = scan(target)
    for line in lines:
        print(line)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
