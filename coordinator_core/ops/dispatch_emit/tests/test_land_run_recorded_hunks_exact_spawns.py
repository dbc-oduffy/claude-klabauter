"""land_run refuses a commit set that is not exactly the run's recorded hunks, and judges import closure
against the committed image (HEAD plus the landing bytes), never the worktree."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit import write_baseline as wb
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TARGET = "pkg/shared.py"
OTHER = "pkg/other.py"
SEED = "".join(f"line{i}\n" for i in range(1, 13))
OWN = SEED.replace("line10\n", "own10\n")
PEER = SEED.replace("line1\n", "peer1\n")
BOTH = OWN.replace("line1\n", "peer1\n")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, **no_console_creationflags()
    ).stdout


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    for key, value in (("user.email", "t@example.com"), ("user.name", "t"), ("commit.gpgsign", "false"),
                       ("core.autocrlf", "false")):
        _git(root, "config", key, value)
    _write(root, TARGET, SEED)
    _write(root, OTHER, SEED)
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "seed")
    return root


def _blob(root: Path, text: str) -> str:
    return git_objects.write_object(resolve_git_common_dir(root), b"blob", text.encode("utf-8"))


def _lines(root, commit_set, measured_text, baseline_paths=(TARGET,), skip=(), settled=None, base=None):
    baseline = base if base is not None else wb.capture(root, list(baseline_paths))
    measured = {"blobs": {TARGET: _blob(root, measured_text)}}
    return land_run._unrecorded_hunk_lines(
        root, set(commit_set), set(), settled or {}, baseline, measured, set(skip),
    )


def test_a_commit_set_equal_to_the_recorded_hunks_is_not_refused(repo):
    baseline = wb.capture(repo, [TARGET])
    _write(repo, TARGET, OWN)
    assert _lines(repo, [TARGET], OWN, base=baseline) == []


def test_a_hunk_the_run_did_not_record_refuses_naming_the_hunk(repo):
    baseline = wb.capture(repo, [TARGET])
    _write(repo, TARGET, BOTH)
    got = _lines(repo, [TARGET], OWN, base=baseline)
    assert len(got) == 1 and got[0].startswith(f"{TARGET}: carries unrecorded hunk") and "peer1" in got[0], got


def test_an_own_only_staged_blob_beside_peer_dirt_is_not_refused(repo):
    baseline = wb.capture(repo, [TARGET])
    _write(repo, TARGET, BOTH)
    assert _lines(repo, [TARGET], OWN, base=baseline, settled={TARGET: _blob(repo, OWN)}) == []


def test_a_recorded_hunk_the_commit_drops_refuses(repo):
    baseline = wb.capture(repo, [TARGET])
    _write(repo, TARGET, OWN)
    got = _lines(repo, [TARGET], OWN, base=baseline, settled={TARGET: _blob(repo, SEED)})
    assert len(got) == 1 and "omits recorded hunk" in got[0] and "own10" in got[0], got


def test_a_baseline_path_absent_from_the_landing_set_refuses(repo):
    baseline = wb.capture(repo, [TARGET])
    _write(repo, TARGET, OWN)
    got = _lines(repo, [OTHER], OWN, base=baseline)
    assert len(got) == 1 and "absent from the landing set" in got[0] and "own10" in got[0], got


def test_an_absent_path_whose_recorded_hunks_head_already_holds_is_not_refused(repo):
    baseline = wb.capture(repo, [TARGET])
    _write(repo, TARGET, OWN)
    _git(repo, "add", TARGET)
    _git(repo, "commit", "-q", "-m", "own landed earlier")
    assert _lines(repo, [OTHER], OWN, base=baseline) == []


def test_a_skipped_path_is_not_judged(repo):
    baseline = wb.capture(repo, [TARGET])
    _write(repo, TARGET, OWN)
    assert _lines(repo, [OTHER], OWN, base=baseline, skip=[TARGET]) == []


def test_a_path_the_run_recorded_no_hunk_on_is_not_judged_but_a_recorded_one_still_refuses(repo):
    _write(repo, TARGET, PEER)
    baseline = wb.capture(repo, [TARGET])
    _write(repo, TARGET, BOTH)
    assert _lines(repo, [TARGET], PEER, base=baseline) == []
    _write(repo, TARGET, BOTH.replace("line5\n", "extra5\n"))
    got = _lines(repo, [TARGET], OWN, base=baseline)
    assert len(got) == 1 and "extra5" in got[0], got


def test_a_path_with_no_baseline_blob_is_not_judged(repo):
    _write(repo, TARGET, OWN)
    assert _lines(repo, [OTHER], OWN, base={TARGET: None}) == []


def test_import_closure_judges_the_committed_image_not_the_worktree(repo):
    """The landing path imports a name only another path's worktree dirt defines: HEAD would raise."""
    _write(repo, "pkg/__init__.py", "")
    _write(repo, "pkg/lib.py", "def old():\n    return 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "lib")
    _write(repo, "pkg/lib.py", "def old():\n    return 1\n\n\ndef newer():\n    return 2\n")
    _write(repo, "pkg/user.py", "from pkg.lib import newer\n\n\ndef go():\n    return newer()\n")
    findings = land_run._integrity(repo, {}, ["pkg/user.py"], [], None, "ask-20261119T000000-aaaaaa", None)
    assert any(f.reason == "import-closure-untracked" and f.path == "pkg/user.py" for f in findings), findings


def test_import_closure_accepts_a_name_the_landing_itself_carries(repo):
    _write(repo, "pkg/__init__.py", "")
    _write(repo, "pkg/lib.py", "def old():\n    return 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "lib")
    _write(repo, "pkg/lib.py", "def old():\n    return 1\n\n\ndef newer():\n    return 2\n")
    _write(repo, "pkg/user.py", "from pkg.lib import newer\n\n\ndef go():\n    return newer()\n")
    findings = land_run._integrity(
        repo, {}, ["pkg/user.py", "pkg/lib.py"], [], None, "ask-20261119T000000-aaaaaa", None,
    )
    assert not [f for f in findings if f.reason == "import-closure-untracked"], findings


def _peer_defined_name_setup(repo):
    _write(repo, "pkg/__init__.py", "")
    _write(repo, "pkg/lib.py", "def old():\n    return 1\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "lib")
    own_only = _blob(repo, "def old():\n    return 1\n")
    # The worktree lib also carries a peer's `newer`; the settled own-only blob does not.
    _write(repo, "pkg/lib.py", "def old():\n    return 1\n\n\ndef newer():\n    return 2\n")
    _write(repo, "pkg/user.py", "from pkg.lib import newer\n\n\ndef go():\n    return newer()\n")
    return own_only


def test_import_closure_reads_the_settled_blob_not_worktree_bytes(repo):
    own_only = _peer_defined_name_setup(repo)
    run = "ask-20261119T000000-aaaaaa"
    paths = ["pkg/user.py", "pkg/lib.py"]
    settled = land_run._integrity(repo, {}, paths, [], None, run, None, landing_blobs={"pkg/lib.py": own_only})
    assert any(f.reason == "import-closure-untracked" and f.path == "pkg/user.py" for f in settled), settled
    worktree = land_run._integrity(repo, {}, paths, [], None, run, None)
    assert not [f for f in worktree if f.reason == "import-closure-untracked"], worktree
