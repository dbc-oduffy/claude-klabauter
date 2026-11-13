"""Falsifier: the landing set's scoped tests run in a shared clone of the candidate commit (verify stage), so
peer dirt in the worktree neither fails nor greens them; land_run only reads the keyed verdict, landing on green,
pre_existing or missing (the last two with an advisory) and refusing on red, inconclusive or a mismatched key, and
never runs a test itself."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.ops.dispatch_emit.tests.test_ask_ledger_resume_e2e import RUN2, halted  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_ask_run_product_commit import _git, _write, repo  # noqa: F401
from coordinator_core.ops.dispatch_emit.tests.test_land_run_foreign_dirt_in_reach_e2e import (
    FIXTURE,
    LANDING,
    _VERDICT,
    _track_fixture,
)
from coordinator_core.ops.dispatch_emit.tests.test_ledger_remaining_leg_footprint_fence import _terminal_with
from coordinator_core.ops.dispatch_emit.tests.test_reverify_measures_landing_tree_e2e import _stage

pytestmark = [pytest.mark.spawns_process, pytest.mark.complete]

READER = "t/test_reader.py"
_READER_SRC = (
    "import pathlib\n\n\ndef test_reads_data():\n"
    "    assert pathlib.Path(__file__).with_name('data.txt').read_text().strip() == 'ok'\n"
)


def _repo(tmp_path, *, data: str = "ok\n"):
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True, **no_console_creationflags())
    _write(root, READER, _READER_SRC)
    _write(root, "t/data.txt", data)
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "init")
    return root, _git(root, "rev-parse", "HEAD").strip()


def test_peer_dirt_in_the_worktree_does_not_fail_the_overlay(tmp_path):
    root, head = _repo(tmp_path)
    _write(root, "t/data.txt", "peer dirt\n")

    out = cov.verify(root, "run1", head, [READER])

    assert out.verdict == "green", out
    assert not cov.overlay_root(root, "run1").exists()


def test_in_repo_overlay_holder_is_ledgered_for_the_run_end_reap(tmp_path):
    from coordinator_core.ops.dispatch_emit import run_scratch

    root, head = _repo(tmp_path)

    cov.verify(root, "run1", head, [READER])

    assert "scratch/clean-overlay/run1" in run_scratch.read_ledger(root, "run1")["paths"]


def test_out_of_repo_overlay_holder_is_not_ledgered(tmp_path):
    from coordinator_core.ops.dispatch_emit import run_scratch

    root, head = _repo(tmp_path)
    bulk = tmp_path / "bulk"
    (root / "coordinator.local.md").write_text(f'---\nbulk_scratch_dir: "{bulk}"\n---\n', encoding="utf-8")

    cov.verify(root, "run1", head, [READER])

    assert run_scratch.read_ledger(root, "run1")["paths"] == []


def test_a_red_in_the_candidate_is_red_even_when_the_worktree_is_clean(tmp_path):
    root, head = _repo(tmp_path, data="broken\n")

    out = cov.verify(root, "run1", head, [READER])

    assert out.red, out
    assert not cov.overlay_root(root, "run1").exists()


def test_overlay_clone_keeps_the_live_repo_directory_name(tmp_path):
    root, head = _repo(tmp_path)
    _write(
        root, "t/test_name.py",
        "import pathlib\n\n\ndef test_name():\n"
        "    assert pathlib.Path(__file__).resolve().parents[1].name == 'repo'\n",
    )
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "name")
    head = _git(root, "rev-parse", "HEAD").strip()

    out = cov.verify(root, "run-id-not-repo", head, ["t/test_name.py"])

    assert out.verdict == "green", out
    assert not cov.overlay_root(root, "run-id-not-repo").exists()


def _pytest_argv_head(monkeypatch, root, head, pin):
    if pin is not None:
        (root / "coordinator.local.md").write_text(f'---\ntargeted_test_python: "{pin}"\n---\n', encoding="utf-8")
    seen = []
    real = cov._run

    def spy(argv, cwd, timeout, **kw):
        seen.append(argv[0])
        return real([sys.executable, *argv[1:]] if "pytest" in argv else argv, cwd, timeout, **kw)

    monkeypatch.setattr(cov, "_run", spy)
    cov.verify(root, "run1", head, [READER])
    return seen[-1]


def test_overlay_runs_pytest_under_the_repo_pinned_interpreter(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    shim = tmp_path / "pins" / "python3.99"
    shim.parent.mkdir()
    shim.write_text("#!/bin/sh\n", encoding="utf-8")
    shim.chmod(0o755)

    assert _pytest_argv_head(monkeypatch, root, head, shim) == str(shim)


def test_overlay_falls_back_to_sys_executable_when_no_pin_or_the_pin_is_unresolved(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    assert _pytest_argv_head(monkeypatch, root, head, None) == sys.executable
    assert _pytest_argv_head(monkeypatch, root, head, tmp_path / "missing" / "python3.99") == sys.executable


def test_the_overlay_checkout_runs_under_the_overlay_bound_not_the_2s_plumbing_budget(tmp_path, monkeypatch):
    """A full-tree checkout under run_git's 2s budget timed out on the live repo: rc -1, empty stderr."""
    root, head = _repo(tmp_path)
    seen = []
    real = cov._run

    def spy(argv, cwd, timeout, **kw):
        seen.append((argv[1:2], timeout))
        return real(argv, cwd, timeout, **kw)

    monkeypatch.setattr(cov, "_run", spy)
    assert cov.verify(root, "run1", head, [READER]).verdict == "green"
    assert (["checkout"], cov.TEST_TIMEOUT_S) in seen


