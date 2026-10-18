"""Tests for attestation.restore against a throwaway git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

from coordinator_core.architecture_survey import attestation
from coordinator_core.win_portability import no_console_creationflags

SYS = "docs/architecture/systems"


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, **no_console_creationflags())


def _page(stamp: str, nl: str) -> bytes:
    return nl.join(["---", f"last_attested: {stamp}", "title: x", "---", "body", ""]).encode()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@t")
    _git(tmp_path, "config", "user.name", "t")
    (tmp_path / SYS).mkdir(parents=True)
    return tmp_path


def _commit(root: Path) -> None:
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "base")


def test_untouched_is_none_touched(repo: Path) -> None:
    (repo / SYS / "a.md").write_bytes(_page("2026-01-01", "\n"))
    _commit(repo)
    assert attestation.restore(repo).chosen == "none-touched"


def test_bumped_stamp_restored(repo: Path) -> None:
    p = repo / SYS / "a.md"
    p.write_bytes(_page("2026-01-01", "\n"))
    _commit(repo)
    p.write_bytes(_page("2026-10-12", "\n"))
    d = attestation.restore(repo)
    assert d.chosen == "restored"
    assert p.read_bytes() == _page("2026-01-01", "\n")
    assert "2026-01-01" in d.evidence and "2026-10-12" in d.evidence


def test_new_page_stamp_removed(repo: Path) -> None:
    (repo / SYS / "a.md").write_bytes(_page("2026-01-01", "\n"))
    _commit(repo)
    p = repo / SYS / "b.md"
    p.write_bytes(_page("2026-10-12", "\n"))
    _git(repo, "add", "-N", str(p))
    attestation.restore(repo)
    assert b"last_attested" not in p.read_bytes()
    assert b"title: x" in p.read_bytes()


def test_crlf_and_lf_preserved(repo: Path) -> None:
    a, b = repo / SYS / "a.md", repo / SYS / "b.md"
    a.write_bytes(_page("2026-01-01", "\r\n"))
    b.write_bytes(_page("2026-01-01", "\n"))
    _commit(repo)
    a.write_bytes(_page("2026-10-12", "\r\n"))
    b.write_bytes(_page("2026-10-12", "\n"))
    attestation.restore(repo)
    assert a.read_bytes() == _page("2026-01-01", "\r\n")
    assert b.read_bytes() == _page("2026-01-01", "\n")


def test_ten_pages_one_spawn(repo: Path) -> None:
    for i in range(10):
        (repo / SYS / f"p{i}.md").write_bytes(_page("2026-01-01", "\n"))
    _commit(repo)
    for i in range(10):
        (repo / SYS / f"p{i}.md").write_bytes(_page("2026-10-12", "\n"))
    calls = []

    def runner(root, sdir):
        calls.append(1)
        return attestation._run_diff(root, sdir)

    d = attestation.restore(repo, runner=runner)
    assert len(calls) == 1 and d.chosen == "restored"
    assert all((repo / SYS / f"p{i}.md").read_bytes() == _page("2026-01-01", "\n") for i in range(10))


def test_nonzero_exit_raises(repo: Path) -> None:
    def runner(root, sdir):
        return subprocess.CompletedProcess([], 128, b"", b"fatal")

    with pytest.raises(attestation.AttestationError):
        attestation.restore(repo, runner=runner)
