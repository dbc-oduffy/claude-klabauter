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


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    """The persistent mirror lives under the system Temp's coordinator root; every test gets its own."""
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


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
    assert (root / "t" / "data.txt").read_text() == "peer dirt\n"


def test_the_mirror_lives_under_the_managed_temp_root_outside_the_repo_and_survives_the_verify(tmp_path):
    from coordinator_core import temp_layout

    root, head = _repo(tmp_path)

    assert cov.verify(root, "run1", head, [READER]).verdict == "green"

    mirror = cov.mirror_root(root)
    assert mirror.parent == temp_layout.coordinator_temp_root(root) / cov.MIRROR_DIRNAME
    assert mirror.is_dir() and (mirror / root.name / ".git").exists()
    assert root not in mirror.parents and not (root / "scratch").exists()


def test_a_configured_bulk_location_does_not_move_the_mirror(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    bulk = tmp_path / "bulk"
    (root / "coordinator.local.md").write_text(f'---\nbulk_scratch_dir: "{bulk}"\n---\n', encoding="utf-8")
    dests = _clone_dests(monkeypatch)

    assert cov.verify(root, "run1", head, [READER]).verdict == "green"

    assert dests == [str(cov.mirror_root(root) / root.name)] and not bulk.exists()


def test_the_mirror_name_is_not_a_managed_temp_shape_and_a_reap_pass_spares_it(tmp_path):
    from coordinator_core import temp_layout

    root, head = _repo(tmp_path)
    assert cov.verify(root, "run1", head, [READER]).verdict == "green"
    assert temp_layout._MANAGED_DIR_RE.fullmatch(cov.MIRROR_DIRNAME) is None
    dead = temp_layout.managed_tempdir(root) / temp_layout.RUN_TEMP_DIRNAME / "99999999-abcdef-dead"
    dead.mkdir(parents=True)

    temp_layout._reap_dead_owner_dirs(dead.parent)

    assert not dead.exists()
    assert (cov.mirror_root(root) / root.name / ".git").exists()
    assert cov.verify(root, "run2", head, [READER]).verdict == "green"


def test_a_red_in_the_candidate_is_red_even_when_the_worktree_is_clean(tmp_path):
    root, head = _repo(tmp_path, data="broken\n")

    out = cov.verify(root, "run1", head, [READER])

    assert out.red, out


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
    assert cov.mirror_root(root).exists()
    assert _pytest_argv_head(monkeypatch, root, head, tmp_path / "missing" / "python3.99") == sys.executable


def test_the_mirror_fill_runs_under_the_overlay_bound_not_the_2s_plumbing_budget(tmp_path, monkeypatch):
    """A full-tree checkout under run_git's 2s budget timed out on the live repo: rc -1, empty stderr."""
    root, head = _repo(tmp_path)
    seen = []
    real = cov._run

    def spy(argv, cwd, timeout, **kw):
        seen.append((argv[1:2], timeout))
        return real(argv, cwd, timeout, **kw)

    monkeypatch.setattr(cov, "_run", spy)
    assert cov.verify(root, "run1", head, [READER]).verdict == "green"
    assert (["read-tree"], cov.TEST_TIMEOUT_S) in seen
    assert not [s for s in seen if s[0] == ["checkout"]]


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
    assert argv_a[argv_a.index("--rootdir") + 1] == str(cov.mirror_root(tmp_path / "repo") / "repo")


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
    assert cov.verify(root, "run2", head, [READER, _SECOND]).verdict == "red"


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
    assert f"timed out after {cov.TEST_TIMEOUT_S}s" in out.detail and f"{cov.TEST_TIMEOUT_S} seconds" in out.detail, out.detail


def _clone_dests(monkeypatch):
    dests = []
    real = cov.run_git

    def spy(args, **kw):
        if args[:1] == ["clone"]:
            dests.append(args[-1])
        return real(args, **kw)

    monkeypatch.setattr(cov, "run_git", spy)
    return dests


def test_no_seeds_and_an_unreachable_candidate_are_inconclusive_never_red(tmp_path):
    root, head = _repo(tmp_path)

    assert cov.verify(root, "run1", head, []).verdict == "inconclusive"
    assert cov.verify(root, "run1", "0" * 40, [READER]).verdict == "inconclusive"
    assert cov.verify(root, "run2", head, [READER]).verdict == "green"


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


_RUN_MOD_TEST = "t/test_run_mod.py"


def _peer_import_repo(tmp_path):
    """HEAD holds only the reader; the run's declared test imports `t/peer_mod.py`, an uncommitted peer file."""
    root, head = _repo(tmp_path)
    _write(root, _RUN_MOD_TEST, "import peer_mod\n\n\ndef test_uses_peer():\n    assert peer_mod.VALUE == 1\n")
    _write(root, "t/peer_mod.py", "VALUE = 1\n")
    return root, head


def _pytest_in(root, export, *ids):
    return cov._run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", *ids], export, 120).returncode


def test_clean_export_carries_head_plus_declared_paths_and_no_other_worktree_dirt(tmp_path):
    root, _head = _peer_import_repo(tmp_path)
    _write(root, "t/data.txt", "peer dirt\n")

    with cov.clean_export(root, "run1", [_RUN_MOD_TEST]) as export:
        assert (export / _RUN_MOD_TEST).is_file()
        assert not (export / "t" / "peer_mod.py").exists()
        assert (export / "t" / "data.txt").read_text().strip() == "ok"
        assert _pytest_in(root, export, _RUN_MOD_TEST) != 0
    assert _pytest_in(root, root, _RUN_MOD_TEST) == 0


def test_a_declared_peer_module_is_in_the_export_and_a_declared_deletion_is_applied(tmp_path):
    root, _head = _peer_import_repo(tmp_path)
    (root / READER).unlink()

    with cov.clean_export(root, "run1", [_RUN_MOD_TEST, "t/peer_mod.py", READER]) as export:
        assert (export / "t" / "peer_mod.py").is_file() and not (export / READER).exists()
        assert _pytest_in(root, export, _RUN_MOD_TEST) == 0


def test_a_later_export_drops_the_previous_runs_overlay_and_a_bad_path_fails_before_any_write(tmp_path):
    root, _head = _peer_import_repo(tmp_path)

    with cov.clean_export(root, "run1", [_RUN_MOD_TEST, "t/peer_mod.py"]) as first:
        assert (first / "t" / "peer_mod.py").is_file()
    with cov.clean_export(root, "run2", [_RUN_MOD_TEST]) as second:
        assert second == first and not (second / "t" / "peer_mod.py").exists()
        assert _pytest_in(root, second, _RUN_MOD_TEST) != 0

    with pytest.raises(cov.ExportError):
        with cov.clean_export(root, "run3", ["../escape.txt"]):
            raise AssertionError("a bad declared path must fail before the body runs")


def _legs(into):
    """A leg hook that records `(name, files_written)` and offers the extra dict a `LegMeter` would."""
    from contextlib import contextmanager

    @contextmanager
    def leg(name):
        extra = {}
        yield extra
        into.append((name, extra.get("files_written")))

    return leg


def test_a_second_verify_reuses_the_mirror_and_writes_only_the_changed_paths(tmp_path, monkeypatch):
    root, base, head = _two_commits(tmp_path, "ok\n", "ok\n\n")
    clones = _clone_dests(monkeypatch)
    legs: list = []

    assert cov.verify(root, "run1", base, [READER], leg=_legs(legs)).verdict == "green"
    cold = dict(legs)
    legs.clear()
    assert cov.verify(root, "run2", head, [READER, _SECOND], leg=_legs(legs)).verdict == "green"
    legs_warm = dict(legs)
    legs.clear()
    assert cov.verify(root, "run3", head, [READER, _SECOND], leg=_legs(legs)).verdict == "green"

    assert len(clones) == 1 and "clone" in cold and "clone" not in legs_warm
    assert cold["checkout"] == 2 and legs_warm["checkout"] == 3
    assert dict(legs)["checkout"] == 0 and "clone" not in dict(legs)


def test_a_warm_advance_writes_the_diff_by_index_info_never_a_tree_walk_or_full_checkout(tmp_path, monkeypatch):
    root, base, head = _two_commits(tmp_path, "ok\n", "ok\n\n")
    assert cov.verify(root, "run1", base, [READER]).verdict == "green"
    seen = []
    real = cov._run

    def spy(argv, cwd, timeout, **kw):
        seen.append(argv[1:])
        return real(argv, cwd, timeout, **kw)

    monkeypatch.setattr(cov, "_run", spy)
    assert cov.verify(root, "run2", head, [READER, _SECOND]).verdict == "green"

    assert any(a[:1] == ["diff-tree"] and a[-2:] == [_git(root, "rev-parse", base + "^{commit}").strip(), head] for a in seen), seen
    assert any(a[:1] == ["update-index"] and "--index-info" in a for a in seen), seen
    assert any(a[:1] == ["checkout-index"] for a in seen), seen
    assert not [a for a in seen if a[:1] in (["read-tree"], ["checkout"]) or "--detach" in a]


def test_a_warm_advance_reproduces_the_target_tree_across_add_modify_delete_mode_and_dir_swaps(tmp_path):
    root, base = _repo(tmp_path)
    mroot = cov.mirror_root(root)
    with cov._mirror_lock(root) as held:
        dest = cov._prepare_guarded(root, held, base, cov._no_leg)
    _write(root, "t/data.txt", "changed\n")
    _write(root, "t/new/deep/file.txt", "new\n")
    _write(root, "archive/hidden.txt", "hidden\n")
    _write(root, "swap", "a file\n")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "second")
    second = _git(root, "rev-parse", "HEAD").strip()
    (root / "swap").unlink()
    _write(root, "swap/inner.txt", "now a dir\n")
    (root / "t" / "test_reader.py").unlink()
    _git(root, "add", "-A")
    _git(root, "update-index", "--chmod=+x", "t/data.txt")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "third")
    third = _git(root, "rev-parse", "HEAD").strip()

    for target in (second, third):
        with cov._mirror_lock(root) as held:
            cov._prepare_guarded(root, held, target, cov._no_leg)
        assert _git(dest, "write-tree").strip() == _git(root, "rev-parse", target + "^{tree}").strip()
        assert _git(dest, "diff", "--stat").strip() == ""

    assert not (dest / "t" / "test_reader.py").exists() and (dest / "swap" / "inner.txt").is_file()
    assert (dest / "t" / "new" / "deep" / "file.txt").read_text() == "new\n"
    assert not (dest / "archive").exists()
    assert cov._state_read(mroot) == third