def _pytest_calls(monkeypatch, tmp_path, run_ids):
    from coordinator_core.testing import scoped_pytest

    monkeypatch.setattr(
        scoped_pytest.machine_resolver, "registry_get",
        lambda key: str(tmp_path / "ptbase") if key == scoped_pytest.RUN_ROOT_KEY else None,
    )
    root, head = _repo(tmp_path)
    calls = []
    real = cov._run

    def spy(argv, cwd, timeout, **kw):
        if "pytest" in argv:
            calls.append((argv, kw.get("env")))
        return real(argv, cwd, timeout, **kw)

    monkeypatch.setattr(cov, "_run", spy)
    for run_id in run_ids:
        assert cov.verify(root, run_id, head, [READER]).verdict == "green"
    return calls


def test_each_overlay_run_gets_its_own_basetemp_with_matching_tmp_under_the_registry_root(tmp_path, monkeypatch):
    (argv_a, env_a), (argv_b, env_b) = _pytest_calls(monkeypatch, tmp_path, ["run-a", "run-b"])
    base_a, base_b = (Path(a[a.index("--basetemp") + 1]) for a in (argv_a, argv_b))
    assert base_a == tmp_path / "ptbase" / "run-a" / "bt" and base_b == tmp_path / "ptbase" / "run-b" / "bt"
    assert Path(env_a["TEMP"]) == base_a.parent / "tmp" and env_a["TMP"] == env_a["TEMP"]
    assert Path(env_b["TEMP"]) == base_b.parent / "tmp"
    assert not base_a.parent.exists() and not base_b.parent.exists()


def test_overlay_without_a_registry_root_falls_back_to_the_temp_layout_never_pytest_of_user(tmp_path, monkeypatch):
    from coordinator_core import temp_layout
    from coordinator_core.testing import scoped_pytest

    monkeypatch.setattr(scoped_pytest.machine_resolver, "registry_get", lambda key: None)
    root, head = _repo(tmp_path)
    seen = []
    real = cov._run

    def spy(argv, cwd, timeout, **kw):
        if "pytest" in argv:
            seen.append(Path(argv[argv.index("--basetemp") + 1]))
        return real(argv, cwd, timeout, **kw)

    monkeypatch.setattr(cov, "_run", spy)
    assert cov.verify(root, "run-a", head, [READER]).verdict == "green"
    assert seen[0].parent.parent == temp_layout.pytest_basetemp_root(root)
    assert "pytest-of-" not in str(seen[0])


_PASSED_THEN_SESSIONFINISH_ERROR = (
    "..                                [100%]\n"
    "INTERNALERROR> Traceback (most recent call last):\n"
    "INTERNALERROR>   File \"_pytest/tmpdir.py\", line 1, in pytest_sessionfinish\n"
    "INTERNALERROR>     cleanup_dead_symlinks(root)\n"
    "INTERNALERROR> PermissionError: [WinError 5] Access is denied: 'pytest-current'\n"
    "2 passed in 0.10s\n"
)


