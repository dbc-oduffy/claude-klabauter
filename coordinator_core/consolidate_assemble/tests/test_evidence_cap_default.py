from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from coordinator_core import consolidate_assemble as ca

_N = 25
_SEP = "\x1f"
_LOG = "".join(
    f"{i:040x}{_SEP}{(i + 1):040x}{_SEP}{i:07x} commit {i}\n" for i in range(_N)
)
_ROWS = [
    ("refs/heads/current", "current", "me@x"),
    ("refs/heads/main", "main", "me@x"),
    ("refs/heads/work/stale", "work/stale", "me@x"),
]


def _fake_git(shown: list[str]):
    def run_git(args: list[str], cwd: Path) -> SimpleNamespace:
        if args[:2] == ["config", "user.email"]:
            return SimpleNamespace(returncode=0, stdout="me@x\n", stderr="")
        if args[:2] == ["rev-parse", "--abbrev-ref"]:
            return SimpleNamespace(returncode=0, stdout="current\n", stderr="")
        if args[0] == "rev-parse" and args[-1].endswith("^{commit}"):
            return SimpleNamespace(returncode=0, stdout=f"{0:040x}\n", stderr="")
        if args[:2] == ["rev-parse", "--verify"]:
            return SimpleNamespace(returncode=0 if args[-1] == "main" else 1, stdout="", stderr="")
        if args[0] == "for-each-ref":
            out = "".join(f"{r}\t{s}\t{e}\n" for r, s, e in _ROWS)
            return SimpleNamespace(returncode=0, stdout=out, stderr="")
        if args[0] == "log" and args[1].startswith("--format="):
            return SimpleNamespace(returncode=0, stdout=_LOG, stderr="")
        if args[0] == "show":
            shown.extend(a for a in args if len(a) == 40 and not a.startswith("-"))
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if args[0] == "worktree":
            return SimpleNamespace(
                returncode=0, stdout="worktree /repo\nHEAD abc\nbranch refs/heads/current\n", stderr=""
            )
        return SimpleNamespace(returncode=1, stdout="", stderr="")

    return run_git


def _evidence(decision: dict) -> dict:
    jps = [j for j in decision["judgment_points"] if j["id"] == "j-absorb-work/stale"]
    assert jps, decision["judgment_points"]
    return jps[0]["evidence"]


def test_default_caps_commits_and_inspections(tmp_path: Path) -> None:
    shown: list[str] = []
    ev = _evidence(ca.brief(repo_root=tmp_path, run_git=_fake_git(shown)))

    assert ev["commit_count"] == _N
    assert len(ev["commits"]) == ca.EVIDENCE_COMMIT_CAP
    assert len(ev["inspections"]) == ca.EVIDENCE_COMMIT_CAP
    assert ev["omitted"] == _N - ca.EVIDENCE_COMMIT_CAP
    assert len(set(shown)) <= ca.EVIDENCE_COMMIT_CAP


def test_full_evidence_keeps_every_commit_and_no_omitted(tmp_path: Path) -> None:
    ev = _evidence(ca.brief(repo_root=tmp_path, run_git=_fake_git([]), full_evidence=True))

    assert ev["commit_count"] == _N
    assert len(ev["commits"]) == _N
    assert len(ev["inspections"]) == _N
    assert "omitted" not in ev


def test_at_cap_branch_has_no_omitted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(ca, "EVIDENCE_COMMIT_CAP", _N)
    ev = _evidence(ca.brief(repo_root=tmp_path, run_git=_fake_git([])))

    assert len(ev["commits"]) == _N
    assert "omitted" not in ev


def test_main_parses_full_evidence_flag(monkeypatch) -> None:
    seen: dict = {}

    def fake_brief(**kw):
        seen.update(kw)
        return {}

    monkeypatch.setattr(ca, "brief", fake_brief)
    assert ca.main(["brief", "--full-evidence", "--superseded", "b=abc"]) == ca.EXIT_OK
    assert seen == {"superseded": {"b": "abc"}, "full_evidence": True}

    seen.clear()
    assert ca.main(["brief"]) == ca.EXIT_OK
    assert seen == {}


def test_cap_stat_truncates_file_lines_and_keeps_summary() -> None:
    n = ca.EVIDENCE_STAT_FILE_CAP + 5
    block = "commit abc\n\n    subj\n\n" + "".join(f" f{i} | 1 +\n" for i in range(n)) + f" {n} files changed\n"
    out = ca._cap_stat(block)

    assert out.count(" | ") == ca.EVIDENCE_STAT_FILE_CAP
    assert "5 more file(s) omitted" in out
    assert out.endswith(f" {n} files changed\n")


def test_cap_stat_leaves_small_block_unchanged() -> None:
    block = "commit abc\n\n f | 1 +\n 1 file changed\n"
    assert ca._cap_stat(block) == block


def test_main_rejects_unknown_brief_flag() -> None:
    assert ca.main(["brief", "--bogus"]) == ca.EXIT_USAGE
