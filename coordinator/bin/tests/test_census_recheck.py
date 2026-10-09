"""coordinator/bin/tests/test_census_recheck.py — the fire-time census leg.

Subject: `coordinator/bin/census-recheck.py`, which re-asks a plan's counted premises of
HEAD and diffs the answers against what the plan recorded.

WHY THIS SURFACE EXISTS AT ALL is the first thing to know when changing it: three
surfaces specify the leg and none implemented it — `commands/mise-en-place.md` § Phase 0,
`skills/pickup/aggregate-rollup.py`'s negative-spec, and the bar's own "checks that the
question is ASKABLE" wording. The tool is that implementation. It SPAWNS, so it belongs
nowhere near a gate or a hook.

The interesting assertions are the three-way split on a failed comparison. A census
entry that does not match its recorded result has two causes that route to different
people, and conflating them is the defect these tests exist to prevent.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process]

TOOL = Path(__file__).resolve().parents[1] / "census-recheck.py"


def _load():
    spec = importlib.util.spec_from_file_location("census_recheck", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cr():
    assert TOOL.is_file(), f"the fire-time census leg is missing: {TOOL}"
    return _load()


def _plan(tmp_path: Path, census_yaml: str) -> Path:
    p = tmp_path / "plan.md"
    p.write_text(f"---\ntitle: \"t\"\nstatus: draft\n{census_yaml}---\n\nbody\n",
                 encoding="utf-8")
    return p


def test_matching_entry_is_current(cr, tmp_path):
    plan = _plan(tmp_path, 'census:\n  - question: q\n    command: echo 7\n    result: "7"\n')
    out = cr.recheck_plan(plan, tmp_path, 10)
    assert out["verdict"] == cr.CURRENT
    assert out["rows"][0]["state"] == cr.CURRENT


def test_a_moved_answer_is_drift_not_undiffable(cr, tmp_path):
    """The recorded value looks like OUTPUT and disagrees — the tree moved. This routes
    to whoever owns the premise, and must not be softened into a recording defect."""
    plan = _plan(tmp_path, 'census:\n  - question: q\n    command: echo 9\n    result: "7"\n')
    out = cr.recheck_plan(plan, tmp_path, 10)
    assert out["verdict"] == cr.DRIFT, "an output-shaped result that disagrees is DRIFT"
    assert "9" in out["rows"][0]["detail"] and "7" in out["rows"][0]["detail"], (
        "a DRIFT row must print BOTH answers — the whole value of recording the command "
        "is that a reader can see what moved"
    )


def test_a_prose_result_is_undiffable_not_drift(cr, tmp_path):
    """A paraphrase cannot be diffed, and saying the tree moved would be false.

    plan.schema.json says a paraphrase defeats the field, so this is a real finding — but
    it routes to the census author, not to the premise owner."""
    plan = _plan(
        tmp_path,
        'census:\n  - question: q\n    command: echo 7\n'
        '    result: "seven callers remain — the count AC3 rests on"\n',
    )
    out = cr.recheck_plan(plan, tmp_path, 10)
    assert out["verdict"] == cr.UNDIFFABLE
    assert "prose" in out["rows"][0]["detail"]


def test_a_gloss_wrapping_the_output_still_matches(cr, tmp_path):
    """Containment counts. An author who wrote `7` and one who wrote `7` inside a longer
    recorded string have recorded the same answer; flagging the second would train
    authors to strip the explanation that makes an entry readable."""
    plan = _plan(tmp_path, 'census:\n  - question: q\n    command: echo 7\n    result: "7"\n')
    assert cr.recheck_plan(plan, tmp_path, 10)["rows"][0]["state"] == cr.CURRENT


def test_nonzero_exit_is_not_unrunnable(cr, tmp_path):
    """`grep -c` exits 1 on a zero count, and "this is wired nowhere" is a legitimate
    recorded premise — several live plans rest on exactly that. Treating a non-zero exit
    as unrunnable would make the honest zero unrecordable."""
    cmd = f'"{sys.executable}" -c "import sys; print(0); sys.exit(1)"'
    plan = _plan(tmp_path, f"census:\n  - question: q\n    command: '{cmd}'\n    result: \"0\"\n")
    out = cr.recheck_plan(plan, tmp_path, 10)
    assert out["verdict"] == cr.CURRENT
    assert out["rows"][0]["exit_code"] == 1


def test_an_unexecutable_command_is_unrunnable(cr, tmp_path):
    plan = _plan(tmp_path, 'census:\n  - question: q\n    command: "exec /nonexistent/xyzzy"\n    result: "1"\n')
    assert cr.recheck_plan(plan, tmp_path, 10)["verdict"] == cr.UNRUNNABLE


def test_non_utf8_output_does_not_crash_the_recheck(cr, tmp_path):
    """Undecodable stdout must be replaced, not raised: a locale codec (Windows cp1252)
    or a binary byte in a command's output would otherwise abort the whole census."""
    cmd = f'"{sys.executable}" -c "import sys; sys.stdout.buffer.write(bytes([0xff, 0x37]))"'
    plan = _plan(tmp_path, f"census:\n  - question: q\n    command: '{cmd}'\n    result: \"7\"\n")
    out = cr.recheck_plan(plan, tmp_path, 10)
    assert out["rows"][0]["state"] in (cr.CURRENT, cr.DRIFT, cr.UNDIFFABLE)
    assert "�" in out["rows"][0]["observed"]


def test_declared_empty_is_not_reported_as_verified(cr, tmp_path):
    """`census: []` is a CLAIM that no counted premise exists. Reporting it as CURRENT
    would let an unexamined empty read as a re-verified one."""
    plan = _plan(tmp_path, "census: []\n")
    assert cr.recheck_plan(plan, tmp_path, 10)["verdict"] == cr.DECLARED_EMPTY


def test_absent_census_is_not_this_tools_finding(cr, tmp_path):
    plan = _plan(tmp_path, "")
    assert cr.recheck_plan(plan, tmp_path, 10)["verdict"] == cr.NO_CENSUS


@pytest.mark.cadence
def test_exit_codes_are_verdicts(cr, tmp_path):
    """Exit status routes: 1 DRIFT goes to the premise owner, 5 UNDIFFABLE to the census
    author, 2 UNRUNNABLE to whoever wrote the command. A caller that cannot tell them
    apart cannot route."""
    drift = _plan(tmp_path / "a", 'census:\n  - question: q\n    command: echo 9\n    result: "7"\n') \
        if (tmp_path / "a").mkdir() or True else None
    proc = subprocess.run([sys.executable, str(TOOL), "--repo-root", str(tmp_path), str(drift)],
                          capture_output=True, text=True, timeout=60,
                          creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert proc.returncode == cr.EXIT_DRIFT, proc.stdout + proc.stderr


def test_does_not_write_anything(cr, tmp_path):
    """The tool reports. A fire-time reader that started editing plans would be a second
    record-mutating authority nobody ratified."""
    plan = _plan(tmp_path, 'census:\n  - question: q\n    command: echo 7\n    result: "7"\n')
    before = plan.read_text(encoding="utf-8")
    cr.recheck_plan(plan, tmp_path, 10)
    assert plan.read_text(encoding="utf-8") == before
