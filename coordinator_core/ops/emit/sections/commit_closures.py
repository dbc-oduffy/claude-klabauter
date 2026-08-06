"""Section porter — commit closures (envelope key: ``commit_closures``).

Emits one ``CommitClosure`` record per ``(sha, item_id)`` pair recovered from a commit's
``Closes:`` git trailer — the deterministic "did a landed commit close this work item"
fact cockpit's ``recs-05``/B4 code-complete auto-assert needs (source memo:
2026-07-17-example-cockpit-repo-em-wsc-commit-closure-emit.md).

``collect(ctx)`` performs EXACTLY ONE bounded ``git log`` subprocess (DECISION-1/AC5): no
per-commit spawn, no new durable store — git history itself is the store. The scan range
(DECISION-3) is commits reachable from ``origin/main`` PLUS the current branch's unmerged
commits (``git log origin/main HEAD ...`` — a union-of-refs query, not a range — dedupes
naturally when HEAD already sits on origin/main), bounded by a generous ``--since`` horizon
comfortably exceeding any realistic open-item age. A fixed git history therefore yields a
fixed ``commit_closures`` array (idempotent re-emit). Extraction reuses the git-native
trailer-block parser via ``%(trailers:key=Closes,valueonly)`` (house precedent:
``coverage.py``'s ``Session-Id`` extraction, :932) rather than hand-rolling raw-body regex —
git already isolates the trailer VALUE, so a commit body's prose "closes the loop" cannot
reach this porter at all. Each commit's isolated trailer value(s) are handed to C1's
``parse_closure_trailers`` normalizer to recover ``item_id``s (DECISION-2); one record is
emitted per ``(sha, item_id)`` pair (DECISION-4 — no cross-commit dedup at write time).

``reachable_on_default_branch`` is left ``None`` here by construction — this porter's
``collect()`` never resolves reachability itself (that would mean a merge-base spawn per
distinct SHA inside collect(), violating the single-git-log claim). Reachability is stamped
by the post-collect enricher ``envelope._stamp_closure_reachability``, called from
``envelope.build()`` alongside ``_stamp_shipped_sha`` (AC4).

Malformed rows: a well-formed ``git log`` run using this porter's own NUL-delimited format
string cannot itself produce a truncated record, but a defensively-validated SHA (must match
40 lowercase hex chars — %H's own output shape) that fails validation is quarantined rather
than emitted as a record with a corrupt identity key.

Negative-spec:
  - Does NOT add any code to the commit hot path (``wsc_tail.py`` / ``commit_anchors.py`` /
    ``run_commit_pipeline``) — Anti-scope. This porter runs at emit time only.
  - Does NOT compute or freeze reachability at commit time — DECISION-1's whole point is that
    reachability is a moving target (a SHA becomes default-branch-reachable only post-merge),
    so it is intentionally left null here and resolved fresh at every emit.
  - Does NOT invoke ``parse_closure_trailers`` on raw commit bodies — only on the already-
    isolated trailer VALUES git's own trailer-block parser produced (DECISION-2).
  - Does NOT scan unbounded history — the ``--since`` horizon bounds the single ``git log``
    call (DECISION-3); a bare ``--since``/``-n`` cap alone and a fork-point-only range were
    both considered and rejected (see plan DECISION-3) for silently dropping decision-relevant
    closures.

Spec backlink: docs/plans/2026-07-17-commit-closure-emission-fact.md § Chunk C3, DECISION-1,
  DECISION-2, DECISION-3, AC4, AC5.
Parity oracle: none — net-new record type; no bash equivalent ever emitted this fact.
"""

from __future__ import annotations

import re
import subprocess

from coordinator_core.ops.emit.closure_trailer import parse_closure_trailers
from coordinator_core.ops.emit.context import EmitContext

# Generous scan horizon (DECISION-3): ~18 months — a deliberate buffer over the plan's own
# "on the order of a year" illustrative example. A closure whose commit predates this ages
# out of the emission by deliberate design (a bound on an unbounded-history scan, not a
# durable-store guarantee).
# Review: code-reviewer — comment previously echoed the plan's "on the order of a year"
# phrasing verbatim, which reads as if it were quoting the constant itself rather than the
# plan's example; a future reader tuning this value had no signal for why 540 (vs. 365).
_SCAN_SINCE_HORIZON = "540 days ago"

