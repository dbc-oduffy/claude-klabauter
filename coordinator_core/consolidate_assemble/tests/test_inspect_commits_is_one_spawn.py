"""inspect_commits is one stdin-fed git process whatever the sha count."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from coordinator_core import consolidate_assemble as ca


def _shas(n: int) -> list[str]:
    return [f"{i:040x}" for i in range(n)]


def _fake_log(calls: list[tuple[list[str], str | None]]):
    def run_git(args: list[str], cwd: Path, input: str | None = None) -> SimpleNamespace:
        calls.append((list(args), input))
        out = "".join(f"commit {s}\nAuthor: x\n\n    msg\n\n" for s in (input or "").split())
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    return run_git


def test_one_call_for_one_and_for_thousands_with_shas_on_stdin_only():
    for n in (1, 5000):
        shas = _shas(n)
        calls: list[tuple[list[str], str | None]] = []
        ca.inspect_commits(_fake_log(calls), Path("."), shas)
        assert len(calls) == 1
        argv, stdin = calls[0]
        assert "--stdin" in argv
        assert not any(s in argv for s in shas)
        assert stdin.split() == shas


def test_blocks_are_complete_and_ordered():
    shas = _shas(5000)
    blocks = ca.inspect_commits(_fake_log([]), Path("."), shas)
    assert list(blocks) == shas
    assert blocks[shas[-1]].startswith(f"commit {shas[-1]}")
    assert not blocks[shas[0]].endswith("\n\n\n")


def test_empty_input_spawns_nothing():
    calls: list[tuple[list[str], str | None]] = []
    assert ca.inspect_commits(_fake_log(calls), Path("."), []) == {}
    assert calls == []


def test_brief_inspects_only_the_newest_commits_per_branch_in_one_spawn():
    cap = ca.INSPECTED_COMMITS_PER_BRANCH
    chain = [f"{i + 1:040x}" for i in range(cap + 5)]
    inspected: list[list[str]] = []
    real = ca.inspect_commits

    def spy(run_git, repo_root, shas):
        inspected.append(list(shas))
        return real(run_git, repo_root, shas)

    def run_git(args, cwd, input=None):
        out = ""
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            out = "main\n"
        elif args[:2] == ["rev-parse", "--verify"]:
            out = "x\n" if args[-1] == "main" else ""
        elif args[0] == "for-each-ref":
            out = "refs/heads/main\tmain\tme@x\nrefs/heads/old\told\tother@x\n"
        elif args[0] == "log" and args[1].startswith("--format=%H"):
            rows = [f"{s}\x1f{chain[i + 1] if i + 1 < len(chain) else ''}\x1f{s[-7:]} c{i}" for i, s in enumerate(chain)]
            out = "\n".join(rows) + "\n"
        elif args[:2] == ["rev-parse", "old^{commit}"]:
            out = chain[0] + "\n"
        elif args[0] == "log" and "--stdin" in args:
            out = "".join(f"commit {s}\nAuthor: x\n\n    m\n\n" for s in input.split())
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    original = ca.inspect_commits
    ca.inspect_commits = spy
    try:
        decision = ca.brief(repo_root=Path("."), my_email="me@x", run_git=run_git)
    finally:
        ca.inspect_commits = original
    short = [s[-7:] for s in chain]
    assert inspected == [short[:cap]]
    jp = next(j for j in decision["judgment_points"] if j["id"] == "j-absorb-old")
    assert jp["evidence"]["commit_count"] == cap + 5
    assert len(jp["evidence"]["commits"]) == cap
    assert [i["sha"] for i in jp["evidence"]["inspections"]] == short[:cap]
