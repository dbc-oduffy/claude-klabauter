"""shipped_gate names each refusing claim's source and the landing's reason, and routes row and peer causes."""

from __future__ import annotations

import subprocess

import pytest

from coordinator_core.ops.dispatch_emit.shipped_claims import (
    NOT_DECLARED,
    ROW_GRADE_OWED,
    SHIPPED_CLAIM_REFUSED,
    SHIPPED_VERDICTS,
    ShippedClaim,
    judge_shipped_claims,
    refused_code,
    shipped_gate,
)
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

GRADE = "executor PARTIAL: one optional test could not be built"


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
    (root / "tracked.py").write_text("a\n", encoding="utf-8", newline="\n")
    git("add", ".")
    git("commit", "-q", "-m", "seed")
    (root / "tracked.py").write_text("b\n", encoding="utf-8", newline="\n")
    for name in ("new.py", "other.py", "peer.py", "held.py"):
        (root / name).write_text("n\n", encoding="utf-8", newline="\n")
    return root


def _note(*paths):
    return "## What shipped\n" + "".join(f"- `{p}` delivered\n" for p in paths)


def _verdicts(root, *paths, **kw):
    return {v.claim.path: v for v in judge_shipped_claims(root, [ShippedClaim(p, f"`{p}`") for p in paths], **kw)}


def test_new_verdicts_are_declared_and_not_in_the_unbacked_set():
    assert ROW_GRADE_OWED == "partial-row-grade-owed"
    assert {"peer-owned", "row-ungraded"} <= set(SHIPPED_VERDICTS)


def test_legacy_call_keeps_leading_refusal_text_and_reply_keys(repo):
    refusal, reply = shipped_gate(repo, _note("new.py", "tracked.py"))
    assert refusal.startswith("shipped-claim-unbacked: `new.py` is untracked: ")
    assert "re-fire the same sizing" in refusal
    assert set(reply) == {"checked", "unbacked", "unresolved"}
    assert len(reply["unbacked"]) == 2


@pytest.mark.parametrize(
    "path,withheld",
    [("tracked.py", ()), ("new.py", ()), ("held.py", ["held.py"])],
)
def test_row_of_reroutes_a_would_refuse_claim_to_row_ungraded(repo, path, withheld):
    refusal, reply = shipped_gate(
        repo, _note(path), withheld_paths=withheld, row_of={path: ("C3", GRADE)}, run_id="run-1"
    )
    assert refusal.startswith("partial-row-grade-owed:")
    assert "C3" in refusal and GRADE in refusal and f"`{path}`" in refusal
    assert 'workflow.revise run_id=run-1 credit_chunks=[{chunk: "C3", evidence}]' in refusal
    assert reply["row_grade_owed"] == [{"chunk": "C3", "path": path, "grade": GRADE}]
    assert reply["unbacked"] == []
    assert refused_code(reply) == ROW_GRADE_OWED


def test_a_reasoned_withhold_on_a_row_path_keeps_refusing_unbacked(repo):
    refusal, reply = shipped_gate(
        repo, _note("held.py"), withheld_paths={"held.py": "never-commit"}, row_of={"held.py": ("C3", GRADE)}
    )
    assert refusal.startswith(SHIPPED_CLAIM_REFUSED)
    assert "never-commit" in refusal and "What shipped" in refusal
    assert "row_grade_owed" not in reply
    assert refused_code(reply) == SHIPPED_CLAIM_REFUSED


def test_owners_is_called_once_with_only_non_own_non_row_would_refuse_paths(repo):
    calls = []

    def owners(paths):
        calls.append(list(paths))
        return {"peer.py": "run-sib"}

    refusal, reply = shipped_gate(
        repo, _note("peer.py", "other.py", "tracked.py", "new.py"),
        own_paths={"other.py"}, row_of={"tracked.py": ("C3", GRADE)}, owners=owners,
    )
    assert calls == [["peer.py", "new.py"]]
    assert reply["peer_owned"] == [{"path": "peer.py", "owner": "run-sib"}]
    assert "peer.py" not in refusal
    assert "`new.py`" in refusal and NOT_DECLARED in refusal


def test_peer_owned_alone_does_not_refuse(repo):
    refusal, reply = shipped_gate(repo, _note("peer.py"), owners=lambda paths: {"peer.py": "run-sib"})
    assert refusal is None
    assert reply["peer_owned"] == [{"path": "peer.py", "owner": "run-sib"}] and reply["unbacked"] == []


def test_owners_is_not_called_on_a_passing_note_or_with_no_candidate(repo):
    def boom(paths):
        raise AssertionError("owners called")

    assert shipped_gate(repo, _note("tracked.py"), landing_paths=["tracked.py"], owners=boom)[0] is None
    shipped_gate(repo, _note("other.py"), own_paths={"other.py"}, owners=boom, landing_paths=["other.py"])
    shipped_gate(repo, _note("tracked.py"), row_of={"tracked.py": ("C3", GRADE)}, owners=boom)


def test_an_owners_that_raises_leaves_the_refusal(repo):
    def boom(paths):
        raise RuntimeError("index unreadable")

    refusal, reply = shipped_gate(repo, _note("new.py"), owners=boom)
    assert refusal.startswith(SHIPPED_CLAIM_REFUSED) and "peer_owned" not in reply


def test_every_refusing_claim_is_named_with_source_and_reason_unbacked_first(repo):
    refusal, reply = shipped_gate(
        repo, _note("tracked.py", "new.py", "held.py"),
        withheld_paths={"held.py": "never-commit"}, own_paths={"tracked.py", "held.py"},
        row_of={"tracked.py": ("C3", GRADE)}, run_id="run-1",
    )
    assert refusal.startswith("shipped-claim-unbacked:")
    assert refusal.index("`held.py`") < refusal.index("`tracked.py`")
    assert refusal.index("`new.py`") < refusal.index("`tracked.py`")
    assert "delivery note's What shipped" in refusal
    assert "never-commit" in refusal and NOT_DECLARED in refusal
    assert "Decide row C3" in refusal and 'chunk: "C3"' in refusal
    assert refused_code(reply) == SHIPPED_CLAIM_REFUSED
    assert len(reply["unbacked"]) == 2 and len(reply["row_grade_owed"]) == 1


def test_left_out_reason_per_verdict(repo):
    got = _verdicts(repo, "new.py", "other.py", "held.py", own_paths={"other.py"}, withheld_paths={"held.py": "peer-held"})
    assert got["new.py"].left_out == NOT_DECLARED
    assert got["other.py"].left_out == got["other.py"].why
    assert got["held.py"].left_out == "peer-held"
