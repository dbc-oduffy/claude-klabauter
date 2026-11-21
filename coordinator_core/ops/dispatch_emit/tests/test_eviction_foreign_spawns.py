"""eviction_foreign: live holders, foreign hunk owners and owner notices of a halt eviction."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.git import git_objects
from coordinator_core.git.git_dir import resolve_git_common_dir
from coordinator_core.ops.dispatch_emit import own_hunk_stage, peer_hunk_gate, sibling_claims
from coordinator_core.ops.dispatch_emit.eviction_foreign import (
    FOREIGN_KEY, NON_RUN_OWNER, foreign_entry, foreign_owners, live_holders, notify_owners,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REL = "f.py"
HEAD = [f"line{i}\n" for i in range(1, 11)]
EDIT_A = {1: "A edit of line2\n"}
EDIT_B = {7: "B edit of line8\n"}


def _lines(*edits: dict[int, str]) -> bytes:
    out = list(HEAD)
    for edit in edits:
        for at, text in edit.items():
            out[at] = text
    return "".join(out).encode()


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "core.autocrlf=false", *args],
        cwd=root, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def root(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / REL).write_bytes(_lines())
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    (repo / REL).write_bytes(_lines(EDIT_A, EDIT_B))
    return repo


def _blob(root: Path, data: bytes) -> str:
    return git_objects.write_object(resolve_git_common_dir(root), b"blob", data)


def _stage(root: Path, run: str, baseline: bytes | None, session: str | None = None, print_: bytes | None = None) -> None:
    run_dir = root / "state" / "warp" / run
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {"write_baseline": {REL: _blob(root, baseline)}} if baseline is not None else {}
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    if session:
        (run_dir / "run-stage.json").write_text(json.dumps({"session_id": session}), encoding="utf-8")
    if print_ is not None:
        (run_dir / "write_prints.json").write_text(json.dumps({REL: _blob(root, print_)}), encoding="utf-8")


def _hunks(root: Path) -> list:
    tree = (root / REL).read_bytes().splitlines(keepends=True)
    return own_hunk_stage._hunks_of(REL, _lines().splitlines(keepends=True), tree)


@pytest.fixture(autouse=True)
def no_candidates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sibling_claims, "_stamped_candidates", lambda root: [])


def test_holder_whose_delta_explains_a_dropped_hunk_owns_it(root: Path) -> None:
    _stage(root, "run-a", _lines())
    _stage(root, "run-b", _lines(EDIT_A))  # staged after A wrote line 2: its delta is line 8 only
    hunks = _hunks(root)
    assert len(hunks) == 2
    got = foreign_owners(root, "run-a", REL, hunks, {"run-b": True})
    assert got == {"run-b": [f"@@ -{hunks[1].old_start},{hunks[1].old_len} @@"]}


def test_hunk_no_other_claimant_explains_is_not_foreign(root: Path) -> None:
    _stage(root, "run-a", _lines())
    _stage(root, "run-b", _lines(EDIT_A, EDIT_B))  # nothing of its own left
    assert foreign_owners(root, "run-a", REL, _hunks(root), {"run-b": True}) == {}


def test_hunks_outside_dropped_are_never_listed(root: Path) -> None:
    _stage(root, "run-a", _lines())
    _stage(root, "run-b", _lines(EDIT_A))
    kept_in_tree = _hunks(root)[:1]
    assert foreign_owners(root, "run-a", REL, kept_in_tree, {"run-b": True}) == {}
    assert foreign_owners(root, "run-a", REL, [], {"run-b": True}) == {}


def test_unlanded_run_with_a_write_print_of_the_path_is_found_without_being_a_holder(
    root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stage(root, "run-a", _lines())
    _stage(root, "run-b", _lines(EDIT_A), print_=_lines(EDIT_A, EDIT_B))
    _stage(root, "run-c", _lines(EDIT_A))  # no print naming the path: not a claimant
    monkeypatch.setattr(sibling_claims, "_stamped_candidates", lambda r: [("run-b", None), ("run-c", None)])
    assert list(foreign_owners(root, "run-a", REL, _hunks(root), {})) == ["run-b"]


def test_unreadable_baseline_names_no_owner(root: Path) -> None:
    _stage(root, "run-a", _lines())
    _stage(root, "run-b", None)
    assert foreign_owners(root, "run-a", REL, _hunks(root), {"run-b": True}) == {}


def test_hunk_off_the_runs_own_write_print_is_a_non_run_edit(root: Path) -> None:
    _stage(root, "run-a", _lines(), print_=_lines(EDIT_A))  # the run wrote line 2 only; line 8 is someone's
    got = foreign_owners(root, "run-a", REL, _hunks(root), {})
    assert list(got) == [NON_RUN_OWNER]
    assert len(got[NON_RUN_OWNER]) == 1


def test_foreign_owners_never_raises(root: Path) -> None:
    assert foreign_owners(root, "run-a", REL, [object()], {"run-b": True}) == {}


def test_live_holders_maps_other_declaring_runs_to_senior(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen = {}

    def fake(root, run_id, landing, declared, **kw):
        seen["args"] = (run_id, list(landing), list(declared))
        return [("f.py", "run-b"), ("g.py", "run-c"), ("f.py", "run-a")]

    monkeypatch.setattr(peer_hunk_gate, "co_declared_peer_holds", fake)
    assert live_holders(tmp_path, "run-a", "f.py") == {"run-b": True}
    assert seen["args"] == ("run-a", ["f.py"], ["f.py"])


def test_foreign_entry_records_session_and_hunks(root: Path) -> None:
    _stage(root, "run-b", None, session="sess-b")
    entry = foreign_entry(root, {"run-b": ["@@ -8,1 @@"], NON_RUN_OWNER: ["@@ -2,1 @@"]})
    assert entry == {
        "run-b": {"session": "sess-b", "hunks": ["@@ -8,1 @@"]},
        NON_RUN_OWNER: {"session": None, "hunks": ["@@ -2,1 @@"]},
    }
    assert FOREIGN_KEY == "foreign"


def test_one_notice_per_resolvable_owner_session(root: Path) -> None:
    _stage(root, "run-b", None, session="sess-b")
    _stage(root, "run-b2", None, session="sess-b")  # same session as run-b
    _stage(root, "run-c", None, session="sess-c")
    _stage(root, "run-nosess", None)
    owners = {
        "run-b": ["@@ -8,1 @@"], "run-b2": ["@@ -9,1 @@"], "run-c": ["@@ -3,1 @@"],
        "run-nosess": ["@@ -4,1 @@"], NON_RUN_OWNER: ["@@ -5,1 @@"],
    }
    got = notify_owners(root, "state/warp/run-a", REL, owners)

    files = sorted(p.relative_to(root).as_posix() for p in (root / "state/peer-notices").rglob("*.json"))
    assert len(files) == 2
    assert {f.split("/")[2] for f in files} == {"sess-b", "sess-c"}
    assert got["run-b"] == got["run-b2"] and (root / got["run-b"]).is_file()
    assert (root / got["run-c"]).is_file()
    assert "no session" in got["run-nosess"] and "no session" in got[NON_RUN_OWNER]
    notice = json.loads((root / got["run-c"]).read_text(encoding="utf-8"))
    assert notice["artifact_path"] == REL and notice["target_session_id"] == "sess-c"
    assert notice["message"] == (
        'halt of run-a withheld your hunks in f.py; restore: dispatch.restore_foreign_hunks '
        '{"record_dir":"state/warp/run-a"}'
    )


def test_notice_failure_is_reported_not_raised(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _stage(root, "run-b", None, session="sess-b")
    from coordinator_core.ops import peer_notice_send

    def refuse(params, repo_root=None):
        raise ValueError("peer_notice.send requires a non-empty message")

    monkeypatch.setattr(peer_notice_send, "_peer_notice_send", refuse)
    got = notify_owners(root, "state/warp/run-a", REL, {"run-b": ["@@ -8,1 @@"]})
    assert got["run-b"].startswith("notice failed: ValueError")
    assert not (root / "state/peer-notices").exists()
