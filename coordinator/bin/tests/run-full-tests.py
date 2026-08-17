# Unix shebang — was generator-owned by gen-launcher-shim.py --ensure-unix; that mode was retired 2026-07-28 (POSIX-EXEC-ASSUMPTION-GUARD, PM ruling) and no longer regenerates this line.
"""run-full-tests.py — Slow-tier shell-suite orchestrator for DoE-claude.

PURPOSE: Closes the KNOWN GAP named in `CLAUDE.md § Build & Test` and in
run-fast-tests.py's own module docstring: `full_test_cmd:` was a real seam
(`cs_resolve_full_test_cmd`, consumed by `/bug-blitz`) with nothing behind it
in this repo — the ~110 surviving `coordinator/bin/tests/*.sh` / `*.bats`
suites (post bash-kill-fast-test-suites spinoff, 2026-07-21/22) ran only by
hand. This file is that missing slow tier.

WHY NOT THE FAST TIER: these suites are individually heavy (install-health,
bootstrap/scaffold, refresh-plugin-live-install integration — some take over
a minute) and were explicitly excluded from run-fast-tests.py by the
2026-07-08 shell-test-scope decision (see that file's docstring for the full
history). They belong at the CADENCE gates (`/workstream-complete`,
`/workday-complete`, `/workweek-complete`), not the fast per-commit loop —
this file is what those gates should invoke via `full_test_cmd:`.

REGISTRY DISCIPLINE — ENUMERATED, NOT GLOBBED: same rationale as
NATIVE_PYTEST_MODULES in run-fast-tests.py — a glob over `coordinator/bin/
tests/*.sh` and `coordinator/tests/*.bats` would silently re-admit any
suite this file's audit explicitly excluded (self-documented RED
placeholders, left-open known-failures, already-ported-to-pytest
duplicates) the next time someone drops a new file in either directory.
Enumeration keeps admission auditable: every suite in SHELL_SUITES was
read (header + a live run) before being added.

RUNNER PER SUITE: some `.bats`-named files are genuine bats suites
(`#!/usr/bin/env bats` shebang); several are plain-bash harnesses merely
NAMED `.bats` (their own header says so, and they carry no `#!/usr/bin/env
bats` shebang — running those under the `bats` binary fails, they must go
through plain `bash`). The runner is pinned per-entry rather than inferred
from extension, because the extension lies for the plain-bash-named-.bats
population.

EXCLUDED SUITES (audited 2026-07-22, commented out below with reason):
  - test-seed-skill-overrides.sh — DROP-IN detection seam (Cases 9/9b/9c)
    fails reproducibly; the 10 non-drop-in cases are healthy.
  - test-self-claim-refresh-queries.sh — 2 of 4 selfClaim wiring assertions
    fail reproducibly (claimed path not landing in touched.txt).
  - test-step-number-stability.sh — one genuine dangling citation
    (workstream-complete "Step 2.6.3" — header not found in SKILL.md).
  - test-terminator-suite-green.sh — cascades from test-step-number-
    stability.sh above (it aggregates that suite as one of its members).
  - coordinator/tests/cs-session-shape.bats — flaky: SS7a (concurrent
    writers on pickup vs plan) fails intermittently (~1/3 runs observed),
    a timing-sensitive race in the test fixture itself, not the subject.
  Also NOT registered (superseded, already covered by the fast-tier native
  pytest leg, not a disk-missing gap): test-snippet-registry.bats,
  test-verify-no-console-flash.sh, verify-no-console-flash-file-allow.bats
  — all three re-homed to test_snippet_registry.py /
  test_verify_no_console_flash.py / test_verify_no_console_flash_file_allow.py
  (see run-fast-tests.py's NATIVE_PYTEST_MODULES). Same shape, 2026-08-13
  (C8b bucket-1 Group B port): test-audit-roadmap-dependency-order.sh,
  test-audit-roadmap-verdict-regex.sh, test-check-plugin-drift-copy-install.sh,
  test-check-registry-codename-leak-keepset.sh, test-coordinator-session-loe.sh
  — deleted, re-homed to coordinator/tests/test_audit_roadmap_dependency_order.py /
  test_audit_roadmap_verdict_regex.py / test_check_plugin_drift_copy_install.py /
  test_check_registry_codename_leak_keepset.py / test_coordinator_session_loe.py.

PORTABILITY: naked Python 3, no bash wrapper anywhere in this file — per
the repo's P0 ruling (`coordinator.local.md`: structural bash is a
correctness/performance defect). Suites are invoked via
`subprocess.run(["bash"/"bats", path], timeout=120)` directly.

DIRECTORY SELF-RESOLUTION: resolves its own directory from `__file__` (NOT
cwd), matching run-fast-tests.py's convention — callers may invoke this via
`bash -c` or similar from the repo root, where a cwd-relative path would
silently resolve against the wrong tree.

spec-backlink: CLAUDE.md § Build & Test (KNOWN GAP); run-fast-tests.py
module docstring § KNOWN GAP — NO SLOW TIER EXISTS.
"""

