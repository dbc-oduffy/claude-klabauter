"""Contract test for coordinator_core.run_scratch; vendoring repos copy it with the module (docs/reference/run-scratch-contract.md)."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from coordinator_core import owner_mark, run_scratch

DEAD_PID = 2_000_000_000
NAME_RE = re.compile(r"^.+\.\d{8}T\d{6}Z\.\d+$")


@pytest.fixture
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    scratch, build = tmp_path / "scratch", tmp_path / "build"
    monkeypatch.setenv("MACHINE_LOCAL_PATHS_SCRATCH_ROOT", str(scratch))
    monkeypatch.setenv("MACHINE_LOCAL_PATHS_BUILD_ROOT", str(build))
    return {"scratch": scratch, "build": build}


@pytest.fixture
def vendored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The module as a vendored copy sees the box: no machine_resolver, a registry dir of TOML files."""
    registry = tmp_path / "machine-local"
    registry.mkdir()
    monkeypatch.setattr(run_scratch, "_machine_resolver", lambda: None)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry))
    for key in ("MACHINE_LOCAL_PATHS_SCRATCH_ROOT", "MACHINE_LOCAL_PATHS_BUILD_ROOT"):
        monkeypatch.delenv(key, raising=False)
    return registry


def _dead_claim(kind_dir: Path, slug: str, stamp: str, complete: bool = False) -> Path:
    path = kind_dir / f"{slug}.{stamp}.{DEAD_PID}"
    path.mkdir(parents=True)
    (path / "payload.txt").write_text("x", encoding="utf-8")
    mark = {"pid": DEAD_PID, "procStart": None, "startedAt": stamp, "complete": complete, "root": "build"}
    (path / ".owner.json").write_text(json.dumps(mark), encoding="utf-8")
    return path


def test_env_wins_over_registry_and_names_both_roots(roots, tmp_path: Path) -> None:
    assert run_scratch.scratch_root() == roots["scratch"]
    assert run_scratch.build_root() == roots["build"]


@pytest.mark.parametrize("value", ["/mnt/box/scratch", "\\\\fileserver\\share\\scratch", "relative\\scratch", "relative/scratch"])
def test_registry_values_of_every_os_path_shape_round_trip(vendored: Path, value: str) -> None:
    (vendored / "registry.toml").write_text('[paths]\nscratch_root = ' + json.dumps(value) + "\n", encoding="utf-8")
    assert run_scratch.scratch_root() == Path(value)


def test_local_registry_beats_registry_and_flat_keys_read(vendored: Path) -> None:
    (vendored / "registry.toml").write_text('"paths.scratch_root" = "/base/s"\n[paths]\nbuild_root = "/base/b"\n', encoding="utf-8")
    assert run_scratch.scratch_root() == Path("/base/s")
    assert run_scratch.build_root() == Path("/base/b")
    (vendored / "registry.local.toml").write_text('[paths]\nscratch_root = "/local/s"\n', encoding="utf-8")
    assert run_scratch.scratch_root() == Path("/local/s")
    assert run_scratch.build_root() == Path("/base/b")


