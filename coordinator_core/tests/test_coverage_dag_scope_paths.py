
from __future__ import annotations

import subprocess
from pathlib import Path
from typing import List

import pytest

from coordinator_core import coverage as cov
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=True,
    **no_console_creationflags(),
)


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def _write_and_commit(repo: Path, rel_paths: List[str], message: str) -> str:
    for rel in rel_paths:
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(f"{message}\n")
        _git(["add", rel], repo)
    _git(["commit", "-m", message], repo)
    return _git(["rev-parse", "HEAD"], repo).stdout.strip()


def test_filter_shas_by_scope_paths_normalizes_abbreviated_shas(tmp_path: Path) -> None:
    """An ABBREVIATED, out-of-scope sha must not be credited. `git diff-tree
    --stdin` echoes an unmatched abbreviation back verbatim (unlike a full
    sha, which it suppresses on no-match) -- without normalizing to the full
    object name first, that echo would land back in the matched set and the
    scope filter would silently pass the commit through uncredited-checked.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    out_of_scope_full = _write_and_commit(repo, ["docs/readme.md"], "out-of-scope commit")
    in_scope_full = _write_and_commit(repo, ["src/foo.py"], "in-scope commit")

    out_of_scope_abbrev = out_of_scope_full[:8]
    in_scope_abbrev = in_scope_full[:8]

    filtered, note = cov._filter_shas_by_scope_paths(
        [out_of_scope_abbrev, in_scope_abbrev], ["src/"], str(repo)
    )

    assert note is None, f"expected no failure note; got {note!r}"
    assert filtered is not None
    assert set(filtered) == {in_scope_full}, (
        f"abbreviated out-of-scope sha {out_of_scope_abbrev!r} must not be "
        f"credited via echo-back; filtered={filtered!r}"
    )


def test_filter_shas_by_scope_paths_fails_closed_on_git_error(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    _write_and_commit(repo, ["src/foo.py"], "seed commit")

    bogus_sha = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    filtered, note = cov._filter_shas_by_scope_paths([bogus_sha], ["src/"], str(repo))

    assert filtered is None
    assert note is not None and "scope_paths filter failed" in note
