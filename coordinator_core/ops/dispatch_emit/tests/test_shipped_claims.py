"""shipped_claims extracts a note's shipped-file claims and judges each in process."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit import land_run
from coordinator_core.ops.dispatch_emit.shipped_claims import (
    SHIPPED_CLAIM_REFUSED,
    SHIPPED_VERDICTS,
    ShippedClaim,
    extract_shipped_claims,
    judge_shipped_claims,
    shipped_gate,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _paths(note: str) -> list[str]:
    return [c.path for c in extract_shipped_claims(note)]


def test_extraction_reads_shipped_sections_only():
    note = (
        "# A hand-shipped sizing\n`outside.py`\n"
        "## What shipped\n- `a/b.py:12` and `c\\d.py#frag`\n- `a/b.py` again\n"
        "```\n`fenced.py`\n```\n"
        "### Detail\n`deep.py`\n"
        "## Other\n`other.py`\n"
        "### R2 unshipped\n`unshipped.py`\n"
        "## Shipped files\n`op_scopes.py`\r\n"
        "## Verdicts\n## Shipped\n`late.py`\n"
    )
    assert _paths(note) == ["a/b.py", "c/d.py", "deep.py", "op_scopes.py"]


def test_a_heading_that_only_contains_the_word_is_not_a_shipped_section():
    assert _paths("# hand-shipped sizings\n`x.py`\n") == []
    assert _paths("## R1 unshipped\n`x.py`\n") == []


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()

    def git(*args):
        subprocess.run(
            ["git", *args], cwd=str(root), check=True, capture_output=True, **no_console_creationflags()
        )

    git("init", "-q")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    git("config", "core.autocrlf", "false")
    (root / "clean.py").write_text("a\n", encoding="utf-8", newline="\n")
    (root / "dirty.py").write_text("a\n", encoding="utf-8", newline="\n")
    (root / "gone.py").write_text("a\n", encoding="utf-8", newline="\n")
    (root / "pkg").mkdir()
    (root / "pkg" / "m.py").write_text("a\n", encoding="utf-8", newline="\n")
    git("add", ".")
    git("commit", "-q", "-m", "seed")
    (root / "dirty.py").write_text("b\n", encoding="utf-8", newline="\n")
    (root / "gone.py").unlink()
    (root / "new.py").write_text("n\n", encoding="utf-8", newline="\n")
    (root / "landing.py").write_text("n\n", encoding="utf-8", newline="\n")
    (root / "held.py").write_text("n\n", encoding="utf-8", newline="\n")
    return root


def _judge(root, *paths, **kw):
    claims = [ShippedClaim(p, f"`{p}`") for p in paths]
    return {v.claim.path: v.verdict for v in judge_shipped_claims(root, claims, **kw)}


def test_each_verdict_with_zero_subprocess_calls(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)

    got = _judge(
        repo, "clean.py", "dirty.py", "new.py", "landing.py", "held.py", "gone.py", "nope.py",
        "op_scopes.py", "pkg", "..\\x.py", "../x.py",
        landing_paths=["landing.py", "held.py"], withheld_paths=["held.py"],
    )

    assert got["clean.py"] == "backed"
    assert got["landing.py"] == "backed"
    assert got["dirty.py"] == "not-in-commit"
    assert got["new.py"] == "untracked"
    assert got["held.py"] == "withheld"
    assert got["gone.py"] == "unresolved"
    assert got["nope.py"] == "unresolved"
    assert got["op_scopes.py"] == "unresolved"
    assert got["pkg"] == "unresolved"
    assert got["../x.py"] == "unresolved"
    assert set(got.values()) <= set(SHIPPED_VERDICTS)


def test_a_deleted_landing_path_is_backed(repo):
    assert _judge(repo, "gone.py", deleted_paths=["gone.py"]) == {"gone.py": "backed"}


def test_a_filtered_tracked_file_is_unresolved_not_not_in_commit(repo, monkeypatch):
    monkeypatch.setattr(land_run, "_worktree_blob", lambda root, rel: land_run._FILTERED)
    assert _judge(repo, "clean.py", "new.py") == {"clean.py": "unresolved", "new.py": "untracked"}


def test_gate_refuses_an_untracked_claim_and_names_it(repo):
    note = "## What shipped\n- `new.py`\n- `op_scopes.py`\n- `clean.py`\n"
    refusal, reply = shipped_gate(repo, note)
    assert refusal and refusal.startswith(SHIPPED_CLAIM_REFUSED) and "`new.py`" in refusal
    assert "re-fire the same sizing" in refusal
    assert reply["checked"] == 3
    assert len(reply["unbacked"]) == 1 and "new.py" in reply["unbacked"][0]
    assert len(reply["unresolved"]) == 1 and "op_scopes.py" in reply["unresolved"][0]


def test_gate_lands_backed_and_unresolved_claims(repo):
    refusal, reply = shipped_gate(repo, "## What shipped\n`clean.py`, `op_scopes.py`\n")
    assert refusal is None
    assert reply == {"checked": 2, "unbacked": [], "unresolved": [reply["unresolved"][0]]}


def test_gate_is_empty_without_a_note_or_a_shipped_heading(repo):
    assert shipped_gate(repo, None) == (None, {})
    assert shipped_gate(repo, "## Summary\n`new.py`\n") == (None, {})
