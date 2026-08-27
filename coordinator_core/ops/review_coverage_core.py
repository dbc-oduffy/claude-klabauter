"""
coordinator_core.ops.review_coverage_core — Port of: review-coverage-core.sh
(DoE c187f5b9, 2026-07-21) — BIG_PORT Wave C, direct-import trampoline,
template-variant #1.

Purpose: shared coverage-computation core for review-trail gates. Exposes two
CLI modes via `main(argv)`:

    --reviewed-set [<trail-path> ...]
        Prints one reviewed SHA per line on stdout — the resident
        reviewed-set store's full membership (docs/plans/2026-08-27-the-
        reviewed-set-is-a-file-not-a-computation.md), optionally narrowed by
        --intersect. Positional trail-path args are accepted for CLI/back-
        compat but are NOT read in this mode (see "Migration note" below):
        the store is never path- or trail-file-scoped, matching
        `coordinator_core.ops.gate_dimension_review`'s identical migration
        (C3, same plan). Returns exit 0 on success; this mode cannot fail.

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

Migration note (C4, docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-
computation.md § C4): --reviewed-set mode no longer loads, classifies, or
resolves any trail record itself. All five credit rules (verdict filter,
HEAD-anchored exclusion, kind partitioning, foreign-session narrowing, the
never-path-scoped rule) already ran at WRITE time
(`coordinator_core.review_trail.backfill.resolve_and_fold`, called by
`ops/review_trail_write.py` and the one-shot `run_backfill`) and their
result is already folded into the resident reviewed-set store. This mode is
now a pure membership read of that store
(`coordinator_core.review_trail.reviewed_set.read_reviewed_set`) — zero
added git spawns, never a subprocess — exactly mirroring
`coordinator_core.ops.gate_dimension_review`'s C3 migration onto the same
store. `--on-record-error` / `--on-unresolvable-ref` / positional trail-path
args are accepted but inert in this mode: there is nothing left for either
flag to govern once no record is loaded or resolved here. --segments-json
mode is UNCHANGED by this migration (see below) — it still needs each
record's own per-range file attribution for seam detection, which the
reviewed-set store's flat SHA union does not carry.

Negative-spec:
    - --segments-json mode never batches its single `git log --format=%H
      --name-only <range>` spawn (C17 merged the former separate `git
      rev-list` + `git log --name-only` legs into this one call, per range)
      across DISTINCT ranges — it IS memoised per DISTINCT sha_range
      (build_segments's segment_memo) since it is a pure function of
      (sha_range, cwd), but combining ranges into one call is a separate,
      still-forbidden operation (SAFE_RANGE admits symbolic/live-HEAD
      endpoints — see build_segments's own comment).
    - --reviewed-set mode does NOT call
      `coordinator_core.review_trail.backfill.resolve_and_fold` (or any
      other fold-triggering path) — that primitive is write-path-only ("NOT
      a gate-path cost... no gate caller may trigger it", per its own
      module docstring); a read-side caller that folded on demand would
      reintroduce the per-call git-spawn cost this migration exists to
      remove. A record not yet folded (write-time resolution pending, or a
      backfill not yet run) is simply not yet in the store — the same
      conservative "uncredited until folded" direction every other reader
      of this store already accepts.
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
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Dict, List, Optional, Sequence, Set, Tuple

from coordinator_core.coverage import (
    SAFE_RANGE,
    _REVLIST_MAX_WORKERS,
    _TrailParseError,
    _parse_trail_file,
    _verdict_counts,
    emit_unrecognized_kind_warning,
)
from coordinator_core.review_trail.reviewed_set import read_reviewed_set
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


def _classify_shape(
    rec: dict,
    warn: bool = True,
    unrecognized_sink: Optional[Dict[str, int]] = None,
) -> Optional[Tuple[str, str, str]]:
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

    `unrecognized_sink`, when given, accumulates unrecognized-`scope_kind`
    counts (keyed by the kind string) instead of printing a WARN per record —
    the per-record flood buried the real trailing error on a legacy corpus
    (2026-08-15 example-retrieval-repo-em memo). The caller (`build_reviewed_set`,
    `build_segments`) owns the dict and emits ONE aggregated WARN after its
    walk. `None` (the default) preserves the old per-call print, for any
    direct caller of this function that has no walk to aggregate over.

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
            # Only an actual diff-typed record's empty sha_range is a real
            # signal worth a WARN. The `scope_kind == "diff"` guard means
            # EVERY other kind skips silently here on empty sha_range — not
            # just recognized ones like "plan", but unrecognized kinds too
            # (e.g. "inline-dispatch"): this `return None` precedes the
            # unrecognized-kind accumulation branch below, so an unrecognized
            # kind with no sha_range is never counted there either, in
            # addition to producing no WARN. This mirrors coverage.py's
            # build_reviewed_set, which `continue`s here with no print
            # regardless of kind (see its "skip silently" comment on the
            # identical branch).
            if scope_kind == "diff" and warn:
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
            if unrecognized_sink is not None:
                unrecognized_sink[scope_kind] = unrecognized_sink.get(scope_kind, 0) + 1
            else:
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


def _classify(
    rec: dict,
    unrecognized_sink: Optional[Dict[str, int]] = None,
) -> Optional[Tuple[str, str, str]]:
    """Return (sha_range, artifact, kind) if the record passes all filters,
    else None (a WARN/INFO has already been emitted to stderr for the skip).

    `unrecognized_sink` is forwarded to `_classify_shape` unchanged — see its
    docstring."""
    shaped = _classify_shape(rec, warn=True, unrecognized_sink=unrecognized_sink)
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
# --reviewed-set mode — a pure membership read of the resident reviewed-set
# store (docs/plans/2026-08-27-the-reviewed-set-is-a-file-not-a-computation.md
# § C4). No record loading, no classification, no git calls: every credit
# rule already ran at write time (`review_trail.backfill.resolve_and_fold`)
# and its result is already folded into the store this reads. This mirrors
# `coordinator_core.ops.gate_dimension_review`'s identical C3 migration onto
# the same store — see this module's own docstring "Migration note".
# ---------------------------------------------------------------------------


def build_reviewed_set(
    intersect_shas: Set[str],
    cwd: Optional[str] = None,
) -> Set[str]:
    """Return the resident reviewed-set store's full membership, optionally
    narrowed to `intersect_shas` (verdict-preserving: the gate/CLI caller
    only ever tests membership of its OWN commit list, so extra SHAs never
    change the answer). Zero git spawns — `read_reviewed_set` is a resident,
    `os.stat`-revalidated file read; see the module's "Migration note"."""
    reviewed = read_reviewed_set(cwd or os.getcwd())
    if intersect_shas:
        return {sha for sha in reviewed if sha in intersect_shas}
    return set(reviewed)


