"""A path a delivery note cites as an input or provenance is not a shipped claim."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.shipped_claims import extract_shipped_claims, shipped_gate
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

INPUT = "scratch/overnight/carry/16cd3b-margins-line224.diff"


def _paths(note: str) -> list[str]:
    return [c.path for c in extract_shipped_claims(note)]


def test_inline_provenance_and_input_sections_are_not_claims():
    note = (
        "## What shipped\n"
        f"- `a/b.py` (source: `{INPUT}`)\n"
        f"- `c.py`, derived from `d.diff`\n"
        "- `e.md` - provenance\n"
        "### Inputs\n- `f.diff`\n"
        "### More\n- `g.py`\n"
    )
    assert _paths(note) == ["a/b.py", "c.py", "g.py"]


def test_plain_delivered_paths_still_count():
    assert _paths("## Shipped\n- `a.py` and `b.py` from the spec\n") == ["a.py", "b.py"]


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    kw = no_console_creationflags()
    for args in (("init", "-q"), ("config", "user.email", "t@e.com"), ("config", "user.name", "t")):
        subprocess.run(["git", *args], cwd=str(root), check=True, capture_output=True, **kw)
    (root / "a.py").write_text("a\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "add", "a.py"], cwd=str(root), check=True, capture_output=True, **kw)
    subprocess.run(["git", "commit", "-qm", "i"], cwd=str(root), check=True, capture_output=True, **kw)
    d = root / "scratch/overnight/carry"
    d.mkdir(parents=True)
    (d / "16cd3b-margins-line224.diff").write_text("x\n", encoding="utf-8", newline="\n")
    return root


def test_untracked_em_context_input_cited_as_provenance_does_not_refuse(repo):
    note = f"## What shipped\n- `a.py` (changed)\n- Source text: `{INPUT}`\n"
    refusal, _ = shipped_gate(repo, note, landing_paths=["a.py"])
    assert refusal is None


def test_same_path_listed_as_delivered_refuses_and_names_the_remedy(repo):
    refusal, _ = shipped_gate(repo, f"## What shipped\n- `{INPUT}`\n", landing_paths=["a.py"])
    assert refusal and INPUT in refusal and "cite it as provenance" in refusal and "drop it" in refusal
