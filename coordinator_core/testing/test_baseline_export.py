"""Behavior of baseline_export's run path against throwaway repos built under tmp_path."""
import json
import os
import shutil
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import pytest

from coordinator_core.testing import baseline_export as be

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

ENGINE_ROOT = Path(__file__).resolve().parents[2]
_NO_CONSOLE = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
POSIX_ONLY = pytest.mark.skipif(os.name == "nt", reason="POSIX process-group semantics")


def _git(repo, *args):
    return subprocess.run(
        ["git", "-c", "user.email=t@example.invalid", "-c", "user.name=t", *args],
        cwd=repo, check=True, capture_output=True, text=True, **_NO_CONSOLE,
    ).stdout.strip()


@pytest.fixture(autouse=True)
def _slots_home(tmp_path, monkeypatch):
    home = tmp_path / "settings-home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))
    return home


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / "pkg").mkdir(parents=True)
    _git(root, "init", "-q", ".")
    (root / "a.txt").write_text("one\n")
    (root / "pkg" / "b.txt").write_text("bee\n")
    _git(root, "add", "a.txt", "pkg/b.txt")
    _git(root, "commit", "-q", "-m", "first")
    (root / "a.txt").write_text("two\n")
    _git(root, "commit", "-q", "-am", "second")
    root = Path(os.path.realpath(root))
    return root


def _base(repo):
    return _git(repo, "rev-parse", "HEAD~1")