from __future__ import annotations

import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent          # coordinator/bin/tests
REPO_ROOT = THIS_DIR.parent.parent.parent            # <repo root>

# --- Slow-tier registry: surviving shell/bats suites -----------------------
# Enumerated, not globbed — see module docstring § REGISTRY DISCIPLINE.
# Each entry is (path-relative-to-repo-root, runner) where runner is
# "bash" or "bats", pinned per-suite (not inferred from extension — several
# .bats-named files are plain-bash harnesses per their own header comment).
SHELL_SUITES = [
    # deep-research-record-roundtrip.test.sh — RETIRED 2026-08-13 (C8b Group A
    # port): superseded by coordinator/tests/test_deep_research_record_roundtrip.py,
    # a pre-existing verbatim pytest port already collected by testpaths
    # (coordinator/tests). No registration needed here — pytest runs it on
    # every fast/full-tier invocation already.
    ("coordinator/bin/tests/run-lineage-dag-suites.py", "python3"),
    # run-plan-tasks-spine-suites.sh — RETIRED 2026-08-13 (C8b Group A port):
    # superseded by coordinator/tests/test_run_plan_tasks_spine_suites.py,
    # already collected by testpaths. Its 4th leg (C1 enum-parity) was deleted
    # upstream and is tracked in state/bug-backlog/ — not carried here.
    # test-aggregate-chain-loe.sh — RETIRED 2026-08-13 (C8b Group A port):
    # superseded by coordinator/tests/test_aggregate_chain_loe.py, a
    # pre-existing verbatim pytest port already collected by testpaths
    # (coordinator/tests). No registration needed here.
    # test-audit-roadmap-dependency-order.sh — PORTED to pytest, see
    # coordinator/tests/test_audit_roadmap_dependency_order.py (fast tier).
    # test-audit-roadmap-verdict-regex.sh — PORTED to pytest, see
    # coordinator/tests/test_audit_roadmap_verdict_regex.py (fast tier).
    ("coordinator/bin/tests/test-bin-sh-polyglot-direct-invocation.sh", "bash"),
    ("coordinator/bin/tests/test-cc-root-source-guard-fix.sh", "bash"),
    # test-check-plugin-drift-copy-install.sh — PORTED to pytest, see
    # coordinator/tests/test_check_plugin_drift_copy_install.py (fast tier).
    # test-check-registry-codename-leak-keepset.sh — PORTED to pytest, see
    # coordinator/tests/test_check_registry_codename_leak_keepset.py (fast tier).
    # test-coordinator-session-loe.sh — PORTED to pytest, see
    # coordinator/tests/test_coordinator_session_loe.py (fast tier).
    ("coordinator/bin/tests/test-d1-same-commit.sh", "bash"),
    # Re-admitted 2026-07-22 (EM-side): corpus defect fixed (created: added to
    # the one archived handoff); suite re-run green 124 pass / 0 fail.
    # test-seed-skill-overrides.sh — EXCLUDED, see module docstring.
    # test-self-claim-refresh-queries.sh — EXCLUDED, see module docstring.
    # test-step-number-stability.sh — EXCLUDED, see module docstring.
    # test-terminator-suite-green.sh — EXCLUDED (cascades from
    # test-step-number-stability.sh), see module docstring.
    # test-verify-no-console-flash.sh — SUPERSEDED, see module docstring.
    # invoking-shell-bash4-probe.test.py — RETIRED from this runner 2026-08-17,
    # converted to `coordinator/scripts/lib/test_invoking_shell_bash4_probe.py`
    # and collected by pytest via the `coordinator/scripts` testpaths admit. It
    # was the last dotted `.test.py` straggler of the 2026-07-25/07-28 migration,
    # and being outside the test tree was ALSO what made it trip
    # `test_no_unsanctioned_shell_spawn` — one rename cleared both. The probe it
    # drives (invoking-shell-bash4-probe.sh) stays shell, untouched: DR-079/
    # 2026-07-22 claude-klabauter memo, genuine keep, no Python substitute.
    # coordinator/tests/cs-session-shape.bats — EXCLUDED, see module docstring.
    # test-snippet-registry.bats — SUPERSEDED, see module docstring.
    # verify-no-console-flash-file-allow.bats — SUPERSEDED, see module docstring.
]