# ---------------------------------------------------------------------------
# --segments-json mode — per-record git rev-list + git log --name-only.
# Mirrors the bash oracle. NOT batched across distinct ranges (SAFE_RANGE
# admits symbolic/live-HEAD endpoints; git computes reachable(positives) \
# reachable(negatives) as ONE set expression per range — combining ranges
# would silently drop coverage on a linear chain, with no test failure).
#
# The two legs ARE now ONE spawn per DISTINCT sha_range instead of two
# (C17, docs/plans/2026-08-15-composition-invocation-budgets.md): `git
# rev-list <range>` and `git log --name-only --format= <range>` walk the
# IDENTICAL commit set for the same range (no pathspec, no --first-parent
# on either side), so `git log --format=%H --name-only <range>` answers
# both in a single spawn — one `%H` line per commit (the rev-list leg) plus
# that commit's changed-file lines (the name-only leg), same as today: a
# merge commit still emits its `%H` line and no file lines under both the
# old two-call form and this combined one, since --name-only shows no diff
# for a merge either way. `_parse_combined_log_output` splits the two
# interleaved sets back apart. This is a leg MERGE, not a range collapse —
# each distinct sha_range is still resolved by its own independent spawn;
# the forbidden operation (batching >1 range into one git invocation) is
# untouched. Memoised per DISTINCT sha_range exactly as the two-leg form
# was: two records citing the same range emit their own segment dict, the
# memoised (shas, files) pair is just not re-resolved by a fresh spawn.
# ---------------------------------------------------------------------------


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _parse_combined_log_output(text: str) -> Tuple[Set[str], Set[str]]:
    """Split `git log --format=%H --name-only <range>` output into the SHA
    set (`%H` lines) and the changed-file set (every other non-blank line).

    Line-shape disambiguation: a `%H` line is always exactly 40 lowercase hex
    characters; a repo path that happens to look like one is not a realistic
    collision this corpus has ever produced (same assumption the rest of this
    module and coverage.py already make about SHA-shaped lines in git output).
    """
    shas: Set[str] = set()
    files: Set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if _FULL_SHA_RE.match(line):
            shas.add(line)
        else:
            files.add(line)
    return shas, files


