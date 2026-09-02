"""coordinator_core.frontmatter.falsifier_rerun_report -- read-only roster of
terminal, falsifier-carrying plans and whether their falsifier was re-run.

Spec backlink: docs/plans/2026-09-02-the-falsifier-nobody-re-ran.md, chunk C1.

What this answers, for each plan under `docs/plans/*.md` whose `status` is
terminal AND whose `prime_exit_criterion` carries a `falsifier`: does the plan
also carry a record of that falsifier being re-observed after the work?

Ground truth this module measured, correcting the plan's own first-draft
Problem framing (reported, not silently reconciled -- the plan body is not
this module's to edit): `prime_exit_criterion.falsifier` is
`additionalProperties: false` and genuinely cannot hold a re-run field --
that part holds. But the sibling top-level `exit_criterion_met` object
(plan.schema.json 2.9.0+) already carries `falsifier_output`/
`falsifier_verdict`, which IS the re-run half of the same instrument (same
`how`, re-run against HEAD, judged against `expected_when_true`), and it is
POPULATED on this corpus for every live terminal+falsifier plan measured so
far.

That does not make the underlying problem moot -- it sharpens it. Every
`additionalProperties: false` object under a plan's close-out area
(`prime_exit_criterion.falsifier`, `prime_exit_criterion.falsifier_exemption`,
`exit_criterion_met`, `gated_exit_criteria[]`) rejects any key the schema did
not anticipate, and this corpus shows repeated, independently-invented
attempts to smuggle close-out evidence past that rejection under a dozen-plus
distinct hand-invented spellings (`close_out_evidence`, `met_evidence`,
`observed_sha`, `caveat`, `proven_on`, ... -- one plan's rejected keys are
entire prose sentences standing in for a key name). This module derives that
roster from `validate_frontmatter`'s own additionalProperties errors --
never a hand-maintained list, which would silently miss the next invented
spelling, and that drift is exactly the shape this report exists to catch.
FRAMING: this is authors repeatedly trying to record evidence the schema has
nowhere to put, not authors declining to. Do not word findings as
non-compliance.

This module makes no claim about whether a recorded re-run is FRESH or
honest (a stamp can quote a stale baseline_ref, as this session's own
motivating incident did) -- that is a correctness judgment, not a presence
check, and is explicitly out of scope (see the plan's Anti-scope bullets 1
and 5: no platform-mismatch detector, no reshaping into the retired
acceptance oracle). Presence is the entire question this module answers.

Negative-spec: no subprocess, no `git`, no network. One `open()`+`read()`
per plan file. No second YAML/schema-validation path -- frontmatter
extraction is `schema_validate.parse_frontmatter` and additionalProperties
detection is `schema_validate.validate_frontmatter` against the vendored
`plan.schema.json`, the same two read paths every other frontmatter-
consuming report in this package uses. Do not touch `plan.schema.json`
itself (vendored, byte-pinned; see the plan's Anti-scope bullet 2).
"""

from __future__ import annotations

import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from coordinator_core.frontmatter.schema_validate import (
    SchemaVersionError,
    parse_frontmatter,
    validate_frontmatter,
)

# coordinator_core/frontmatter/falsifier_rerun_report.py -> parents[2] is the
# repo root (coordinator_core/frontmatter/ -> coordinator_core/ -> repo root),
# same convention as engine_provenance_counter._OWN_REPO_ROOT and the
# Path(__file__).resolve().parents[N] sites in coordinator_core/test_*.py --
# no `git rev-parse --show-toplevel` subprocess for a pure file-read report.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PLANS_DIR = _REPO_ROOT / "docs" / "plans"
_PLAN_SCHEMA_PATH = Path(__file__).parent / "schemas" / "plan.schema.json"

