"""A `--type sizing-object` scaffold refuses an absent `--title` instead of minting.

Measured 2026-09-11 on example-cockpit-repo: `coordinator-doc-new --type sizing-object`
run with no arguments, to discover the interface, wrote an unfilled template into
`state/sizings/` whose intent read "PLACEHOLDER — replace with the PM's ask,
verbatim". The second-order problem was worse than the first — the EM could not
delete it either, because the destructive-rm guard correctly refuses an untracked
file, so the stray outlived the tool that made it.

A sizing-object is the one scaffold with no useful untitled form: its title IS the
PM's ask. Every other type keeps its placeholder, because scaffolding untitled and
filling the title in afterwards is a supported workflow there.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.spawns_process]

_CLI_PATH = Path(__file__).resolve().parent.parent / "coordinator-doc-new.py"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q", ".")
    return tmp_path


def _run(repo: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(_CLI_PATH), *args],
        cwd=str(repo),
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_an_untitled_sizing_object_is_refused_and_writes_nothing(repo: Path):
    result = _run(repo, "--type", "sizing-object")

    assert result.returncode != 0
    assert "--title is required" in result.stderr
    assert "Nothing was written" in result.stderr
    assert list(repo.rglob("*.yaml")) == []


def test_a_titled_sizing_object_still_scaffolds(repo: Path):
    """The refusal is about the absent title, not about the type — the positive
    verdict has to keep working or the guard has removed the tool."""
    result = _run(repo, "--type", "sizing-object", "--title", "A real PM ask")

    assert result.returncode == 0, result.stderr
    minted = list((repo / "state" / "sizings").glob("*.yaml"))
    assert len(minted) == 1
    # Other template fields legitimately carry their own PLACEHOLDER prompts; what
    # must not survive is the ASK itself being one.
    assert "PLACEHOLDER — replace with the PM's ask" not in minted[0].read_text(
        encoding="utf-8"
    )


def test_the_scaffolded_id_comment_names_the_join_route(repo: Path):
    """The stamp used to read "minted at scaffold time — do not hand-edit", which is
    wrong advice for the commonest case: a sizing object belonging to an EXISTING
    baton must carry that baton's id, joined, never re-minted. Measured 2026-09-11 on
    example-store-repo: the scaffolder produced a wrong value and then told the author not
    to fix it. That author overrode it; the next one might obey and leave two ids on
    one deliverable."""
    result = _run(repo, "--type", "sizing-object", "--title", "A real PM ask")

    assert result.returncode == 0, result.stderr
    stamped = (repo / "state" / "sizings").glob("*.yaml")
    text = next(stamped).read_text(encoding="utf-8")
    assert "--deliverable-id" in text
    assert "do not hand-edit" not in text


def test_an_explicit_id_is_joined_not_re_minted(repo: Path):
    """The flag the comment now names has to actually carry the id through."""
    carried = "dlv-an-existing-baton-101c06"

    result = _run(
        repo,
        "--type", "sizing-object",
        "--title", "Sized against an existing baton",
        "--deliverable-id", carried,
    )

    assert result.returncode == 0, result.stderr
    text = next((repo / "state" / "sizings").glob("*.yaml")).read_text(encoding="utf-8")
    assert carried in text
