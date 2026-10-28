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


def test_a_recorded_widen_counts_as_declared_and_an_outside_path_stays_undeclared(repo):
    from coordinator_core.ops.dispatch_emit.shipped_claims import NOT_DECLARED

    claims = [ShippedClaim(p, f"`{p}`") for p in ("new.py", "dirty.py")]
    vs = {v.claim.path: v for v in judge_shipped_claims(repo, claims, own_paths={"x.py"}, widened=["new.py"])}
    assert vs["new.py"].verdict == "untracked" and vs["new.py"].left_out != NOT_DECLARED
    assert vs["dirty.py"].left_out == NOT_DECLARED


def test_a_widened_claim_skips_the_peer_owner_lookup(repo):
    seen = []

    def owners(paths):
        seen.extend(paths)
        return {p: "peer" for p in paths}

    note = "## What shipped\n- `new.py`\n- `dirty.py`\n"
    shipped_gate(repo, note, own_paths={"x.py"}, widened=["new.py"], owners=owners)
    assert seen == ["dirty.py"]


def test_each_verdict_with_zero_subprocess_calls(repo, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)

    got = _judge(
        repo, "clean.py", "dirty.py", "new.py", "landing.py", "held.py", "gone.py", "nope.py",
        "op_scopes.py", "..\\x.py", "../x.py",
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
    assert got["../x.py"] == "unresolved"
    assert set(got.values()) <= set(SHIPPED_VERDICTS)


def _write(root, rel, text="n\n"):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8", newline="\n")


def test_an_undeclared_directory_is_a_mention_never_a_claim(repo):
    for f in ("run/a.txt", "run/sub/b.txt"):
        _write(repo, f)
    _write(repo, "pkg/m.py", "changed\n")
    assert _judge(repo, "run", "run/", "pkg", "pkg/") == {
        "run": "unresolved", "run/": "unresolved", "pkg": "unresolved", "pkg/": "unresolved",
    }
    (repo / "empty").mkdir()
    assert _judge(repo, "empty", "ghost") == {"empty": "unresolved", "ghost": "unresolved"}


def test_a_directory_the_landing_declares_is_backed_or_withheld(repo):
    _write(repo, "run/a.txt")
    _write(repo, "run/untracked_peer.txt")
    assert _judge(repo, "run", landing_paths=["run/a.txt"]) == {"run": "backed"}
    got = _judge(repo, "run", landing_paths=["run/a.txt"], withheld_paths=["run/a.txt"])
    assert got == {"run": "withheld"}
    assert _judge(repo, "run", deleted_paths=["run/a.txt"]) == {"run": "backed"}


def test_directory_claims_cost_no_spawn(repo, monkeypatch):
    from coordinator_core.git import run as git_run

    calls = []
    real = git_run.run_git
    monkeypatch.setattr(git_run, "run_git", lambda *a, **k: calls.append(a) or real(*a, **k))
    _write(repo, "run/a.txt")
    _judge(repo, "clean.py", "new.py", "run", "pkg", landing_paths=["run/a.txt"])
    assert calls == []


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


def test_a_backticked_directory_in_a_shipped_note_is_not_refused_but_an_undelivered_file_is(repo):
    _write(repo, "run/a.txt")
    note = "## What shipped\n- `clean.py`\n- test case: redirect into `run`, and `run/` again\n"
    refusal, reply = shipped_gate(repo, note)
    assert refusal is None and reply["checked"] == 3
    assert shipped_gate(repo, "## What shipped\n- `run/`\n1. `run`\n")[0] is None
    assert shipped_gate(repo, "## What shipped\n- redirect into `run`\n- `new.py`\n")[0].startswith(
        SHIPPED_CLAIM_REFUSED
    )
    assert shipped_gate(repo, "## What shipped\nWrote `new.py` here.\n")[0].startswith(SHIPPED_CLAIM_REFUSED)


def test_gate_is_empty_without_a_note_or_a_shipped_heading(repo):
    assert shipped_gate(repo, None) == (None, {})
    assert shipped_gate(repo, "## Summary\n`new.py`\n") == (None, {})
