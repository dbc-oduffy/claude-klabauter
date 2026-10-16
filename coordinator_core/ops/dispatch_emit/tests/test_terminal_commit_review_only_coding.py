"""A review-only terminal commit codes only the review rows open at emit and still open, whose writes a commit in run_base..HEAD changed."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.execute_plan_assemble.row_spans import _row_disposition
from coordinator_core.frontmatter.body_blocks import locate_fenced_block
from coordinator_core.ops.dispatch_emit import terminal_commit
from coordinator_core.ops.dispatch_emit.commit_request import (
    ChunkCommit,
    CommitRequest,
    parse_marker,
    render_marker,
)

# The _git helper drives a real git repo; needs a real process.
pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_REL = "docs/plan.md"
_PASS = {
    "prep": {"slice_files": ["c1.py"]},
    "delivery": {"verdict": "PASS"},
    "integration": {"unresolved": [], "confinement_violations": 0},
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


def _plan(dispositions: dict, extra_fm: str = "") -> str:
    rows = "".join(
        f"- id: {i}\n  title: t{i}\n  change_kind: code-edit\n  surface: {i.lower()}.py\n"
        f"  disposition: {d}\n  deferred: false\n"
        for i, d in dispositions.items()
    )
    return f"---\ntitle: p\n{extra_fm}---\n\n## Tasks\n\n```yaml plan-tasks\n{rows}```\n"


def _rows(root: Path) -> dict:
    text = (root / _REL).read_text(encoding="utf-8")
    return {r["id"]: r for r in yaml.safe_load(locate_fenced_block(text).body)}


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    (tmp_path / "docs").mkdir()
    (tmp_path / _REL).write_text(_plan({"C1": "open", "C2": "open", "C3": "open"}), newline="\n")
    for f in ("c1.py", "c2.py", "c3.py"):
        (tmp_path / f).write_text("0\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "base")
    base = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "c1.py").write_text("1\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "hand commit for C1")
    hand = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "other.txt").write_text("x\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-qm", "terminal")
    return tmp_path, base, hand, _git(tmp_path, "rev-parse", "HEAD")


def _request(base, rows=("C1", "C2"), open_rows=("C1", "C2"), chunks=("C1", "C2", "C3")):
    return CommitRequest(
        chunks=tuple(ChunkCommit(id=i, title=i, paths=(f"{i.lower()}.py",), prefixes=(), report="") for i in chunks),
        plan_path=_REL, review_only=True,
        review_open_rows=tuple(open_rows), review_run_base=base,
    )


def test_row_is_coded_at_the_hand_commit_and_the_plan_flips(repo, monkeypatch):
    root, base, hand, terminal = repo
    coded, no_delta = terminal_commit._review_only_coding(root, terminal, _request(base), _PASS)
    assert coded == {hand: {_REL: {"C1"}}}
    assert no_delta == ["C2"]

    monkeypatch.setattr(terminal_commit, "_changed_paths", lambda _r, paths: set(paths))
    out = terminal_commit._stamp_coded_commit(
        lambda p, r: {"committed": True, "sha": "f" * 40}, root, root, {_REL: set()}, terminal, None,
        checkpoint_rows=coded,
    )
    assert out["rows_coded"] == {_REL: ["C1"]}
    assert _rows(root)["C1"]["disposition_ref"] == hand
    assert _row_disposition(_rows(root)["C2"]) == "open"


def test_plan_status_flips_once_every_row_is_coded(repo, monkeypatch):
    root, base, hand, terminal = repo
    met = "review_stamp:\n  criterion:\n    status: met\n    observation: it holds\n"
    (root / _REL).write_text(_plan({"C1": "open"}, met), newline="\n")
    coded, _ = terminal_commit._review_only_coding(root, terminal, _request(base, ("C1",), ("C1",), ("C1",)), _PASS)
    monkeypatch.setattr(terminal_commit, "_changed_paths", lambda _r, paths: set(paths))
    monkeypatch.setattr(
        terminal_commit, "_stamp_plan_implemented",
        lambda r, plan, sha, **_kw: ({"plan_status": "implemented"} if _row_disposition(_rows(r)["C1"]) == "coded"
                              else {"plan_status": "refused"}),
    )
    out = terminal_commit._stamp_coded_commit(
        lambda p, r: {"committed": True, "sha": "f" * 40}, root, root, {_REL: set()}, terminal, None,
        checkpoint_rows=coded, flip_plan=_REL,
    )
    assert out["_flip"]["plan_status"] == "implemented"


def test_row_absent_from_the_marker_open_rows_is_not_coded(repo):
    root, base, _hand, terminal = repo
    coded, no_delta = terminal_commit._review_only_coding(
        root, terminal, _request(base, open_rows=("C2",)), _PASS
    )
    assert coded == {} and "C1" not in no_delta


def test_row_no_longer_open_now_is_not_coded(repo):
    root, base, _hand, terminal = repo
    (root / _REL).write_text(_plan({"C1": "wont_do", "C2": "open", "C3": "open"}), newline="\n")
    coded, no_delta = terminal_commit._review_only_coding(root, terminal, _request(base), _PASS)
    assert coded == {} and no_delta == ["C2"]


def test_marker_without_the_field_codes_nothing(repo):
    root, base, _hand, terminal = repo
    bare = CommitRequest(chunks=_request(base).chunks, plan_path=_REL, review_only=True)
    assert terminal_commit._review_only_coding(root, terminal, bare, _PASS) == ({}, [])
    parsed = parse_marker(render_marker(bare))
    assert parsed.review_open_rows == () and parsed.review_run_base is None


def test_marker_round_trips_the_open_rows(repo):
    _root, base, _hand, _terminal = repo
    parsed = parse_marker(render_marker(_request(base)))
    assert parsed.review_open_rows == ("C1", "C2") and parsed.review_run_base == base


def test_unreached_run_base_codes_nothing_and_lists_the_open_rows(repo):
    root, _base, _hand, terminal = repo
    coded, no_delta = terminal_commit._review_only_coding(root, terminal, _request("e" * 40), _PASS)
    assert coded == {} and no_delta == ["C1", "C2"]


def test_base_beyond_the_walk_cap_is_unreached(repo, monkeypatch):
    root, base, _hand, terminal = repo
    monkeypatch.setattr(terminal_commit, "_REVIEW_WALK_CAP", 2)
    coded, no_delta = terminal_commit._review_only_coding(root, terminal, _request(base), _PASS)
    assert coded == {} and no_delta == ["C1", "C2"]


def test_merge_commit_is_diffed_against_its_first_parent(repo):
    root, base, _hand, _terminal = repo
    main = _git(root, "rev-parse", "--abbrev-ref", "HEAD")
    _git(root, "checkout", "-q", "-b", "side", base)
    (root / "c2.py").write_text("2\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "side C2")
    _git(root, "checkout", "-q", main)
    _git(root, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    merge = _git(root, "rev-parse", "HEAD")
    coded, no_delta = terminal_commit._review_only_coding(root, merge, _request(base), _PASS)
    assert coded[merge] == {_REL: {"C2"}} and no_delta == []


@pytest.mark.parametrize(
    "review",
    [
        {**_PASS, "delivery": {"verdict": "FAIL"}},
        {**_PASS, "integration": {"unresolved": ["x"], "confinement_violations": 0}},
        {**_PASS, "criterion": {"status": "not_met"}},
        {},
    ],
)
def test_failed_review_codes_nothing(repo, review):
    root, base, _hand, terminal = repo
    assert terminal_commit._review_only_coding(root, terminal, _request(base), review) == ({}, [])


def test_rows_outside_the_review_rows_are_untouched(repo):
    root, base, _hand, terminal = repo
    (root / "c3.py").write_text("3\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "C3 hand commit")
    terminal = _git(root, "rev-parse", "HEAD")
    coded, no_delta = terminal_commit._review_only_coding(
        root, terminal, _request(base, open_rows=("C1",)), _PASS
    )
    assert [r for by in coded.values() for ids in by.values() for r in ids] == ["C1"]
    assert no_delta == []


def test_the_walk_spawns_nothing(repo, monkeypatch):
    root, base, _hand, terminal = repo
    spawned: list = []
    real = subprocess.Popen

    def spy(*a, **k):
        spawned.append(a)
        return real(*a, **k)

    monkeypatch.setattr(subprocess, "Popen", spy)
    terminal_commit._review_only_coding(root, terminal, _request(base), _PASS)
    assert spawned == []
