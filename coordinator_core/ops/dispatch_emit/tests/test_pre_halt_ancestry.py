"""cited_at_or_before over a real tmp_path repo: A -> B (halt) -> C (post-halt), orphan O, a blob."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import pre_halt_ancestry as mod
from coordinator_core.ops.dispatch_emit.pre_halt_ancestry import (
    MAX_CITED,
    cited_at_or_before,
    verified_post_commit_credit,
)


def _g(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(root: Path, name: str) -> str:
    (root / name).write_text(name)
    _g(root, "add", name)
    _g(root, "commit", "-m", name)
    return _g(root, "rev-parse", "HEAD")


pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


@pytest.fixture()
def repo(tmp_path: Path):
    _g(tmp_path, "init", "-q")
    a = _commit(tmp_path, "a.txt")
    b = _commit(tmp_path, "b.txt")
    c = _commit(tmp_path, "c.txt")
    _g(tmp_path, "checkout", "-q", "--orphan", "other")
    o = _commit(tmp_path, "o.txt")
    _g(tmp_path, "checkout", "-q", "-f", "master" if "master" in _g(tmp_path, "branch", "-a") else "main")
    blob = _g(tmp_path, "rev-parse", "HEAD:a.txt")
    return tmp_path, {"A": a, "B": b, "C": c, "O": o, "blob": blob}


def _item(rid: str, met_by, status: str = "met") -> dict:
    return {"id": rid, "status": status, "met_by": met_by}


def _run(repo, items, ids=("R1",)):
    root, sh = repo
    return cited_at_or_before(root, sh["B"], items, ids)


def test_names_halt_commit(repo):
    assert _run(repo, [_item("R1", repo[1]["B"][:10])]) == {"R1": repo[1]["B"]}


def test_names_ancestor_by_short_sha(repo):
    a = repo[1]["A"]
    # sha_tokens needs a digit AND a letter; the commit sha is random, so take the shortest qualifying prefix.
    tok = next(
        (a[:n] for n in range(7, 41) if any(c.isdigit() for c in a[:n]) and any(c.isalpha() for c in a[:n])),
        a,
    )
    assert _run(repo, [_item("R1", [tok])]) == {"R1": a}


def test_post_halt_commit_not_credited(repo):
    assert _run(repo, [_item("R1", repo[1]["C"])]) == {}


def test_orphan_commit_not_credited(repo):
    assert _run(repo, [_item("R1", repo[1]["O"])]) == {}


def test_unresolvable_token_not_credited(repo):
    assert _run(repo, [_item("R1", "abcdef1234")]) == {}


def test_blob_not_credited_no_raise(repo):
    assert _run(repo, [_item("R1", repo[1]["blob"])]) == {}


def test_not_met_not_credited(repo):
    assert _run(repo, [_item("R1", repo[1]["B"], status="not_met")]) == {}


def test_default_statuses_do_not_cite_post_commit(repo):
    assert _run(repo, [_item("R1", repo[1]["B"], status="post_commit")]) == {}


def test_post_commit_statuses_cite_post_commit(repo):
    root, sh = repo
    items = [_item("R1", sh["B"], status="post_commit")]
    assert cited_at_or_before(root, sh["B"], items, ["R1"], statuses=("post_commit",)) == {"R1": sh["B"]}


def test_verified_post_commit_credit_halt_sha_verified(repo):
    root, sh = repo
    items = [_item("R1", sh["B"], status="post_commit")]
    assert verified_post_commit_credit(root, sh["B"], items, {"R1"}) == frozenset({"R1"})


def test_verified_post_commit_credit_post_halt_sha_not_verified(repo):
    root, sh = repo
    items = [_item("R1", sh["C"], status="post_commit")]
    assert verified_post_commit_credit(root, sh["B"], items, {"R1"}) == frozenset()


def test_verified_post_commit_credit_unresolvable_sha_not_verified(repo):
    root, sh = repo
    items = [_item("R1", "abcdef1234", status="post_commit")]
    assert verified_post_commit_credit(root, sh["B"], items, {"R1"}) == frozenset()


def test_verified_post_commit_credit_unresolvable_sha_never_exempts_itself(repo):
    root, sh = repo
    items = [
        _item("R1", "abcdef1234", status="post_commit"),
        _item("R2", sh["B"], status="post_commit"),
    ]
    assert verified_post_commit_credit(root, sh["B"], items, {"R1", "R2"}) == frozenset({"R2"})


def test_verified_post_commit_credit_git_failure_fails_closed(repo, tmp_path_factory):
    notrepo = tmp_path_factory.mktemp("norepo")
    items = [_item("R1", repo[1]["B"], status="post_commit")]
    assert verified_post_commit_credit(notrepo, repo[1]["B"], items, {"R1"}) == frozenset()


def test_verified_post_commit_credit_met_only_spawns_nothing(repo, monkeypatch):
    root, sh = repo
    calls: list = []
    monkeypatch.setattr(mod, "run_git", lambda args, **kw: calls.append(args))
    items = [_item("R1", sh["B"])]
    assert verified_post_commit_credit(root, sh["B"], items, {"R1"}) == frozenset()
    assert calls == []


def test_id_outside_ids_absent(repo):
    items = [_item("R1", repo[1]["B"]), _item("R2", repo[1]["B"])]
    assert _run(repo, items, ids=("R1",)) == {"R1": repo[1]["B"]}


def test_mixed_unresolvable_and_ancestor(repo):
    a = repo[1]["A"]
    assert _run(repo, [_item("R1", ["abcdef1234", a])]) == {"R1": a}


def test_only_first_max_cited_tokens_considered(repo):
    fillers = [f"1234567{i}ab" for i in range(MAX_CITED)]
    items = [_item("R1", [*fillers, repo[1]["B"]])]
    assert _run(repo, items) == {}
    items = [_item("R1", [*fillers[: MAX_CITED - 1], repo[1]["B"]])]
    assert _run(repo, items) == {"R1": repo[1]["B"]}


@pytest.mark.parametrize("items", [[None], ["x"], [{"id": "R1"}], "notalist", None, [{"id": "R1", "status": "met", "met_by": None}]])
def test_malformed_input_returns_empty(repo, items):
    assert _run(repo, items) == {}


def test_git_failure_returns_empty(repo, tmp_path_factory):
    notrepo = tmp_path_factory.mktemp("norepo")
    assert cited_at_or_before(notrepo, repo[1]["B"], [_item("R1", repo[1]["B"])], ["R1"]) == {}


def test_no_spawn_when_no_token_and_at_most_two_otherwise(repo, monkeypatch):
    calls: list = []
    real = mod.run_git

    def counting(args, **kw):
        calls.append(list(args))
        return real(args, **kw)

    monkeypatch.setattr(mod, "run_git", counting)
    assert _run(repo, [_item("R1", "no sha here"), _item("R2", None)], ids=("R1", "R2")) == {}
    assert calls == []
    items = [_item("R1", repo[1]["B"]), _item("R2", repo[1]["A"]), _item("R3", repo[1]["C"])]
    got = _run(repo, items, ids=("R1", "R2", "R3"))
    assert got == {"R1": repo[1]["B"], "R2": repo[1]["A"]}
    assert len(calls) == 2