def test_a_second_export_writes_only_the_overlaid_paths_that_differ(tmp_path):
    root, _head = _peer_import_repo(tmp_path)
    legs: list = []

    declared = [_RUN_MOD_TEST, "t/peer_mod.py"]
    with cov.clean_export(root, "run1", declared, leg=_legs(legs)) as export:
        untouched = (export / READER).stat().st_mtime_ns
    assert dict(legs)["checkout"] == 2 + 2
    legs.clear()
    with cov.clean_export(root, "run2", declared, leg=_legs(legs)) as export:
        assert (export / READER).stat().st_mtime_ns == untouched
    assert dict(legs)["checkout"] == 2 + 2 and "clone" not in dict(legs)
    legs.clear()
    _write(root, "t/peer_mod.py", "VALUE = 22\n")
    with cov.clean_export(root, "run3", declared, leg=_legs(legs)) as export:
        assert (export / "t" / "peer_mod.py").read_text() == "VALUE = 22\n"
        assert (export / READER).stat().st_mtime_ns == untouched
    assert dict(legs)["checkout"] == 2 + 2


def test_a_mirror_that_cannot_advance_is_rebuilt_once_and_the_verify_still_runs(tmp_path, monkeypatch):
    root, base, head = _two_commits(tmp_path, "ok\n", "ok\n\n")
    clones = _clone_dests(monkeypatch)
    assert cov.verify(root, "run1", base, [READER]).verdict == "green"
    cov._state_write(cov.mirror_root(root), "0" * 40)

    assert cov.verify(root, "run2", head, [READER, _SECOND]).verdict == "green"

    assert len(clones) == 2