def _run(repo, argv, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    code = be.main(argv)
    cap = capsys.readouterr()
    last = cap.out.strip().splitlines()[-1] if cap.out.strip() else ""
    result = json.loads(last)["baseline_export"] if last.startswith("{") else None
    return code, result, cap.err


def _exports(repo):
    scratch = repo / "scratch"
    return [p.name for p in scratch.iterdir() if p.name.startswith(be.ROOT_PREFIX)] if scratch.is_dir() else []


def _py(code, *args):
    return ["--", sys.executable, "-c", code, *args]


def test_green_and_red_runner(repo, monkeypatch, capsys):
    sha = _base(repo)
    code, res, _ = _run(repo, ["--sha", sha[:8], *_py("import sys; sys.exit(0)")], monkeypatch, capsys)
    assert code == 0
    assert res == {"sha": sha, "export": "ok", "why": None, "runner_exit": 0, "removed": True}
    code, res, _ = _run(repo, ["--sha", sha, *_py("import sys; sys.exit(7)")], monkeypatch, capsys)
    assert code == 0 and res["runner_exit"] == 7 and res["removed"] is True
    assert _exports(repo) == []


def test_export_holds_base_content_in_counterpart_cwd(repo, monkeypatch, capsys, tmp_path):
    out = tmp_path / "seen.json"
    probe = (
        "import json, os, sys\n"
        "json.dump({'cwd': os.getcwd(), 'a': open('../a.txt').read(), 'b': open('b.txt').read()}, open(sys.argv[1], 'w'))\n"
    )
    monkeypatch.chdir(repo / "pkg")
    code = be.main(["--sha", _base(repo), *_py(probe, str(out))])
    capsys.readouterr()
    assert code == 0
    seen = json.loads(out.read_text())
    assert seen["a"] == "one\n" and seen["b"] == "bee\n"
    parts = Path(seen["cwd"]).parts
    assert parts[-1] == "pkg" and parts[-3] == "scratch" and parts[-2].startswith(be.ROOT_PREFIX)
    assert _exports(repo) == []


@pytest.mark.parametrize("sha", ["zzzz", "0123abcd", "abc"])
def test_bad_sha_exits_3_and_leaves_no_directory(repo, monkeypatch, capsys, sha):
    code, res, err = _run(repo, ["--sha", sha, *_py("pass")], monkeypatch, capsys)
    assert code == be.EXIT_EXPORT_FAILED
    assert res["export"] == "failed" and res["why"] and res["runner_exit"] is None and res["sha"] == sha
    assert _exports(repo) == []


def test_non_hex_sha_spawns_no_git(repo, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(be, "_git_popen", lambda *a, **k: calls.append(a) or pytest.fail("git spawned"))
    code, _, _ = _run(repo, ["--sha", "main", *_py("pass")], monkeypatch, capsys)
    assert code == be.EXIT_EXPORT_FAILED and calls == []


def test_exactly_two_git_spawns(repo, monkeypatch, capsys):
    real = be._git_popen
    seen = []

    def counting(args, cwd, **kw):
        seen.append(args[0] if args[0] != "--no-optional-locks" else args[1])
        return real(args, cwd, **kw)

    monkeypatch.setattr(be, "_git_popen", counting)
    code, res, _ = _run(repo, ["--sha", _base(repo), *_py("pass")], monkeypatch, capsys)
    assert code == 0 and res["export"] == "ok"
    assert seen == ["rev-parse", "archive"]


def test_archive_stream_failure_exits_3_without_spawning_runner(repo, monkeypatch, capsys, tmp_path):
    marker = tmp_path / "ran"

    def boom(*a, **k):
        raise tarfile.ReadError("stream broke")

    monkeypatch.setattr(be.tarfile, "open", boom)
    code, res, _ = _run(repo, ["--sha", _base(repo), *_py("import sys; open(sys.argv[1], 'w')", str(marker))], monkeypatch, capsys)
    assert code == be.EXIT_EXPORT_FAILED
    assert res["export"] == "failed" and res["removed"] is True and "stream broke" in res["why"]
    assert not marker.exists() and _exports(repo) == []


@POSIX_ONLY
def test_member_rejected_by_data_filter_exits_3(repo, monkeypatch, capsys, tmp_path):
    os.symlink("/etc/passwd", repo / "evil")
    _git(repo, "add", "evil")
    _git(repo, "commit", "-q", "-m", "evil link")
    marker = tmp_path / "ran"
    code, res, _ = _run(repo, ["--sha", _git(repo, "rev-parse", "HEAD"), *_py("import sys; open(sys.argv[1], 'w')", str(marker))], monkeypatch, capsys)
    assert code == be.EXIT_EXPORT_FAILED and res["export"] == "failed"
    assert not marker.exists() and _exports(repo) == []


def test_removal_failure_exits_4_naming_the_path(repo, monkeypatch, capsys):
    monkeypatch.setattr(be, "_remove_tree", lambda root: None)
    code, res, err = _run(repo, ["--sha", _base(repo), *_py("pass")], monkeypatch, capsys)
    left = [repo / "scratch" / n for n in _exports(repo)]
    try:
        assert code == be.EXIT_REMOVAL_FAILED
        assert len(left) == 1 and str(left[0]) in err
        assert res["removed"] is False
    finally:
        for p in left:
            shutil.rmtree(p, ignore_errors=True)


def test_removal_failure_after_export_failure_is_still_exit_4(repo, monkeypatch, capsys):
    def boom(*a, **k):
        raise tarfile.ReadError("stream broke")

    monkeypatch.setattr(be.tarfile, "open", boom)
    monkeypatch.setattr(be, "_remove_tree", lambda root: None)
    code, res, err = _run(repo, ["--sha", _base(repo), *_py("pass")], monkeypatch, capsys)
    left = [repo / "scratch" / n for n in _exports(repo)]
    try:
        assert code == be.EXIT_REMOVAL_FAILED and res["export"] == "failed" and str(left[0]) in err
    finally:
        for p in left:
            shutil.rmtree(p, ignore_errors=True)


def test_existing_root_is_refused_and_untouched(repo, monkeypatch, capsys):
    sha = _base(repo)
    root = be.export_root(repo, sha, os.getpid())
    root.mkdir(parents=True)
    (root / "keep.txt").write_text("x")
    code, res, _ = _run(repo, ["--sha", sha, *_py("pass")], monkeypatch, capsys)
    assert code == be.EXIT_EXPORT_FAILED and res["export"] == "failed"
    assert (root / "keep.txt").read_text() == "x"


def test_unspawnable_runner_is_export_ok(repo, monkeypatch, capsys):
    code, res, _ = _run(repo, ["--sha", _base(repo), "--", "definitely-not-a-program-xyz"], monkeypatch, capsys)
    assert code == 0
    assert res["export"] == "ok" and res["runner_exit"] is None and res["removed"] is True
    assert "definitely-not-a-program-xyz" in res["why"]
    assert _exports(repo) == []


def test_link_is_visible_to_runner_and_removed_as_a_link(repo, monkeypatch, capsys, tmp_path):
    nm = repo / "node_modules"
    (nm / "dep").mkdir(parents=True)
    (nm / "dep" / "index.js").write_text("module.exports = 1\n")
    out = tmp_path / "link.json"
    probe = (
        "import json, os, sys\n"
        "json.dump({'islink': os.path.islink('node_modules') or os.path.isdir('node_modules'),"
        " 'body': open('node_modules/dep/index.js').read()}, open(sys.argv[1], 'w'))\n"
    )
    code, res, _ = _run(repo, ["--sha", _base(repo), "--link", "node_modules", *_py(probe, str(out))], monkeypatch, capsys)
    assert code == 0 and res["removed"] is True
    seen = json.loads(out.read_text())
    assert seen == {"islink": True, "body": "module.exports = 1\n"}
    assert (nm / "dep" / "index.js").read_text() == "module.exports = 1\n"
    assert _exports(repo) == []


def test_link_to_missing_target_exits_3(repo, monkeypatch, capsys):
    code, res, _ = _run(repo, ["--sha", _base(repo), "--link", "nope", *_py("pass")], monkeypatch, capsys)
    assert code == be.EXIT_EXPORT_FAILED and "nope" in res["why"] and _exports(repo) == []


def test_remove_tree_does_not_follow_links(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    (target / "keep.txt").write_text("k")
    root = tmp_path / "root"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / "ro.txt").write_text("r")
    os.chmod(root / "sub" / "ro.txt", 0o444)
    os.symlink(target, root / "sub" / "lnk", target_is_directory=True)
    os.symlink(target / "keep.txt", root / "flnk")
    assert be.remove(root) is True
    assert (target / "keep.txt").read_text() == "k"


def test_no_ref_index_or_head_write(repo, monkeypatch, capsys):
    def snap():
        return (
            (repo / ".git" / "HEAD").read_bytes(),
            (repo / ".git" / "index").read_bytes(),
            _git(repo, "for-each-ref"),
        )

    before = snap()
    code, _, _ = _run(repo, ["--sha", _base(repo), *_py("pass")], monkeypatch, capsys)
    assert code == 0
    assert snap() == before


@POSIX_ONLY
def test_sigterm_runs_the_finally_and_kills_the_group(repo, tmp_path):
    pidfile = tmp_path / "grandchild.pid"
    runner = (
        "import subprocess, sys, time\n"
        "g = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "open(sys.argv[1], 'w').write(str(g.pid))\n"
        "time.sleep(60)\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ENGINE_ROOT)}
    proc = subprocess.Popen(
        [sys.executable, "-m", "coordinator_core.testing.baseline_export", "--sha", _base(repo), *_py(runner, str(pidfile))],
        cwd=repo, env=env, stdout=subprocess.PIPE, text=True, **_NO_CONSOLE,
    )
    try:
        deadline = time.monotonic() + 20
        while not pidfile.exists() or not pidfile.read_text():
            assert time.monotonic() < deadline and proc.poll() is None, "runner never started"
            time.sleep(0.05)
        grandchild = int(pidfile.read_text())
        assert len(_exports(repo)) == 1
        proc.send_signal(signal.SIGTERM)
        out, _ = proc.communicate(timeout=20)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    assert proc.returncode == 128 + signal.SIGTERM
    result = json.loads(out.strip().splitlines()[-1])["baseline_export"]
    assert result["removed"] is True and result["why"] == "interrupted: SIGTERM"
    assert _exports(repo) == []
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(grandchild, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        os.kill(grandchild, signal.SIGKILL)
        pytest.fail("grandchild outlived the CLI")


def test_export_drains_archive_padding_and_extracts_committed_files(repo, tmp_path, monkeypatch):
    real = be._git_popen
    killed = []

    class _StillRunning:
        """git has not yet exited when the tar end-of-archive marker is read."""

        def __init__(self, proc):
            self._p = proc

        def poll(self):
            return None

        def kill(self):
            killed.append(True)
            self._p.kill()

        def __getattr__(self, name):
            return getattr(self._p, name)

    class _Door:
        def __init__(self, cm):
            self._cm = cm

        def __enter__(self):
            return _StillRunning(self._cm.__enter__())

        def __exit__(self, *exc):
            return self._cm.__exit__(*exc)

    monkeypatch.setattr(be, "_git_popen", lambda *a, **kw: _Door(real(*a, **kw)))
    root = tmp_path / "out"
    root.mkdir()
    be.export(str(repo), _git(repo, "rev-parse", "HEAD"), root)
    assert killed == []
    assert (root / "a.txt").read_text() == "two\n"
    assert (root / "pkg" / "b.txt").read_text() == "bee\n"


def test_runs_as_a_module_process(repo):
    env = {**os.environ, "PYTHONPATH": str(ENGINE_ROOT)}
    done = subprocess.run(
        [sys.executable, "-m", "coordinator_core.testing.baseline_export", "--sha", _base(repo), *_py("pass")],
        cwd=repo, env=env, capture_output=True, text=True, **_NO_CONSOLE,
    )
    assert done.returncode == 0
    assert json.loads(done.stdout.strip().splitlines()[-1])["baseline_export"]["runner_exit"] == 0
    assert _exports(repo) == []


def test_data_dirs_are_linked_from_the_live_tree_not_extracted(repo, monkeypatch, capsys, tmp_path):
    (repo / "state").mkdir()
    (repo / "state" / "row.yaml").write_text("at-base\n")
    _git(repo, "add", "state/row.yaml")
    _git(repo, "commit", "-q", "-m", "state")
    (repo / "state" / "row.yaml").write_text("live\n")
    out = tmp_path / "data.json"
    probe = (
        "import json, os, sys\n"
        "json.dump({'linked': os.path.islink('state') or os.path.isjunction('state'),"
        " 'row': open('state/row.yaml').read(), 'code': open('a.txt').read()}, open(sys.argv[1], 'w'))\n"
    )
    sha = _git(repo, "rev-parse", "HEAD")
    code, res, _ = _run(repo, ["--sha", sha, "--link", "state/row.yaml", *_py(probe, str(out))], monkeypatch, capsys)
    assert code == 0 and res["removed"] is True
    assert json.loads(out.read_text()) == {"linked": True, "row": "live\n", "code": "two\n"}
    assert (repo / "state" / "row.yaml").read_text() == "live\n"
    assert _exports(repo) == []


def _hold_all_slots():
    held = [be._acquire_slot() for _ in range(be.EXPORT_SLOT_CAP)]
    assert all(held)
    return held


def test_no_free_slot_is_refused_without_a_tree_or_git_archive(repo, monkeypatch, capsys, tmp_path):
    held = _hold_all_slots()
    marker = tmp_path / "ran"
    calls = []
    real = be._git_popen
    monkeypatch.setattr(be, "_git_popen", lambda a, *r, **k: calls.append(a[0]) or real(a, *r, **k))
    code, res, _ = _run(repo, ["--sha", _base(repo), *_py("import sys; open(sys.argv[1], 'w')", str(marker))], monkeypatch, capsys)
    assert code == be.EXIT_REFUSED == 5
    assert res["export"] == "refused" and "no free export slot" in res["why"]
    assert res["runner_exit"] is None
    assert not marker.exists() and _exports(repo) == []
    assert "archive" not in calls and not (repo / "scratch").exists()
    for s in held:
        be._release_slot(s)


def test_slot_is_released_after_a_run_and_after_an_export_failure(repo, monkeypatch, capsys):
    for _ in range(be.EXPORT_SLOT_CAP + 1):
        code, _, _ = _run(repo, ["--sha", _base(repo), *_py("pass")], monkeypatch, capsys)
        assert code == 0
    monkeypatch.setattr(be.tarfile, "open", lambda *a, **k: (_ for _ in ()).throw(tarfile.TarError("x")))
    for _ in range(be.EXPORT_SLOT_CAP + 1):
        code, _, _ = _run(repo, ["--sha", _base(repo), *_py("pass")], monkeypatch, capsys)
        assert code == be.EXIT_EXPORT_FAILED
    held = _hold_all_slots()
    for s in held:
        be._release_slot(s)


def test_dead_holder_slot_is_reclaimed(repo, monkeypatch, capsys):
    from coordinator_core.testing import suite_mutex

    dead = subprocess.Popen([sys.executable, "-c", "pass"], **_NO_CONSOLE)
    dead.wait()
    for i in range(be.EXPORT_SLOT_CAP):
        path = be._slot_path(i)
        path.parent.mkdir(parents=True, exist_ok=True)
        assert suite_mutex.acquire(f"dead#{i}", "x", 0.0, pid=dead.pid, path=path)
    code, res, _ = _run(repo, ["--sha", _base(repo), *_py("pass")], monkeypatch, capsys)
    assert code == 0 and res["export"] == "ok"


def test_dead_pid_trees_are_reaped_and_live_ones_kept(repo, monkeypatch, capsys):
    dead = subprocess.Popen([sys.executable, "-c", "pass"], **_NO_CONSOLE)
    dead.wait()
    scratch = repo / "scratch"
    orphan = scratch / f"{be.ROOT_PREFIX}abcdef12-{dead.pid}"
    live = scratch / f"{be.ROOT_PREFIX}abcdef12-{os.getppid()}"
    other = scratch / "keep-me"
    for d in (orphan, live, other):
        (d / "sub").mkdir(parents=True)
        (d / "sub" / "f.txt").write_text("x")
    code, _, _ = _run(repo, ["--sha", _base(repo), *_py("pass")], monkeypatch, capsys)
    assert code == 0
    assert not orphan.exists() and live.exists() and other.exists()
