"""
coordinator_core.ops.review_coverage_core — Port of: review-coverage-core.sh
(DoE c187f5b9, 2026-07-21) — BIG_PORT Wave C, direct-import trampoline,
template-variant #1.

Purpose: shared coverage-computation core for review-trail gates. Exposes two
CLI modes via `main(argv)`:

    --reviewed-set <trail-path> [<trail-path>...]
        Prints one reviewed SHA per line on stdout (union across all records).
        Returns exit 0 on success, 1 on fatal error.

    --segments-json <trail-path> [<trail-path>...]
        Prints a JSON array of segment objects on stdout:
          [{"sha_range":"...","shas":["..."],"files":["..."]}]
        Each entry represents one valid diff trail record with its per-commit
        coverage info (per-segment file attribution for seam detection).
        Returns exit 0 on success, 1 on fatal error.

Env (mirrors the bash oracle):
    TRAIL_FILES — newline-separated list of trail-file paths (alternative to
                  positional args; used by coordinator_core.ops.workweek_trail_scope).
    WEEK_START  — if set (with TODAY), only records whose filename date-prefix
                  falls within [WEEK_START, TODAY] are processed.
    TODAY       — upper bound for date-prefix filtering (pairs with WEEK_START).

VERDICT FILTER (shared with coordinator_core.coverage — see EXCLUDED_VERDICTS
there): pending → EXCLUDED; ok/warn/blocked/waived/absent → INCLUDED.

Two DISTINCT error-class flags (guards-match-conditions-not-containers):
    --on-record-error skip|fail (default: fail)
        Governs JSON/JSONL PARSE failures. Default fail: a malformed record in
        the narrow current-week live dir is a fresh defect the operator must
        fix. The chain-end gate (coordinator_core.coverage.run_coverage_gate)
        passes skip because it scans the full archive.
    --on-unresolvable-ref skip|fail (default: inherits --on-record-error)
        Governs git REF-RESOLUTION failures (sha_range passes SAFE_RANGE but
        `git rev-list` fails — e.g. a cross-machine SHA not reachable locally).
        Fail-safe either way: an unparseable/unresolvable record credits NO
        commits, so affected commits surface as MORE review, never less.

Negative-spec:
    - --segments-json mode never batches `git rev-list` or `git log
      --name-only` across DISTINCT ranges — both legs ARE memoised per
      DISTINCT sha_range (build_segments's revlist_memo/namelog_memo) since
      each is a pure function of (sha_range, cwd), but combining ranges
      into one call is a separate, still-forbidden operation (SAFE_RANGE
      admits symbolic/live-HEAD endpoints — see build_segments's own
      comment).
    - --reviewed-set mode also resolves each DISTINCT range independently
      (one `git rev-list` per range, deduplicated, unioned) rather than
      reproducing the bash oracle's single-batched-call optimization — see
      build_reviewed_set's own docstring for why that optimization is a
      confirmed open defect, not faithfully reproduced here.
    - --intersect (reviewed-set mode only) filters the emitted union to SHAs
      present in a newline-separated file — verdict-preserving, mirrors
      review-coverage-gate.sh's --intersect tmpfile optimisation.

Reuses SAFE_RANGE / _parse_trail_file / _verdict_counts / _TrailParseError
from coordinator_core.coverage (same argument-injection validator, same
JSON-OR-JSONL parser, same verdict filter) rather than re-deriving them —
those are the shared primitives coverage.py already ported for
build_reviewed_set/run_coverage_gate; this module is deliberately NOT a
second independent reimplementation of that parsing layer (avoids drift
between the two coverage-computation surfaces).

Spec backlink: docs/plans/2026-06-23-chain-end-review-coverage-gate.md § C2
Port backlink: docs/plans/2026-07-16-bash-clean-slate-residual-migration.md

Central-reg: this op is a PLAIN MODULE (no @register_op) — direct-import
trampoline variant (template-variant #1; see DoE-claude
tasks/2026-07-16-clean-slate-recon/r1-doe-port-template.md § 1).
NOT wired into ops/__init__.py / _registry_map.py / ipc.py /
authz/classification.py — no registration action needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from coordinator_core.coverage import (
    SAFE_RANGE,
    _REVLIST_MAX_WORKERS,
    _TrailParseError,
    _classify_bookkeeping_shas,
    _parse_trail_file,
    _verdict_counts,
)
from coordinator_core.win_portability import no_console_creationflags

_CREATIONFLAGS = no_console_creationflags()

_GIT_TIMEOUT_SECS = 60

EXIT_OK = 0
EXIT_ERROR = 1


class _FatalError(Exception):
    """Raised on a fail-mode error (record-parse or ref-resolution) so `main`
    can catch it and return EXIT_ERROR — never sys.exit() from a helper, since
    this module is direct-imported in-process (workweek_trail_scope.py, tests)
    as well as run as a standalone trampoline CLI."""

_USAGE = (
    "Usage: review-coverage-core (--reviewed-set | --segments-json) "
    "[--on-record-error skip|fail] [--on-unresolvable-ref skip|fail] "
    "[<trail-path>...]\n"
    "  or set TRAIL_FILES env var and call with a mode flag only.\n"
    "  --on-record-error (default: fail): governs JSON/JSONL parse failures "
    "(fresh defects — fail loud).\n"
    "  --on-unresolvable-ref (default: inherits --on-record-error): governs "
    "git ref-resolution failures.\n"
    "    Cross-machine SHAs are structurally unresolvable on multi-machine "
    "weeks; skip-with-warning is safe.\n"
)


def _run(cmd: List[str], cwd: Optional[str] = None) -> Tuple[int, str, str]:
    """Run cmd; return (returncode, stdout.strip(), stderr). Never raises —
    a spawn failure or timeout degrades to a non-zero rc + diagnostic stderr,
    same shape as a normal git failure (A2: timeout + stdin=DEVNULL always set)."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=cwd,
            timeout=_GIT_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            **_CREATIONFLAGS,
        )
        return result.returncode, result.stdout.strip(), result.stderr
    except subprocess.TimeoutExpired:
        print(f"skip: _run: result = subprocess.run( failed: {sys.exc_info()[1]}", file=sys.stderr)
        return 1, "", f"command timed out after {_GIT_TIMEOUT_SECS}s: {' '.join(cmd)}"
    except OSError as exc:
        print(f"skip: _run: result = subprocess.run( failed: {exc}", file=sys.stderr)
        return 1, "", str(exc)


