"""A done row with no product hunk closes coded at the commit outside the run that landed its writes."""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from coordinator_core.frontmatter.body_blocks import locate_fenced_block
from coordinator_core.ops.ceremony import close_background as cb
from coordinator_core.ops.dispatch_emit import plan_row_coding
from coordinator_core.ops.dispatch_emit.delivery_credit import NOOP_DETAIL_PREFIX

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
    _commit(tmp_path, {"seed": "0", "x.py": "0"}, "seed")
    _commit(tmp_path, {_REL: _PLAN}, "plan")
    return tmp_path


def _chunk(cid: str, *paths: str):
    return SimpleNamespace(id=cid, paths=list(paths))


def _row(cid: str, paths: list) -> str:
    return (
        f"- id: {cid}\n  title: t{cid}\n  change_kind: code-edit\n  surface: {cid.lower()}.py\n"
        "  disposition: open\n  deferred: false\n  writes:\n"
        + "".join(f"    - {p}\n" for p in paths)
    )


def _close(repo: Path, terminal: str, chunk) -> dict:
    rows = "".join(_row(c, chunk.paths if c == chunk.id else [f"{c.lower()}.py"]) for c in ("C1", "C2"))
    (repo / _REL).write_text(_PLAN.replace(_ROWS, rows), encoding="utf-8", newline="\n")
    ctx = cb.JobContext(
        repo, repo / ".git", "run-1", (), None, terminal_sha=terminal,
        record={"plan_rel": _REL, "terminal_sha": terminal, "inputs": {"credited_rows": [chunk.id]}},
    )
    assert cb._coded_stamp(ctx).status == "ran"
    return {r["id"]: r for r in yaml.safe_load(locate_fenced_block((repo / _REL).read_text(encoding="utf-8")).body)}


def test_hand_commit_without_trailer_is_the_landing_sha(tmp_path):
    repo = _repo(tmp_path)
    landed = _commit(repo, {"c1.py": "1", "c1b.py": "1"}, "hand edit")
    _commit(repo, {"unrelated": "1"}, "later")
    terminal = _commit(repo, {"unrelated": "2"}, "terminal")
    chunk = _chunk("C1", "c1.py", "c1b.py")
    assert plan_row_coding.outside_landing_attribution(repo, terminal, _REL, [chunk]) == {"C1": landed}
    row = _close(repo, terminal, chunk)["C1"]
    assert row["disposition"] == "coded"
    assert row["disposition_ref"] == landed
    assert row["disposition_detail"].startswith(NOOP_DETAIL_PREFIX)
    assert landed[:10] in row["disposition_detail"]


def test_writes_split_over_two_commits_close_at_the_newer(tmp_path):
    repo = _repo(tmp_path)
    older = _commit(repo, {"c1.py": "1"}, "first")
    newer = _commit(repo, {"c1b.py": "1"}, "second")
    terminal = _commit(repo, {"unrelated": "2"}, "terminal")
    chunk = _chunk("C1", "c1.py", "c1b.py")
    assert plan_row_coding.outside_landing_attribution(repo, terminal, _REL, [chunk]) == {"C1": newer}
    detail = _close(repo, terminal, chunk)["C1"]["disposition_detail"]
    assert older[:10] in detail and newer[:10] in detail


def test_unchanged_write_keeps_the_run_sha_close(tmp_path):
    repo = _repo(tmp_path)
    _commit(repo, {"c1.py": "1"}, "hand edit")
    terminal = _commit(repo, {"unrelated": "2"}, "terminal")
    chunk = _chunk("C1", "c1.py", "x.py")
    assert plan_row_coding.outside_landing_attribution(repo, terminal, _REL, [chunk]) == {}
    row = _close(repo, terminal, chunk)["C1"]
    assert row["disposition_ref"] == terminal
    assert row["disposition_detail"] == plan_row_coding.NOOP_DETAIL


def test_commit_before_the_plan_existed_lands_nothing(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, {"c1.py": "1"}, "seed")
    _commit(tmp_path, {_REL: _PLAN}, "plan")
    terminal = _commit(tmp_path, {"unrelated": "2"}, "terminal")
    assert plan_row_coding.outside_landing_attribution(tmp_path, terminal, _REL, [_chunk("C1", "c1.py")]) == {}


def test_plan_creating_commit_lands_a_write(tmp_path):
    _git(tmp_path, "init", "-q")
    _commit(tmp_path, {"seed": "0"}, "seed")
    plan = _commit(tmp_path, {_REL: _PLAN, "c1.py": "1"}, "plan and write")
    terminal = _commit(tmp_path, {"unrelated": "2"}, "terminal")
    assert plan_row_coding.outside_landing_attribution(tmp_path, terminal, _REL, [_chunk("C1", "c1.py")]) == {"C1": plan}


def test_runs_own_checkpoint_is_stepped_over_and_keeps_its_attribution(tmp_path):
    repo = _repo(tmp_path)
    checkpoint = _commit(
        repo, {"c2.py": "1"}, f"checkpoint(wave 1): 1 rows — C2\n\nCheckpoint-Plan: {_REL}\nCheckpoint-Base: {'a' * 40}",
    )
    terminal = _commit(repo, {"unrelated": "2"}, "terminal")
    chunk = _chunk("C2", "c2.py")
    assert plan_row_coding.outside_landing_attribution(repo, terminal, _REL, [chunk]) == {}
    assert cb._checkpoint_of(
        cb.JobContext(repo, repo / ".git", "run-1", (), None), _REL, terminal, [chunk]
    ) == {"C2": checkpoint}


def test_attribution_makes_no_subprocess_call(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    landed = _commit(repo, {"c1.py": "1"}, "hand edit")
    terminal = _commit(repo, {"unrelated": "2"}, "terminal")

    def boom(*_a, **_k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    assert plan_row_coding.outside_landing_attribution(repo, terminal, _REL, [_chunk("C1", "c1.py")]) == {"C1": landed}