# NUL-delimited format: a NUL precedes every commit's %H so the whole subprocess output can
# be split unambiguously on "\x00" even though %(trailers:...valueonly) may itself emit
# multiple newline-separated lines when a commit carries more than one Closes: trailer.
_LOG_FORMAT = "%x00%H%x00%(trailers:key=Closes,valueonly)"

# %H always emits a full 40-char lowercase hex SHA; a line failing this shape indicates a
# corrupt/unexpected git-log record, not a valid commit identity.
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def _extract_closure_commits(ctx: EmitContext) -> tuple[list[tuple[str, list[str]]], list[dict]]:
    """Run the ONE bounded ``git log`` and return ``(sha, trailer_values)`` pairs + malformed rows.

    Scoped to DECISION-3's range: commits reachable from ``origin/main`` OR ``HEAD`` (a
    union-of-positive-refs query — git de-dupes commits reachable from either, so this is
    naturally idempotent when HEAD sits on or ahead of origin/main), bounded by
    ``_SCAN_SINCE_HORIZON``. Any subprocess failure (missing git, no origin/main configured,
    offline) degrades to ``([], [])`` — this section never aborts emit (matches every other
    porter's fail-open posture, e.g. ``sections/roadmap_dag.py._query_roadmap_records``).
    """
    cmd = [
        "git", "-C", str(ctx.repo_root), "log",
        "origin/main", "HEAD",
        f"--since={_SCAN_SINCE_HORIZON}",
        f"--format={_LOG_FORMAT}",
    ]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            stdin=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return [], []
    if proc.returncode != 0:
        return [], []

    # parts[0] is whatever precedes the first commit's leading NUL (empty on a well-formed
    # run); each subsequent pair is (sha, trailer_block) per the _LOG_FORMAT layout.
    parts = proc.stdout.split("\x00")

    commits: list[tuple[str, list[str]]] = []
    malformed: list[dict] = []
    i = 1
    while i + 1 < len(parts):
        sha = parts[i].strip()
        trailer_block = parts[i + 1]
        if not _SHA_RE.match(sha):
            malformed.append({
                "sha": sha,
                "reason": "git-log record failed 40-char lowercase-hex SHA validation",
            })
            i += 2
            continue
        trailer_values = [line.strip() for line in trailer_block.splitlines() if line.strip()]
        commits.append((sha, trailer_values))
        i += 2

    return commits, malformed


def collect(ctx: EmitContext) -> tuple[list[dict], list[dict]]:
    """Build ``(records, malformed)`` for commit-closure facts (DECISION-1/2/3/4, AC4/AC5).

    One record per ``(sha, item_id)`` pair; ``reachable_on_default_branch`` is left ``None``
    here — stamped later by ``envelope._stamp_closure_reachability`` (AC4).
    """
    commits, malformed = _extract_closure_commits(ctx)

    records: list[dict] = []
    for sha, trailer_values in commits:
        if not trailer_values:
            continue
        # Review: code-reviewer — dedupe within this commit's own trailer values so a
        # commit with a repeated `Closes:` line (copy-paste, cherry-pick artifact) does not
        # emit two bit-identical rows for the same (repo, item_id, sha) triple, which would
        # violate DECISION-4/AC2's declared "one row per distinct triple" identity.
        for item_id in dict.fromkeys(parse_closure_trailers(trailer_values)):
            records.append({
                "repo": ctx.repo_name,
                "coordinator_root_path": ".",
                # "local_fs" (not "git_commit") matches the subprocess-git-derived
                # convention used by branch.py/coordinator_roots.py — not because this
                # record isn't git-derived. The record's own `sha` field carries the commit
                # identity; ref is intentionally left null here.
                "provenance": ctx.provenance("local_fs", path="", derivation="parsed"),
                "item_id": item_id,
                "sha": sha,
                "reachable_on_default_branch": None,
            })

    return records, malformed