@pytest.mark.parametrize("rc", [1, 3])
def test_a_sessionfinish_error_after_every_test_passed_is_green_and_keeps_the_error(tmp_path, monkeypatch, rc):
    root, head = _repo(tmp_path)
    real = cov._run

    def fake(argv, cwd, timeout, **kw):
        if "pytest" in argv:
            return subprocess.CompletedProcess(argv, rc, _PASSED_THEN_SESSIONFINISH_ERROR, "")
        return real(argv, cwd, timeout, **kw)

    monkeypatch.setattr(cov, "_run", fake)
    out = cov.verify(root, "run1", head, [READER])
    assert out.verdict == "green" and "PermissionError" in out.detail


@pytest.mark.parametrize(
    "rc,text",
    [
        (1, "F.   [100%]\\nFAILED t/test_x.py::test_a\\n1 failed, 1 passed in 0.1s\\n"),
        (1, _PASSED_THEN_SESSIONFINISH_ERROR + "FAILED t/test_x.py::test_a\\n1 failed, 2 passed in 0.1s\\n"),
        (1, "ERROR t/test_x.py::test_a\\n1 passed, 1 error in 0.1s\\n"),
        (3, "INTERNALERROR> Traceback\\nINTERNALERROR> KeyError: x\\n2 passed in 0.1s\\n"),
        (2, _PASSED_THEN_SESSIONFINISH_ERROR),
        (1, "PermissionError in pytest_sessionfinish\\n"),
    ],
)
def test_a_failed_test_or_a_non_teardown_internal_error_stays_red_or_inconclusive(rc, text):
    assert not cov.teardown_only_error(rc, text.replace("\\n", "\n"))


def test_a_real_failed_test_is_still_red(tmp_path):
    root, head = _repo(tmp_path, data="bad\n")
    assert cov.verify(root, "run1", head, [READER]).verdict == "red"


_SECOND = "t/test_second.py"


def _two_commits(tmp_path, base_data: str, head_data: str, second_data: str = "ok\n"):
    """Base commit holds `base_data`; the candidate commit holds `head_data` and a second reader test."""
    root, base = _repo(tmp_path, data=base_data)
    _write(root, "t/data.txt", head_data)
    _write(root, _SECOND, _READER_SRC.replace("test_reads_data", "test_reads_second").replace("data.txt", "second.txt"))
    _write(root, "t/second.txt", second_data)
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "candidate")
    return root, base, _git(root, "rev-parse", "HEAD").strip()


def test_a_red_that_fails_identically_at_base_is_pre_existing_and_names_the_ids(tmp_path):
    root, base, head = _two_commits(tmp_path, "broken\n", "broken\n")

    out = cov.verify(root, "run1", head, [READER, _SECOND], base_sha=base)

    assert out.verdict == "pre_existing", out
    assert f"{READER}::test_reads_data" in out.detail
    assert not cov.overlay_root(root, "run1").exists()


def test_a_red_that_passes_at_base_stays_red(tmp_path):
    root, base, head = _two_commits(tmp_path, "ok\n", "broken\n")

    assert cov.verify(root, "run1", head, [READER, _SECOND], base_sha=base).verdict == "red"


def test_a_mix_of_base_failing_and_newly_failing_ids_stays_red(tmp_path):
    root, base, head = _two_commits(tmp_path, "broken\n", "broken\n", second_data="bad\n")

    out = cov.verify(root, "run1", head, [READER, _SECOND], base_sha=base)

    assert out.verdict == "red", out


def test_a_red_with_no_base_sha_stays_red(tmp_path):
    root, _base, head = _two_commits(tmp_path, "broken\n", "broken\n")

    assert cov.verify(root, "run1", head, [READER]).verdict == "red"


def test_pre_existing_is_a_readable_verdict_that_is_not_red(tmp_path):
    root, head = _repo(tmp_path)
    measured = {"blobs": {"a": "1"}, "base_sha": "b" * 40, "sha": head}
    cov.write_verdict(root, "run1", measured, cov.OverlayVerdict("pre_existing", "ids", ("t/x.py",)))

    out = cov.check_verdict(root, "run1", measured)

    assert out.verdict == "pre_existing" and not out.red


