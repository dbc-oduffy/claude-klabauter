"""
coordinator_core.ops.workweek_trail_scope — direct-import trampoline shape,
template-variant #1 (DOE-PORT BIG_PORT wave).

Purpose: Step 7 prelude for /workweek-complete. Reads the workstream-complete
review-trail records for the current week and computes the narrowed scope for
The Staff Engineer reviewer:

    staff_eng_scope = unreviewed_week_SHAs ∪ cross-segment-seam SHAs

A "segment" is the sha_range of one trail record (one workstream-complete
review). Cross-segment seams are file paths touched by >=2 distinct segments —
computed by intersecting the files-touched sets pairwise.

Output: writes a session-keyed shard
  state/review-trail/.weekly-reviewer-scopes-<TIMESTAMP>-<SID_SHORT>.json
with shape:
  { "staff_eng": [sha...], "staff_eng_seam_files": [path...], "mechanical_workers": "full" }

Wire-vocabulary note (2026-08-04): the shard keys were renamed from their
prior persona-named forms to the current role-based `staff_eng`/
`staff_eng_seam_files` per the PM ruling that a persona slug must never be a
wire key/value/enum member (state/sizings/2026-08-04-persona-wire-vocabulary-
rename.yaml; docs/decisions/DR-262 Amendment cl.3). Coordinated with example-doctrine-repo
via cross-repo memo (cross-repo/inbox/2026-08-04-example-doctrine-repo-em-correction-the-
shard-key-coupling-is-three-keys-five-files.md) — example-doctrine-repo's five reader sites move
in the same window, no back-compat/dual-spelling transition.

Session-keyed append-only (not a singleton overwrite): two concurrent
/workweek-complete weekly gates each write their own shard rather than
clobbering a shared filename. Consumers select the most-recent shard for
their own session (falling back to the newest shard overall) — see
coordinator/commands/workweek-complete.md § Step 7 read logic.

MUST be invoked as a subprocess op, never imported as a long-lived service —
every call re-derives WEEK_START/TODAY/TRAIL_FILES from repo state under cwd
(no caching, no daemon mode).

Delegation, not reimplementation: SAFE_RANGE argument-injection validation,
JSON-OR-JSONL dual-shape trail-record parsing, scope_kind classification, and
the verdict filter (pending excluded) all live in
coordinator_core.ops.review_coverage_core (shared with the chain-end coverage
gate's coordinator_core.coverage) — this port does NOT reimplement that
logic; it calls collect_segments() directly (same-process, in-package import;
equivalent to `--segments-json --on-unresolvable-ref skip`). Originally this
shelled out to the still-bash example-doctrine-repo-owned `coordinator/lib/
review-coverage-core.sh` oracle via subprocess (that file is now itself a
polyglot trampoline over this same coordinator_core.ops.review_coverage_core
module) — once BOTH sides of that call lived in claude-klabauter, the subprocess hop
was a needless extra process per /workweek-complete Step 7 invocation, so
this port finishes the strangler: same-process call, zero subprocess, zero
cross-repo dependency for this piece. Seam detection (pairwise file-set
intersection across segments) IS native Python here — it was already a
Python heredoc block in the bash oracle, lifted essentially unchanged.

Session-id resolution delegates to coordinator_core.session.core.resolve_session_id
(C19 — retired the bash-spawn bridge that shelled out to source the example-doctrine-repo-owned
`coordinator/lib/coordinator-session.sh` and call its `cs_resolve_session_id`).
That module is itself a full native port of the same 4-tier chain, including
the tier-4 concurrency-ambiguity guard over `coordinator_core.session.liveness`
— same-process, zero subprocess, zero cross-repo dependency for this piece too.

Timestamp generation is REIMPLEMENTED natively (not shelled to `date`):
Python's `datetime.now()` carries microsecond precision on every platform, so
the bash oracle's `date +...%N`-probe-with-PID-suffix-fallback (needed only
because BSD/macOS `date` lacks nanosecond support) has no analogous gap here —
this is a legitimate native-equivalent simplification, not a scope-drop: the
consumer-visible contract (a collision-resistant, filename-safe, roughly
time-sortable string; "most-recent-shard-wins" read logic) is preserved.

Exit codes:
  0 - success, scope shard written.
  1 - business failure: HEADER_FILE missing/unparseable,
      review_coverage_core.collect_segments reported a business error
      (record-parse or git-ref-resolution failure; message printed verbatim —
      see negative-spec below), git-log failure, session-id unresolvable, or
      the scope-shard JSON write failed.

  (No distinct transport-failure exit code: collect_segments() and
  session-id resolution (tier 4 included) are now same-process, same-package
  calls — there is no subprocess/cross-repo hop left to fail independently of
  a business outcome. An unresolvable session id is itself a business-fail
  condition for this op regardless of cause.)

Negative-spec (faithfully reproduced from the bash oracle — do NOT "fix" mid-port):
  - No CLI-argument validation of any kind beyond --help: like the oracle,
    unrecognized argv is silently ignored (this op has never accepted flags).
  - collect_segments()'s own fatal-error message is propagated as EXIT_BUSINESS_FAIL,
    mirroring the bash oracle's `set -e` propagation of the coverage-core
    subprocess's exact exit status (now a same-process exception instead).
  - Nanosecond-precision truncation cosmetic quirk (bash oracle's 23-char
    slice of a 27-char `%N`-stamped string) is NOT reproduced — see timestamp
    note above; this is the one deliberate divergence and it does not change
    any consumer-visible contract.

Port of: workweek-trail-scope.sh (example-doctrine-repo 6fb5fb37, 2026-07-22).
Spec backlink: docs/plans/2026-06-23-chain-end-review-coverage-gate.md § C2
             + docs/plans/2026-07-16-bash-clean-slate-residual-migration.md
Central-reg: this op is a PLAIN MODULE (no @register_op) — direct-import
trampoline variant (template-variant #1; see example-doctrine-repo
tasks/2026-07-16-clean-slate-recon/r1-doe-port-template.md § 1, exemplified by
coordinator_core.hooks.auto_push / coordinator_core.ops.handoff_gate_aging).
NOT wired into ops/__init__.py / _registry_map.py / ipc.py /
authz/classification.py — no registration action needed.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Sequence, Set

from coordinator_core.session.declared_writes import declare_write
from coordinator_core.ops.review_coverage_core import (
    _FatalError as _CoverageFatalError,
    collect_segments,
)

_GIT_LOG_TIMEOUT_SECS = 30

EXIT_OK = 0
EXIT_BUSINESS_FAIL = 1

_CREATIONFLAGS = getattr(subprocess, "CREATE_NO_WINDOW", 0)

_WEEK_START_RE = re.compile(r"^\*\*Week starting:\*\*\s+(\d{4}-\d{2}-\d{2})")

_USAGE = """Usage: workweek-trail-scope [--help]

