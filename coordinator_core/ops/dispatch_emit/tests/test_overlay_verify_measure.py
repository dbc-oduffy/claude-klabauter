"""overlay_verify_measure records child process time and spawn count per verify leg (the bounded figures),
beside wall_ms and files_written recorded only to tell IO wait apart, and `verify` enters the leg hook around
exactly its clone (cold mirror only), checkout, pytest and base-rerun legs."""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import contextmanager

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import overlay_verify_measure as ovm
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

_PASSING = "t/test_ok.py"
_FAILING = "t/test_bad.py"


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, **no_console_creationflags())
    _write(root, _PASSING, "def test_ok():\n    assert True\n")
    _write(root, _FAILING, "def test_bad():\n    assert False\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return root, _git(root, "rev-parse", "HEAD").strip()


def _recording():
    seen: list = []

    @contextmanager
    def leg(name):
        seen.append(name)
        yield {}

    return seen, leg


def test_meter_records_cpu_and_spawns_of_a_real_child_and_wall_time_apart():
    with ovm.LegMeter() as meter:
        with meter("burn"):
            subprocess.run(
                [sys.executable, "-c", "sum(i * i for i in range(3_000_000))"], check=True, **no_console_creationflags(),
            )
    (rec,) = meter.records
    assert rec["leg"] == "burn"
    assert rec["spawns"] >= 1
    assert rec["process_ms"] > 0
    assert rec["wall_ms"] > 0 and rec["files_written"] == 0


def test_meter_with_no_child_records_zero_spawns_and_the_files_a_leg_reports():
    with ovm.LegMeter() as meter:
        with meter("idle"):
            pass
        with meter("write") as extra:
            extra["files_written"] = 7
    assert meter.records[0]["spawns"] == 0
    assert meter.records[1]["files_written"] == 7


def test_verify_enters_the_hook_for_clone_checkout_pytest_in_order_on_a_cold_mirror_only(tmp_path):
    root, head = _repo(tmp_path)
    seen, leg = _recording()

    out = cov.verify(root, "r1", head, [_PASSING], leg=leg)

    assert out.verdict == "green", out
    assert seen == ["clone", "checkout", "pytest"]

    seen.clear()
    assert cov.verify(root, "r1b", head, [_PASSING], leg=leg).verdict == "green"
    assert seen == ["checkout", "pytest"]


def test_verify_enters_base_rerun_only_for_a_red_with_a_distinct_base(tmp_path):
    root, head = _repo(tmp_path)
    _write(root, "t/extra.txt", "x\n")
    _git(root, "add", "t/extra.txt")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "second")
    tip = _git(root, "rev-parse", "HEAD").strip()
    seen, leg = _recording()

    red = cov.verify(root, "r2", tip, [_FAILING], base_sha=head, leg=leg)
    assert seen == ["clone", "checkout", "pytest", "base_rerun"]
    assert red.verdict == "pre_existing", red

    seen.clear()
    cov.verify(root, "r3", tip, [_FAILING], base_sha=None, leg=leg)
    assert "base_rerun" not in seen


def test_measure_reports_every_leg_with_numbers_for_a_ran_leg_and_a_total(tmp_path):
    root, head = _repo(tmp_path)

    result = ovm.measure(root, [_PASSING], candidate_sha=head)

    assert result["verdict"] == "green"
    assert [leg["leg"] for leg in result["legs"]] == list(ovm.LEGS)
    ran = {leg["leg"]: leg for leg in result["legs"]}
    for name in ("clone", "checkout", "pytest"):
        assert ran[name]["ran"] is True
        assert ran[name]["spawns"] >= 1
        assert ran[name]["process_ms"] is not None
        assert ran[name]["wall_ms"] > 0
    assert ran["checkout"]["files_written"] == 2
    assert ran["base_rerun"]["ran"] is False
    total = result["total"]
    assert total["total_spawns"] == sum(ran[n]["spawns"] for n in ("clone", "checkout", "pytest"))
    assert total["total_wall_ms"] > 0 and total["total_files_written"] == 2


def test_a_warm_measure_skips_the_clone_leg_and_writes_no_file_and_cold_rebuilds(tmp_path):
    root, head = _repo(tmp_path)
    ovm.measure(root, [_PASSING], candidate_sha=head)

    warm = {leg["leg"]: leg for leg in ovm.measure(root, [_PASSING], candidate_sha=head)["legs"]}
    assert warm["clone"]["ran"] is False
    assert warm["checkout"]["files_written"] == 0

    cold = {leg["leg"]: leg for leg in ovm.measure(root, [_PASSING], candidate_sha=head, cold=True)["legs"]}
    assert cold["clone"]["ran"] is True and cold["checkout"]["files_written"] == 2


def test_the_cli_prints_one_json_line_per_leg_then_the_verdict_with_totals(tmp_path, capsys):
    root, _head = _repo(tmp_path)

    assert ovm.main(["--repo-root", str(root), "--seeds", _PASSING, "--cold"]) == 0

    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [line["leg"] for line in lines[: len(ovm.LEGS)]] == list(ovm.LEGS)
    assert lines[-1]["verdict"] == "green" and "total_wall_ms" in lines[-1] and "total_files_written" in lines[-1]


def test_the_mirror_leaves_the_run_artifact_trees_unchecked_out_and_keeps_the_rest(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, **no_console_creationflags())
    _write(root, "state/warp/run1/x.json", "{}\n")
    _write(root, "state/handoffs/h.md", "h\n")
    _write(root, "pkg/mod.py", "X = 1\n")
    _write(root, _PASSING, "def test_ok():\n    assert True\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")

    with cov.clean_export(root, "run1", []) as dest:
        assert not (dest / "state" / "warp").exists()
        assert (dest / "state" / "handoffs" / "h.md").is_file()
        assert (dest / "pkg" / "mod.py").is_file()