def test_a_mirror_that_stays_unusable_after_one_rebuild_fails_naming_the_cause(tmp_path, monkeypatch):
    root, base, head = _two_commits(tmp_path, "ok\n", "ok\n\n")
    assert cov.verify(root, "run1", base, [READER]).verdict == "green"
    clones = _clone_dests(monkeypatch)
    real = cov._run

    def broken(argv, cwd, timeout, **kw):
        if argv[1] in ("diff-tree", "read-tree"):
            return subprocess.CompletedProcess(argv, 128, "", "fatal: index is corrupt")
        return real(argv, cwd, timeout, **kw)

    monkeypatch.setattr(cov, "_run", broken)
    out = cov.verify(root, "run2", head, [READER, _SECOND])

    assert out.verdict == "inconclusive" and "index is corrupt" in out.detail, out
    assert len(clones) == 1


def test_an_interrupted_advance_leaves_the_mirror_marked_and_the_next_verify_rebuilds_it(tmp_path, monkeypatch):
    root, base, head = _two_commits(tmp_path, "ok\n", "ok\n\n")
    assert cov.verify(root, "run1", base, [READER]).verdict == "green"
    real = cov._run

    def timeout_on_update_index(argv, cwd, timeout, **kw):
        if argv[1:2] == ["update-index"]:
            raise subprocess.TimeoutExpired(argv, timeout)
        return real(argv, cwd, timeout, **kw)

    monkeypatch.setattr(cov, "_run", timeout_on_update_index)
    assert cov.verify(root, "run2", head, [READER, _SECOND]).verdict == "inconclusive"
    monkeypatch.setattr(cov, "_run", real)
    clones = _clone_dests(monkeypatch)

    assert cov.verify(root, "run3", head, [READER, _SECOND]).verdict == "green"

    assert len(clones) == 1


