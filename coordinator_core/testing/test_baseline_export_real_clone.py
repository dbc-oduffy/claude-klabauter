"""The default baseline-export run executes its runner in a real clone with a .git root."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.testing import baseline_cache as bc
from coordinator_core.testing import baseline_export as be
from coordinator_core.testing.tmp_git_repo import init_tmp_git_repo, tmp_git

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_PROBE = (
    "import os, subprocess, sys;"
    "top = subprocess.run(['git', 'rev-parse', '--show-toplevel'], capture_output=True, text=True).stdout.strip();"
    "open(sys.argv[1], 'w').write(top + '\\n' + str(os.path.exists('.git')))"
)


@pytest.fixture(autouse=True)
def _settings_home(tmp_path, monkeypatch):
    home = tmp_path / "settings-home"
    home.mkdir()
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(home))


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    init_tmp_git_repo(root)
    (root / "a.txt").write_text("one\n", encoding="utf-8")
    tmp_git(root, "add", "a.txt")
    tmp_git(root, "commit", "-q", "-m", "second")
    return Path(os.path.realpath(root))


def _run(repo, monkeypatch, capsys, argv):
    monkeypatch.chdir(repo)
    code = be.main(argv)
    out = capsys.readouterr().out.strip().splitlines()
    return code, json.loads(out[-1])["baseline_export"]


def _sha(repo):
    return tmp_git(repo, "rev-parse", "HEAD").stdout.strip()


def test_default_run_executes_the_runner_in_a_real_clone(repo, monkeypatch, capsys, tmp_path):
    out = tmp_path / "probe.txt"
    sha = _sha(repo)
    code, res = _run(repo, monkeypatch, capsys, ["--sha", sha, "--", sys.executable, "-c", _PROBE, str(out)])
    assert code == be.EXIT_OK and res["export"] == "ok" and res["runner_exit"] == 0
    toplevel, has_git = out.read_text(encoding="utf-8").splitlines()
    entry = bc.entry_path(str(repo), sha)
    assert os.path.realpath(toplevel) == os.path.realpath(entry)
    assert has_git == "True"


def test_a_git_less_tree_exits_3_and_never_spawns_the_runner(repo, monkeypatch, capsys, tmp_path):
    marker = tmp_path / "ran"

    def archive_like(toplevel, sha40, root):
        (Path(root) / "a.txt").write_text("one\n", encoding="utf-8")

    monkeypatch.setattr(be, "clone_tree", archive_like)
    code, res = _run(
        repo, monkeypatch, capsys,
        ["--sha", _sha(repo), "--", sys.executable, "-c", "open(__import__('sys').argv[1], 'w')", str(marker)],
    )
    assert code == be.EXIT_EXPORT_FAILED
    assert res["export"] == "failed" and ".git" in res["why"]
    assert not marker.exists()