# Terminal for THIS question only ("has the certifying stamp landed"),
# deliberately its own partition -- coordinator_core.lifecycle_constants
# already documents at least four independent, deliberately-disagreeing
# plan-status partitions (PLAN_ARCHIVABLE_STATUS / PLAN_TERMINAL_STATUS /
# ops.plan_status_transition._FROZEN_STATUSES / PLAN_ORPHAN_TERMINAL_STATUS)
# and none of them answers this one. `landed` is excluded on the same basis
# PLAN_ORPHAN_TERMINAL_STATUS excludes it: plan.schema.json's own `status`
# enum documents `landed` as explicitly NOT terminal ("not every spine row
# has reached a disposition yet"), and this report's own measured
# population (19 of 40) only holds under that reading -- a set including
# `landed` yields 25. `complete`/`shipped` never appear in this schema's
# `status` enum at all (measured on this corpus); named defensively only,
# same tolerance as PLAN_ORPHAN_TERMINAL_STATUS's docstring.
TERMINAL_STATUSES: frozenset[str] = frozenset({"implemented", "complete", "shipped"})

# Where a close-out evidence attempt lands in the frontmatter tree -- scopes
# validate_frontmatter's additionalProperties errors to the close-out area,
# excluding unrelated additionalProperties violations elsewhere in the
# document (a plan can be malformed in ways that have nothing to do with
# recording a falsifier re-run).
_CLOSE_OUT_FIELD_PREFIXES: tuple[str, ...] = (
    "prime_exit_criterion.falsifier",  # covers both falsifier and falsifier_exemption
    "exit_criterion_met",
    "gated_exit_criteria[",
)

_ADHOC_KEY_RE = re.compile(r'additional property "(.+)" not allowed')


@dataclass(frozen=True)
class PlanFalsifierRecord:
    """One falsifier-carrying plan's re-run status."""

    repo_rel_path: str
    status: str
    rerun_recorded: bool
    falsifier_verdict: str | None


@dataclass(frozen=True)
class CloseOutKeyAttempt:
    """One schema-rejected close-out-evidence key, as measured, grouped by
    the exact spelling invented -- never a hand-maintained enumeration."""

    key: str
    count: int
    repo_rel_paths: tuple[str, ...]


@dataclass(frozen=True)
class FalsifierRerunReport:
    """The whole-corpus roster this module exists to produce."""

    scanned_files: int
    with_falsifier: int
    terminal_with_falsifier: tuple[PlanFalsifierRecord, ...]
    close_out_key_attempts: tuple[CloseOutKeyAttempt, ...]
    process_time_seconds: float

    @property
    def missing_rerun(self) -> tuple[PlanFalsifierRecord, ...]:
        """Terminal, falsifier-carrying plans with NO re-run recorded."""
        return tuple(r for r in self.terminal_with_falsifier if not r.rerun_recorded)

    @property
    def with_rerun(self) -> tuple[PlanFalsifierRecord, ...]:
        """Terminal, falsifier-carrying plans WITH a re-run recorded."""
        return tuple(r for r in self.terminal_with_falsifier if r.rerun_recorded)

    def render(self) -> str:
        """One-fact-once plain-text rendering. Never a green count, never a
        blanket "all N are broken" -- states what was measured, split by
        whether a re-run is recorded, and names schema-rejected close-out
        keys as authors repeatedly trying to record evidence the schema has
        nowhere to put -- never as non-compliance.
        """
        lines: list[str] = []
        lines.append(
            f"falsifier-rerun report: {self.scanned_files} plans scanned, "
            f"{self.with_falsifier} carry a falsifier, "
            f"{len(self.terminal_with_falsifier)} of those are terminal "
            f"({sorted(TERMINAL_STATUSES)})."
        )
        missing = self.missing_rerun
        present = self.with_rerun
        lines.append(
            f"{len(present)} of {len(self.terminal_with_falsifier)} terminal "
            f"falsifier-carrying plans have a re-run recorded in "
            f"exit_criterion_met.falsifier_output; {len(missing)} do not."
        )
        if missing:
            lines.append("No re-run recorded:")
            for r in missing:
                lines.append(f"  - {r.repo_rel_path} (status={r.status})")
        if present:
            lines.append("Re-run recorded:")
            for r in present:
                verdict = r.falsifier_verdict or "(no verdict field)"
                lines.append(f"  - {r.repo_rel_path} (status={r.status}, verdict={verdict})")

        attempts = self.close_out_key_attempts
        if attempts:
            lines.append(
                "Close-out evidence the schema has nowhere to put -- "
                f"{len(attempts)} distinct invented spellings, rejected by "
                "additionalProperties: false (authors repeatedly trying to "
                "record this, not declining to):"
            )
            for a in attempts:
                lines.append(f"  - {a.key!r} x{a.count}: {list(a.repo_rel_paths)}")
        lines.append(f"process_time_seconds={self.process_time_seconds:.4f}")
        return "\n".join(lines)