def test_a_busy_mirror_fails_the_verify_naming_the_lock_and_never_runs_unlocked(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    monkeypatch.setattr(cov, "TEST_TIMEOUT_S", 1)
    ran = []
    real = cov._run
    monkeypatch.setattr(cov, "_run", lambda *a, **k: ran.append(a) or real(*a, **k))

    with cov._mirror_lock(root, "peer"):
        out = cov.verify(root, "run1", head, [READER])

    assert out.verdict == "inconclusive" and "busy" in out.detail and "1s" in out.detail, out
    assert not ran


def test_verify_holds_the_mirror_lock_across_pytest_and_the_base_rerun(tmp_path, monkeypatch):
    root, base, head = _two_commits(tmp_path, "broken\n", "broken\n")
    depth = {"now": 0, "pytest_and_base": []}
    real_lock = cov._mirror_lock

    @__import__("contextlib").contextmanager
    def counted(*a, **k):
        with real_lock(*a, **k) as m:
            depth["now"] += 1
            try:
                yield m
            finally:
                depth["now"] -= 1

    real = cov._run

    def spy(argv, cwd, timeout, **kw):
        if "pytest" in argv:
            depth["pytest_and_base"].append(depth["now"])
        return real(argv, cwd, timeout, **kw)

    monkeypatch.setattr(cov, "_mirror_lock", counted)
    monkeypatch.setattr(cov, "_run", spy)

    assert cov.verify(root, "run1", head, [READER, _SECOND], base_sha=base).verdict == "pre_existing"

    assert depth["pytest_and_base"] == [1, 1] and depth["now"] == 0


def _record(root, run, head, paths):
    record = cov.measured_rev_record_path(root, run)
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(json.dumps({"sha": head, "base_sha": head, "paths": paths}), encoding="utf-8")


def test_export_run_runs_the_command_in_the_mirror_under_its_lock_and_passes_its_status_through(tmp_path, monkeypatch, capsys):
    root, head = _peer_import_repo(tmp_path)
    _record(root, "run1", head, [_RUN_MOD_TEST])
    held = []
    real = cov._run

    def spy(argv, cwd, timeout, **kw):
        if "pytest" in argv:
            with pytest.raises(cov.ExportError, match="busy"):
                monkeypatch.setattr(cov, "TEST_TIMEOUT_S", 1)
                with cov._mirror_lock(root):
                    pass
            monkeypatch.setattr(cov, "TEST_TIMEOUT_S", 120)
            held.append(Path(cwd))
        return real(argv, cwd, timeout, **kw)

    monkeypatch.setattr(cov, "_run", spy)
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", _RUN_MOD_TEST]

    code = cov.main(["--export-run", "--run-id", "run1", "--repo-root", str(root), "--", *cmd])

    assert code != 0 and held == [cov.mirror_root(root) / root.name]
    assert "peer_mod" in capsys.readouterr().out


def test_export_run_is_green_when_the_declared_paths_carry_what_the_command_needs(tmp_path, capsys):
    root, head = _peer_import_repo(tmp_path)
    _record(root, "run1", head, [_RUN_MOD_TEST, "t/peer_mod.py"])
    cmd = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", _RUN_MOD_TEST]

    assert cov.main(["--export-run", "--run-id", "run1", "--repo-root", str(root), "--", *cmd]) == 0


def test_export_run_without_a_command_a_record_or_a_good_path_exits_nonzero(tmp_path, capsys):
    root, head = _repo(tmp_path)

    with pytest.raises(SystemExit):
        cov.main(["--export-run", "--run-id", "run1", "--repo-root", str(root)])
    with pytest.raises(SystemExit):
        cov.main(["--run-id", "run1", "--repo-root", str(root), "--", "echo"])
    assert cov.main(["--export-run", "--run-id", "run1", "--repo-root", str(root), "--", "echo"]) == 2

    _record(root, "run2", head, ["/abs/path.py"])
    assert cov.main(["--export-run", "--run-id", "run2", "--repo-root", str(root), "--", sys.executable, "-c", "pass"]) == 2
    assert "clean export failed" in capsys.readouterr().err


def test_export_only_is_retired(tmp_path):
    root, head = _repo(tmp_path)
    _record(root, "run1", head, [READER])

    with pytest.raises(SystemExit):
        cov.main(["--export-only", "--run-id", "run1", "--repo-root", str(root)])


_PEER_IMPORT_COMMIT_TEST = "t/test_run_imports_peer.py"


def test_a_run_that_imports_an_uncommitted_peer_module_is_red_in_verify(tmp_path):
    """The candidate commit carries the run's test; the module it imports exists only in the shared worktree."""
    root, base = _repo(tmp_path)
    _write(root, _PEER_IMPORT_COMMIT_TEST, "import peer_mod\n\n\ndef test_uses_peer():\n    assert peer_mod.VALUE == 1\n")
    _git(root, "add", _PEER_IMPORT_COMMIT_TEST)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "run")
    head = _git(root, "rev-parse", "HEAD").strip()
    _write(root, "t/peer_mod.py", "VALUE = 1\n")
    assert _pytest_in(root, root, _PEER_IMPORT_COMMIT_TEST) == 0

    out = cov.verify(root, "run1", head, [_PEER_IMPORT_COMMIT_TEST], base_sha=base)

    assert out.red and "collection failed" in out.detail and "peer_mod" in out.detail, out


