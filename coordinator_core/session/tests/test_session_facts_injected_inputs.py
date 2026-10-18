"""The facade's diff and dirty facts read injected inputs (a `SessionHistory`, a `_dirty_paths` record)
and spawn nothing when handed them; expected values are literals computed by the pre-change walks on
the same deterministic fixture."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.ceremony import branch_resolution as br
from coordinator_core.session import session_facts
from coordinator_core.win_portability import no_console_creationflags
from coordinator_core.workstream_complete.session_history import read_session_history

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

SID = "11111111-2222-3333-4444-555555555555"
PEER = "99999999-8888-7777-6666-555555555555"
STARTED_AT = "2026-10-01T10:00:30Z"


def _git(root: Path, *args: str, minute: int = 0) -> str:
    stamp = f"2026-10-01T10:{minute:02d}:00+00:00"
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
        "GIT_AUTHOR_DATE": stamp, "GIT_COMMITTER_DATE": stamp,
    }
    out = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false",
         "-c", "core.autocrlf=false", *args],
        cwd=root, capture_output=True, text=True, check=True, env=env, **no_console_creationflags(),
    )
    return out.stdout.strip()


def _commit(root: Path, minute: int, files: dict[str, bytes], sid: str | None) -> str:
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    _git(root, "add", "-A", minute=minute)
    message = f"commit {minute}\n\nbody\n" + (f"\nSession-Id: {sid}\n" if sid else "")
    _git(root, "commit", "-q", "-m", message, minute=minute)
    return _git(root, "rev-parse", "HEAD")


def _rename_commit(root: Path, minute: int, old: str, new: str, extra: bytes, sid: str) -> str:
    (root / new).parent.mkdir(parents=True, exist_ok=True)
    _git(root, "mv", old, new, minute=minute)
    if extra:
        with (root / new).open("ab") as fh:
            fh.write(extra)
    _git(root, "add", "-A", minute=minute)
    _git(root, "commit", "-q", "-m", f"commit {minute}\n\nbody\n\nSession-Id: {sid}\n", minute=minute)
    return _git(root, "rev-parse", "HEAD")


def _session_files(root: Path, sid: str, *, floor: str | None) -> None:
    sdir = root / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "started_at").write_text(STARTED_AT + "\n", encoding="utf-8")
    if floor is not None:
        (sdir / "head_at_start").write_text(floor + "\n", encoding="utf-8")


def _seeded(root: Path) -> str:
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    return _commit(root, 0, {"seed.txt": b"seed\n"}, None)


def build_own_session(root: Path) -> str:
    """Own commits across code, doc, rename, relocation and binary rows, with a peer commit between."""
    floor = _seeded(root)
    _session_files(root, SID, floor=floor)
    _commit(root, 1, {"docs/a.md": b"one\ntwo\nthree\n"}, SID)
    _commit(root, 2, {"coordinator_core/x.py": b"".join(b"line %d\n" % i for i in range(10))}, SID)
    _commit(root, 3, {"peer.txt": b"p\n"}, PEER)
    _rename_commit(root, 4, "docs/a.md", "docs/b.md", b"four\n", SID)
    _rename_commit(root, 5, "coordinator_core/x.py", "coordinator_core/y.py", b"", SID)
    _commit(root, 6, {"bin/blob.dat": b"\x00\x01\x02\x00"}, SID)
    _commit(root, 7, {"state/sizings/s.yaml": b"a: 1\nb: 2\n"}, SID)
    return floor


def build_foreign_only_session(root: Path) -> str:
    """No own commit and HEAD moved: the trailer is unreliable and every window commit is foreign."""
    floor = _seeded(root)
    _session_files(root, SID, floor=floor)
    _commit(root, 1, {"peer1.txt": b"1\n"}, PEER)
    _commit(root, 2, {"peer2.txt": b"2\n"}, PEER)
    return floor


@pytest.fixture
def spawns(monkeypatch):
    calls: list = []
    real = subprocess.Popen

    class Counting(real):
        def __init__(self, args, *a, **k):
            calls.append(args)
            super().__init__(args, *a, **k)

    monkeypatch.setattr(subprocess, "Popen", Counting)
    return calls


_BRIGHTLINE = {"novel_loc": 500, "novel_commit_count": 5, "novel_surface_count": 4}

# Computed by the pre-change walks (`_novel_loc_split`, `_session_touched_paths`, the `--since`
# range) on `build_own_session`. `touched_paths` differs by design: the walk spelled renames
# `docs/{a.md => b.md}`, the history carries the destination path.
_OWN_FLOOR = "692a4aada71ed71103a64b96bf56598021eb29d9"
_OWN_FIRST = "cace3d220e9b22b57a659f4b64a26455031bae85"
_OWN_HEAD = "a925d686766c0597673d11222bf6a630f145f757"
_OWN_EXPECTED = {
    "scoping_method": br.SCOPING_METHOD_TRAILER,
    "trustworthy": True,
    "sha_range": f"{_OWN_FIRST}^..{_OWN_HEAD}",
    "commit_count": 6,
    "surface_count": 4,
    "novel_surface_count": 2,
    "touched_paths": [
        "state/sizings/s.yaml",
        "bin/blob.dat",
        "coordinator_core/y.py",
        "docs/b.md",
        "coordinator_core/x.py",
        "docs/a.md",
    ],
    "gross_loc": 16,
    "doc_only_loc": 6,
    "relocated_files": 1,
    "novel_loc": 10,
    "novel_commit_count": 1,
    "brightline": _BRIGHTLINE,
    "breached": [],
    "under_brightline": True,
}
_FOREIGN_FIRST = "229fee26e2084cc67c42110e1b1585cd9e6acfb1"
_FOREIGN_HEAD = "cdd1e4fcbc393d6d1fb84f2190af1d8176cc53b7"
_FOREIGN_EXPECTED = {
    "scoping_method": br.SCOPING_METHOD_AMBIGUOUS,
    "trustworthy": False,
    "sha_range": f"{_FOREIGN_FIRST}^..{_FOREIGN_HEAD}",
    "commit_count": 0,
    "surface_count": 0,
    "novel_surface_count": 0,
    "touched_paths": [],
    "gross_loc": 0,
    "doc_only_loc": 0,
    "relocated_files": 0,
    "novel_loc": 0,
    "novel_commit_count": 0,
    "brightline": _BRIGHTLINE,
    "breached": [],
    "under_brightline": True,
}


def test_fixture_shas_are_deterministic(tmp_path):
    assert build_own_session(tmp_path / "own") == _OWN_FLOOR
    assert _git(tmp_path / "own", "rev-parse", "HEAD") == _OWN_HEAD
    assert build_foreign_only_session(tmp_path / "foreign") == _OWN_FLOOR
    assert _git(tmp_path / "foreign", "rev-parse", "HEAD") == _FOREIGN_HEAD


@pytest.mark.parametrize(
    ("builder", "expected"),
    [(build_own_session, _OWN_EXPECTED), (build_foreign_only_session, _FOREIGN_EXPECTED)],
)
def test_diff_brightline_on_history_equals_the_pre_change_walk(tmp_path, spawns, builder, expected):
    root = tmp_path / "r"
    builder(root)
    history = read_session_history(root, SID)
    spawns.clear()

    record = session_facts.session_diff_brightline(root, root / ".git", SID, history)

    assert spawns == []
    assert record["degraded"] is False
    assert record["value"] == expected


def test_diff_brightline_on_unresolved_history_degrades_with_its_reason_and_spawns_nothing(
    tmp_path, spawns
):
    root = tmp_path / "r"
    build_own_session(root)
    (root / ".git" / "coordinator-sessions" / SID / "head_at_start").unlink()
    history = read_session_history(root, SID)
    spawns.clear()

    record = session_facts.session_diff_brightline(root, root / ".git", SID, history)

    assert spawns == []
    assert history.reason == "floor-missing"
    assert record["degraded"] is True
    assert "floor-missing" in record["evidence"]
    assert "value" not in record


def _dirty_tree(root: Path) -> None:
    """Committed and dirty sizings, committed and untracked fold sidecars, a dirty plan."""
    _commit(
        root, 1,
        {
            "state/sizings/a.yaml": b"---\nstatus: shipped\n---\n",
            "state/sizings/b.yaml": b"---\nstatus: declined\n---\n",
            "state/sizings/c.yaml": b"---\nstatus: open\n---\n",
            "state/execution-records/x.json": b"{}\n",
            "docs/plans/p.md": b"---\nstatus: executing\n---\n",
        },
        None,
    )
    (root / "state/sizings/b.yaml").write_bytes(b"---\nstatus: declined\n---\nedit\n")
    (root / "state/execution-records/y.json").write_bytes(b"{}\n")
    (root / "docs/plans/p.md").write_bytes(b"---\nstatus: executing\n---\nedit\n")


@pytest.fixture
def dirty_repo(tmp_path, monkeypatch):
    from coordinator_core.session import claimed_plan

    root = tmp_path / "d"
    _seeded(root)
    _dirty_tree(root)
    monkeypatch.setattr(claimed_plan, "list_held_plan_claims", lambda cwd=None: [("docs/plans/p.md", 1)])
    return root


def test_each_dirty_fact_equals_its_spawning_form(dirty_repo):
    root = dirty_repo
    dirty = session_facts._dirty_paths(root)
    assert dirty["degraded"] is False

    for fact, args in (
        (session_facts.session_terminal_sizings, ()),
        (session_facts.session_fold_sidecars, ()),
        (session_facts.session_governing_plan, (root / ".git", SID)),
    ):
        spawning = fact(root, *args)
        injected = fact(root, *args, dirty=dirty)
        assert injected == spawning
        assert injected["collision"] is True

    sizings = session_facts.session_terminal_sizings(root, dirty=dirty)
    assert {e["path"]: e["dirty"] for e in sizings["value"]["terminal"]} == {
        "state/sizings/a.yaml": False,
        "state/sizings/b.yaml": True,
    }
    assert session_facts.session_fold_sidecars(root, dirty=dirty)["value"]["paths"] == [
        "state/execution-records/x.json",
        "state/execution-records/y.json",
    ]


def test_dirty_facts_and_diff_spawn_nothing_when_handed_both_inputs(dirty_repo, spawns):
    root = dirty_repo
    _session_files(root, SID, floor=_git(root, "rev-parse", "HEAD~1"))
    history = read_session_history(root, SID)
    dirty = session_facts._dirty_paths(root)
    spawns.clear()

    records = [
        session_facts.session_diff_brightline(root, root / ".git", SID, history),
        session_facts.session_governing_plan(root, root / ".git", SID, dirty=dirty),
        session_facts.session_terminal_sizings(root, dirty=dirty),
        session_facts.session_fold_sidecars(root, dirty=dirty),
    ]

    assert spawns == []
    assert [r["degraded"] for r in records] == [False] * 4


def test_a_degraded_dirty_record_degrades_each_dirty_fact_with_its_evidence(dirty_repo, spawns):
    root = dirty_repo
    degraded = {"degraded": True, "evidence": "status boom", "source": "x"}
    spawns.clear()

    records = [
        session_facts.session_governing_plan(root, root / ".git", SID, dirty=degraded),
        session_facts.session_terminal_sizings(root, dirty=degraded),
        session_facts.session_fold_sidecars(root, dirty=degraded),
    ]

    assert spawns == []
    assert all(r["degraded"] and r["evidence"] == "status boom" for r in records)