# ---------------------------------------------------------------------------
# Trail-file collection (positional args + TRAIL_FILES env, deduplicated,
# .json-suffix-only — mirrors the bash oracle).
# ---------------------------------------------------------------------------


def _collect_trail_files(trail_files_env: str, trail_args: Sequence[str]) -> List[str]:
    seen: Set[str] = set()
    out: List[str] = []
    joined = trail_files_env + "\n" + "\n".join(trail_args)
    for line in joined.split("\n"):
        p = line.strip()
        if p and p.endswith(".json") and p not in seen:
            seen.add(p)
            out.append(p)
    return out


def _load_intersect_shas(intersect_file: str) -> Set[str]:
    """Load the --intersect SHA set. Fail-safe: unreadable file disables the
    filter (returns empty set + WARN), mirrors the bash oracle."""
    shas: Set[str] = set()
    if not intersect_file:
        return shas
    try:
        with open(intersect_file, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                sha = line.strip()
                if sha:
                    shas.add(sha)
    except OSError as exc:
        print(
            f"WARN: --intersect file {intersect_file!r} unreadable ({exc}) "
            "— intersect filter disabled, returning full reviewed_set",
            file=sys.stderr,
        )
        return set()
    return shas


# ---------------------------------------------------------------------------
# Record loading — date-prefix filter (WEEK_START/TODAY) + on_record_error
# policy. Mirrors the bash oracle.
# ---------------------------------------------------------------------------


def _load_records(
    trail_files: Sequence[str],
    week_start: str,
    today: str,
    on_record_error: str,
) -> List[Tuple[str, dict]]:
    all_records: List[Tuple[str, dict]] = []
    for f in trail_files:
        basename = os.path.basename(f)
        if week_start and today:
            date_prefix = basename[:10]
            if not (week_start <= date_prefix <= today):
                continue
        try:
            recs = _parse_trail_file(f)
        except _TrailParseError as exc:
            if on_record_error == "skip":
                print(f"WARN: skipping unparseable trail record: {exc}", file=sys.stderr)
                continue
            print(f"ERROR: {exc}", file=sys.stderr)
            raise _FatalError(str(exc)) from exc
        for rec in recs:
            all_records.append((f, rec))
    return all_records


# ---------------------------------------------------------------------------
# Shared classification: scope_kind + SAFE_RANGE + verdict filter.
# Mirrors the identical block duplicated in the bash oracle between its
# --reviewed-set and --segments-json code paths.
# ---------------------------------------------------------------------------


def _classify_shape(rec: dict, warn: bool = True) -> Optional[Tuple[str, str, str]]:
    """Return (sha_range, artifact, kind) if the record is a diff- or
    plan-shaped record with a SAFE_RANGE-valid sha_range, else None.
    VERDICT-BLIND by construction — the verdict filter lives in `_classify`,
    which is the only coverage-crediting entry point.

    `kind` mirrors `coordinator_core.coverage.build_reviewed_set`'s Phase 1
    (C5, docs/plans/2026-08-05-coverage-gate-planning-artifact-class.md § C5):
    "diff" for a legacy/no-scope_kind record or an explicit scope_kind="diff"
    (or future value); "plan" for scope_kind="plan" — now RESOLVED like a diff
    record instead of skipped, so `_classify`'s caller can credit it, but ONLY
    against planning-artifact commits (see `_credit_from_kind_partition` — the
    filtering happens in `build_reviewed_set`, not here, since Phase-1 shape
    classification does no git calls). `scope_kind="integration"` remains
    skipped entirely — NOT reopened by this chunk (plan's Anti-scope): only
    "plan" becomes creditable, matching coverage.py's `build_reviewed_set`
    exactly, so this CLI-facing entry point and that one answer AC6 the same
    way (C6, docs/plans/2026-08-05-coverage-gate-planning-artifact-class.md
    § C6 — "a plan gate that answers differently by entry point is the defect
    this chunk exists to prevent").

    `warn=False` runs the identical checks silently, for a second, diagnostic-only
    read of the same records (`classify_pending_records`) that must not duplicate
    the crediting path's stderr.

    Spec backlink: pln-open-review-loops-are-a-named--6e8fea § C2
    Spec backlink (kind): pln-planning-artifacts-are-a-third-77111f § C6
    """
    sha_range = rec.get("sha_range", "")
    artifact = rec.get("artifact", "<unknown>")
    scope_kind = rec.get("scope_kind")

    if scope_kind is not None:
        if scope_kind == "integration":
            return None  # legitimately non-diff; skip silently — not reopened by this chunk
        if not sha_range:
            if warn:
                print(
                    f"WARN: diff-typed trail record has empty sha_range: {artifact}",
                    file=sys.stderr,
                )
            return None
        if scope_kind not in ("diff", "plan") and warn:
            # Per-record degrade, not global fatal (2026-08-10 coverage-gate
            # wedge: an unrecognized scope_kind anywhere in the trail corpus
            # must never take the whole gate down before it reaches a
            # VERDICT — see cross-repo/inbox/2026-08-10-example-retrieval-repo-ue-addon-
            # em-coverage-gate-crashes-on-chunk-and-inline-dispatch-kinds.md).
            # The record still flows through and resolves like any other, but
            # `_credit_from_kind_partition` never reads an unrecognized kind's
            # bucket, so it earns zero credit — fail-closed, unchanged safety
            # direction.
            print(
                f"WARN: unrecognized scope_kind {scope_kind!r} — record "
                f"credits nothing: {artifact}",
                file=sys.stderr,
            )
        # scope_kind == "diff" credits unconditionally (fall through);
        # scope_kind == "plan" (or future value) is resolved here and filtered
        # to planning-artifact commits downstream in build_reviewed_set.
        kind = scope_kind
    else:
        # Legacy record — no scope_kind. Use ".." inference. Always "diff".
        if not sha_range or ".." not in sha_range:
            if warn:
                print(
                    f"WARN: skipping non-diff trail record (sha_range={sha_range!r}): {artifact}",
                    file=sys.stderr,
                )
            return None
        kind = "diff"

    if not SAFE_RANGE.match(sha_range):
        if warn:
            print(
                f"WARN: skipping unsafe sha_range {sha_range!r} "
                f"(failed rev-range validation): {artifact}",
                file=sys.stderr,
            )
        return None

    return sha_range, artifact, kind


def _classify(rec: dict) -> Optional[Tuple[str, str, str]]:
    """Return (sha_range, artifact, kind) if the record passes all filters,
    else None (a WARN/INFO has already been emitted to stderr for the skip)."""
    shaped = _classify_shape(rec, warn=True)
    if shaped is None:
        return None
    sha_range, artifact, kind = shaped

    if not _verdict_counts(rec):
        print(
            f"INFO: skipping verdict=pending trail record (not yet reviewed): {artifact}",
            file=sys.stderr,
        )
        return None

    return sha_range, artifact, kind


# ---------------------------------------------------------------------------
# --reviewed-set mode — two-phase: classify (no git calls), then resolve each
# DISTINCT range independently via its own `git rev-list` and union the result.
#
# Deliberate divergence from the bash oracle's fix-1b "one batched git rev-list
# over every valid range" optimization — NOT reproduced here. That batching is
# a confirmed, OPEN, tracked defect (state/bug-backlog/
# 2026-07-03-review-coverage-core-batch-git-rev-list.yaml): `git rev-list
# A..B B..C` treats the shared boundary SHA as an exclude-anchor for the
# second range, dropping the first segment's commits from the union whenever
# two records have adjacent/touching sha_ranges (confirmed via
# review-coverage-core.test.sh TC14, which fails on the current bash oracle
# baseline — 21/22, not 22/22). Fail-safe direction (under-count → those
# commits surface as MORE required review, never a false pass) but still a
# correctness defect with a filed fix proposal: "accumulate the union
# per-range; the per-record fallback loop already accumulates correctly."
# This port adopts that exact proposed fix — one `git rev-list` spawn PER
# DISTINCT range (deduplicated), unioned — matching the approach
# coordinator_core.coverage.build_reviewed_set already uses for the same
# reason (see that module's own negative-spec on batching). Per Porter-brief-
# addendum rule 7: a tracked, fail-safe-direction, already-proposed-fix defect
# is fixed forward, not faithfully re-broken.
# ---------------------------------------------------------------------------


#: `kind` values a bucket is credited against unconditionally, no differently
#: from the pre-C6 behaviour — mirrors coverage.py's own
#: `_UNRESTRICTED_CREDIT_KINDS` (C5). Kept as a tuple rather than a bare
#: string comparison so a future kind addition greps to one obvious spot.
_UNRESTRICTED_CREDIT_KINDS: Tuple[str, ...] = ("diff",)


def _credit_from_kind_partition(
    reviewed_by_kind: Dict[str, Set[str]],
    cwd: Optional[str],
) -> Set[str]:
    """Collapse a per-kind reviewed partition into ONE credited set — the
    same kind-aware crediting rule as `coordinator_core.coverage`'s
    `_credit_from_kind_partition` (C5), applied here to this module's separate
    resolve-and-union implementation so the two live copies answer AC6
    identically (C6).

    "diff" (and any future unrestricted kind, see `_UNRESTRICTED_CREDIT_KINDS`)
    credits its resolved SHAs unconditionally, exactly as before this chunk.

    "plan" credits ONLY the subset of its resolved SHAs that
    `_classify_bookkeeping_shas` independently classifies PLANNING — reusing
    the already-landed C2 classifier (imported from `coordinator_core.coverage`)
    rather than inventing a second notion of "planning commit". This is the
    fix for the naive "just delete the skip" shortcut named in the plan's
    Anti-scope: that shortcut would credit a plan review's ENTIRE resolved
    range unconditionally, including any code commits it happens to span —
    worse than the false tail it replaces, and exploit-shaped (AC6).

    Any other kind (there are none yet reachable — "integration" is skipped
    in `_classify_shape` and never reaches this function) credits nothing,
    fail-closed.
    """
    credited: Set[str] = set()
    for kind in _UNRESTRICTED_CREDIT_KINDS:
        credited |= reviewed_by_kind.get(kind, set())

    plan_raw = reviewed_by_kind.get("plan", set())
    if plan_raw:
        _, planning_set, _note = _classify_bookkeeping_shas(list(plan_raw), cwd or ".", {})
        credited |= (plan_raw & planning_set)

    return credited


def build_reviewed_set(
    all_records: Sequence[Tuple[str, dict]],
    on_unresolvable_ref: str,
    intersect_shas: Set[str],
    cwd: Optional[str] = None,
) -> Set[str]:
    """Phase 1: classify (no git calls). Phase 2: resolve each DISTINCT
    (range, kind) pair independently (dedup identical range+kind pairs first —
    many trail records cite the same sha_range) and union into reviewed_set,
    PARTITIONED BY `kind` (C6, mirroring coverage.py's build_reviewed_set —
    C5) so a "plan" record's resolved SHAs are filtered to planning-artifact
    commits before crediting (see `_credit_from_kind_partition`) rather than
    crediting its whole range unconditionally."""
    valid_ranges: List[Tuple[str, str, str]] = []
    for _source_path, rec in all_records:
        classified = _classify(rec)
        if classified is not None:
            valid_ranges.append(classified)

    if not valid_ranges:
        return set()

    # `kind` joins the dedup key — without it, a "plan" record and a "diff"
    # record citing the identical sha_range would collapse to whichever
    # parses first via `setdefault`, dropping the other kind's credit
    # entirely (same collision case C5 fixed in coverage.py's dedup key).
    distinct: Dict[Tuple[str, str], str] = {}
    for sha_range, artifact, kind in valid_ranges:
        distinct.setdefault((sha_range, kind), artifact)

    reviewed_by_kind: Dict[str, Set[str]] = {}
    for (sha_range, kind), artifact in distinct.items():
        rc, shas_out, rev_err = _run(["git", "rev-list", sha_range], cwd=cwd)
        if rc != 0:
            last_err = rev_err.strip().splitlines()[-1] if rev_err.strip() else "git rev-list failed"
            if on_unresolvable_ref == "skip":
                print(
                    f"WARN: skipping trail record with unresolvable range {sha_range!r}: "
                    f"{last_err} ({artifact})",
                    file=sys.stderr,
                )
                continue
            print(f"ERROR: command failed: git rev-list {sha_range}\n{rev_err}", file=sys.stderr)
            raise _FatalError(f"git rev-list {sha_range} failed")
        bucket = reviewed_by_kind.setdefault(kind, set())
        if shas_out:
            for sha in shas_out.splitlines():
                if sha and (not intersect_shas or sha in intersect_shas):
                    bucket.add(sha)
    return _credit_from_kind_partition(reviewed_by_kind, cwd)


# ---------------------------------------------------------------------------
# --segments-json mode — per-record git rev-list + git log --name-only.
# Mirrors the bash oracle. Neither leg is BATCHED across distinct ranges
# (SAFE_RANGE admits symbolic/live-HEAD endpoints; git computes
# reachable(positives) \ reachable(negatives) as ONE set expression per
# range — combining ranges would silently drop coverage on a linear chain).
# Both legs ARE memoised per DISTINCT sha_range (revlist_memo / namelog_memo
# in build_segments): `git log --name-only --format= <sha_range>` is a pure
# function of (sha_range, cwd) with `kind` already discarded by `_classify`,
# so two records citing the same range resolve to the identical `files` set.
# Per-segment ATTRIBUTION is still preserved — each record still gets its
# own segment dict, the memoised set is just emitted into every one of them
# instead of being re-resolved by a fresh spawn each time.
# ---------------------------------------------------------------------------


def build_segments(
    all_records: Sequence[Tuple[str, dict]],
    on_unresolvable_ref: str,
    cwd: Optional[str] = None,
) -> List[Dict[str, object]]:
    segments: List[Dict[str, object]] = []

    # Per-range memo for the `git rev-list` leg (keyed on sha_range alone —
    # build_segments has no kind-partition, unlike build_reviewed_set). Each
    # DISTINCT range is still resolved independently (no multi-range
    # batching: SAFE_RANGE admits symbolic/live-HEAD endpoints, and git
    # computes reachable(positives) \ reachable(negatives) as ONE set
    # expression per range — combining ranges would silently drop coverage
    # on a linear chain with no test failure). This memo only eliminates
    # RE-resolving a range already seen in this same build_segments call —
    # the top repeater fires 22x with zero dedup before this fix.
    #
    # The sibling `git log --name-only` leg gets the identical treatment
    # (namelog_memo/namelog_skip below): memoised per DISTINCT sha_range,
    # never batched across ranges. `git log --name-only --format= <range>`
    # is a pure function of (sha_range, cwd) — `_classify` already discarded
    # `kind` before this point — so two records citing the same range get an
    # identical `files` set. Per-segment file ATTRIBUTION (the reason this
    # leg exists) is preserved because the memoised set is still emitted
    # into every record's own segment dict below, not deduplicated away.
    revlist_memo: Dict[str, Optional[Set[str]]] = {}
    revlist_skip: Set[str] = set()
    namelog_memo: Dict[str, Set[str]] = {}
    namelog_skip: Set[str] = set()

    for _source_path, rec in all_records:
        classified = _classify(rec)
        if classified is None:
            continue
        sha_range, artifact, _kind = classified

        if sha_range in revlist_skip:
            continue

        if sha_range in revlist_memo:
            shas = revlist_memo[sha_range]
        else:
            rc, shas_out, rev_err = _run(["git", "rev-list", sha_range], cwd=cwd)
            if rc != 0:
                last_err = rev_err.strip().splitlines()[-1] if rev_err.strip() else "git rev-list failed"
                if on_unresolvable_ref == "skip":
                    print(
                        f"WARN: skipping trail record with unresolvable range {sha_range!r}: "
                        f"{last_err} ({artifact})",
                        file=sys.stderr,
                    )
                    revlist_skip.add(sha_range)
                    continue
                print(f"ERROR: command failed: git rev-list {sha_range}\n{rev_err}", file=sys.stderr)
                raise _FatalError(f"git rev-list {sha_range} failed")
            shas = set(shas_out.splitlines()) if shas_out else set()
            revlist_memo[sha_range] = shas

        if sha_range in namelog_skip:
            continue

        if sha_range in namelog_memo:
            files = namelog_memo[sha_range]
        else:
            rc2, log_out, log_err = _run(
                ["git", "log", "--name-only", "--format=", sha_range], cwd=cwd
            )
            if rc2 != 0:
                if on_unresolvable_ref == "skip":
                    print(
                        f"WARN: skipping trail record (git log failed) {sha_range!r} ({artifact})",
                        file=sys.stderr,
                    )
                    namelog_skip.add(sha_range)
                    continue
                print(f"ERROR: command failed: git log {sha_range}\n{log_err}", file=sys.stderr)
                raise _FatalError(f"git log {sha_range} failed")
            files = set(line for line in log_out.splitlines() if line.strip()) if log_out else set()
            namelog_memo[sha_range] = files

        segments.append({"sha_range": sha_range, "shas": shas, "files": files})

    return [
        {
            "sha_range": seg["sha_range"],
            "shas": sorted(seg["shas"]),
            "files": sorted(seg["files"]),
        }
        for seg in segments
    ]


# ---------------------------------------------------------------------------
# Pending-record closure — DERIVED state, never a stored field.
#
# A pending (verdict=pending) trail record is the "review round opened" marker
# a freeze emits. It is CLOSED when some non-pending record's resolved SHA set
# is a SUPERSET of the pending record's resolved SHA set: the round it opened
# has since been verdicted, by a record that covers at least everything the
# freeze froze. No `loop_state` field exists or should exist (plan Anti-scope:
# a parallel field would give two sources of truth for the same fact).
#
# ADDITIVE-ONLY: this is a second, diagnostic read of records the crediting
# path already parsed. Nothing here feeds reviewed_set / segments, and a
# pending record still credits ZERO coverage whether closed or not (AC4).
# ---------------------------------------------------------------------------


def classify_pending_records(
    all_records: Sequence[Tuple[str, dict]],
    resolve_range: Optional[Callable[[str], Optional[Set[str]]]] = None,
    cwd: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Derive, per pending trail record, whether its review round is CLOSED and
    by which record.

    Closure is set containment: pending record P is closed iff some NON-pending
    record N (diff-shaped, SAFE_RANGE-valid) resolves to a SHA set that is a
    superset of P's non-empty resolved SHA set. Among several candidates the
    TIGHTEST closer wins (smallest SHA set; `sha_range` compared lexically to
    break a remaining tie) so the attribution is deterministic across runs.

    Args:
        all_records:   The `(source_path, record)` pairs `_load_records` already
                       produced — the SAME parsed records the crediting path
                       consumes, never a re-read of disk.
        resolve_range: Optional `Callable[[str], Optional[Set[str]]]` mapping a
                       sha_range to its resolved SHAs, or None for "unresolvable".
                       A caller that has already resolved these ranges (e.g. the
                       chain gate) passes its own resolver so this pass adds ZERO
                       git spawns. Default: one `git rev-list` per DISTINCT range
                       across all classified records, memoized.
        cwd:           Repo root for the default resolver's git calls.

    Returns:
        One dict per pending record, in input order:
            {"artifact": str,             # record's artifact, "<unknown>" if absent
             "sha_range": str,            # the pending record's range
             "shas": List[str],           # sorted resolved SHAs; [] if unresolved
             "resolved": bool,            # False iff the range would not resolve
             "closed": bool,
             "closed_by_artifact": Optional[str],   # None iff closed is False
             "closed_by_sha_range": Optional[str]}  # None iff closed is False

    Negative-spec:
        - Pending records never close other pending records — only a non-pending
          record's set can close a round.
        - An unresolvable range, or one resolving to ZERO commits, reports
          `closed: False` with a null closer rather than being trivially closed
          by every non-empty set. It credits nothing and covers nothing, so a
          consumer intersecting `shas` against uncovered commits never matches it.
        - No stderr is emitted: shape/SAFE_RANGE rejections are re-checked
          silently here (`_classify_shape(warn=False)`) because the crediting
          path already warned about the same records.

    Spec backlink: pln-open-review-loops-are-a-named--6e8fea § C2
    """
    pending: List[Tuple[str, str]] = []
    non_pending: List[Tuple[str, str]] = []
    for _source_path, rec in all_records:
        shaped = _classify_shape(rec, warn=False)
        if shaped is None:
            continue
        # Closure is kind-oblivious (unchanged by C6): drop `kind` here — a
        # "plan" record's set-containment closure semantics are identical to
        # a "diff" record's, and this pass credits nothing regardless (AC4).
        sha_range, artifact, _kind = shaped
        if _verdict_counts(rec):
            non_pending.append((sha_range, artifact))
        else:
            pending.append((sha_range, artifact))

    if not pending:
        return []

    memo: Dict[str, Optional[Set[str]]] = {}

    if resolve_range is not None:
        def _resolve(sha_range: str) -> Optional[Set[str]]:
            if sha_range in memo:
                return memo[sha_range]
            resolved = resolve_range(sha_range)
            memo[sha_range] = resolved
            return resolved
    else:
        # Batch pre-scan for the default resolver (no caller-injected
        # resolve_range — i.e. no single graph_range window exists to walk
        # in one shot, see _make_chain_range_resolver's own docstring for
        # when that is): resolve every DISTINCT range across pending AND
        # non_pending in ONE bounded-parallel sweep, instead of the
        # closers/pending loops below triggering one `git rev-list` spawn
        # per distinct range each as they walk their own inputs in series.
        # Same command, same per-distinct-range memoization, same result —
        # only the SCHEDULING changes (concurrent instead of serial), so
        # this cannot change which record ends up in which bucket. Worker
        # cap reuses coverage.py's own `_REVLIST_MAX_WORKERS`, the identical
        # bound already accepted in this codebase for the identical
        # primitive (build_reviewed_set's Strategy B fan-out).
        _distinct_ranges = sorted(
            {sha_range for sha_range, _artifact in pending}
            | {sha_range for sha_range, _artifact in non_pending}
        )

        def _resolve_one(sha_range: str) -> Tuple[str, Optional[Set[str]]]:
            rc, shas_out, _err = _run(["git", "rev-list", sha_range], cwd=cwd)
            resolved = (
                set(s for s in shas_out.splitlines() if s) if rc == 0 else None
            )
            return sha_range, resolved

        _max_workers = min(len(_distinct_ranges), _REVLIST_MAX_WORKERS)
        if _max_workers <= 1:
            _resolved_pairs = [_resolve_one(r) for r in _distinct_ranges]
        else:
            with ThreadPoolExecutor(max_workers=_max_workers) as pool:
                _resolved_pairs = list(pool.map(_resolve_one, _distinct_ranges))
        memo.update(_resolved_pairs)

        def _resolve(sha_range: str) -> Optional[Set[str]]:
            return memo.get(sha_range)

    closers: List[Tuple[str, str, Set[str]]] = []
    for sha_range, artifact in non_pending:
        shas = _resolve(sha_range)
        if shas:
            closers.append((sha_range, artifact, shas))

    out: List[Dict[str, object]] = []
    for sha_range, artifact in pending:
        shas = _resolve(sha_range)
        entry: Dict[str, object] = {
            "artifact": artifact,
            "sha_range": sha_range,
            "shas": sorted(shas) if shas else [],
            "resolved": shas is not None,
            "closed": False,
            "closed_by_artifact": None,
            "closed_by_sha_range": None,
        }
        if shas:
            candidates = [c for c in closers if c[2] >= shas]
            if candidates:
                best = min(candidates, key=lambda c: (len(c[2]), c[0]))
                entry["closed"] = True
                entry["closed_by_artifact"] = best[1]
                entry["closed_by_sha_range"] = best[0]
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# Direct-import convenience wrapper — for a Python caller that wants the
# --segments-json result as data (no subprocess, no JSON round-trip). Used by
# coordinator_core.ops.workweek_trail_scope, which used to shell out to this
# module's own CLI trampoline (coordinator/lib/review-coverage-core.sh,
# DoE-claude) via `bash <script> --segments-json --on-unresolvable-ref skip`;
# now a same-process call now that both live in coordinator_core.
# ---------------------------------------------------------------------------


def collect_segments(
    trail_files: Sequence[str],
    week_start: str = "",
    today: str = "",
    on_record_error: str = "fail",
    on_unresolvable_ref: str = "skip",
    cwd: Optional[str] = None,
) -> List[Dict[str, object]]:
    """Load + classify trail records, then build --segments-json's payload.

    Raises _FatalError on a fail-mode record-parse or git-ref-resolution
    failure (mirrors the CLI's exit-1 contract) — callers that want the CLI's
    "exit 1, no output" behavior on error should catch this; `main()` does.
    """
    all_records = _load_records(trail_files, week_start, today, on_record_error)
    return build_segments(all_records, on_unresolvable_ref, cwd=cwd)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None, cwd: Optional[str] = None) -> int:
    # A4/Windows-hazard note: stdout text-mode emits \r\n on Windows; bash
    # $()+mapfile capture (a still-bash caller shelling out to this trampoline)
    # would preserve \r and corrupt SHA-set keys → false UNCOVERED verdict.
    # Force LF output here (mirrors review-coverage-core.sh:191).
    try:
        sys.stdout.reconfigure(newline="\n")  # Python >= 3.7
    except AttributeError:
        print(f"skip: main: sys.stdout.reconfigure(newline=\"\\n\")  # Python >= 3.7 failed: {sys.exc_info()[1]}", file=sys.stderr)
        pass

    argv = list(argv) if argv is not None else sys.argv[1:]

    if not argv:
        sys.stderr.write(_USAGE)
        return EXIT_ERROR

    mode = argv[0]
    rest = argv[1:]
    if mode not in ("--reviewed-set", "--segments-json"):
        sys.stderr.write(_USAGE)
        return EXIT_ERROR

    on_record_error = "fail"
    on_unresolvable_ref = ""  # empty = inherit from on_record_error
    intersect_file = ""
    trail_path_args: List[str] = []

    i = 0
    while i < len(rest):
        tok = rest[i]
        if tok == "--on-record-error":
            on_record_error = rest[i + 1] if i + 1 < len(rest) else ""
            i += 2
        elif tok == "--on-unresolvable-ref":
            on_unresolvable_ref = rest[i + 1] if i + 1 < len(rest) else ""
            i += 2
        elif tok == "--intersect":
            intersect_file = rest[i + 1] if i + 1 < len(rest) else ""
            i += 2
        else:
            trail_path_args.append(tok)
            i += 1

    if on_record_error not in ("skip", "fail"):
        print(
            f"ERROR: --on-record-error must be 'skip' or 'fail', got: {on_record_error}",
            file=sys.stderr,
        )
        return EXIT_ERROR
    if not on_unresolvable_ref:
        on_unresolvable_ref = on_record_error
    if on_unresolvable_ref not in ("skip", "fail"):
        print(
            f"ERROR: --on-unresolvable-ref must be 'skip' or 'fail', got: {on_unresolvable_ref}",
            file=sys.stderr,
        )
        return EXIT_ERROR

    trail_files_env = os.environ.get("TRAIL_FILES", "")
    week_start = os.environ.get("WEEK_START", "")
    today = os.environ.get("TODAY", "")

    intersect_shas = _load_intersect_shas(intersect_file)

    trail_files = _collect_trail_files(trail_files_env, trail_path_args)
    try:
        all_records = _load_records(trail_files, week_start, today, on_record_error)

        if mode == "--reviewed-set":
            reviewed = build_reviewed_set(all_records, on_unresolvable_ref, intersect_shas, cwd=cwd)
            for sha in sorted(reviewed):
                print(sha)
            return EXIT_OK

        # mode == "--segments-json"
        segments = build_segments(all_records, on_unresolvable_ref, cwd=cwd)
        print(json.dumps(segments, indent=2))
        return EXIT_OK
    except _FatalError:
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