def test_a_collection_error_is_red_naming_it_and_a_non_collection_exit_two_is_not():
    boom = "ERROR collecting t/test_x.py\nModuleNotFoundError: No module named 'peer_mod'\n1 error in 0.1s\n"
    assert cov.collection_error(2, boom)
    assert cov.collection_error(2, "Interrupted: 1 error during collection\n")
    assert not cov.collection_error(2, "KeyboardInterrupt\n")
    assert not cov.collection_error(1, boom)


_PROBE_MOD = "coordinator_core/__init__.py"
_PROBE_TEST = "t/test_probe_import.py"


def test_import_coordinator_core_resolves_to_the_mirror_never_the_callers_repo(tmp_path, monkeypatch):
    root, _head = _repo(tmp_path)
    _write(root, _PROBE_MOD, "WHERE = 'mirror'\n")
    _write(
        root, _PROBE_TEST,
        "import pathlib\nimport coordinator_core\n\n\ndef test_resolves_inside_the_export():\n"
        "    here = pathlib.Path(__file__).resolve().parents[1]\n"
        "    assert pathlib.Path(coordinator_core.__file__).resolve().is_relative_to(here)\n"
        "    assert coordinator_core.WHERE == 'mirror'\n",
    )
    _git(root, "add", _PROBE_MOD, _PROBE_TEST)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "probe")
    head = _git(root, "rev-parse", "HEAD").strip()
    decoy = tmp_path / "decoy"
    _write(decoy, "coordinator_core/__init__.py", "WHERE = 'caller'\n")
    monkeypatch.setenv("PYTHONPATH", str(decoy))
    monkeypatch.setenv("PYTHONSAFEPATH", "1")

    out = cov.verify(root, "run1", head, [_PROBE_TEST])

    assert out.verdict == "green", out
    assert cov._mirror_env({"PYTHONPATH": "caller"}, Path("mirror"))["PYTHONPATH"] == "mirror"


_HELPER = "t/helper_mod.py"
_IMPORTER = "t/test_uses_helper.py"
_UNRELATED = "t/test_unrelated_slow.py"


