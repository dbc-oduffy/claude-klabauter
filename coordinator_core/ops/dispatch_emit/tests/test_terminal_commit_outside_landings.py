"""Outside-landing detection, attribution and ``outside_rows`` stamping against real commits in a tmp_path git repo."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from coordinator_core.frontmatter.body_blocks import locate_fenced_block
from coordinator_core.ops.dispatch_emit import terminal_commit

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REL = "plan.md"
_ROWS = "".join(
    f"- id: {i}\n  title: t{i}\n  change_kind: code-edit\n  surface: {i.lower()}.py\n  disposition: open\n  deferred: false\n"
    for i in ("C1", "C2")
)
_PLAN = f"---\ntitle: p\n---\n\n## Tasks\n\n```yaml plan-tasks\n{_ROWS}```\n"


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _commit(root: Path, files: dict, message: str) -> str:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", message)
    return _git(root, "rev-parse", "HEAD")


def _repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, {"seed": "0"}, "seed")
    _commit(tmp_path, {_REL: _PLAN}, "plan")
    return tmp_path


def _chunk(cid: str, *paths: str):
    return SimpleNamespace(id=cid, paths=list(paths))


def _stamp(repo: Path, terminal: str, landings: dict, monkeypatch) -> dict:
    monkeypatch.setattr(terminal_commit, "_changed_paths", lambda _root, paths: set(paths))
    terminal_commit._stamp_coded_commit(
        lambda p, r: {"committed": True, "sha": "f" * 40}, repo, repo, {}, terminal, None,
        outside_rows={_REL: landings},
    )
    return {r["id"]: r for r in yaml.safe_load(locate_fenced_block((repo / _REL).read_text(encoding="utf-8")).body)}


def test_landings_are_newest_first_and_stamped_with_every_sha(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    older = _commit(repo, {"c1.py": "1"}, "first")
    newer = _commit(repo, {"c1b.py": "1"}, "second")
    terminal = _commit(repo, {"unrelated": "2"}, "terminal")
    chunk = _chunk("C1", "c1.py", "c1b.py")
    landings = terminal_commit._outside_landings(repo, terminal, _REL, [chunk])
    assert landings == {"C1": [newer, older]}
    assert terminal_commit._outside_landing_attribution(repo, terminal, _REL, [chunk]) == {"C1": newer}
    rows = _stamp(repo, terminal, landings, monkeypatch)
    assert rows["C1"]["disposition"] == "coded"
    assert rows["C1"]["disposition_ref"] == newer
    assert rows["C1"]["disposition_detail"] == terminal_commit._outside_landing_detail([newer, older])
    assert rows["C2"]["disposition"] == "open"


def test_chunk_with_one_unlanded_path_is_absent(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, {"c1.py": "1"}, "only one write")
    terminal = _commit(repo, {"unrelated": "2"}, "terminal")
    assert terminal_commit._outside_landings(repo, terminal, _REL, [_chunk("C1", "c1.py", "never.py")]) == {}


def test_chunks_sharing_a_path_resolve_independently(tmp_path):
    repo = _repo(tmp_path)
    shared = _commit(repo, {"shared.py": "1"}, "shared")
    own = _commit(repo, {"c2.py": "1"}, "own")
    terminal = _commit(repo, {"unrelated": "2"}, "terminal")
    chunks = [_chunk("C1", "shared.py"), _chunk("C2", "shared.py", "c2.py"), _chunk("C3")]
    assert terminal_commit._outside_landings(repo, terminal, _REL, chunks) == {"C1": [shared], "C2": [own, shared]}


def test_stamp_commit_is_stepped_over(tmp_path):
    repo = _repo(tmp_path)
    landed = _commit(repo, {"c1.py": "1"}, "hand edit")
    _commit(repo, {"c1.py": "2"}, "mark 1 rows coded (abc1234)\n\nCheckpoint-Stamp: " + "a" * 40)
    terminal = _commit(repo, {"unrelated": "2"}, "terminal")
    assert terminal_commit._outside_landings(repo, terminal, _REL, [_chunk("C1", "c1.py")]) == {"C1": [landed]}


def test_checkpoint_of_another_plan_is_a_landing(tmp_path):
    repo = _repo(tmp_path)
    other = _commit(
        repo, {"c1.py": "1"}, f"checkpoint(wave 1): 1 rows — C1\n\nCheckpoint-Plan: other.md\nCheckpoint-Base: {'a' * 40}",
    )
    terminal = _commit(repo, {"unrelated": "2"}, "terminal")
    assert terminal_commit._outside_landings(repo, terminal, _REL, [_chunk("C1", "c1.py")]) == {"C1": [other]}


def test_no_plan_path_or_no_writes_lands_nothing(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, {"c1.py": "1"}, "hand edit")
    terminal = _commit(repo, {"unrelated": "2"}, "terminal")
    assert terminal_commit._outside_landings(repo, terminal, None, [_chunk("C1", "c1.py")]) == {}
    assert terminal_commit._outside_landings(repo, terminal, _REL, [_chunk("C1")]) == {}


def test_rows_landed_outside_executor_counts_head_but_not_the_plan_creation_commit(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, {"seed": "0"}, "seed")
    _commit(tmp_path, {_REL: _PLAN, "c1.py": "1"}, "plan and write")
    head = _commit(tmp_path, {"c2.py": "1"}, "hand edit at head")
    rows = [
        {"id": "C1", "writes": ["c1.py"]},
        {"id": "C2", "writes": ["c2.py"]},
        {"id": "C3"},
    ]
    assert terminal_commit.rows_landed_outside_executor(tmp_path, _REL, rows) == {"C2": head}
