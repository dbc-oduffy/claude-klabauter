"""test_emit_goal_from_artifact.py — pytest suite for emit-goal-from-artifact.py.

Converted from a hand-rolled `.test.py` runner (print-based PASS/FAIL, its own
main()/sys.exit) into a collectable top-level test_* function; assertion intent
preserved 1:1. The original suite built up shared repo/log state across sequential
checks (T6 adds a second goal to the repo T1-T5 already populated, etc.) so the
conversion keeps that sequence inside one test function rather than splitting into
independently-ordered test_* functions and risking silently dropping the shared-state
dependency.

Port of: emit-goal-from-artifact.test.sh (3d785330, 2026-07-21) —
de-bash-coordinator campaign, Plan C, Wave E3-d. Verifies that
emit-goal-from-artifact.py reads per-repo state/goals/*.yaml whole-document YAML records
and invokes append-goal-event.py once per goal with correct params (period, period_value,
text, repo, root); verifies invocation count and wire-conformant arg pass-through.

Fix-in-port (DR-059): the retired bash test's append-goal-event.py PATH-shim was itself a
bash script invoked via the oracle's `python "$_APPEND_HELPER"` call — running a bash
script through the python interpreter is a syntax error, so 9/14 of the original suite's
assertions FAILED on every machine that has both `python` and `jq` on PATH (verified
against the pre-port oracle on this machine: 5 passed, 9 failed). This port's shim is a
real Python script, exercised via `sys.executable`, restoring full coverage.

Test isolation: uses COORDINATOR_APPEND_GOAL_HELPER to inject a Python shim (recording
each invocation's args to a log file, exit 0) so tests run without requiring CLAUDE_KLABAUTER_ROOT
to be configured for the append-goal-event.py leg — CLAUDE_KLABAUTER_ROOT IS required for
read-frontmatter-field's in-process import (emit-goal-from-artifact.py's own frontmatter
reads), which every assertion below implicitly exercises.

Spec backlink: docs/plans/2026-07-06-goal-setting-okr-legibility-system.md § C7
Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md (Plan C, Wave E3-d)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from coordinator_core.win_portability import no_console_creationflags

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT = os.path.join(SCRIPT_DIR, "emit-goal-from-artifact.py")


_SHIM_BODY = '''#!/usr/bin/env python3
import os
import sys

log = os.environ.get("SHIM_LOG")
with open(log, "a", encoding="utf-8") as f:
    f.write(" ".join(sys.argv[1:]) + "\\n")
sys.exit(0)
'''


def _write_shim(shim_dir: str) -> str:
    """Write a real Python shim recording each invocation's args to $SHIM_LOG.

    Unlike the retired bash test's bash-scripted shim (see module docstring's
    Fix-in-port note), this shim is executed via sys.executable, matching how
    emit-goal-from-artifact.py itself invokes append-goal-event.py.

    Deliberately NOT chmod +x: the subject spawns this path as
    `[sys.executable, append_helper, ...]` (see that module's `cmd = [`), so
    the exec bit was never the invocation mechanism and nothing reads it as a
    decision input. Setting it only manufactured a posix_mode_bits finding for
    a site that is already portable — on Windows the bit does not exist and
    the shim runs regardless.
    """
    os.makedirs(shim_dir, exist_ok=True)
    path = os.path.join(shim_dir, "append-goal-event.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_SHIM_BODY)
    return path


def _run_emitter(repo: str, shim: str, log: str, extra_args: list[str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["COORDINATOR_APPEND_GOAL_HELPER"] = shim
    env["SHIM_LOG"] = log
    args = [sys.executable, SUBJECT, "--root", repo, "--repo", "dbc-oduffy/doe-test"]
    if extra_args:
        args += extra_args
    return subprocess.run(args, capture_output=True, text=True, env=env, **no_console_creationflags())


def _write_goal(repo: str, filename: str, content: str) -> None:
    goals_dir = os.path.join(repo, "state", "goals")
    os.makedirs(goals_dir, exist_ok=True)
    with open(os.path.join(goals_dir, filename), "w", encoding="utf-8") as f:
        f.write(content)


def _read_log(log: str) -> list[str]:
    if not os.path.isfile(log):
        return []
    with open(log, encoding="utf-8") as f:
        return [ln for ln in f.read().splitlines() if ln]


FIXTURE_LEGIBILITY = """schema: goal
id: goal-legibility
title: "Test Goal: Legibility"
status: active
objective: "Make the system fully legible to new engineers"
key_results:
  - id: kr-1
    text: "Onboarding docs cover every subsystem"
    kind: outcome
    status: not-started
    weekly_perceptible: true
    evidence_source: null
created: "2026-07-07"
owner: doe-em
period: repo
period_value: "coordinator-claude-2026"
"""

FIXTURE_TOOLING = """schema: goal
id: goal-tooling
title: "Test Goal: Tooling"
status: active
objective: "All EM actions are one CLI invocation"
key_results:
  - id: kr-1
    text: "Every EM action maps to a single CLI command"
    kind: outcome
    status: not-started
    weekly_perceptible: true
    evidence_source: null
created: "2026-07-07"
owner: doe-em
period: week
period_value: "2026-W28"
"""

FIXTURE_BAD = """schema: goal
id: goal-bad
title: "Bad Goal"
status: active
objective: "Missing period fields"
key_results:
  - id: kr-1
    text: "N/A"
    kind: outcome
    status: not-started
    weekly_perceptible: true
    evidence_source: null
created: "2026-07-07"
"""

FIXTURE_NO_PARENT = """schema: goal
id: goal-no-parent
title: "Test Goal: No Parent"
status: active
objective: "Goal with no parent_goal_id and no weekly_perceptible"
key_results:
  - id: kr-1
    text: "N/A"
    kind: outcome
    status: not-started
    weekly_perceptible: true
    evidence_source: null
created: "2026-07-07"
period: week
period_value: "2026-W28"
"""

FIXTURE_WITH_PARENT = """schema: goal
id: goal-with-parent
title: "Test Goal: With Parent"
status: active
objective: "Goal with parent_goal_id and top-level weekly_perceptible set"
key_results:
  - id: kr-1
    text: "N/A"
    kind: outcome
    status: not-started
    weekly_perceptible: true
    evidence_source: null
created: "2026-07-07"
period: week
period_value: "2026-W28"
weekly_perceptible: true
parent_goal_id: goal-parent-quarter
"""

FIXTURE_STATUS_ACTIVE = """schema: goal
id: goal-status-active
title: "Test Goal: Status Active"
status: active
objective: "Active goal"
created: "2026-07-07"
period: week
period_value: "2026-W28"
"""

FIXTURE_STATUS_ACHIEVED = """schema: goal
id: goal-status-achieved
title: "Test Goal: Status Achieved"
status: achieved
objective: "Achieved goal"
created: "2026-07-07"
period: week
period_value: "2026-W28"
"""

FIXTURE_STATUS_ABANDONED = """schema: goal
id: goal-status-abandoned
title: "Test Goal: Status Abandoned"
status: abandoned
objective: "Abandoned goal"
created: "2026-07-07"
period: week
period_value: "2026-W28"
"""

FIXTURE_KR_STATUS = """schema: goal
id: goal-kr-status
title: "Test Goal: KR Status Projection"
status: active
objective: "Goal exercising key_results_status[] projection"
key_results:
  - id: kr-1
    text: "First key result"
    kind: outcome
    status: in-progress
    weekly_perceptible: true
    evidence_source: "state/some-evidence.md"
created: "2026-07-07"
period: week
period_value: "2026-W28"
"""


def test_emit_goal_from_artifact(tmp_path):
    tmp_base = str(tmp_path)
    shim_dir = os.path.join(tmp_base, "shim-bin")
    shim = _write_shim(shim_dir)

    repo = os.path.join(tmp_base, "repo")
    _write_goal(repo, "goal-legibility.yaml", FIXTURE_LEGIBILITY)

    # --- T1/T2: single valid goal -> exit 0, exactly one invocation ---
    log1 = os.path.join(tmp_base, "log1.log")
    r1 = _run_emitter(repo, shim, log1)
    assert r1.returncode == 0, f"emitter exits 0 for a valid goal artifact: stderr={r1.stderr}"
    lines1 = _read_log(log1)
    assert len(lines1) == 1, f"append-goal-event.py invoked exactly once, got {len(lines1)}"

    # --- T3/T4/T5: invocation carries period / period_value / identity-chain text ---
    inv = lines1[0]
    assert "--period repo" in inv, inv
    assert "coordinator-claude-2026" in inv, inv
    assert "goal-legibility" in inv, inv

    # --- T6: two goal artifacts -> two invocations ---
    _write_goal(repo, "goal-tooling.yaml", FIXTURE_TOOLING)
    log6 = os.path.join(tmp_base, "log6.log")
    r6 = _run_emitter(repo, shim, log6)
    assert r6.returncode == 0, f"emitter exits 0 for two goal artifacts: rc={r6.returncode}"
    lines6 = _read_log(log6)
    assert len(lines6) == 2, f"two goal artifacts -> two invocations, got {len(lines6)}"

    # --- T7: identity chain stability across two independent runs ---
    log7a = os.path.join(tmp_base, "log7a.log")
    log7b = os.path.join(tmp_base, "log7b.log")
    _run_emitter(repo, shim, log7a)
    _run_emitter(repo, shim, log7b)
    assert sorted(_read_log(log7a)) == sorted(_read_log(log7b)) and _read_log(log7a), \
        "identity chain stable — same args across two runs"

    # --- T8: missing 'period' field -> exit 2, not forwarded ---
    repo_bad = os.path.join(tmp_base, "repo-bad")
    _write_goal(repo_bad, "bad-goal.yaml", FIXTURE_BAD)
    log8 = os.path.join(tmp_base, "log8.log")
    r8 = _run_emitter(repo_bad, shim, log8)
    assert r8.returncode == 2, f"missing period field -> exit 2, rc={r8.returncode}"
    assert not _read_log(log8), "bad goal not forwarded to append-goal-event.py"

    # --- T9: absent state/goals dir -> exit 0 ---
    repo_nogoals = os.path.join(tmp_base, "repo-nogoals")
    os.makedirs(repo_nogoals, exist_ok=True)
    r9 = _run_emitter(repo_nogoals, shim, os.path.join(tmp_base, "log9.log"))
    assert r9.returncode == 0, f"absent state/goals dir -> exit 0, rc={r9.returncode}"

    # --- T10: empty state/goals dir -> exit 0 ---
    repo_empty = os.path.join(tmp_base, "repo-emptygoals")
    os.makedirs(os.path.join(repo_empty, "state", "goals"), exist_ok=True)
    r10 = _run_emitter(repo_empty, shim, os.path.join(tmp_base, "log10.log"))
    assert r10.returncode == 0, f"empty state/goals dir -> exit 0, rc={r10.returncode}"

    # --- T11: --dry-run does not invoke append-goal-event.py ---
    log11 = os.path.join(tmp_base, "log11.log")
    _run_emitter(repo, shim, log11, extra_args=["--dry-run"])
    assert not _read_log(log11), "--dry-run does not invoke append-goal-event.py"

    # --- T12: invocation carries --repo and --root ---
    log12 = os.path.join(tmp_base, "log12.log")
    env12 = dict(os.environ)
    env12["COORDINATOR_APPEND_GOAL_HELPER"] = shim
    env12["SHIM_LOG"] = log12
    subprocess.run(
        [sys.executable, SUBJECT, "--root", repo, "--repo", "myorg/myrepo"],
        capture_output=True, text=True, env=env12, **no_console_creationflags(),
    )
    lines12 = _read_log(log12)
    assert lines12, "shim log not created"
    inv12 = lines12[0]
    assert "--repo" in inv12, inv12
    assert "myorg/myrepo" in inv12, inv12
    assert "--root" in inv12, inv12

    # --- T13/T14: C11 passthrough — parent_goal_id present-as-null, weekly_perceptible absent-when-absent ---
    repo_c11 = os.path.join(tmp_base, "repo-c11")
    _write_goal(repo_c11, "goal-no-parent.yaml", FIXTURE_NO_PARENT)
    log13 = os.path.join(tmp_base, "log13.log")
    _run_emitter(repo_c11, shim, log13)
    lines13 = _read_log(log13)
    assert lines13 and "--parent-goal-id " in lines13[0], \
        f"parent_goal_id absent-from-artifact still emits --parent-goal-id (D9): {lines13}"
    assert lines13 and "--weekly-perceptible" not in lines13[0], \
        f"weekly_perceptible absent-from-artifact -> flag absent (D9): {lines13}"

    # --- T15: C11 passthrough — parent_goal_id + weekly_perceptible present ---
    _write_goal(repo_c11, "goal-with-parent.yaml", FIXTURE_WITH_PARENT)
    log15 = os.path.join(tmp_base, "log15.log")
    _run_emitter(repo_c11, shim, log15)
    lines15 = [ln for ln in _read_log(log15) if "goal-with-parent" in ln]
    inv15 = lines15[0] if lines15 else ""
    assert "--parent-goal-id goal-parent-quarter" in inv15, inv15
    assert "--weekly-perceptible true" in inv15, inv15

    # --- T16: key_results_status[] projection drops evidence_source + per-KR weekly_perceptible ---
    repo_kr = os.path.join(tmp_base, "repo-krstatus")
    _write_goal(repo_kr, "goal-kr-status.yaml", FIXTURE_KR_STATUS)
    log16 = os.path.join(tmp_base, "log16.log")
    _run_emitter(repo_kr, shim, log16)
    lines16 = _read_log(log16)
    inv16 = lines16[0] if lines16 else ""
    assert "--key-results-status" in inv16, inv16
    assert '"kind":"outcome"' in inv16 or '"kind": "outcome"' in inv16, inv16
    assert "evidence_source" not in inv16, "key_results_status[] JSON drops evidence_source (C11 field map)"
    assert "weekly_perceptible" not in inv16, "key_results_status[] JSON drops per-KR weekly_perceptible (C11 field map)"

    # --- T17: key_results_status[] JSON is valid and machine-parseable ---
    # Uses raw_decode (not a full-string json.loads) because --key-results-status
    # is no longer guaranteed to be the last flag on the invocation line
    # (--status, forwarded after it as of 2026-07-25, trails it here).
    assert inv16 and "--key-results-status" in inv16
    json_start = inv16.index("--key-results-status") + len("--key-results-status ")
    parsed, _end = json.JSONDecoder().raw_decode(inv16[json_start:])
    assert isinstance(parsed, list) and parsed and parsed[0].get("id") == "kr-1", parsed

    # --- T18-T20: artifact status -> wire status mapping (--status forwarding) ---
    repo_status = os.path.join(tmp_base, "repo-status")
    _write_goal(repo_status, "goal-status-active.yaml", FIXTURE_STATUS_ACTIVE)
    _write_goal(repo_status, "goal-status-achieved.yaml", FIXTURE_STATUS_ACHIEVED)
    _write_goal(repo_status, "goal-status-abandoned.yaml", FIXTURE_STATUS_ABANDONED)
    log18 = os.path.join(tmp_base, "log18.log")
    _run_emitter(repo_status, shim, log18)
    lines18 = _read_log(log18)

    inv_active = next((ln for ln in lines18 if "goal-status-active" in ln), "")
    assert "--status active" in inv_active, "artifact status 'active' maps to wire status 'active'"

    inv_achieved = next((ln for ln in lines18 if "goal-status-achieved" in ln), "")
    assert "--status done" in inv_achieved, "artifact status 'achieved' maps to wire status 'done'"

    inv_abandoned = next((ln for ln in lines18 if "goal-status-abandoned" in ln), "")
    assert "--status dropped" in inv_abandoned, "artifact status 'abandoned' maps to wire status 'dropped'"
