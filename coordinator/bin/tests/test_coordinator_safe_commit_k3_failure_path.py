"""klabauter #72 K3 falsifier: the `coordinator-safe-commit` failure path, driven as a subprocess
in its default pathspec form (`"<subject>" -- <paths>`) against a throwaway `tmp_path` repo.

One case per defect. At base (0089c7010c) cases (a), (c) and (d) are red; (b) is green because no
ERROR-then-exit-0 site reproduces in the pathspec form (see `test_b_*`).

  (a) `test_a_*`  a mixed product+process call whose process half is rollback-refused lands the
      product commit, then reports "Nothing was written" beside "The product commit <sha> landed".
  (b) `test_b_*`  every printed `ERROR:` exits non-zero.
  (c) `test_c_*`  `--dry-run` previews a call the real run refuses as an exact-blob rollback.
  (d) `test_d_*`  a 0-byte `index.lock` with no holder is named "a peer holds the index".

Reply shape C1/C2 share, on any refusal AFTER a landing: `committed: true, partial: true, sha,
landed_paths, unlanded_paths, error`; the error text never contains "Nothing was written". A
refusal BEFORE any landing keeps `committed: false` and "Nothing was written" (true there).
"""

from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_WRAPPER = pathlib.Path(__file__).resolve().parent.parent / "coordinator-safe-commit.py"
_SUBJECT = "land the change now"
_NOPE = "Nothing was written"
_PEER = "a peer holds the index"


def _git(args, cwd) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout


def _repo(tmp_path: pathlib.Path) -> pathlib.Path:
    """A repo whose `state/n/a.md` history is v1, v2, v3, so writing v1 back is a depth-2 rollback."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(["add", "--", "seed.txt"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    (repo / "state" / "n").mkdir(parents=True)
    (repo / "src").mkdir()
    for i in (1, 2, 3):
        (repo / "state/n/a.md").write_text(f"v{i}\n", encoding="utf-8")
        _git(["add", "--", "state/n/a.md"], repo)
        _git(["commit", "-q", "-m", f"c{i}"], repo)
    return repo


def _head(repo: pathlib.Path) -> str:
    return _git(["rev-parse", "HEAD"], repo).strip()


def _wrapper(repo: pathlib.Path, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_WRAPPER), *argv], cwd=str(repo), capture_output=True,
        text=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _roll_back(repo: pathlib.Path) -> None:
    (repo / "state/n/a.md").write_text("v1\n", encoding="utf-8")


def test_a_mixed_call_with_a_refused_process_half_leaves_no_dishonest_partial_landing(tmp_path):
    repo = _repo(tmp_path)
    base = _head(repo)
    _roll_back(repo)
    (repo / "src/app.py").write_text("p\n", encoding="utf-8")

    run = _wrapper(repo, _SUBJECT, "--", "src/app.py", "state/n/a.md")

    assert run.returncode != 0, run.stderr
    if _head(repo) != base:
        assert _NOPE not in run.stderr, run.stderr
        assert "state/n/a.md" in run.stderr and "partial" in run.stderr.lower(), run.stderr


def test_b_every_printed_error_exits_non_zero(tmp_path):
    repo = _repo(tmp_path)
    _roll_back(repo)

    run = _wrapper(repo, _SUBJECT, "--", "state/n/a.md")

    assert "ERROR:" in run.stderr, run.stderr
    assert run.returncode != 0, run.stderr


def test_c_dry_run_refuses_what_the_real_run_refuses(tmp_path):
    repo = _repo(tmp_path)
    base = _head(repo)
    _roll_back(repo)

    real = _wrapper(repo, _SUBJECT, "--", "state/n/a.md")
    dry = _wrapper(repo, "--dry-run", _SUBJECT, "--", "state/n/a.md")

    assert real.returncode != 0 and "staged rollback detected" in real.stderr, real.stderr
    assert dry.returncode != 0, dry.stderr
    assert "staged rollback detected" in dry.stderr, dry.stderr
    assert _head(repo) == base


def test_c_dry_run_still_previews_a_clean_call_and_commits_nothing(tmp_path):
    repo = _repo(tmp_path)
    base = _head(repo)
    (repo / "src/app.py").write_text("p\n", encoding="utf-8")

    dry = _wrapper(repo, "--dry-run", _SUBJECT, "--", "src/app.py")

    assert dry.returncode == 0, dry.stderr
    assert "src/app.py" in dry.stderr
    assert _head(repo) == base


def test_d_a_lock_with_no_holder_is_reaped_not_named_a_peer(tmp_path):
    repo = _repo(tmp_path)
    (repo / "src/app.py").write_text("p\n", encoding="utf-8")
    lock = repo / ".git" / "index.lock"
    lock.write_text("", encoding="utf-8")

    run = _wrapper(repo, _SUBJECT, "--", "src/app.py")

    assert _PEER not in run.stderr, run.stderr
    assert not lock.exists(), run.stderr
