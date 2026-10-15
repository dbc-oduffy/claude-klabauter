"""coordinator_core.ops.ceremony.tests.test_skill_weight_admission -- the
Coordinator-content-repo skill-weight leg runs on engine commits.

The fixture repo carries stand-ins for DoE's predicate, harness and baseline at
their real paths; the leg loads whatever the committing repo ships, so the
stand-ins exercise the loader, scope, refusal text and hygiene.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
import time

import pytest

from coordinator_core.ops.ceremony import commit_admission, git_native
from coordinator_core.win_portability import no_console_creationflags

from .fixtures.real_git import real_git_repo

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_ADMISSION = '''
import sys
from pathlib import Path

_TESTS_DIR = Path(__file__).resolve().parents[2] / "tests"
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))
import _bare_helper as shared


def admission_violations(proposed, base, baseline):
    lines = []
    total, base_total = sum(proposed.values()), sum(base.values())
    limit = baseline["fleet"]["resident_limit"]
    if shared.over(total, limit) and total > base_total:
        lines.append(f"fleet resident: {total} B > {limit} B (+{total - base_total})")
    for name in sorted(proposed):
        grown = proposed[name] - base.get(name, 0)
        if proposed[name] > baseline["exceptions"].get(name, 100) and grown > 0:
            lines.append(f"skill {name}: {proposed[name]} B (+{grown})")
    return lines
'''

_HARNESS = '''
def measure_all_skills(*, sizes):
    out = {}
    for key, size in sizes.items():
        parts = key.split("/")
        if len(parts) == 4 and parts[3] == "SKILL.md":
            out[parts[2]] = {"resident": size}
    return out
'''

_BARE_HELPER = "def over(total, limit):\n    return total > limit\n"

_ADMISSION_REL = "coordinator/hooks/scripts/_skill_weight_admission.py"
_HARNESS_REL = "coordinator/tests/fixtures/skill-weight/skill_weight_harness.py"
_BASELINE_REL = "coordinator/tests/baselines/skill-weight-budget.json"


def _git(args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    )


def _write(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _doe_repo(tmp_path, skills=3, fleet_limit=100_000):
    root = real_git_repo(tmp_path)
    _write(root, _ADMISSION_REL, _ADMISSION)
    _write(root, _HARNESS_REL, _HARNESS)
    _write(root, "coordinator/tests/_bare_helper.py", _BARE_HELPER)
    _write(
        root,
        _BASELINE_REL,
        json.dumps({"fleet": {"resident_limit": fleet_limit}, "exceptions": {}}),
    )
    for i in range(skills):
        _write(root, f"coordinator/skills/s{i}/SKILL.md", "x" * 50)
    _git(["add", "-A"], root)
    _git(["commit", "-q", "-m", "doe seed"], root)
    return root


def _head(root):
    return _git(["rev-parse", "HEAD"], root).stdout.strip()


def _commit(root, path, content):
    msg = root / ".git" / "TEST_MSG"
    msg.write_text("test commit\n", encoding="utf-8")
    return git_native.commit_authored_content(path, content, msg, root)


def test_growth_past_a_skill_limit_is_refused_with_the_guard_lines(tmp_path):
    root = _doe_repo(tmp_path)
    before = _head(root)
    result = _commit(root, "coordinator/skills/s0/SKILL.md", "x" * 500)
    assert not result.ok
    assert "[skill-weight-budget-guard] commit refused: skill growth over budget" in result.stderr
    assert "  skill s0: 500 B (+450)" in result.stderr
    assert _head(root) == before


def test_fleet_overrun_is_refused(tmp_path):
    root = _doe_repo(tmp_path, fleet_limit=160)
    result = _commit(root, "coordinator/skills/s1/SKILL.md", "x" * 90)
    assert not result.ok
    assert "fleet resident" in result.stderr


def test_a_new_skill_over_its_limit_is_refused(tmp_path):
    root = _doe_repo(tmp_path)
    new = {"coordinator/skills/fresh/SKILL.md": (0o100644, _blob(root, "x" * 500))}
    refusal = commit_admission.skill_weight_check(root, new)[0]
    assert refusal is not None and "skill fresh" in refusal


def test_a_shrink_and_an_unrelated_path_are_admitted(tmp_path):
    root = _doe_repo(tmp_path)
    assert _commit(root, "coordinator/skills/s0/SKILL.md", "x" * 10).ok
    assert _commit(root, "seed.txt", "seed\nmore\n").ok


def test_a_baseline_raise_in_the_same_commit_is_honoured(tmp_path):
    root = _doe_repo(tmp_path, fleet_limit=100)
    raised = json.dumps({"fleet": {"resident_limit": 100_000}, "exceptions": {"s0": 600}})
    assert _commit(root, _BASELINE_REL, raised).ok


def test_a_repo_without_the_admission_script_reads_no_blob(tmp_path, monkeypatch):
    root = real_git_repo(tmp_path)
    reads = []
    monkeypatch.setattr(
        commit_admission, "read_object", lambda *a, **k: reads.append(a) or None
    )
    assembled = {"coordinator/skills/s0/SKILL.md": (0o100644, "0" * 40)}
    assert commit_admission.skill_weight_check(root, assembled)[0] is None
    assert reads == []


def test_the_leg_leaves_sys_path_and_modules_untouched_and_is_fast(tmp_path):
    root = _doe_repo(tmp_path, skills=132, fleet_limit=10_000_000)
    path_before, modules_before = list(sys.path), set(sys.modules)
    grown = {"coordinator/skills/s0/SKILL.md": (0o100644, _blob(root, "x" * 70))}
    start = time.process_time()
    assert commit_admission.skill_weight_check(root, grown)[0] is None
    assert time.process_time() - start < 0.2
    assert sys.path == path_before
    assert set(sys.modules) == modules_before


def _blob(root, text):
    return subprocess.run(
        ["git", "hash-object", "-w", "--stdin"], cwd=str(root), input=text,
        capture_output=True, text=True, check=True, **no_console_creationflags(),
    ).stdout.strip()


def test_a_plumbing_failure_admits_with_a_note(tmp_path, caplog):
    root = _doe_repo(tmp_path)
    (root / _HARNESS_REL).write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    grown = {"coordinator/skills/s0/SKILL.md": (0o100644, _blob(root, "x" * 500))}
    with caplog.at_level(logging.WARNING):
        assert commit_admission.skill_weight_check(root, grown)[0] is None
    assert "did not run this commit" in caplog.text
    refusal, note = commit_admission.skill_weight_check(root, grown)
    assert refusal is None
    assert note.startswith("[skill-weight-budget-guard] check did not run: ")
    assert "boom" in note


def test_a_verdict_never_yields_a_note_and_an_admit_yields_neither(tmp_path):
    root = _doe_repo(tmp_path)
    over = {"coordinator/skills/s0/SKILL.md": (0o100644, _blob(root, "x" * 500))}
    refusal, note = commit_admission.skill_weight_check(root, over)
    assert refusal is not None and note is None
    shrunk = {"coordinator/skills/s0/SKILL.md": (0o100644, _blob(root, "x" * 10))}
    assert commit_admission.skill_weight_check(root, shrunk) == (None, None)


def test_an_already_over_skill_held_at_its_base_size_is_admitted(tmp_path):
    root = _doe_repo(tmp_path)
    assert _commit(root, _BASELINE_REL, json.dumps(
        {"fleet": {"resident_limit": 100_000}, "exceptions": {"s0": 10}}
    )).ok
    same = {"coordinator/skills/s0/SKILL.md": (0o100644, _blob(root, "x" * 50))}
    assert commit_admission.skill_weight_check(root, same) == (None, None)


def test_the_working_tree_copy_is_not_measured(tmp_path):
    root = _doe_repo(tmp_path)
    (root / "coordinator/skills/s0/SKILL.md").write_text("x" * 5000, encoding="utf-8")
    staged = {"coordinator/skills/s0/SKILL.md": (0o100644, _blob(root, "x" * 60))}
    assert commit_admission.skill_weight_check(root, staged) == (None, None)


def _v2(root, path):
    from coordinator_core.ops.ceremony import commit_v2

    return commit_v2._handler(
        {"paths": [path], "message": "skill edit\n",
         "session_id": "11111111-2222-4333-8444-555555555555"},
        repo_root=root / ".git",
    )


def test_commit_v2_refuses_a_growth_and_moves_no_ref(tmp_path):
    root = _doe_repo(tmp_path)
    before = _head(root)
    (root / "coordinator/skills/s0/SKILL.md").write_text("x" * 500, encoding="utf-8")
    result = _v2(root, "coordinator/skills/s0/SKILL.md")
    assert result["committed"] is False
    assert "  skill s0: 500 B (+450)" in result["error"]
    assert _head(root) == before


def test_commit_v2_lands_and_warns_when_the_check_could_not_run(tmp_path):
    root = _doe_repo(tmp_path)
    (root / _HARNESS_REL).write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    _git(["commit", "-q", "-am", "break harness"], root)
    (root / "coordinator/skills/s0/SKILL.md").write_text("x" * 500, encoding="utf-8")
    result = _v2(root, "coordinator/skills/s0/SKILL.md")
    assert result["committed"] is True
    assert any(w.startswith("[skill-weight-budget-guard] check did not run") for w in result["warnings"])


def test_commit_v2_non_triggering_commit_carries_no_warning(tmp_path):
    root = _doe_repo(tmp_path)
    (root / "other.txt").write_text("o\n", encoding="utf-8")
    result = _v2(root, "other.txt")
    assert result["committed"] is True
    assert not any("skill-weight" in w for w in result["warnings"])
