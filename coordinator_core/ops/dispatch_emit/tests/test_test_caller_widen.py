"""Test-referencer widening of wave rows' writes at emit time."""
import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.test_caller_widen import widen_waves
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

MOD = "pkg/sub/mod.py"


def _row(rid, writes, body=""):
    return WaveRow(id=rid, title=rid, surface="s", writes=list(writes), reads=[], depends_on=[], body=body)


def _grep(output):
    calls = []

    def run(pattern, pathspecs):
        calls.append((pattern, pathspecs))
        return output

    run.calls = calls
    return run


def test_row_gets_module_test_referencers():
    grep = _grep(
        "pkg/tests/test_a.py:from pkg.sub.mod import f\n"
        "pkg/tests/test_b.py:import pkg.sub.mod\n"
        "pkg/tests/test_c.py:from pkg.sub import mod\n"
        "pkg/tests/test_other.py:from pkg.sub.modx import f\n"
    )
    [[row]] = widen_waves([[_row("A", [MOD])]], grep)
    assert row.writes == [MOD, "pkg/tests/test_a.py", "pkg/tests/test_b.py", "pkg/tests/test_c.py"]
    assert len(grep.calls) == 1


def test_referencer_another_same_wave_row_writes_is_excluded_and_named():
    grep = _grep("pkg/tests/test_a.py:from pkg.sub.mod import f\n")
    wave = [_row("A", [MOD], body="b"), _row("B", ["pkg/tests/test_a.py"])]
    [[a, b]] = widen_waves([wave], grep)
    assert a.writes == [MOD]
    assert "pkg/tests/test_a.py" in a.body and a.body.startswith("b")
    assert b.writes == ["pkg/tests/test_a.py"] and b.body == ""


def test_referencer_written_by_row_in_another_wave_is_added():
    grep = _grep("pkg/tests/test_a.py:from pkg.sub.mod import f\n")
    waves = [[_row("A", [MOD])], [_row("B", ["pkg/tests/test_a.py"])]]
    [[a], _] = widen_waves(waves, grep)
    assert "pkg/tests/test_a.py" in a.writes


def test_no_modules_means_no_grep_and_no_change():
    grep = _grep("x")
    waves = [[_row("A", ["pkg/tests/test_a.py", "doc.md"])]]
    assert widen_waves(waves, grep) == waves
    assert grep.calls == []


def test_failed_grep_widens_nothing():
    waves = [[_row("A", [MOD])]]
    assert widen_waves(waves, _grep(None)) == waves


@pytest.mark.spawns_process
@pytest.mark.cadence
def test_git_grep_pattern_finds_referencers_in_a_real_repo(tmp_path):
    def git(*a):
        subprocess.run(
            ["git", *a],
            cwd=tmp_path,
            check=True,
            capture_output=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )

    (tmp_path / "pkg" / "sub").mkdir(parents=True)
    (tmp_path / "pkg" / "tests").mkdir()
    (tmp_path / "pkg" / "sub" / "mod.py").write_text("x = 1\n")
    (tmp_path / "pkg" / "tests" / "test_a.py").write_text("from pkg.sub.mod import x\n")
    (tmp_path / "pkg" / "tests" / "test_b.py").write_text("from pkg.sub import other\n")
    git("init", "-q")
    git("add", ".")
    waves = emit._widen_waves_with_test_referencers([[_row("A", [MOD])]], tmp_path)
    assert waves[0][0].writes == [MOD, "pkg/tests/test_a.py"]