def _importer_repo(tmp_path):
    root, _base = _repo(tmp_path)
    _write(root, _HELPER, "VALUE = 1\n")
    _write(root, _IMPORTER, "import helper_mod\n\n\ndef test_helper():\n    assert helper_mod.VALUE == 1\n")
    _write(root, _UNRELATED, "def test_unrelated():\n    assert True\n")
    _git(root, "add", _HELPER, _IMPORTER, _UNRELATED)
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "helper")
    return root, _git(root, "rev-parse", "HEAD").strip()


def _pytest_targets(monkeypatch):
    seen = []
    real = cov._run

    def spy(argv, cwd, timeout, **kw):
        if "pytest" in argv:
            seen.append(argv[argv.index("--basetemp") + 2:])
        return real(argv, cwd, timeout, **kw)

    monkeypatch.setattr(cov, "_run", spy)
    return seen


def test_the_pytest_set_is_the_touched_tests_plus_the_test_files_that_import_a_touched_module(tmp_path, monkeypatch):
    root, head = _importer_repo(tmp_path)
    seen = _pytest_targets(monkeypatch)

    out = cov.verify(root, "run1", head, [READER], touched=[_HELPER, READER])

    assert out.verdict == "green", out
    assert seen == [[READER, _IMPORTER]]


def test_no_touched_module_means_the_seeds_alone_and_a_runaway_importer_set_is_capped(tmp_path, monkeypatch):
    root, head = _importer_repo(tmp_path)
    seen = _pytest_targets(monkeypatch)
    assert cov.verify(root, "run1", head, [READER]).verdict == "green"
    assert seen == [[READER]]

    monkeypatch.setattr(cov, "IMPORTER_CAP", 1)
    for n in range(3):
        _write(root, f"t/test_more_{n}.py", "import helper_mod\n\n\ndef test_m():\n    assert helper_mod.VALUE == 1\n")
    _git(root, "add", *[f"t/test_more_{n}.py" for n in range(3)])
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "more")
    head = _git(root, "rev-parse", "HEAD").strip()
    seen.clear()

    out = cov.verify(root, "run2", head, [READER], touched=[_HELPER])

    assert len(seen[0]) == 2 and "importer cap 1" in out.detail, (seen, out)


def test_verify_timeout_detail_names_the_timeout_and_its_budget(tmp_path, monkeypatch):
    root, head = _repo(tmp_path)
    monkeypatch.setattr(cov, "TEST_TIMEOUT_S", 7)

    def slow(argv, cwd, timeout, **kw):
        raise subprocess.TimeoutExpired(argv, timeout)

    monkeypatch.setattr(cov, "_run", slow)
    out = cov.verify(root, "run1", head, [READER])

    assert out.verdict == "inconclusive" and "timed out after 7s" in out.detail, out.detail


def test_the_base_rerun_moves_the_mirror_by_index_info_never_a_second_full_checkout(tmp_path, monkeypatch):
    root, base, head = _two_commits(tmp_path, "broken\n", "broken\n")
    seen = []
    real = cov._run

    def spy(argv, cwd, timeout, **kw):
        seen.append(argv[1:])
        return real(argv, cwd, timeout, **kw)

    monkeypatch.setattr(cov, "_run", spy)
    out = cov.verify(root, "run1", head, [READER, _SECOND], base_sha=base)

    assert out.verdict == "pre_existing", out
    assert not [a for a in seen if a[:1] == ["checkout"] or "--detach" in a]
    assert any(a[:1] == ["diff-tree"] and base in a and head in a for a in seen)
    assert any(a[:1] == ["update-index"] and "--force-remove" in a for a in seen)
    assert not [a for a in seen if a[:1] == ["read-tree"] and len(a) > 4]


def test_a_path_the_candidate_added_is_absent_when_the_base_is_rerun_and_the_mirror_still_advances(tmp_path):
    """READER passes at base only if the candidate-added `extra.txt` is gone; the next verify starts from base."""
    root, base = _repo(tmp_path, data="broken\n")
    _write(root, "t/extra.txt", "x\n")
    _git(root, "add", "t/extra.txt")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "candidate")
    head = _git(root, "rev-parse", "HEAD").strip()
    dest = cov.mirror_root(root) / root.name

    assert cov.verify(root, "run1", head, [READER], base_sha=base).verdict == "pre_existing"
    assert not (dest / "t" / "extra.txt").exists() and (dest / "t" / "data.txt").read_text() == "broken\n"

    assert cov.verify(root, "run2", head, [READER]).verdict == "red"
    assert (dest / "t" / "extra.txt").exists()