def _falsifier_dict(frontmatter: dict[str, Any]) -> dict[str, Any] | None:
    pec = frontmatter.get("prime_exit_criterion")
    if not isinstance(pec, dict):
        return None
    falsifier = pec.get("falsifier")
    if not isinstance(falsifier, dict):
        return None
    return falsifier


def _rerun_recorded(frontmatter: dict[str, Any]) -> tuple[bool, str | None]:
    ecm = frontmatter.get("exit_criterion_met")
    if not isinstance(ecm, dict):
        return False, None
    output = ecm.get("falsifier_output")
    verdict = ecm.get("falsifier_verdict")
    recorded = isinstance(output, str) and output.strip() != ""
    return recorded, verdict if isinstance(verdict, str) else None


def _close_out_adhoc_keys(frontmatter: dict[str, Any]) -> list[str]:
    """Derive schema-rejected close-out-evidence key names from
    `validate_frontmatter`'s own additionalProperties errors -- never a
    hand-maintained list, which drifts the moment a new spelling appears.
    Broad exception handling by design: a malformed/version-incompatible
    record should not crash a read-only corpus scan; it is simply excluded
    from this measurement (present in `scanned_files`, absent from findings).
    """
    try:
        errors = validate_frontmatter(frontmatter, _PLAN_SCHEMA_PATH)
    except SchemaVersionError:
        return []
    except Exception:
        return []

    keys: list[str] = []
    for err in errors:
        field_path = err.get("field", "")
        if not any(field_path.startswith(p) for p in _CLOSE_OUT_FIELD_PREFIXES):
            continue
        match = _ADHOC_KEY_RE.search(err.get("error", ""))
        if match:
            keys.append(match.group(1))
    return keys


def scan_plans(plans_dir: Path | str = _DEFAULT_PLANS_DIR) -> FalsifierRerunReport:
    """Scan `plans_dir` for `*.md` files and build the report.

    Pure file reads: one `open()`+`read()` per plan, no subprocess, no git,
    no network. Process time (not wall clock) is measured with
    `time.process_time()` over the whole scan.
    """
    plans_dir = Path(plans_dir)
    start = time.process_time()

    scanned = 0
    with_falsifier = 0
    terminal_records: list[PlanFalsifierRecord] = []
    key_to_files: dict[str, list[str]] = defaultdict(list)

    for path in sorted(plans_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        scanned += 1

        parsed = parse_frontmatter(text)
        frontmatter = parsed.get("frontmatter")
        if not isinstance(frontmatter, dict):
            continue

        try:
            repo_rel = str(path.relative_to(_REPO_ROOT))
        except ValueError:
            repo_rel = str(path)

        for key in _close_out_adhoc_keys(frontmatter):
            key_to_files[key].append(repo_rel)

        falsifier = _falsifier_dict(frontmatter)
        if falsifier is None:
            continue
        with_falsifier += 1

        status = frontmatter.get("status")
        status = status if isinstance(status, str) else ""
        if status not in TERMINAL_STATUSES:
            continue

        rerun_recorded, verdict = _rerun_recorded(frontmatter)
        terminal_records.append(
            PlanFalsifierRecord(
                repo_rel_path=repo_rel,
                status=status,
                rerun_recorded=rerun_recorded,
                falsifier_verdict=verdict,
            )
        )

    attempts = tuple(
        CloseOutKeyAttempt(key=key, count=len(files), repo_rel_paths=tuple(files))
        for key, files in sorted(key_to_files.items())
    )

    elapsed = time.process_time() - start
    return FalsifierRerunReport(
        scanned_files=scanned,
        with_falsifier=with_falsifier,
        terminal_with_falsifier=tuple(terminal_records),
        close_out_key_attempts=attempts,
        process_time_seconds=elapsed,
    )


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint -- `python -m coordinator_core.frontmatter.falsifier_rerun_report`."""
    report = scan_plans()
    print(report.render())
    return 0


if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
