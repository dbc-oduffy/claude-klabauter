"""A run that imports a peer's uncommitted module goes red under the default `clean_overlay_verify.main`
(HEAD plus the run's own declared paths), and a verify timeout exits 2 with a detail naming it; a real git
repo, the real `measured_rev` producer."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import measured_rev as mr
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "run-clean-export-e2e"
RUN_MOD = "run_mod.py"
RUN_TEST = "test_run_mod.py"
PEER_MOD = "peer_mod.py"
_CALL_TIME_IMPORT = "def answer():\n    from peer_mod import VALUE\n\n    return VALUE\n"
_COLLECTION_TIME_IMPORT = "from peer_mod import VALUE\n\n\ndef answer():\n    return VALUE\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(repo: Path, rel: str, text: str) -> None:
    (repo / rel).write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    """The persistent mirror lives under the system Temp's coordinator root; every test gets its own."""
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


@pytest.fixture
def repo(tmp_path):
    """A committed base; the run's declared files and a peer's module are uncommitted in the worktree."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "commit.gpgsign", "false")
    _write(root, ".gitignore", "scratch/\n.coordinator-local/\n")
    _write(root, "README.md", "seed\n")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "base")
    _write(root, PEER_MOD, "VALUE = 7\n")
    _write(root, RUN_MOD, _CALL_TIME_IMPORT)
    _write(root, RUN_TEST, "from run_mod import answer\n\n\ndef test_answer():\n    assert answer() == 7\n")
    return root


def _measure(root: Path) -> None:
    mr.measured_rev(RUN, [RUN_MOD, RUN_TEST], repo_root=root)


def _verdict(root: Path) -> dict:
    return json.loads(cov.verdict_path(root, RUN).read_text(encoding="utf-8"))


def test_a_run_importing_an_uncommitted_peer_module_goes_red_on_the_clean_export(repo, capsys):
    _measure(repo)
    assert (repo / PEER_MOD).is_file()
    assert _git(repo, "ls-files", "--", PEER_MOD).strip() == ""

    code = cov.main(["--run-id", RUN, "--repo-root", str(repo)])

    assert code == 1, capsys.readouterr()
    assert _verdict(repo)["verdict"] == "red"
    assert PEER_MOD.removesuffix(".py") in _verdict(repo)["detail"]


def test_a_collection_time_import_of_an_uncommitted_peer_module_is_never_green(repo):
    _write(repo, RUN_MOD, _COLLECTION_TIME_IMPORT)
    _measure(repo)

    code = cov.main(["--run-id", RUN, "--repo-root", str(repo)])

    verdict = _verdict(repo)
    assert code != 0 and verdict["verdict"] in ("red", "inconclusive"), verdict
    assert "No module named 'peer_mod'" in verdict["detail"], verdict


def test_the_same_run_is_green_when_the_peer_module_is_part_of_it(repo):
    mr.measured_rev(RUN, [RUN_MOD, RUN_TEST, PEER_MOD], repo_root=repo)

    assert cov.main(["--run-id", RUN, "--repo-root", str(repo)]) == 0
    assert _verdict(repo)["verdict"] == "green"


def test_a_verify_timeout_exits_inconclusive_and_the_detail_names_the_timeout(repo, monkeypatch):
    _measure(repo)
    monkeypatch.setattr(cov, "TEST_TIMEOUT_S", 0.001)

    code = cov.main(["--run-id", RUN, "--repo-root", str(repo)])

    assert code == 2
    verdict = _verdict(repo)
    assert verdict["verdict"] == "inconclusive"
    assert "timed out" in verdict["detail"], verdict