def test_a_timed_out_overlay_checkout_is_named_with_its_budget_never_silent(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)

    def slow(argv, cwd, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(cov, "_run", slow)
    out = cov.verify(root, "run1", head, [READER])
    assert out.verdict == "inconclusive"
    assert "TimeoutExpired" in out.detail and f"{cov.TEST_TIMEOUT_S} seconds" in out.detail, out.detail


def _clone_dests(monkeypatch):
    dests = []
    real = cov.run_git

    def spy(args, **kw):
        if args[:1] == ["clone"]:
            dests.append(args[-1])
        return real(args, **kw)

    monkeypatch.setattr(cov, "run_git", spy)
    return dests


def test_a_configured_bulk_location_receives_the_overlay_clone_and_is_reaped(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    bulk = tmp_path / "bulk"
    (root / "coordinator.local.md").write_text(f'---\nbulk_scratch_dir: "{bulk}"\n---\n', encoding="utf-8")
    dests = _clone_dests(monkeypatch)

    out = cov.verify(root, "run1", head, [READER])

    assert out.verdict == "green", out
    assert dests == [str(bulk / "clean-overlay" / "run1" / root.resolve().name)]
    assert not (root / "scratch").exists()
    assert not (bulk / "clean-overlay" / "run1").exists()


def test_unconfigured_or_relative_bulk_falls_back_to_repo_scratch(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    expected = str(root / "scratch" / "clean-overlay" / "run1" / root.resolve().name)
    dests = _clone_dests(monkeypatch)

    assert cov.verify(root, "run1", head, [READER]).verdict == "green"
    (root / "coordinator.local.md").write_text("---\nbulk_scratch_dir: rel/dir\n---\n", encoding="utf-8")
    assert cov.verify(root, "run1", head, [READER]).verdict == "green"

    assert dests == [expected, expected]
    assert not cov.overlay_root(root, "run1").exists()


def test_a_bulk_overlay_is_reaped_when_the_candidate_is_unreachable(tmp_path):
    root, head = _repo(tmp_path)
    bulk = tmp_path / "bulk"
    (root / "coordinator.local.md").write_text(f'---\nbulk_scratch_dir: "{bulk}"\n---\n', encoding="utf-8")

    assert cov.verify(root, "run1", "0" * 40, [READER]).verdict == "inconclusive"
    assert not cov.overlay_root(root, "run1").exists()


def test_no_seeds_and_an_unreachable_candidate_are_inconclusive_never_red(tmp_path):
    root, head = _repo(tmp_path)

    assert cov.verify(root, "run1", head, []).verdict == "inconclusive"
    assert cov.verify(root, "run1", "0" * 40, [READER]).verdict == "inconclusive"
    assert not cov.overlay_root(root, "run1").exists()


def _no_spawn(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("land_run must not run the overlay verify")

    monkeypatch.setattr(cov, "verify", boom)
    monkeypatch.setattr(cov, "_run", boom)


def _verdict(root, measured, verdict, **over):
    cov.write_verdict(root, RUN2, {**measured, **over}, cov.OverlayVerdict(verdict, "boom", ("t/x.py",)))


def _dirty_land(halted, verdict, *, mutate=None):
    _track_fixture(halted)
    resume, token = _stage(halted)
    measured = mr.measured_rev(RUN2, LANDING, repo_root=halted)
    if verdict:
        _verdict(halted, measured, verdict, **(mutate or {}))
    _write(halted, FIXTURE, '{"a": 9, "dirty": true}\n')
    return measured, _terminal_with(halted, token, resume, _VERDICT)


def test_land_run_lands_on_a_green_verdict_despite_foreign_dirt(halted, monkeypatch):  # noqa: F811
    _no_spawn(monkeypatch)

    _, result = _dirty_land(halted, "green")

    assert result["committed"] is True, result
    assert FIXTURE in result["reach_dirt_foreign"]
    assert result["clean_overlay_verify"]["verdict"] == "green"
    assert FIXTURE in _git(halted, "status", "--porcelain", "--", FIXTURE)


def test_land_run_refuses_a_red_verdict(halted, monkeypatch):  # noqa: F811
    _no_spawn(monkeypatch)

    _, result = _dirty_land(halted, "red")

    assert result["committed"] is False
    assert result["refused"] == "verify-overlay-red", result
    assert _git(halted, "log", "-1", "--format=%s").strip() == "fixture"


def test_land_run_refuses_an_inconclusive_verdict(halted, monkeypatch):  # noqa: F811
    _no_spawn(monkeypatch)

    _, result = _dirty_land(halted, "inconclusive")

    assert result["committed"] is False
    assert result["refused"] == "verify-overlay-unverified", result
    assert "inconclusive" in result["error"], result
    assert _git(halted, "log", "-1", "--format=%s").strip() == "fixture"
    assert FIXTURE in result["reach_dirt_foreign"]


def test_land_run_lands_a_pre_existing_verdict_with_an_advisory_trailer(halted, monkeypatch):  # noqa: F811
    _no_spawn(monkeypatch)

    _, result = _dirty_land(halted, "pre_existing")

    assert result["committed"] is True, result
    assert not result.get("refused")
    assert result["clean_overlay_verify"]["verdict"] == "pre_existing"
    assert "pre_existing" in result["clean_overlay_verify"]["advisory"]
    assert _git(halted, "log", "-1", "--format=%(trailers:key=Overlay,valueonly)").strip().startswith("pre_existing"), _git(halted, "log", "-1", "--format=%B")


def test_land_run_reverifies_a_mismatched_verdict_and_lands_when_pre_existing(halted, monkeypatch):  # noqa: F811
    _reverify_spy(monkeypatch, "pre_existing")

    _, result = _dirty_land(halted, "green", mutate={"blobs": {"x": "0" * 40}})

    assert result["committed"] is True, result
    assert result["clean_overlay_verify"]["verdict"] == "pre_existing"


def test_land_run_lands_a_missing_verdict_with_an_advisory_trailer(halted, monkeypatch):  # noqa: F811
    _no_spawn(monkeypatch)

    _, result = _dirty_land(halted, None)

    assert result["committed"] is True, result
    assert not result.get("refused")
    assert result["clean_overlay_verify"]["verdict"] == "missing"
    assert "no overlay verdict was written" in result["clean_overlay_verify"]["advisory"]
    assert FIXTURE in result["reach_dirt_foreign"]
    assert _git(halted, "log", "-1", "--format=%(trailers:key=Overlay,valueonly)").strip().startswith("missing — no overlay verdict"), _git(halted, "log", "-1", "--format=%B")


@pytest.mark.parametrize("field", ["blobs", "base_sha"])
def test_land_run_refuses_a_verdict_keyed_to_another_landing_set_or_head(halted, monkeypatch, field):  # noqa: F811
    _no_spawn(monkeypatch)
    _track_fixture(halted)
    measured = mr.measured_rev(RUN2, LANDING, repo_root=halted)
    other = {"blobs": {"x": "0" * 40}} if field == "blobs" else {"base_sha": "1" * 40}
    assert cov.check_verdict(halted, RUN2, measured).verdict == "missing"
    _verdict(halted, measured, "green", **other)

    out = cov.check_verdict(halted, RUN2, measured)

    assert out.verdict == "mismatch", out


@pytest.mark.parametrize("field", ["blobs", "base_sha"])
def test_land_run_refuses_a_mismatched_verdict_when_the_reverify_is_inconclusive(halted, monkeypatch, field):  # noqa: F811
    calls = _reverify_spy(monkeypatch, None)
    other = {"blobs": {"x": "0" * 40}} if field == "blobs" else {"base_sha": "1" * 40}

    _, result = _dirty_land(halted, "green", mutate=other)

    assert result["committed"] is False
    assert result["refused"] == "verify-overlay-unverified", result
    assert len(calls) == 1


def _reverify_spy(monkeypatch, verdict):
    """Stand in for the detached re-verify land_run reaches through `reverify_on_mismatch`; `verdict`
    None models a re-verify that never produced a green/red verdict (the stale mismatch comes back)."""
    calls = []

    def fake(root, run, measured, stale, **_):
        calls.append((run, cov.overlay_candidate(measured), tuple(stale.seeds)))
        return stale if verdict is None else cov.OverlayVerdict(verdict, "reverified", tuple(stale.seeds))

    monkeypatch.setattr(cov, "reverify_on_mismatch", fake)
    monkeypatch.setattr(cov, "verify", lambda *a, **k: (_ for _ in ()).throw(AssertionError("land_run must not verify in-process")))
    return calls


@pytest.mark.parametrize("field", ["blobs", "base_sha"])
def test_land_run_reverifies_a_mismatched_verdict_and_lands_when_green(halted, monkeypatch, field):  # noqa: F811
    calls = _reverify_spy(monkeypatch, "green")
    other = {"blobs": {"x": "0" * 40}} if field == "blobs" else {"base_sha": "1" * 40}

    measured, result = _dirty_land(halted, "green", mutate=other)

    assert result["committed"] is True, result
    assert not result.get("refused")
    assert result["clean_overlay_verify"]["verdict"] == "green"
    assert calls == [(RUN2, cov.overlay_candidate(measured), ("t/x.py",))]


def test_land_run_reverifies_a_mismatched_verdict_and_refuses_red(halted, monkeypatch):  # noqa: F811
    calls = _reverify_spy(monkeypatch, "red")

    _, result = _dirty_land(halted, "green", mutate={"blobs": {"x": "0" * 40}})

    assert result["committed"] is False
    assert result["refused"] == "verify-overlay-red", result
    assert len(calls) == 1
    assert _git(halted, "log", "-1", "--format=%s").strip() == "fixture"


def test_a_mismatch_with_no_recorded_seeds_is_not_reverified(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(cov, "verify", lambda *a, **k: calls.append(a))
    stale = cov.OverlayVerdict("mismatch", "keyed to a different landing set blobs", ())

    out = cov.reverify_on_mismatch(tmp_path, "run1", {"sha": "a" * 40}, stale)

    assert out is stale and calls == []


@pytest.mark.parametrize("fresh,expect", [("green", "green"), ("red", "red"), ("inconclusive", "mismatch")])
def test_reverify_spawns_detached_and_polls_the_verdict_file(tmp_path, fresh, expect):
    measured = {"sha": "a" * 40, "base_sha": "b" * 40, "blobs": {"x": "1" * 40}}
    stale = cov.OverlayVerdict("mismatch", "keyed to a different landing set blobs", ("t/x.py",))
    spawned = []

    def spawn(engine, script, argv):
        spawned.append(argv)
        cov.write_verdict(tmp_path, "run1", measured, cov.OverlayVerdict(fresh, "d", ("t/x.py",)))
        return True

    out = cov.reverify_on_mismatch(tmp_path, "run1", measured, stale, spawn=spawn, sleep=lambda s: None)

    assert out.verdict == expect
    assert len(spawned) == 1 and spawned[0][:2] == ["--run-id", "run1"]


def test_reverify_gives_up_at_the_wait_bound_and_returns_the_stale_verdict(tmp_path):
    measured = {"sha": "a" * 40, "base_sha": "b" * 40, "blobs": {"x": "1" * 40}}
    stale = cov.OverlayVerdict("mismatch", "keyed to a different landing set blobs", ("t/x.py",))
    now = [0.0]

    def sleep(s):
        now[0] += s

    out = cov.reverify_on_mismatch(
        tmp_path, "run1", measured, stale, spawn=lambda *a: True, wait_s=3.0, sleep=sleep, clock=lambda: now[0],
    )

    assert out is stale and now[0] >= 3.0


def test_no_foreign_dirt_needs_no_verdict_and_runs_no_overlay(halted, monkeypatch):  # noqa: F811
    _no_spawn(monkeypatch)
    _track_fixture(halted)
    resume, token = _stage(halted)
    mr.measured_rev(RUN2, LANDING, repo_root=halted)

    result = _terminal_with(halted, token, resume, _VERDICT)

    assert result["committed"] is True, result


def test_cli_writes_a_verdict_land_run_reads(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    run = "warp-2026-11-01-cli-abc123"
    record = cov.measured_rev_record_path(root, run)
    record.parent.mkdir(parents=True, exist_ok=True)
    measured = {"sha": head, "base_sha": head, "paths": [READER], "blobs": {READER: "b"}}
    record.write_text(json.dumps(measured), encoding="utf-8")
    _write(root, "t/data.txt", "peer dirt\n")

    assert cov.main(["--run-id", run, "--repo-root", str(root)]) == 0

    assert cov.check_verdict(root, run, measured).verdict == "green"
    assert not cov.overlay_root(root, run).exists()


def test_reading_the_verdict_stays_inside_the_brightline(tmp_path):
    measured = {"sha": "a" * 40, "base_sha": "b" * 40, "blobs": {"p": "c" * 40}}
    cov.write_verdict(tmp_path, RUN2, measured, cov.OverlayVerdict("green", "", ("t.py",)))
    start = time.process_time()
    for _ in range(20):
        assert cov.check_verdict(tmp_path, RUN2, measured).verdict == "green"
    assert (time.process_time() - start) / 20 < 0.05


def _peer_dirty(tmp_path, monkeypatch, *, splittable: bool):
    """READER passes at HEAD ('ok'); the worktree data.txt carries a peer's hunk that breaks it."""
    from coordinator_core.ops.dispatch_emit import hunk_provenance as hp

    root, head = _repo(tmp_path)
    _write(root, "t/data.txt", "peer dirt\n")
    ok = _git(root, "rev-parse", f"{head}:t/data.txt").strip()
    if splittable:
        found = ({"t/data.txt": {"blob": ok, "owner": "run-peer", "lines": "1"}}, {})
    else:
        found = ({}, {"t/data.txt": {"owner": "run-peer", "lines": "1"}})
    monkeypatch.setattr(hp, "peer_split", lambda root, run, paths, **k: found)
    run = "warp-2026-11-12-peer-abc123"
    measured = mr.measured_rev(run, ["t/data.txt", READER], repo_root=root)
    return root, run, measured


def test_overlay_over_the_split_candidate_is_green_where_the_whole_file_candidate_is_red(tmp_path, monkeypatch):
    root, run, measured = _peer_dirty(tmp_path, monkeypatch, splittable=True)

    assert cov.verify(root, run, measured["sha"], [READER]).red
    assert cov.main(["--run-id", run, "--repo-root", str(root)]) == 0

    verdict = cov.check_verdict(root, run, measured)
    assert verdict.verdict == "green"
    assert "run-peer" in verdict.detail and "split out" in verdict.detail


def test_an_unsplittable_peer_hunk_turns_the_red_into_an_owner_naming_inconclusive(tmp_path, monkeypatch):
    root, run, measured = _peer_dirty(tmp_path, monkeypatch, splittable=False)

    assert cov.main(["--run-id", run, "--repo-root", str(root)]) == 2

    verdict = cov.check_verdict(root, run, measured)
    assert verdict.verdict == "inconclusive" and not verdict.red
    assert "run-peer" in verdict.detail and "t/data.txt" in verdict.detail


def test_a_red_with_no_peer_hunk_recorded_stays_red(tmp_path):
    root, head = _repo(tmp_path, data="bad\n")
    measured = {"sha": head, "base_sha": head, "paths": [READER], "blobs": {}}

    out = cov.name_peer_owners(cov.verify(root, "run1", head, [READER]), measured)

    assert out.red and "peer" not in out.detail


def _green_over(root, measured):
    cov.write_verdict(root, "run1", measured, cov.OverlayVerdict("green", "ok", ()))


def test_measured_rev_records_the_touched_paths_it_measured(tmp_path):
    root, _head = _repo(tmp_path)
    _write(root, READER, _READER_SRC + "\n# edited\n")

    measured = mr.measured_rev("run1", [READER], repo_root=root)

    assert json.loads(mr.touched_record_path(root, "run1").read_text(encoding="utf-8")) == [READER]
    assert "touched" not in measured
    _green_over(root, measured)
    assert cov.check_verdict(root, "run1", measured).verdict == "green"


def test_check_verdict_reads_the_recorded_touched_set_when_the_record_carries_none(tmp_path):
    root, head = _repo(tmp_path)
    measured = {"sha": head, "base_sha": head, "blobs": {READER: "a" * 40}, "removed": []}
    _green_over(root, measured)
    path = mr.touched_record_path(root, "run1")
    path.write_text(json.dumps([READER, "t/data.txt"]), encoding="utf-8")

    out = cov.check_verdict(root, "run1", measured)

    assert out.verdict == "mismatch" and "t/data.txt" in out.detail, out


def test_a_landing_set_omitting_a_run_touched_path_is_a_mismatch_naming_it(tmp_path):
    root, head = _repo(tmp_path)
    measured = {
        "sha": head, "base_sha": head, "blobs": {READER: "a" * 40}, "removed": [],
        "touched": [READER, "t/data.txt"],
    }
    _green_over(root, {**measured, "touched": [READER]})

    out = cov.check_verdict(root, "run1", measured)

    assert out.verdict == "mismatch", out
    assert "t/data.txt" in out.detail and READER not in out.detail


def test_a_removed_touched_path_and_a_record_without_touched_are_not_leftovers(tmp_path):
    root, head = _repo(tmp_path)
    base = {"sha": head, "base_sha": head, "blobs": {READER: "a" * 40}, "removed": ["t/gone.txt"]}
    _green_over(root, base)

    assert cov.check_verdict(root, "run1", {**base, "touched": [READER, "t/gone.txt"]}).verdict == "green"
    assert cov.check_verdict(root, "run1", base).verdict == "green"
