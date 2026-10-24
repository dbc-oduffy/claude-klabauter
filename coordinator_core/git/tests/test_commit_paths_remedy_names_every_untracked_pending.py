"""The unclassified-name refusal's remedy names --pending for every untracked
pathspec path under a published-tree row, not only the refused names."""

import subprocess

import pytest

from coordinator_core.git import commit as gcommit
from coordinator_core.git.commit import CommitRefused

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}

_PORTABLE = (
    "claude-klabauter-coordinator-bin|mirror|publish-mirror:x|coordinator/bin|"
    "coordinator/bin||existing_cli.py,classified-tool.py\n"
)

_DECLARATIONS = "rows:\n  claude-klabauter-coordinator-bin:\n    deny: []\n"


def _git(repo, *args):
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True, check=True, **_NOWIN
    )


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", "-b", "work/p")
    _git(r, "config", "user.email", "t@local")
    _git(r, "config", "user.name", "t")
    _write(r / "setup" / "publish-targets.portable", _PORTABLE)
    _write(r / "setup" / "publish-allowlist-declarations.yaml", _DECLARATIONS)
    _write(r / "coordinator" / "bin" / "existing_cli.py", "x = 1\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    return r


def test_remedy_names_untracked_classified_sibling_and_skips_tracked(repo):
    _write(repo / "coordinator" / "bin" / "classified-tool.py", "x = 1\n")
    _write(repo / "coordinator" / "bin" / "new-tool.py", "x = 1\n")
    (repo / "coordinator" / "bin" / "existing_cli.py").write_text(
        "x = 2\n", encoding="utf-8", newline="\n"
    )
    paths = [
        "coordinator/bin/existing_cli.py",
        "coordinator/bin/classified-tool.py",
        "coordinator/bin/new-tool.py",
    ]

    with pytest.raises(CommitRefused) as excinfo:
        gcommit.commit_paths(repo, paths, "bin tools")

    msg = str(excinfo.value)
    assert "--pending coordinator/bin/new-tool.py" in msg
    assert "--pending coordinator/bin/classified-tool.py" in msg
    assert "--pending coordinator/bin/existing_cli.py" not in msg
