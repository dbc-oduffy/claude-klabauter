"""`brief` reports ancestor-of / same-tree-as relations between candidate branches."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from coordinator_core import consolidate_assemble as ca

NOW = 1_800_000_000.0
OLD = int(NOW - ca.RECENCY_FLOOR_SECONDS - 3600)
SEP = "\x1f"


def _sha(n: int) -> str:
    return f"{n:040x}"


def _fake_git(branches: dict[str, int], commits: dict[int, tuple[list[int], str]]):
    """`branches` maps name to tip commit id; `commits` maps id to (parent ids, tree)."""
    rows = (
        f"refs/heads/current\tcurrent\tme@x\t\t{OLD}\n"
        f"refs/heads/main\tmain\tme@x\t\t{OLD}\n"
        + "".join(f"refs/heads/{n}\t{n}\tme@x\t\t{OLD}\n" for n in branches)
    )
    calls: list[list[str]] = []

    def run_git(args: list[str], cwd: Path, input: str | None = None) -> SimpleNamespace:
        calls.append(list(args))
        while args and args[0].startswith("-"):
            args = args[1:]
        out, rc = "", 0
        if args[:2] == ["config", "user.email"]:
            out = "me@x\n"
        elif args[:2] == ["rev-parse", "--abbrev-ref"]:
            out = "current\n"
        elif args[:2] == ["rev-parse", "--verify"]:
            rc = 0 if args[-1] == "main" else 1
        elif args[0] == "for-each-ref":
            out = rows
        elif args[0] == "log" and args[1].startswith("--format="):
            assert "%T" in args[1]
            reach: set[int] = set()
            stack = [branches[r] for r in args[2:] if r in branches]
            while stack:
                c = stack.pop()
                if c not in reach:
                    reach.add(c)
                    stack.extend(commits[c][0])
            out = "".join(
                f"{_sha(c)}{SEP}{' '.join(_sha(p) for p in commits[c][0])}{SEP}{commits[c][1]}{SEP}c{c} subj{c}\n"
                for c in sorted(reach, reverse=True)
            )
        elif args[0] == "rev-parse" and args[-1].endswith("^{commit}"):
            out = "".join(f"{_sha(branches[a[: -len('^{commit}')]])}\n" for a in args[1:])
        elif args[0] == "worktree":
            out = "worktree /repo\nHEAD abc\nbranch refs/heads/current\n"
        elif args[0] == "merge-base":
            rc = 0
        return SimpleNamespace(returncode=rc, stdout=out, stderr="")

    run_git.calls = calls  # type: ignore[attr-defined]
    return run_git


def _brief(branches, commits):
    run_git = _fake_git(branches, commits)
    return ca.brief(repo_root=Path("/repo"), run_git=run_git, now=NOW), run_git


def test_three_branches_behind_a_merge_tip_yield_three_ancestor_rows() -> None:
    commits = {
        1: ([], "t1"),
        2: ([], "t2"),
        3: ([], "t3"),
        4: ([1, 2, 3], "t4"),
    }
    obj, _ = _brief({"a": 1, "b": 2, "c": 3, "d": 4}, commits)
    rels = obj["gates"]["relations"]
    assert sorted((r["branch"], r["other"]) for r in rels if r["relation"] == "ancestor-of") == [
        ("a", "d"),
        ("b", "d"),
        ("c", "d"),
    ]
    assert not [r for r in rels if r["relation"] == "same-tree-as"]
    jp = next(j for j in obj["judgment_points"] if j["id"] == "j-absorb-a")
    assert jp["evidence"]["covered_by"] == ["d"]
    assert "Covered by d" in jp["question"]
    jp_d = next(j for j in obj["judgment_points"] if j["id"] == "j-absorb-d")
    assert "covered_by" not in jp_d["evidence"]


def test_equal_tree_tips_yield_one_same_tree_row() -> None:
    commits = {1: ([], "tX"), 2: ([], "tX")}
    obj, _ = _brief({"a": 1, "b": 2}, commits)
    rels = obj["gates"]["relations"]
    assert rels == [{"relation": "same-tree-as", "branch": "a", "other": "b"}]
    jp_a = next(j for j in obj["judgment_points"] if j["id"] == "j-absorb-a")
    jp_b = next(j for j in obj["judgment_points"] if j["id"] == "j-absorb-b")
    assert jp_a["evidence"]["covered_by"] == ["b"]
    assert jp_b["evidence"]["covered_by"] == ["a"]


def test_unrelated_branches_yield_no_relations() -> None:
    commits = {1: ([], "t1"), 2: ([], "t2")}
    obj, _ = _brief({"a": 1, "b": 2}, commits)
    assert obj["gates"]["relations"] == []
    jp = next(j for j in obj["judgment_points"] if j["id"] == "j-absorb-a")
    assert "covered_by" not in jp["evidence"]


def test_relations_cost_no_extra_git_log() -> None:
    commits = {1: ([], "t1"), 2: ([1], "t2")}
    _, run_git = _brief({"a": 1, "b": 2}, commits)
    logs = [c for c in run_git.calls if c and c[0] == "log" and c[1].startswith("--format=%H")]
    assert len(logs) == 1


def test_three_field_log_rows_still_parse_without_relations() -> None:
    def run_git(args, cwd):
        if args[0] == "log":
            return SimpleNamespace(returncode=0, stdout=f"{_sha(1)}{SEP}{SEP}c1 s\n", stderr="")
        return SimpleNamespace(returncode=0, stdout=f"{_sha(1)}\n", stderr="")

    assert ca.unique_commits_by_ref(run_git, Path("/repo"), "main", ["a"]) == {"a": ["c1 s"]}


def test_emitted_relations_are_capped_but_covered_by_stays_complete() -> None:
    n = 14
    commits = {i: ([i - 1] if i > 1 else [], f"t{i}") for i in range(1, n + 1)}
    obj, _ = _brief({f"b{i:02d}": i for i in range(1, n + 1)}, commits)
    total = n * (n - 1) // 2
    assert total > ca.RELATIONS_CAP
    assert len(obj["gates"]["relations"]) == ca.RELATIONS_CAP
    assert obj["gates"]["relations_omitted"] == total - ca.RELATIONS_CAP
    jp = next(j for j in obj["judgment_points"] if j["id"] == "j-absorb-b01")
    assert len(jp["evidence"]["covered_by"]) == n - 1


def test_relations_under_cap_report_no_omitted_count() -> None:
    obj, _ = _brief({"a": 1, "b": 2}, {1: ([], "t1"), 2: ([], "t2")})
    assert "relations_omitted" not in obj["gates"]
