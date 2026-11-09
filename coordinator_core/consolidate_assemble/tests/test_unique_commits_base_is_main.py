from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from coordinator_core import consolidate_assemble as ca

_REF_ROWS = [
    ("refs/heads/current", "current", "me@x"),
    ("refs/heads/main", "main", "me@x"),
    ("refs/heads/work/stale", "work/stale", "me@x"),
]


def _fake_git(
    log_ranges: list[str],
    main_exists: bool,
    log_stdout: str = "",
    spawns: list[list[str]] | None = None,
    tip: str | None = None,
):
    def run_git(args: list[str], cwd: Path, **_kw: object) -> SimpleNamespace:
        if spawns is not None:
            spawns.append(args)
        if args[:2] == ["config", "user.email"]:
            return SimpleNamespace(returncode=0, stdout="me@x\n", stderr="")
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return SimpleNamespace(returncode=0, stdout="current\n", stderr="")
        if args[:2] == ["rev-parse", "--verify"]:
            ok = main_exists and args[-1] == "main"
            return SimpleNamespace(returncode=0 if ok else 1, stdout="", stderr="")
        if args[0] == "rev-parse" and tip is not None:
            return SimpleNamespace(returncode=0, stdout=f"{tip}\n", stderr="")
        if args[0] == "for-each-ref":
            rows = _REF_ROWS if main_exists else [r for r in _REF_ROWS if r[1] != "main"]
            out = "".join(f"{r}\t{s}\t{e}\n" for r, s, e in rows)
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        if args[0] == "log" and args[1].startswith("--format="):
            refs = [a for a in args[2:] if not a.startswith("^")]
            bases = [a.removeprefix("^") for a in args[2:] if a.startswith("^")]
            log_ranges.extend(f"{'+'.join(bases)}..{ref}" for ref in refs)
            return SimpleNamespace(returncode=0, stdout=log_stdout, stderr="")
        if args[0] == "worktree":
            return SimpleNamespace(
                returncode=0, stdout="worktree /repo\nHEAD abc\nbranch refs/heads/current\n", stderr=""
            )
        if args[0] == "merge-base":
            return SimpleNamespace(returncode=1, stdout="", stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    return run_git


def _stale_row(result: dict) -> dict:
    return next(b for b in result["gates"]["branches"] if b["name"] == "work/stale")


def test_brief_excludes_both_current_and_main_from_the_unique_walk(tmp_path: Path) -> None:
    ranges: list[str] = []
    ca.brief(repo_root=tmp_path, run_git=_fake_git(ranges, main_exists=True))

    assert ranges == ["current+main..work/stale"]


def test_brief_excludes_only_current_when_no_main_branch_exists(tmp_path: Path) -> None:
    ranges: list[str] = []
    ca.brief(repo_root=tmp_path, run_git=_fake_git(ranges, main_exists=False))

    assert ranges == ["current..work/stale"]


def test_ref_reachable_from_head_gets_delete_directive_and_one_log_spawn(tmp_path: Path) -> None:
    spawns: list[list[str]] = []
    result = ca.brief(repo_root=tmp_path, run_git=_fake_git([], True, spawns=spawns))

    assert _stale_row(result)["unique_commit_count"] == 0
    assert "d-delete-work/stale" in [d["id"] for d in result["directives"]]
    assert not any(j["id"].startswith("j-absorb-") for j in result["judgment_points"])
    assert sum(1 for a in spawns if a[0] == "log" and a[1].startswith("--format=%H")) == 1


def test_ref_with_commits_outside_head_and_main_still_gets_absorb_point(tmp_path: Path) -> None:
    sep = "\x1f"
    tip = "a" * 40
    log = f"{tip}{sep}{sep}{'b' * 40}{sep}aaaaaaa real work\n"

    result = ca.brief(repo_root=tmp_path, run_git=_fake_git([], True, log_stdout=log, tip=tip))

    assert _stale_row(result)["unique_commit_count"] == 1
    assert any(j["id"] == "j-absorb-work/stale" for j in result["judgment_points"])
