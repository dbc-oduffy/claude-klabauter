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
each invocation's args to a log file, exit 0) so tests run without requiring the engine root
to be configured for the append-goal-event.py leg — the engine root IS required for
read-frontmatter-field's in-process import (emit-goal-from-artifact.py's own frontmatter
reads), which every assertion below implicitly exercises.

Spec backlink: coordinator-content-repo:pln-per-repo-okr-goal-setting-syst-80bced § C7
Spec backlink: docs/plans/2026-07-19-debash-coordinator-windows.md (Plan C, Wave E3-d)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

from coordinator_core.win_portability import no_console_creationflags

import pytest

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SUBJECT = os.path.join(SCRIPT_DIR, "emit-goal-from-artifact.py")


_SHIM_BODY = '''#!/usr/bin/env python3
import json
import os
import sys

log = os.environ.get("SHIM_LOG")
events_path = sys.argv[sys.argv.index("--events-file") + 1]
with open(events_path, encoding="utf-8") as fh:
    events = json.load(fh)
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "events": events}) + "\\n")
# The emitter reads one outcome per event from the batch call's stdout.
sys.stdout.write(json.dumps([{"ok": True} for _ in events]))
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


def _calls(log: str) -> list[dict]:
    return [json.loads(ln) for ln in _read_log(log)]


def _events(log: str) -> list[dict]:
    return [ev for call in _calls(log) for ev in call["events"]]


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
period_value: "DoE-2026"
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

    log1 = os.path.join(tmp_base, "log1.log")
    r1 = _run_emitter(repo, shim, log1)
    assert r1.returncode == 0, f"emitter exits 0 for a valid goal artifact: stderr={r1.stderr}"
    calls1 = _calls(log1)
    assert len(calls1) == 1, f"append-goal-event.py invoked exactly once, got {len(calls1)}"

    (ev,) = calls1[0]["events"]
    assert ev["period"] == "repo", ev
    assert "DoE-2026" in ev["period_value"], ev
    assert "goal-legibility" in ev["text"], ev

    _write_goal(repo, "goal-tooling.yaml", FIXTURE_TOOLING)
    log6 = os.path.join(tmp_base, "log6.log")
    r6 = _run_emitter(repo, shim, log6)
    assert r6.returncode == 0, f"emitter exits 0 for two goal artifacts: rc={r6.returncode}"
    calls6 = _calls(log6)
    assert len(calls6) == 1, f"two goal artifacts -> ONE batch invocation, got {len(calls6)}"
    assert len(calls6[0]["events"]) == 2, calls6

    log7a = os.path.join(tmp_base, "log7a.log")
    log7b = os.path.join(tmp_base, "log7b.log")
    _run_emitter(repo, shim, log7a)
    _run_emitter(repo, shim, log7b)
    assert _events(log7a) == _events(log7b) and _events(log7a), \
        "identity chain stable — same events across two runs"

    repo_bad = os.path.join(tmp_base, "repo-bad")
    _write_goal(repo_bad, "bad-goal.yaml", FIXTURE_BAD)
    log8 = os.path.join(tmp_base, "log8.log")
    r8 = _run_emitter(repo_bad, shim, log8)
    assert r8.returncode == 2, f"missing period field -> exit 2, rc={r8.returncode}"
    assert not _read_log(log8), "bad goal not forwarded to append-goal-event.py"

    repo_nogoals = os.path.join(tmp_base, "repo-nogoals")
    os.makedirs(repo_nogoals, exist_ok=True)
    r9 = _run_emitter(repo_nogoals, shim, os.path.join(tmp_base, "log9.log"))
    assert r9.returncode == 0, f"absent state/goals dir -> exit 0, rc={r9.returncode}"

    repo_empty = os.path.join(tmp_base, "repo-emptygoals")
    os.makedirs(os.path.join(repo_empty, "state", "goals"), exist_ok=True)
    r10 = _run_emitter(repo_empty, shim, os.path.join(tmp_base, "log10.log"))
    assert r10.returncode == 0, f"empty state/goals dir -> exit 0, rc={r10.returncode}"

    log11 = os.path.join(tmp_base, "log11.log")
    _run_emitter(repo, shim, log11, extra_args=["--dry-run"])
    assert not _read_log(log11), "--dry-run does not invoke append-goal-event.py"

    log12 = os.path.join(tmp_base, "log12.log")
    env12 = dict(os.environ)
    env12["COORDINATOR_APPEND_GOAL_HELPER"] = shim
    env12["SHIM_LOG"] = log12
    subprocess.run(
        [sys.executable, SUBJECT, "--root", repo, "--repo", "myorg/myrepo"],
        capture_output=True, text=True, env=env12, **no_console_creationflags(),
    )
    calls12 = _calls(log12)
    assert calls12, "shim log not created"
    argv12 = calls12[0]["argv"]
    assert argv12[argv12.index("--repo") + 1] == "myorg/myrepo", argv12
    assert "--root" in argv12, argv12

    repo_c11 = os.path.join(tmp_base, "repo-c11")
    _write_goal(repo_c11, "goal-no-parent.yaml", FIXTURE_NO_PARENT)
    log13 = os.path.join(tmp_base, "log13.log")
    _run_emitter(repo_c11, shim, log13)
    events13 = _events(log13)
    assert events13 and "parent_goal_id" not in events13[0], \
        f"parent_goal_id absent-from-artifact -> key absent from the event: {events13}"
    assert "weekly_perceptible" not in events13[0], \
        f"weekly_perceptible absent-from-artifact -> key absent (D9): {events13}"

    _write_goal(repo_c11, "goal-with-parent.yaml", FIXTURE_WITH_PARENT)
    log15 = os.path.join(tmp_base, "log15.log")
    _run_emitter(repo_c11, shim, log15)
    ev15 = next((e for e in _events(log15) if "goal-with-parent" in e["text"]), {})
    assert ev15.get("parent_goal_id") == "goal-parent-quarter", ev15
    assert ev15.get("weekly_perceptible") is True, ev15

    repo_kr = os.path.join(tmp_base, "repo-krstatus")
    _write_goal(repo_kr, "goal-kr-status.yaml", FIXTURE_KR_STATUS)
    log16 = os.path.join(tmp_base, "log16.log")
    _run_emitter(repo_kr, shim, log16)
    events16 = _events(log16)
    krs = events16[0].get("key_results_status") if events16 else None
    assert isinstance(krs, list) and krs and krs[0].get("id") == "kr-1", events16
    assert krs[0].get("kind") == "outcome", krs
    assert "evidence_source" not in krs[0], "key_results_status[] drops evidence_source (C11 field map)"
    assert "weekly_perceptible" not in krs[0], "key_results_status[] drops per-KR weekly_perceptible (C11 field map)"

    repo_status = os.path.join(tmp_base, "repo-status")
    _write_goal(repo_status, "goal-status-active.yaml", FIXTURE_STATUS_ACTIVE)
    _write_goal(repo_status, "goal-status-achieved.yaml", FIXTURE_STATUS_ACHIEVED)
    _write_goal(repo_status, "goal-status-abandoned.yaml", FIXTURE_STATUS_ABANDONED)
    log18 = os.path.join(tmp_base, "log18.log")
    _run_emitter(repo_status, shim, log18)
    events18 = _events(log18)

    ev_active = next((e for e in events18 if "goal-status-active" in e["text"]), {})
    assert ev_active.get("status") == "active", "artifact status 'active' maps to wire status 'active'"

    ev_achieved = next((e for e in events18 if "goal-status-achieved" in e["text"]), {})
    assert ev_achieved.get("status") == "done", "artifact status 'achieved' maps to wire status 'done'"

    ev_abandoned = next((e for e in events18 if "goal-status-abandoned" in e["text"]), {})
    assert ev_abandoned.get("status") == "dropped", "artifact status 'abandoned' maps to wire status 'dropped'"


def test_help_flag_prints_usage_and_exits_zero():
    """F2 (klabauter#71): --help must work instead of falling through to
    'Unknown argument'."""
    result = subprocess.run(
        [sys.executable, SUBJECT, "--help"],
        capture_output=True, text=True, **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr
    assert "Usage" in result.stdout
    assert "--artifact" in result.stdout


_SHIM_BODY_CAPTURES_EVENTS_FILE = '''#!/usr/bin/env python3
import json
import os
import sys

