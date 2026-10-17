"""since_base_overlap: landing blobs that drop lines a foreign commit added since the run base.

Repos are written in-process with ``git_objects`` (no spawn).
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

from coordinator_core.git import since_base_overlap
from coordinator_core.git.git_objects import build_tree, write_object
from coordinator_core.ops.dispatch_emit.landing_integrity import SINCE_BASE_OVERLAP

PATH = "pkg/mod.py"
PLAN = "docs/plans/own-plan.md"


class _Repo:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.gitdir = root / ".git"
        (self.gitdir / "refs" / "heads").mkdir(parents=True)
        (self.gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        self.head: Optional[str] = None

    def commit(self, files: Dict[str, str], message: str) -> str:
        entries = {
            p: (0o100644, write_object(self.gitdir, b"blob", text.encode())) for p, text in files.items()
        }
        tree = build_tree(self.gitdir, entries)
        parent = f"parent {self.head}\n" if self.head else ""
        body = (
            f"tree {tree}\n{parent}author t <t@t> 1700000000 +0000\n"
            f"committer t <t@t> 1700000000 +0000\n\n{message}\n"
        )
        self.head = write_object(self.gitdir, b"commit", body.encode())
        (self.gitdir / "refs" / "heads" / "main").write_text(self.head + "\n", encoding="utf-8")
        return self.head

    def land(self, text: str) -> None:
        target = self.root / PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(text.encode())


def _base_lines() -> List[str]:
    out = []
    for i in range(30):
        out.append(f"def f{i}(\n")
        out.append("    a,\n")
        out.append(")\n")
        out.append("\n")
    return out


def _join(lines: List[str]) -> str:
    return "".join(lines)


def _setup(tmp_path: Path):
    repo = _Repo(tmp_path)
    base_lines = _base_lines()
    base = repo.commit({PATH: _join(base_lines)}, "base")
    return repo, base, base_lines


def test_incident_shape_stale_landing_with_own_edit_is_refused(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    foreign = repo.commit({PATH: _join(foreign_lines)}, "foreign terminal commit")
    stale = list(base_lines) + ["own_edit = 2\n"]
    repo.land(_join(stale))

    findings = since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None)

    assert [(f.path, f.reason) for f in findings] == [(PATH, SINCE_BASE_OVERLAP)]
    assert foreign[:10] in findings[0].detail


def test_landing_that_keeps_foreign_lines_is_clean(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    repo.commit({PATH: _join(foreign_lines)}, "foreign")
    kept = list(foreign_lines)
    kept[1] = "    changed_by_run,\n"
    repo.land(_join(kept) + "own = 1\n")

    assert since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None) == []


def test_crlf_worktree_that_keeps_foreign_lines_is_clean(tmp_path: Path) -> None:
    # core.autocrlf checkout: LF blobs, CRLF worktree file.
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    repo.commit({PATH: _join(foreign_lines)}, "foreign")
    repo.land((_join(foreign_lines) + "own = 1\n").replace("\n", "\r\n"))

    assert since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None) == []


def test_crlf_worktree_that_drops_foreign_lines_is_refused(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "foreign_change = 1\n")
    repo.commit({PATH: _join(foreign_lines)}, "foreign")
    repo.land((_join(base_lines) + "own = 1\n").replace("\n", "\r\n"))

    findings = since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None)

    assert [(f.path, f.reason) for f in findings] == [(PATH, SINCE_BASE_OVERLAP)]


def test_removing_blank_or_paren_line_the_foreign_commit_did_not_add_is_clean(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(40, "\n")
    foreign_lines.insert(41, ")\n")
    repo.commit({PATH: _join(foreign_lines)}, "foreign adds blank and paren")
    landed = list(foreign_lines)
    del landed[3]  # a base blank line
    del landed[2]  # a base ")" line
    repo.land(_join(landed))

    assert since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None) == []


def test_removing_the_blank_line_the_foreign_commit_added_is_refused(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    foreign_lines = list(base_lines)
    foreign_lines.insert(41, "\n")
    foreign = repo.commit({PATH: _join(foreign_lines)}, "foreign adds a blank line")
    landed = list(foreign_lines)
    del landed[41]
    repo.land(_join(landed))

    findings = since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None)

    assert len(findings) == 1 and foreign[:10] in findings[0].detail


def test_own_checkpoint_commit_lines_do_not_count(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    own_lines = list(base_lines)
    own_lines.insert(40, "own_checkpoint_line = 1\n")
    repo.commit(
        {PATH: _join(own_lines)},
        f"checkpoint\n\nCheckpoint-Plan: {PLAN}\nCheckpoint-Base: {base}",
    )
    repo.land(_join(base_lines))

    assert since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None, plan=PLAN) == []


def test_other_plan_checkpoint_commit_is_foreign(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    other = list(base_lines)
    other.insert(40, "peer_line = 1\n")
    repo.commit(
        {PATH: _join(other)}, f"checkpoint\n\nCheckpoint-Plan: docs/plans/peer.md\nCheckpoint-Base: {base}"
    )
    repo.land(_join(base_lines))

    assert len(since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None, plan=PLAN)) == 1


def test_path_unchanged_since_base_is_skipped(tmp_path: Path) -> None:
    repo, base, base_lines = _setup(tmp_path)
    repo.commit({PATH: _join(base_lines), "other.txt": "x\n"}, "unrelated")
    repo.land(_join(base_lines[:-1]))

    assert since_base_overlap.find_overlaps(tmp_path, [PATH], base, run_id=None) == []


def test_unreachable_base_is_a_note_not_a_finding(tmp_path: Path) -> None:
    repo, _base, base_lines = _setup(tmp_path)
    repo.commit({PATH: _join(base_lines) + "x\n"}, "next")
    repo.land(_join(base_lines))

    findings, notes = since_base_overlap.check(tmp_path, [PATH], "0" * 40)

    assert findings == [] and notes == [since_base_overlap.NOTE_WALK_CAPPED]


def test_module_cannot_spawn() -> None:
    assert not hasattr(since_base_overlap, "subprocess")


def test_path_history_beyond_version_cap_is_a_note_not_a_finding(tmp_path: Path, monkeypatch) -> None:
    repo, base, base_lines = _setup(tmp_path)
    monkeypatch.setattr(since_base_overlap, "VERSIONS_PER_PATH_CAP", 2)
    lines = list(base_lines)
    for n in range(3):
        lines.insert(40, f"foreign_{n} = 1\n")
        repo.commit({PATH: _join(lines)}, f"foreign {n}")
    repo.land(_join(base_lines))

    findings, notes = since_base_overlap.check(tmp_path, [PATH], base)

    assert findings == [] and notes == [since_base_overlap.NOTE_WALK_CAPPED]
