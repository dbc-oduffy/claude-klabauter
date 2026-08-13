"""
coordinator_core.ops.check_import_budget_staleness — staleness predicate for
`coordinator_core/benchmarks/import-budget-manifest.json`.

Purpose: the import-budget manifest records per-entrypoint module-count
measurements that silently stop being true once the code they measure moves.
On 2026-08-13 the manifest's `lazy_channel_note` still asserted a
module-level `psutil` import in `session/core.py` that had been lazy since
~2026-08-08, which misdirected two sessions' engineering decisions; a
separate, real regression from commit `670cf7878` (2026-08-10) sat
undetected in the same manifest for three days because nothing checked
whether the measured import graph had moved since `measured_at`. This module
is the tell that would have caught both: it reports a verdict per
entrypoint, never for the whole file, because the manifest's entrypoints
move independently and only one of them had actually regressed.

Predicate (AND, never OR — see `coordinator_core.ops.doc_staleness`'s module
docstring for the identical rationale applied to a different corpus): an
entrypoint is STALE only when BOTH legs hold —

    1. `measured_at` is more than `STALENESS_THRESHOLD_DAYS` days old, AND
    2. `git log --since=<measured_at>` on that entrypoint's `measured_paths`
       (C1's provenance field) is non-empty — the import graph the
       measurement covers has actually moved since it was taken.

A quiet repo must not nag on the calendar alone (leg 1 without leg 2), and a
busy repo touching unrelated files must not be excused-then-flagged by a
recent commit that never touched the measured graph (leg 2 without leg 1).
Either leg alone is deliberately insufficient.

`STALENESS_THRESHOLD_DAYS = 7`: the 2026-08-10 regression sat undetected for
three days before this session's incidental re-measurement caught it, and
the manifest's own re-baseline cadence in this repo has run roughly weekly
(2026-08-06 -> 2026-08-13). Seven days is the shortest threshold that
outlives one full re-baseline cycle without firing on a manifest that was
simply measured within the last week, while still catching a
multi-day-undetected regression like 2026-08-10 well before it reaches
`STALENESS_THRESHOLD_DAYS` sessions later. `check_arch_audit_staleness.py`'s
own threshold (10 days) is a different cadence for a different artifact
(rotational architecture audits) and is not reused here on that basis alone.

AC3 — `measured_at` is read from the manifest's own JSON, never from the
manifest file's git history: today's session edited the manifest's `note`
fields repeatedly while the underlying measurements were stale, so a
commit-date clock on the file itself would have read FRESH throughout the
exact incident this module exists to catch.

Verdict granularity: per entrypoint (a dict keyed by entrypoint name), not
one verdict for the whole manifest — see module docstring above.

Repo-root resolution: this module resolves the git repo root itself
(`_git_root`, self-contained, same shape as
`check_arch_audit_staleness._git_root`) rather than routing through
`check_weekly_staleness._resolve_state_root` — this check needs the repo
root to locate the manifest and run `git log`, not the coordinator STATE
root (`<repo_root>/state`), which is a different, unrelated directory this
module has no reason to touch. Per this plan's cross-plan coordination
note, `check_weekly_staleness._resolve_state_root` is owned by a live
sibling plan and is not read, imported, or edited here.

Negative-spec:
    - Does NOT re-measure any entrypoint's import graph. Measurement is
      expensive and belongs to the re-baseline runbook; this is a tell that
      measurement is OWED, not a measurement itself. Consequence — this
      predicate CANNOT see a module reached only through a lazy/conditional
      import: `measured_paths` (C1) is derived once, by importing an
      entrypoint in a fresh subprocess and reading `sys.modules`, so any
      module the entrypoint reaches only via a deferred accessor (the same
      `_psutil()`-style lazy-import pattern this repo now uses in places)
      never lands in the provenance list and can never trip leg 2, no
      matter how much it changes. A green FRESH verdict means "the eager,
      already-listed import graph hasn't moved" — it is NOT proof the
      entrypoint's full (eager + lazy) import surface is unchanged. Closing
      this gap would require re-deriving `measured_paths` at check time,
      which the negative-spec above forbids; the mitigation available here
      is re-running the re-baseline runbook whenever an entrypoint's lazy
      imports are suspected to have shifted, not a change to this checker.
    - Does NOT modify the manifest, `git log` output, or any other file.
    - Does NOT run on the commit hot path or join the fast tier — its test
      is marked `cadence` (see
      `coordinator_core/ops/tests/test_check_import_budget_staleness.py`).

Exit codes (`main`): `0` all entrypoints FRESH; `1` (`EXIT_STALE`) at least
one STALE; `2` (`EXIT_UNKNOWN`) no STALE but at least one UNKNOWN (missing
manifest/repo, malformed `measured_at`, empty `measured_paths`, or a failed
`git log` query) — UNKNOWN is a non-zero, failing signal distinct from
STALE, never folded into a silent pass: a manifest entry that never
completed its provenance is checked-nothing, not confirmed-fresh. The
numeric ordering of `EXIT_STALE`/`EXIT_UNKNOWN` does NOT track severity
(STALE is the more definite finding despite the lower number) — callers
must branch on `== EXIT_STALE` / `== EXIT_UNKNOWN` explicitly, never on
exit-code magnitude.

Spec backlink: pln-a-staleness-tell-for-the-impor-531a50 § C2
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from coordinator_core.win_portability import no_console_creationflags

STALENESS_THRESHOLD_DAYS = 7

MANIFEST_RELATIVE_PATH = "coordinator_core/benchmarks/import-budget-manifest.json"


def _git_root(cwd: Optional[str] = None) -> Optional[str]:
    """Resolve a git repo root, or None if not in one.

    Self-contained (same shape as
    `check_arch_audit_staleness._git_root`) — deliberately does NOT route
    through `check_weekly_staleness._resolve_state_root`, which resolves a
    different directory (the coordinator STATE root) owned by a live
    sibling plan (see module docstring).
    """
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            cwd=cwd,
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _git_root: subprocess.run failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return root or None


def _git_log_since(repo_root: Path, since_date: str, paths: list[str]) -> Optional[str]:
    """`git log --since=<since_date> -- <paths...>`.

    Returns the command's stdout (possibly empty — a genuinely quiet repo)
    on success, or None if the git query itself failed (missing binary,
    non-zero exit, e.g. a shallow clone whose history doesn't reach
    `since_date`). None is NOT the same as "" — a caller must not fold a
    failed query into "no movement confirmed" (see `compute_entrypoint_staleness`).
    """
    if not paths:
        # Review: coordinator:code-reviewer — unreachable via the only call
        # site (compute_entrypoint_staleness returns UNKNOWN before this is
        # called); kept as belt-and-suspenders for future callers. "" here
        # means "no paths given", distinct from None (query failure).
        return ""
    try:
        result = subprocess.run(
            ["git", "log", f"--since={since_date}", "--format=%H", "--", *paths],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            **no_console_creationflags(),
        )
    except OSError:
        print(f"skip: _git_log_since: subprocess.run failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _parse_measured_at(value: str) -> Optional[date]:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def compute_entrypoint_staleness(
    repo_root: str | Path,
    entrypoint: str,
    entry: dict[str, Any],
    *,
    today: Optional[date] = None,
) -> dict[str, Any]:
    """Pure-ish predicate (one `git log` shell-out) for a single manifest
    entrypoint entry. Returns a dict with at least a `verdict` key of
    STALE / FRESH / UNKNOWN.

    UNKNOWN: `measured_at` absent or unparseable, or `measured_paths`
    absent — the manifest entry doesn't carry what this predicate needs.
    Never raises on a malformed entry.
    """
    repo_root = Path(repo_root)
    today = today or date.today()

    measured_at_str = entry.get("measured_at")
    measured_at = _parse_measured_at(measured_at_str) if measured_at_str else None
    if measured_at is None:
        return {"entrypoint": entrypoint, "verdict": "UNKNOWN", "reason": "measured_at missing or unparseable"}

    measured_paths = entry.get("measured_paths")
    if not measured_paths or not isinstance(measured_paths, (list, tuple)):
        # Review: coordinator:code-reviewer — a truthiness-only check passes
        # a bare string, which git log then unpacks one pathspec per
        # character (silent false FRESH); require a list/tuple explicitly.
        return {"entrypoint": entrypoint, "verdict": "UNKNOWN", "reason": "measured_paths missing, empty, or not a list"}

    days_since_measured = (today - measured_at).days
    if days_since_measured < 0:
        days_since_measured = 0

    age_stale = days_since_measured > STALENESS_THRESHOLD_DAYS

    log_output = _git_log_since(repo_root, measured_at_str, measured_paths)
    if log_output is None:
        return {
            "entrypoint": entrypoint,
            "verdict": "UNKNOWN",
            "reason": "git log query failed — cannot confirm import graph movement",
            "days_since_measured": days_since_measured,
            "age_stale": age_stale,
            "measured_at": measured_at_str,
        }
    graph_moved = bool(log_output)

    verdict = "STALE" if (age_stale and graph_moved) else "FRESH"

    return {
        "entrypoint": entrypoint,
        "verdict": verdict,
        "days_since_measured": days_since_measured,
        "age_stale": age_stale,
        "graph_moved": graph_moved,
        "measured_at": measured_at_str,
    }


def compute_manifest_staleness(
    repo_root: str | Path,
    manifest: dict[str, Any],
    *,
    today: Optional[date] = None,
) -> dict[str, dict[str, Any]]:
    """Verdict per entrypoint in *manifest* (its `entrypoints` mapping)."""
    entrypoints = manifest.get("entrypoints") or {}
    return {
        name: compute_entrypoint_staleness(repo_root, name, entry, today=today)
        for name, entry in entrypoints.items()
    }


EXIT_STALE = 1
EXIT_UNKNOWN = 2


def main(argv) -> int:
    repo_root = _git_root()
    if not repo_root:
        print("UNKNOWN")
        return EXIT_UNKNOWN

    manifest_path = Path(repo_root) / MANIFEST_RELATIVE_PATH
    if not manifest_path.is_file():
        print("UNKNOWN")
        return EXIT_UNKNOWN

    try:
        with manifest_path.open("r", encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        print(f"UNKNOWN ({exc})")
        return EXIT_UNKNOWN

    results = compute_manifest_staleness(repo_root, manifest)

    any_stale = False
    any_unknown = False
    for name, result in results.items():
        print(f"{result['verdict']} {name}")
        if result["verdict"] == "STALE":
            any_stale = True
        elif result["verdict"] == "UNKNOWN":
            any_unknown = True

    if any_stale:
        return EXIT_STALE
    if any_unknown:
        return EXIT_UNKNOWN
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