def test_env_beats_registry_files_in_the_vendored_path(vendored: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (vendored / "registry.toml").write_text('[paths]\nscratch_root = "/base/s"\n', encoding="utf-8")
    monkeypatch.setenv("MACHINE_LOCAL_PATHS_SCRATCH_ROOT", "/env/s")
    assert run_scratch.scratch_root() == Path("/env/s")


def test_empty_registry_value_reads_unset(vendored: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    (vendored / "registry.toml").write_text('[paths]\nscratch_root = ""\n', encoding="utf-8")
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    assert run_scratch.scratch_root() == tmp_path / "tmp" / "coordinator"


def test_unset_build_falls_back_to_scratch_and_unset_scratch_to_tempdir(vendored: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    assert run_scratch.scratch_root() == tmp_path / "tmp" / "coordinator"
    assert run_scratch.build_root() == tmp_path / "tmp" / "coordinator"
    (vendored / "registry.toml").write_text('[paths]\nscratch_root = "/only/s"\n', encoding="utf-8")
    assert run_scratch.build_root() == Path("/only/s")


def test_claim_creates_named_dir_with_owner_mark(roots) -> None:
    path = run_scratch.claim("repo", "clones", "slug.v1")
    assert path.parent == roots["scratch"] / "repo" / "clones"
    assert NAME_RE.match(path.name) and path.name.endswith(f".{os.getpid()}") and path.name.startswith("slug.v1.")
    mark = json.loads((path / ".owner.json").read_text(encoding="utf-8"))
    assert mark["pid"] == os.getpid() and mark["complete"] is False
    assert mark["procStart"] == run_scratch.proc_start(os.getpid())
    assert mark["startedAt"]


def test_claim_mark_is_readable_by_owner_mark(roots) -> None:
    path = run_scratch.claim("repo", "clones", "s")
    mark = owner_mark.read_mark(path)
    assert mark is not None and mark["owner_kind"] == "pid" and mark["owner_id"] == str(os.getpid())


def test_two_claims_in_one_second_get_distinct_dirs(roots) -> None:
    first = run_scratch.claim("repo", "k", "s")
    second = run_scratch.claim("repo", "k", "s")
    assert first != second and first.exists() and second.exists()


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "c:x"])
def test_claim_refuses_a_non_segment_name(roots, bad: str) -> None:
    for args in ((bad, "k", "s"), ("r", bad, "s"), ("r", "k", bad)):
        with pytest.raises(ValueError):
            run_scratch.claim(*args)


def test_claim_rejects_an_unknown_root(roots) -> None:
    with pytest.raises(ValueError):
        run_scratch.claim("r", "k", "s", root="elsewhere")


def test_finish_deletes_a_scratch_claim(roots) -> None:
    path = run_scratch.claim("repo", "k", "s")
    (path / "sub").mkdir()
    (path / "sub" / "f.txt").write_text("x", encoding="utf-8")
    result = run_scratch.finish(path)
    assert not path.exists() and result["action"] == "deleted"


def test_finish_refuses_a_path_that_is_not_a_claim(roots, tmp_path: Path) -> None:
    victim = tmp_path / "keepme"
    victim.mkdir()
    with pytest.raises(ValueError):
        run_scratch.finish(victim)
    assert victim.exists()


def test_three_successive_builds_leave_the_latest_and_previous(roots) -> None:
    claims = []
    for _ in range(3):
        path = run_scratch.claim("repo", "clone", "b", root="build")
        (path / "out.bin").write_text("x", encoding="utf-8")
        run_scratch.finish(path)
        claims.append(path)
    kind_dir = roots["build"] / "repo" / "clone"
    assert sorted(p.name for p in kind_dir.iterdir()) == sorted(c.name for c in claims[1:])
    assert not claims[0].exists()


def test_retention_key_is_repo_and_kind(roots) -> None:
    for kind in ("a", "b"):
        for _ in range(3):
            run_scratch.finish(run_scratch.claim("repo", kind, "s", root="build"))
    for kind in ("a", "b"):
        assert len(list((roots["build"] / "repo" / kind).iterdir())) == 2


def test_a_keep_if_held_oldest_entry_survives_finish_and_is_not_counted(roots) -> None:
    held = run_scratch.claim("repo", "k", "s", root="build")
    run_scratch.finish(held, keep_if=lambda p: p == held)
    later = [run_scratch.claim("repo", "k", "s", root="build") for _ in range(3)]
    for path in later:
        run_scratch.finish(path, keep_if=lambda p: p == held)
    assert held.exists() and not later[0].exists() and later[1].exists() and later[2].exists()


def test_sweep_keep_if_spares_a_dead_claim_and_a_raising_keep_if_holds(roots) -> None:
    kind_dir = roots["scratch"] / "repo" / "k"
    dead = _dead_claim(kind_dir, "s", "20200101T000000Z")
    other = _dead_claim(kind_dir, "s", "20200102T000000Z")

    def boom(_p):
        raise RuntimeError("lease unreadable")

    assert run_scratch.sweep(roots["scratch"], keep_if=lambda p: p == dead) == [str(other)]
    assert dead.exists()
    assert run_scratch.sweep(roots["scratch"], keep_if=boom) == [] and dead.exists()
    assert run_scratch.sweep(roots["scratch"]) == [str(dead)]


def test_read_mark_and_sweep_kind_are_public(roots) -> None:
    path = run_scratch.claim("repo", "k", "s")
    assert run_scratch.read_mark(path)["pid"] == os.getpid()
    assert run_scratch.read_mark(path.parent) is None
    dead = _dead_claim(path.parent, "s", "20200101T000000Z")
    assert run_scratch.sweep_kind(path.parent) == [str(dead)] and path.exists()


def test_a_claim_sweep_of_a_crowded_kind_dir_is_fast_and_spawn_free(roots, monkeypatch: pytest.MonkeyPatch) -> None:
    kind_dir = roots["scratch"] / "repo" / "k"
    for n in range(40):
        _dead_claim(kind_dir, "s", f"2020{n // 28 + 1:02d}{n % 28 + 1:02d}T000000Z")

    def boom(*_a, **_k):
        raise AssertionError("spawned a subprocess")

    monkeypatch.setattr(subprocess, "Popen", boom)
    started = time.perf_counter()
    run_scratch.claim("repo", "k", "new")
    assert time.perf_counter() - started < 0.5
    assert sum(1 for p in kind_dir.iterdir() if p.name.endswith(f".{DEAD_PID}")) == 40 - run_scratch.CLAIM_SWEEP_CAP


def test_an_incomplete_live_build_does_not_count_against_keep(roots) -> None:
    running = run_scratch.claim("repo", "k", "s", root="build")
    done = [run_scratch.claim("repo", "k", "s", root="build") for _ in range(2)]
    for path in done:
        run_scratch.finish(path)
    assert running.exists() and all(p.exists() for p in done)


def test_sweep_removes_a_dead_pid_claim_and_keeps_a_live_one(roots) -> None:
    kind_dir = roots["scratch"] / "repo" / "k"
    live = run_scratch.claim("repo", "k", "live")
    dead = _dead_claim(kind_dir, "s", "20200101T000000Z")
    removed = run_scratch.sweep(roots["scratch"])
    assert not dead.exists() and live.exists() and str(dead) in removed


def test_a_complete_claim_with_a_dead_owner_is_kept_within_keep(roots) -> None:
    kind_dir = roots["build"] / "repo" / "k"
    older = _dead_claim(kind_dir, "s", "20200101T000000Z", complete=True)
    newer = _dead_claim(kind_dir, "s", "20200102T000000Z", complete=True)
    oldest = _dead_claim(kind_dir, "s", "20190101T000000Z", complete=True)
    run_scratch.sweep(roots["build"])
    assert older.exists() and newer.exists() and not oldest.exists()


def test_sweep_keep_argument_is_honoured(roots) -> None:
    kind_dir = roots["build"] / "repo" / "k"
    claims = [_dead_claim(kind_dir, "s", f"2020010{n}T000000Z", complete=True) for n in range(1, 4)]
    run_scratch.sweep(roots["build"], keep=1)
    assert [c.exists() for c in claims] == [False, False, True]


def test_procstart_mismatch_on_a_live_pid_is_dead_but_null_is_judged_on_liveness(roots) -> None:
    if run_scratch.proc_start(os.getpid()) is None:
        pytest.skip("procStart unreadable on this platform")
    kind_dir = roots["scratch"] / "repo" / "k"
    reused = run_scratch.claim("repo", "k", "reused")
    mark = json.loads((reused / ".owner.json").read_text(encoding="utf-8"))
    mark["procStart"] = "not-this-process"
    (reused / ".owner.json").write_text(json.dumps(mark), encoding="utf-8")
    unreadable = run_scratch.claim("repo", "k", "unreadable")
    mark = json.loads((unreadable / ".owner.json").read_text(encoding="utf-8"))
    mark["procStart"] = None
    (unreadable / ".owner.json").write_text(json.dumps(mark), encoding="utf-8")
    honest = run_scratch.claim("repo", "k", "honest")
    run_scratch.sweep(roots["scratch"])
    assert not reused.exists() and unreadable.exists() and honest.exists() and kind_dir.exists()


def test_markless_claim_named_dir_is_swept_after_24h_only(roots) -> None:
    kind_dir = roots["scratch"] / "repo" / "k"
    old, young = kind_dir / "s.20200101T000000Z.1", kind_dir / "s.20200102T000000Z.1"
    for path in (old, young):
        path.mkdir(parents=True)
    stale = time.time() - 25 * 3600
    os.utime(old, (stale, stale))
    run_scratch.sweep(roots["scratch"])
    assert not old.exists() and young.exists()


def test_children_that_are_not_claim_shaped_are_never_swept(roots) -> None:
    repo_dir = roots["build"] / "repo"
    output = repo_dir / "output"
    output.mkdir(parents=True)
    stale = time.time() - 90 * 86400
    keep = [output / "artifact", repo_dir / "loose-dir", roots["build"] / "top-level"]
    for path in keep:
        path.mkdir(parents=True, exist_ok=True)
        os.utime(path, (stale, stale))
    run_scratch.sweep(roots["build"])
    assert all(p.exists() for p in keep)


def test_a_claim_bounds_its_own_sweep(roots) -> None:
    kind_dir = roots["scratch"] / "repo" / "k"
    dead = [_dead_claim(kind_dir, "s", f"2020{n:04d}T000000Z") for n in range(101, 113)]
    run_scratch.claim("repo", "k", "new")
    assert sum(not p.exists() for p in dead) == run_scratch.CLAIM_SWEEP_CAP


def test_sweep_cap_argument_bounds_removals(roots) -> None:
    kind_dir = roots["scratch"] / "repo" / "k"
    for n in range(5):
        _dead_claim(kind_dir, "s", f"2020010{n}T000000Z")
    assert len(run_scratch.sweep(roots["scratch"], cap=2)) == 2


def test_a_locked_file_never_raises_and_the_next_sweep_retries(roots, monkeypatch: pytest.MonkeyPatch) -> None:
    path = run_scratch.claim("repo", "k", "s")
    real = shutil.rmtree

    def locked(*_a, **_k):
        raise PermissionError("held open")

    monkeypatch.setattr(shutil, "rmtree", locked)
    result = run_scratch.finish(path)
    assert result["removed"] == [] and path.exists()
    assert run_scratch.sweep(roots["scratch"]) == []
    monkeypatch.setattr(shutil, "rmtree", real)
    assert run_scratch.sweep(roots["scratch"]) == [str(path)]
    assert not path.exists()


def test_a_readonly_file_is_cleared_and_deleted(roots) -> None:
    path = run_scratch.claim("repo", "k", "s")
    locked = path / "ro.txt"
    locked.write_text("x", encoding="utf-8")
    os.chmod(locked, 0o444)
    run_scratch.finish(path)
    assert not path.exists()


def test_claim_finish_sweep_spawn_nothing(roots, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_a, **_k):
        raise AssertionError("spawned a subprocess")

    monkeypatch.setattr(subprocess, "Popen", boom)
    monkeypatch.setattr(os, "system", boom)
    _dead_claim(roots["scratch"] / "repo" / "k", "s", "20200101T000000Z")
    run_scratch.finish(run_scratch.claim("repo", "k", "s"))
    run_scratch.finish(run_scratch.claim("repo", "k", "b", root="build"))
    run_scratch.sweep(roots["scratch"])
    run_scratch.sweep(roots["build"])


def test_proc_start_is_stable_for_this_process_and_none_for_a_dead_pid() -> None:
    first = run_scratch.proc_start(os.getpid())
    assert first is None or (isinstance(first, str) and first == run_scratch.proc_start(os.getpid()))
    assert run_scratch.proc_start(DEAD_PID) is None
    assert run_scratch.proc_start(0) is None and run_scratch.proc_start("x") is None


def test_linux_procstart_is_field_22_even_when_comm_has_parens_and_spaces(monkeypatch: pytest.MonkeyPatch) -> None:
    line = "123 (we ) ird (name) S 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 424242 99 100\n"
    import io

    monkeypatch.setattr(run_scratch, "open", lambda *_a, **_k: io.StringIO(line), raising=False)
    assert run_scratch._proc_start_linux(123) == "424242"


def test_pid_alive_reads_true_for_self_and_false_for_nonsense() -> None:
    assert run_scratch.pid_alive(os.getpid())
    assert not run_scratch.pid_alive(DEAD_PID) and not run_scratch.pid_alive(0) and not run_scratch.pid_alive("x")


def _cli(capsys, *argv: str) -> tuple[int, dict]:
    code = run_scratch.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def test_cli_root_prints_both_roots(roots, capsys) -> None:
    code, out = _cli(capsys, "root")
    assert code == 0 and out == {"scratch": str(roots["scratch"]), "build": str(roots["build"])}


def test_cli_claim_finish_sweep_round_trip(roots, capsys) -> None:
    code, out = _cli(capsys, "claim", "repo", "k", "s", "--pid", str(os.getpid()))
    path = Path(out["path"])
    assert code == 0 and path.is_dir() and path.parent == roots["scratch"] / "repo" / "k"
    _dead_claim(roots["scratch"] / "repo" / "k", "s", "20200101T000000Z")
    code, swept = _cli(capsys, "sweep", "--root", "scratch")
    assert code == 0 and len(swept["removed"]) == 1 and path.exists()
    code, done = _cli(capsys, "finish", str(path))
    assert code == 0 and done["action"] == "deleted" and not path.exists()


def test_cli_build_claim_finish_keeps_two(roots, capsys) -> None:
    for _ in range(3):
        _, out = _cli(capsys, "claim", "repo", "k", "s", "--root", "build", "--pid", str(os.getpid()))
        _cli(capsys, "finish", out["path"])
    assert len(list((roots["build"] / "repo" / "k").iterdir())) == 2


def test_cli_reports_an_error_as_json_with_exit_2(roots, capsys) -> None:
    code, out = _cli(capsys, "claim", "a/b", "k", "s")
    assert code == 2 and "error" in out


@pytest.mark.spawns_process
def test_module_entry_prints_json_naming_both_roots(roots) -> None:
    env = dict(os.environ, MACHINE_LOCAL_PATHS_SCRATCH_ROOT=str(roots["scratch"]), MACHINE_LOCAL_PATHS_BUILD_ROOT=str(roots["build"]))
    proc = subprocess.run(
        [sys.executable, "-m", "coordinator_core.run_scratch", "root"],
        capture_output=True, text=True, env=env, check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert json.loads(proc.stdout) == {"scratch": str(roots["scratch"]), "build": str(roots["build"])}