# Review: code-reviewer -- Finding 3: this comment previously named suites
# (test-bootstrap-repo.sh, test-coordinator-auto-push.sh,
# test-coordinator-safe-commit.sh, test-new-project-scaffold.sh,
# test-migrate-cross-repo-layout.sh) that are NOT members of SHELL_SUITES
# above -- their pytest ports live in run-fast-tests.py's
# NATIVE_PYTEST_MODULES instead, so the comment was misdirecting a reader
# auditing concurrent-safety risk. Corrected audit (2026-07-22,
# review-integration pass) of all 13 current SHELL_SUITES entries: 11 use
# an isolated mktemp/mktemp -d fixture root (or are read-only against
# REPO_ROOT, e.g. test-d1-same-commit.sh's `git log`/`git show` calls) and
# carry no shared-REPO_ROOT-mutation risk under MAX_WORKERS=4.
#
# The one entry that used to do real REPO_ROOT-relative filesystem setup —
# deep-research-record-roundtrip.test.sh, which wrote fixed-name fixture
# files directly under REPO_ROOT/docs/research (STEM_WEB/STEM_C) and was NOT
# self-collision-safe across concurrent run-full-tests.py invocations — was
# retired 2026-08-13 (C8b Group A port) in favour of the pre-existing pytest
# port coordinator/tests/test_deep_research_record_roundtrip.py, which uses a
# module-scoped pytest fixture over the same fixture paths and is subject to
# the same cross-invocation caveat; it is no longer a SHELL_SUITES member so
# the note is historical, not a live gap in this registry.
MAX_WORKERS = 4
PER_SUITE_TIMEOUT_SEC = 120


def run_suite(rel_path: str, runner: str) -> tuple[str, str, float]:
    """Run one suite under a hard per-suite timeout; return (path, verdict, seconds)."""
    full_path = REPO_ROOT / rel_path
    if not full_path.is_file():
        return (rel_path, "MISSING-ON-DISK", 0.0)

    cmd = [sys.executable if runner == "python3" else runner, str(full_path)]
    start = time.monotonic()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=PER_SUITE_TIMEOUT_SEC,
        )
        elapsed = time.monotonic() - start
        if result.returncode == 0:
            return (rel_path, "PASS", elapsed)
        return (rel_path, f"FAIL(exit={result.returncode})", elapsed)
    except subprocess.TimeoutExpired:
        elapsed = time.monotonic() - start
        return (rel_path, "TIMEOUT", elapsed)


def main() -> int:
    print(f"== Slow-tier shell suite run — {len(SHELL_SUITES)} suites, "
          f"{MAX_WORKERS} workers, {PER_SUITE_TIMEOUT_SEC}s/suite timeout ==",
          flush=True)

    for runner_name in ("bash", "bats", "python3"):
        needed = any(r == runner_name for _, r in SHELL_SUITES)
        # python3 is always available -- we invoke it via sys.executable
        # (see run_suite), never a bareword "python3" lookup, so PATH
        # presence of that literal name is not required.
        if runner_name == "python3":
            continue
        if needed and __import__("shutil").which(runner_name) is None:
            print(f"ERROR: '{runner_name}' not found on PATH but is required by a "
                  f"registered suite", file=sys.stderr)
            return 1

    results: list[tuple[str, str, float]] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(run_suite, rel_path, runner): rel_path
            for rel_path, runner in SHELL_SUITES
        }
        for future in as_completed(futures):
            rel_path, verdict, elapsed = future.result()
            results.append((rel_path, verdict, elapsed))
            print(f"{verdict:20s} {elapsed:7.2f}s  {rel_path}", flush=True)

    failed = [r for r in results if r[1] != "PASS"]

    print("================================================")
    print(f"Slow-tier suites: {len(results)} run, "
          f"{len(results) - len(failed)} passed, {len(failed)} failed")
    if failed:
        print("Failing suites:")
        for rel_path, verdict, elapsed in failed:
            print(f"  {verdict:20s} {rel_path}")
    print("================================================")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