def build_segments(
    all_records: Sequence[Tuple[str, dict]],
    on_unresolvable_ref: str,
    cwd: Optional[str] = None,
) -> List[Dict[str, object]]:
    segments: List[Dict[str, object]] = []

    # Per-range memo for the combined `git log --format=%H --name-only` spawn
    # (keyed on sha_range alone — build_segments has no kind-partition, unlike
    # build_reviewed_set). Each DISTINCT range is still resolved independently
    # (no multi-range batching: SAFE_RANGE admits symbolic/live-HEAD endpoints,
    # and git computes reachable(positives) \ reachable(negatives) as ONE set
    # expression per range — combining ranges would silently drop coverage on
    # a linear chain with no test failure). This memo only eliminates
    # RE-resolving a range already seen in this same build_segments call.
    #
    # Per-segment file ATTRIBUTION (the reason the name-only leg exists) is
    # preserved because the memoised (shas, files) pair is still emitted into
    # every record's own segment dict below, not deduplicated away.
    segment_memo: Dict[str, Tuple[Set[str], Set[str]]] = {}
    segment_skip: Set[str] = set()
    unrecognized_kind_counts: Dict[str, int] = {}

    for _source_path, rec in all_records:
        classified = _classify(rec, unrecognized_sink=unrecognized_kind_counts)
        if classified is None:
            continue
        sha_range, artifact, _kind = classified

        if sha_range in segment_skip:
            continue

        if sha_range in segment_memo:
            shas, files = segment_memo[sha_range]
        else:
            rc, combined_out, err = _run(
                ["git", "log", "--format=%H", "--name-only", sha_range], cwd=cwd
            )
            if rc != 0:
                last_err = err.strip().splitlines()[-1] if err.strip() else "git log failed"
                if on_unresolvable_ref == "skip":
                    print(
                        f"WARN: skipping trail record with unresolvable range {sha_range!r}: "
                        f"{last_err} ({artifact})",
                        file=sys.stderr,
                    )
                    segment_skip.add(sha_range)
                    continue
                print(
                    f"ERROR: command failed: git log --format=%H --name-only {sha_range}\n{err}",
                    file=sys.stderr,
                )
                raise _FatalError(f"git log {sha_range} failed")
            shas, files = _parse_combined_log_output(combined_out)
            segment_memo[sha_range] = (shas, files)

        segments.append({"sha_range": sha_range, "shas": shas, "files": files})

    emit_unrecognized_kind_warning(unrecognized_kind_counts)

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
        # in one shot): resolve every DISTINCT range across pending AND
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

    try:
        if mode == "--reviewed-set":
            # No record loading here (see module docstring "Migration
            # note"): trail_files_env / trail_path_args / on_record_error /
            # on_unresolvable_ref are accepted for CLI back-compat but inert
            # in this mode — the store is never trail-file-scoped.
            reviewed = build_reviewed_set(intersect_shas, cwd=cwd)
            for sha in sorted(reviewed):
                print(sha)
            return EXIT_OK

        # mode == "--segments-json" — unchanged: still needs per-record
        # classification and file attribution (see module docstring).
        trail_files = _collect_trail_files(trail_files_env, trail_path_args)
        all_records = _load_records(trail_files, week_start, today, on_record_error)
        segments = build_segments(all_records, on_unresolvable_ref, cwd=cwd)
        print(json.dumps(segments, indent=2))
        return EXIT_OK
    except _FatalError:
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