Step 7 prelude for /workweek-complete — computes staff_eng_scope (unreviewed
week SHAs union cross-segment-seam SHAs) and writes a session-keyed shard to
state/review-trail/.weekly-reviewer-scopes-<TIMESTAMP>-<SID_SHORT>.json.

Must be run with cwd = repo root (all paths are cwd-relative, matching the
bash oracle).

Env:
  HEADER_FILE — path to state/week-changelog/HEADER.md (default: that path)

Exit codes:
  0 - success
  1 - business failure (missing/unparseable HEADER_FILE, review-coverage-core
      business error propagated verbatim, git-log failure, unresolvable
      session id, scope-shard write failure)
"""


def _resolve_session_id() -> str:
    """Canonical session-id resolution: delegates to
    coordinator_core.session.core.resolve_session_id, the native in-package
    port of the bash oracle's ``cs_resolve_session_id`` 4-tier chain
    (COORDINATOR_SESSION_ID -> CLAUDE_SESSION_ID -> CLAUDE_CODE_SESSION_ID ->
    tier-4 concurrency-ambiguity-guarded sentinel fallback). C19 — retired the
    `bash -c 'source coordinator-session.sh && cs_resolve_session_id'` bridge;
    same-process call, zero subprocess."""
    from coordinator_core.session.core import resolve_session_id

    return resolve_session_id()


def _today_local() -> str:
    """Local calendar day, YYYY-MM-DD — Python-native equivalent of the bash
    oracle's coordinator_local_day() (`date -I` / `date +%Y-%m-%d` fallback;
    both just render the local calendar day)."""
    return datetime.now().strftime("%Y-%m-%d")


def _parse_week_start(header_file: Path) -> Optional[str]:
    try:
        with header_file.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = _WEEK_START_RE.match(line)
                if m:
                    return m.group(1)
    except OSError:
        print(f"skip: _parse_week_start: with header_file.open(\"r\", encoding=\"utf-8\", errors=\"replace\") as fh: failed: {sys.exc_info()[1]}", file=sys.stderr)
        return None
    return None


def _trail_files() -> List[str]:
    d = Path("state/review-trail")
    if not d.is_dir():
        return []
    return sorted(str(p) for p in d.glob("*.json") if p.is_file())


def main(argv: Optional[Sequence[str]] = None) -> int:
    argv = list(argv) if argv is not None else sys.argv[1:]
    if "--help" in argv or "-h" in argv:
        sys.stdout.write(_USAGE)
        return EXIT_OK

    header_file = Path(os.environ.get("HEADER_FILE", "state/week-changelog/HEADER.md"))
    if not header_file.is_file():
        print(
            f"ERROR: {header_file} not found — run /workweek-start to initialise.",
            file=sys.stderr,
        )
        return EXIT_BUSINESS_FAIL

    week_start = _parse_week_start(header_file)
    if not week_start:
        print(
            f"ERROR: cannot parse 'Week starting:' YYYY-MM-DD from {header_file}",
            file=sys.stderr,
        )
        return EXIT_BUSINESS_FAIL

    today = _today_local()
    trail_files = _trail_files()

    try:
        segments = collect_segments(
            trail_files,
            week_start=week_start,
            today=today,
            on_record_error="fail",
            on_unresolvable_ref="skip",
        )
    except _CoverageFatalError as exc:
        # Verbatim-equivalent of the old bash-oracle exit-1 propagation: a
        # fail-mode record-parse or git-ref-resolution failure already printed
        # its own ERROR line to stderr (review_coverage_core.build_segments /
        # _load_records) — surface as business failure, not transport failure,
        # since this is now a same-process call with nothing to transport.
        print(f"ERROR: coordinator_core.ops.review_coverage_core failed: {exc}", file=sys.stderr)
        return EXIT_BUSINESS_FAIL

    segment_shas: List[Set[str]] = [set(seg.get("shas", [])) for seg in segments]
    segment_files: List[Set[str]] = [set(seg.get("files", [])) for seg in segments]
    reviewed_set: Set[str] = set().union(*segment_shas) if segment_shas else set()

    try:
        git_result = subprocess.run(
            ["git", "log", "origin/main..HEAD", "--format=%H"],
            capture_output=True,
            text=True,
            timeout=_GIT_LOG_TIMEOUT_SECS,
            stdin=subprocess.DEVNULL,
            check=False,
            creationflags=_CREATIONFLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(
            f"ERROR: command failed: git log origin/main..HEAD --format=%H\n{exc}",
            file=sys.stderr,
        )
        return EXIT_BUSINESS_FAIL
    if git_result.returncode != 0:
        print(
            "ERROR: command failed: git log origin/main..HEAD --format=%H\n"
            f"{git_result.stderr}",
            file=sys.stderr,
        )
        return EXIT_BUSINESS_FAIL

    weekly_raw = git_result.stdout.strip()
    weekly_diff_shas: Set[str] = set(weekly_raw.splitlines()) if weekly_raw else set()
    unreviewed_set = weekly_diff_shas - reviewed_set

    cross_segment_seams: Set[str] = set()
    for i in range(len(segment_files)):
        for j in range(i + 1, len(segment_files)):
            cross_segment_seams |= segment_files[i] & segment_files[j]

    seam_shas: Set[str] = set()
    for k, fset in enumerate(segment_files):
        if fset & cross_segment_seams:
            seam_shas |= segment_shas[k]

    staff_eng_shas = sorted(unreviewed_set | seam_shas)
    seam_files = sorted(cross_segment_seams)

    sid = _resolve_session_id()
    if not sid:
        print(
            "ERROR: Could not resolve session-id for scope shard write. Attempted:",
            file=sys.stderr,
        )
        print("  1. CLAUDE_SESSION_ID env var — not set or empty", file=sys.stderr)
        print("  2. CLAUDE_CODE_SESSION_ID env var — not set or empty", file=sys.stderr)
        print(
            "  3. cs_resolve_session_id sentinel fallback — not found, empty, "
            "or ambiguous under concurrency",
            file=sys.stderr,
        )
        print(
            "  Fix: export CLAUDE_SESSION_ID=<harness-id>   "
            "(run from inside the Claude Code session)",
            file=sys.stderr,
        )
        return EXIT_BUSINESS_FAIL
    session_short = sid[:8]

    # Native microsecond-precision timestamp — see module docstring's timestamp note.
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y-%m-%d-%H%M%S-%f")

    scope_path = Path(
        f"state/review-trail/.weekly-reviewer-scopes-{timestamp}-{session_short}.json"
    )
    scope_obj = {
        "staff_eng": staff_eng_shas,
        "staff_eng_seam_files": seam_files,
        "mechanical_workers": "full",
    }
    try:
        with scope_path.open("w", encoding="utf-8") as fh:
            json.dump(scope_obj, fh, indent=2)
        declare_write(scope_path)
    except OSError as exc:
        print(f"ERROR: could not write {scope_path}: {exc}", file=sys.stderr)
        return EXIT_BUSINESS_FAIL

    print(
        f"Scope written: {len(staff_eng_shas)} staff_eng SHA(s), "
        f"{len(seam_files)} seam file(s) → {scope_path}"
    )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