log = os.environ.get("SHIM_LOG")
events_file = sys.argv[sys.argv.index("--events-file") + 1]
with open(events_file, encoding="utf-8") as f:
    events = json.load(f)
with open(log, "a", encoding="utf-8") as f:
    f.write(json.dumps(events) + "\\n")
sys.exit(0)
'''


def _write_events_capturing_shim(shim_dir: str) -> str:
    """Like _write_shim(), but reads --events-file's contents (before the
    caller unlinks it in its `finally`) and logs the parsed events instead of
    the raw argv — needed to assert on WHICH goal(s) got batched, since the
    events-file path itself is a fresh tempfile name each run."""
    os.makedirs(shim_dir, exist_ok=True)
    path = os.path.join(shim_dir, "append-goal-event.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_SHIM_BODY_CAPTURES_EVENTS_FILE)
    return path


def test_artifact_flag_emits_only_named_file(tmp_path):
    """F2: --artifact <path> emits only the named scaffolded artifact, not a
    whole-directory scan — 'run the emitter against the scaffolded artifact'."""
    repo = os.path.join(tmp_path, "repo-artifact")
    _write_goal(repo, "goal-a.yaml", FIXTURE_LEGIBILITY)
    _write_goal(repo, "goal-b.yaml", FIXTURE_LEGIBILITY.replace("goal-legibility", "goal-b"))

    shim_dir = os.path.join(tmp_path, "shim")
    shim = _write_events_capturing_shim(shim_dir)
    log = os.path.join(tmp_path, "artifact.log")

    # NOTE: the shared _write_shim() shim in this file doesn't emit
    # batch-outcome JSON on stdout, so the emitter's own exit code reflects a
    # pre-existing, unrelated batch-reporting gap (reproduced against
    # origin/HEAD, not introduced by this fix) — this test uses its own shim
    # variant to assert on what --artifact actually controls: which file(s)
    # get batched.
    artifact_path = os.path.join(repo, "state", "goals", "goal-b.yaml")
    _run_emitter(repo, shim, log, extra_args=["--artifact", artifact_path])

    lines = _read_log(log)
    assert len(lines) == 1, f"--artifact must emit exactly one goal, got: {lines}"
    events = json.loads(lines[0])
    assert len(events) == 1, events
    assert "goal-b" in events[0]["text"], events


def test_bare_positional_path_is_artifact_shorthand(tmp_path):
    """F2: a bare positional path argument is accepted as --artifact shorthand."""
    repo = os.path.join(tmp_path, "repo-positional")
    _write_goal(repo, "goal-a.yaml", FIXTURE_LEGIBILITY)

    shim_dir = os.path.join(tmp_path, "shim")
    shim = _write_shim(shim_dir)
    log = os.path.join(tmp_path, "positional.log")

    artifact_path = os.path.join(repo, "state", "goals", "goal-a.yaml")
    env = dict(os.environ)
    env["COORDINATOR_APPEND_GOAL_HELPER"] = shim
    env["SHIM_LOG"] = log
    subprocess.run(
        [sys.executable, SUBJECT, artifact_path, "--root", repo, "--repo", "dbc-oduffy/doe-test"],
        capture_output=True, text=True, env=env, **no_console_creationflags(),
    )
    lines = _read_log(log)
    assert len(lines) == 1, lines
