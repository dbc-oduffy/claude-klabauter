"""
coordinator_core.ops.plan_suggest_completion_steps — assist surface that
tells an EM what would complete a plan as a first-class coordinator artifact
(Part 3 of state/handoffs/2026-08-13-vanilla-plan-mode-capture-safety-net.md
§ "Blunt the safety-gate contradiction").

Purpose: harness plan mode (Claude Code's built-in `/plan`) is not a
violation — it is simply a plan authored through a path that does not, on
its own, produce the frontmatter and routing coordinator plans normally
carry. `ExitPlanMode` tells the EM *"User has approved your plan. You can
now start coding"*, which is true of the harness's own approval model but
says nothing about whether this repo's review routing or execution stamp
ever ran — an EM reading it has no signal either way. THIS MODULE IS AN
ASSIST SURFACE, NOT AN ENFORCEMENT ONE: it answers "given a plan artifact
that has reached a state implying execution is imminent, what coordinator-
shape element is it missing, and what supplies it" — never "what did this
EM skip."

THIS IS NOT A GATE. Nothing here blocks, refuses, or asserts a violation
occurred. A consumer built on this (a session-start surfacing, a turn-end
nudge) fires after the fact and never intercepts a write — the same
limitation `coordinator:plan`'s own doctrine already documents for
`nudge-unrouted-sizing.py`. Do not cite this module, or any consumer of it,
as a mechanism that would have caught anything; it is a pointer toward the
next completing step, offered whenever a plan happens to be missing it.

Two coordinator-shape elements this module checks for (either one, alone,
already gives the EM a concrete next step — the two together are the
combination this surface targets, since a plan missing only one has an
obvious single move already visible in its own state):

  1. Execution authorization stamp — `execution_authorized_by` /
     `execution_authorized_at` / `execution_authorized_sha` /
     `execution_authorized_note`, the four-field stamp
     `coordinator_core/frontmatter/schemas/plan.schema.json` declares on
     plan frontmatter, mirrored from `handoff.schema.json`'s identically-
     named fields for the plan->execute-plan Stop-hook seam (spec backlink:
     docs/plans/2026-07-17-execution-handoff-phase-doe-contract.md § C1).
     This module reads `execution_authorized_at` as the presence probe (the
     timestamp leg of the stamp — matches the field name the driving handoff
     names verbatim; the other three legs are written atomically alongside
     it by every known writer, so a plan without `_at` is without the whole
     stamp in every observed case). Supplied by the named PM pre-execute
     step (`coordinator:plan`'s own Exit / `coordinator:review`'s
     integration).
  2. A review trail — a `state/review-trail/*.json` record
     (`review-trail.schema.json`) with `scope_kind: "plan"` whose `sha_range`
     resolves to a commit that touched this plan's own file path. This is
     the same kind-aware plan-crediting shape `coordinator_core.coverage`
     already establishes for the coverage gate (see that module's
     `_credit_from_kind_partition` and its own "13 on-disk scope_kind:'plan'
     records" note) — reused as a correlation check here, not
     re-implemented as a second, divergent notion of "this plan was
     reviewed." Supplied by dispatching `coordinator:review` against the
     plan.

Candidate population — "reached a state implying execution is imminent" —
is `status: approved` or `status: executing`: the two `plan.schema.json`
non-terminal statuses that sit AT or PAST the review-and-approval crossing
point (`draft`/`reviewed` precede it; `landed`/`implemented`/`superseded`/
`abandoned`/`deferred` are terminal-or-past-execution, where "what's still
missing" is either moot or a different question entirely). Mirrors the
`EXECUTING_STATUS`/`APPROVED_STATUS` vocabulary
`coordinator_core.ops.draft_plan_aging` already declares for the same
schema enum — imported from there rather than re-declared, so the two
modules cannot drift on what the enum actually contains.

Scope boundary against the sibling port operation (Part 2 of the same
handoff, converting a `~/.claude/plans/` harness capture into a scaffolded
coordinator plan): that op's input is a file with NO coordinator frontmatter
at all, living outside `docs/plans/` entirely. This op's candidates are
already-scaffolded `docs/plans/*.md` files (a `status:` field is read from
their frontmatter to even become a candidate) — a plan the port op has
already produced, or one authored directly with `coordinator-doc-new`, that
simply hasn't yet accumulated its review/authorization artifacts. The two
surfaces should stay separate on that basis: this one is a completeness
check over already-scaffolded plans, not a second, parallel conversion path
for un-scaffolded ones. If a future need arises to report missing
`plan_id`/`deliverable_id`/`sizing_object` reverse-FK elements too (the
broader "first-class coordinator plan" checklist), that is squarely inside
what the port op already produces at scaffold time — extend that op's own
verification, not this one, to avoid two overlapping notions of plan
completeness.

Invocation contract (for the DoE-claude consumer — see the driving
handoff's Part 3 and its own anti-scope: "every coordinator hook lives in
DoE-claude, not here"):

    JSON-RPC: {"method": "plan.suggest_completion_steps", "params": {}}
    scope: common_dir (the CALLER's own docs/plans/ + state/review-trail/ +
        archive/review-trail/, never claude-klabauter's own — see op_scopes.py entry)
    reply: {"plans": [
        {
            "path": "docs/plans/2026-08-13-foo.md",
            "status": "approved" | "executing",
            "missing": [
                {
                    "element": "execution_authorized_at",
                    "description": "<what this element is>",
                    "supplied_by": "<what produces it>",
                },
                ...
            ],
        },
        ...
    ]}
    Empty `plans` list, never an error, when nothing qualifies — advisory
    only, matches `plan.list_stale_executing`/`plan.list_orphaned`'s own
    never-blocks posture. A calling consumer decides what to DO with a
    non-empty `plans` list (surfacing text, session-start mention, etc.) —
    this op only ever computes and reports the completing steps.

Negative-spec:
    - Does NOT write anything — pure read-only scan (frontmatter reads +
      `git rev-list` queries only).
    - Does NOT report a plan missing only ONE of the two elements — see
      "the two together are the combination this surface targets" above.
    - Does NOT treat `status: landed` as a candidate — `list_stale_executing`
      already covers "stalled after claiming executing"; a `landed` plan has
      already been through whatever execution occurred, so "imminent" no
      longer describes it.
    - Does NOT re-implement `coordinator_core.coverage`'s full coverage-gate
      DAG/chain-ancestry machinery — this only needs "does at least one
      terminal-verdict, scope_kind:plan record's resolved range touch this
      plan's path", a narrow correlation check, not a certified coverage
      ratio.
    - Does NOT emit surfacing/nudge prose itself — the `description`/
      `supplied_by` strings below are structured data naming a mechanism,
      not agent-facing copy; a consuming surface (built in DoE-claude, not
      here) owns actual wording shown to an EM.
    - Does NOT assert, imply, or log that anything was "skipped",
      "violated", or done wrong — see module docstring.

Spec backlink: state/handoffs/2026-08-13-vanilla-plan-mode-capture-safety-net.md § Part 3
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Dict, FrozenSet, List, Optional, Sequence, Set

from coordinator_core.ipc import register_op
from coordinator_core.ops._fm_util import extract_frontmatter_scalar
from coordinator_core.ops.draft_plan_aging import (
    APPROVED_STATUS,
    EXECUTING_STATUS,
    _is_sidecar_file,
)
from coordinator_core.ops.fleet._common import main_worktree_root
from coordinator_core.review_trail.records import _collect as _collect_review_trail_jsons
from coordinator_core.win_portability import no_console_creationflags

_CREATIONFLAGS = no_console_creationflags()
_GIT_TIMEOUT_SECS = 15

#: Candidate statuses — see module docstring's "Candidate population"
#: section. Imported names, not re-declared, so this module and
#: draft_plan_aging.py cannot silently diverge on the schema enum.
_EXECUTION_IMMINENT_STATUSES = frozenset({APPROVED_STATUS, EXECUTING_STATUS})

#: A review-trail record with this verdict is an OPEN loop, not a discharged
#: review — a record `freeze-review-diff.py` writes before
#: any reviewer has run does not attest that one has. "waived" is NOT
#: excluded here: a `reviewer: waived` record carries its own justification
#: (review_trail_write.py's evidence-floor gate) and is a legitimate,
#: deliberate discharge of the review step, not an open loop.
_NON_QUALIFYING_VERDICTS = frozenset({"pending"})

#: Mirrors coordinator_core.coverage.SAFE_RANGE — argument-injection guard
#: before a caller-controlled sha_range string ever reaches a git subprocess
#: argv. Duplicated rather than imported: coverage.py is a large module this
#: op otherwise has no dependency on, and the regex is a two-line constant,
#: not shared logic whose drift would be a correctness risk.
_SAFE_RANGE = re.compile(
    r"^[0-9A-Za-z_/.][0-9A-Za-z_/.~^]*\.\.\.?[0-9A-Za-z_/.][0-9A-Za-z_/.~^]*$"
)

#: The two coordinator-shape elements this op reports on, and the fixed
#: description/supplied_by text each carries — see module docstring's
#: numbered list. A single source of truth so `suggest_completion_steps`'s
#: assembly step never risks the two call sites drifting apart.
_EXECUTION_AUTHORIZATION_ELEMENT = {
    "element": "execution_authorized_at",
    "description": (
        "The PM pre-execute authorization stamp (execution_authorized_by / "
        "_at / _sha / _note, coordinator_core/frontmatter/schemas/"
        "plan.schema.json) — records who authorized execution, when, and why."
    ),
    "supplied_by": (
        "The named PM pre-execute step (coordinator:plan's own Exit, or "
        "coordinator:review's integration) — stamps this when execution is "
        "authorized."
    ),
}
_REVIEW_TRAIL_ELEMENT = {
    "element": "review_trail",
    "description": (
        "A state/review-trail/*.json record (scope_kind: \"plan\") whose "
        "sha_range covers a commit touching this plan's file."
    ),
    "supplied_by": "coordinator:review, dispatched against this plan.",
}


def _has_execution_authorized_at(text: str) -> bool:
    """True iff frontmatter carries a real (non-null, non-empty)
    `execution_authorized_at` value. Mirrors
    `draft_plan_aging._has_execution_authorization`'s field-state branch
    (None / "" / literal "null" all mean absent) for the sibling field.
    """
    value = extract_frontmatter_scalar(text, "execution_authorized_at")
    return value not in (None, "", "null")


def _git(args: List[str], cwd: Path) -> "tuple[int, str, str]":
    """Never-raises git-invocation helper — same never-raise contract as
    every other subprocess call site in this ops package
    (`review_trail_write._git_runner`, `draft_plan_aging`'s git-log calls).
    """
    try:
        proc = subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(f"skip: plan_suggest_completion_steps._git: {args!r} failed: {exc}", file=sys.stderr)
        return 2, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _plan_touching_shas(repo_root: Path, rel_path: str) -> FrozenSet[str]:
    """Every commit SHA that has ever touched *rel_path*, per `git log`.

    Empty on any git failure or a path with no history — fail-closed toward
    "no review trail found" (never manufactures a match from an unreadable
    git state).
    """
    rc, out, _err = _git(["git", "log", "--format=%H", "--", rel_path], repo_root)
    if rc != 0:
        return frozenset()
    return frozenset(line.strip() for line in out.splitlines() if line.strip())


#: A `%H` line from `git log --format=%H --name-only ...` — always exactly
#: 40 lowercase hex characters. Mirrors
#: coordinator_core.ops.review_coverage_core._FULL_SHA_RE (same
#: line-shape disambiguation against the file-path lines interleaved in the
#: same combined-log output).
_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _plan_touching_shas_batch(
    repo_root: Path, rel_paths: Sequence[str]
) -> Dict[str, FrozenSet[str]]:
    """Batched counterpart of `_plan_touching_shas`: resolves every candidate
    plan's touching-commit set in ONE `git log --format=%H --name-only --
    <rel_paths...>` spawn instead of one `git log -- <rel_path>` spawn per
    plan (W8/C8 amplification disposition — this was this op's own per-item
    site: `suggest_completion_steps`'s `candidate_shas` dict comprehension).

    `git log -- pathA pathB ...` is OR-pathspec semantics: it returns every
    commit that touched AT LEAST ONE of the named paths, together with (via
    `--name-only`) the FULL list of files that commit touched — not
    pre-filtered to the candidate set. Attribution back to each candidate
    path is done here by keying each commit's changed-file lines against
    `rel_paths`, mirroring
    `coordinator_core.ops.review_coverage_core.build_segments`'s identical
    SHA/file-line split over the same combined `git log` shape.

    Every `rel_paths` entry is present in the returned dict (empty
    frozenset if it never appears), and empty on any git failure — fail-
    closed, matches `_plan_touching_shas`'s own per-path contract (no
    manufactured match from an unreadable git state).
    """
    result: Dict[str, FrozenSet[str]] = {rel_path: frozenset() for rel_path in rel_paths}
    if not rel_paths:
        return result

    rc, out, _err = _git(["git", "log", "--format=%H", "--name-only", "--", *rel_paths], repo_root)
    if rc != 0:
        return result

    path_set = set(rel_paths)
    buckets: Dict[str, Set[str]] = {rel_path: set() for rel_path in rel_paths}
    current_sha: Optional[str] = None
    for raw_line in out.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _FULL_SHA_RE.match(line):
            current_sha = line
            continue
        if current_sha is not None and line in path_set:
            buckets[line].add(current_sha)
    return {rel_path: frozenset(shas) for rel_path, shas in buckets.items()}


def _resolve_range_shas(repo_root: Path, sha_range: str) -> FrozenSet[str]:
    """Resolve a review-trail record's `sha_range` to its concrete commit
    set via `git rev-list`. Refuses (returns empty) any range failing
    `_SAFE_RANGE` — an unsafe/malformed range correlates to nothing rather
    than being interpolated into a subprocess argv.
    """
    if not sha_range or not _SAFE_RANGE.match(sha_range):
        return frozenset()
    rc, out, _err = _git(["git", "rev-list", sha_range], repo_root)
    if rc != 0:
        return frozenset()
    return frozenset(line.strip() for line in out.splitlines() if line.strip())


def _plans_with_review_trail_coverage(
    repo_root: Path, candidate_shas: Dict[str, FrozenSet[str]]
) -> Set[str]:
    """Return the subset of `candidate_shas` keys (repo-relative plan paths)
    covered by at least one live-or-archived review-trail record that is (a)
    `scope_kind: "plan"`, (b) carries a qualifying (non-"pending") verdict,
    and (c) resolves to a range containing at least one of that plan's own
    touching commits.

    A malformed/unreadable record is silently skipped (fail-closed — it
    simply cannot supply coverage).

    Collects `state/review-trail/*.json` and `archive/review-trail/*.json`
    directly under the EXPLICIT `repo_root` — deliberately NOT
    `list_review_trail_records.list_paths()`, whose own state-root
    resolution reads the CALLING PROCESS's ambient cwd (`git rev-parse
    --show-toplevel`) rather than accepting a repo_root parameter. This
    op's own `repo_root` is the JSON-RPC envelope's resolved
    `_origin_worktree`, which need not equal the daemon process's cwd — the
    same "no ambient-cwd fallback" discipline `list_orphaned`/
    `list_stale_executing` (AC14) already hold this whole package to.
    """
    covered: Set[str] = set()
    if not candidate_shas:
        return covered

    trail_paths = [
        full
        for _basename, full in (
            _collect_review_trail_jsons(str(repo_root / "state" / "review-trail"))
            + _collect_review_trail_jsons(str(repo_root / "archive" / "review-trail"))
        )
    ]

    # Memoizes each DISTINCT sha_range's resolution once, even when several
    # records cite the same range (a real corpus shape — a chain-terminal
    # review-trail write often repeats a range across sibling records).
    range_cache: Dict[str, FrozenSet[str]] = {}

    for trail_path in trail_paths:
        try:
            with open(trail_path, "r", encoding="utf-8") as fh:
                record = json.load(fh)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("scope_kind") != "plan":
            continue
        verdict = str(record.get("verdict") or "").strip().lower()
        if verdict in _NON_QUALIFYING_VERDICTS:
            continue

        sha_range = record.get("sha_range", "")
        if not isinstance(sha_range, str):
            continue
        if sha_range not in range_cache:
            range_cache[sha_range] = _resolve_range_shas(repo_root, sha_range)
        resolved = range_cache[sha_range]
        if not resolved:
            continue

        for rel_path, touching_shas in candidate_shas.items():
            if rel_path in covered:
                continue
            if touching_shas & resolved:
                covered.add(rel_path)

        if len(covered) == len(candidate_shas):
            break  # every candidate already covered — nothing left to prove

    return covered


def suggest_completion_steps(repo_root: Path) -> List[Dict[str, object]]:
    """Scan `<repo_root>/docs/plans/*.md` for plans that would benefit from
    a completing step.

    See module docstring for the full predicate. Returns a list of entries,
    sorted by path for deterministic output; empty when nothing qualifies
    (including when `docs/plans/` itself does not exist).
    """
    plans_dir = repo_root / "docs" / "plans"
    if not plans_dir.is_dir():
        return []

    # Pass 1: collect candidates (status approved/executing) needing an
    # authorization check — cheap, no git call yet.
    candidates: Dict[str, str] = {}  # rel_path -> status
    missing_auth: Set[str] = set()
    for name in sorted(os.listdir(plans_dir)):
        if not name.endswith(".md"):
            continue
        file_path = plans_dir / name
        if _is_sidecar_file(str(file_path)) or not file_path.is_file():
            continue
        try:
            text = file_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        status = extract_frontmatter_scalar(text, "status")
        if status not in _EXECUTION_IMMINENT_STATUSES:
            continue

        rel_path = os.path.join("docs", "plans", name).replace(os.sep, "/")
        candidates[rel_path] = status
        if not _has_execution_authorized_at(text):
            missing_auth.add(rel_path)

    if not candidates:
        return []

    # Pass 2: only candidates already missing the authorization stamp need a
    # review-trail correlation — a plan carrying its own PM stamp is never
    # reported regardless of review-trail state (both elements must be
    # absent — see module docstring), so resolving review coverage for the
    # rest would be wasted git work.
    unauthorized = {path: status for path, status in candidates.items() if path in missing_auth}
    if not unauthorized:
        return []

    candidate_shas = _plan_touching_shas_batch(repo_root, sorted(unauthorized))
    covered = _plans_with_review_trail_coverage(repo_root, candidate_shas)

    results: List[Dict[str, object]] = []
    for rel_path in sorted(unauthorized):
        if rel_path in covered:
            continue
        results.append(
            {
                "path": rel_path,
                "status": unauthorized[rel_path],
                "missing": [
                    dict(_EXECUTION_AUTHORIZATION_ELEMENT),
                    dict(_REVIEW_TRAIL_ELEMENT),
                ],
            }
        )
    return results


@register_op("plan.suggest_completion_steps")
def _plan_suggest_completion_steps(params: dict, repo_root: Optional[Path] = None) -> dict:
    """JSON-RPC 'plan.suggest_completion_steps' handler.

    See module docstring's "Invocation contract" section for the full wire
    contract. Fails loud when repo_root is None — same posture as
    `plan.list_stale_executing`/`plan.list_orphaned` (common_dir scope
    requires `_origin_worktree` in the envelope); no silent fallback to this
    repo's own docs/plans/.
    """
    if repo_root is None:
        raise ValueError(
            "plan.suggest_completion_steps requires a per-repo dispatch key "
            "(_origin_worktree); repo_root is None — op scope must be "
            "'common_dir' and _origin_worktree must be present in the "
            "JSON-RPC envelope. No silent fallback to this repo's own "
            "docs/plans/."
        )
    derived_root = main_worktree_root(repo_root)
    return {"plans": suggest_completion_steps(derived_root)}
