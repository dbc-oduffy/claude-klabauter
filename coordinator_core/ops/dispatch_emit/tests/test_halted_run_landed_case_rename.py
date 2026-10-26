"""dispatch.record_halted_run_landed: withheld paths match casefolded tree names and follow renames."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import ask_fanout_contract as c
from coordinator_core.ops.dispatch_emit import halted_run_landed as op
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

PARENT = "ask-parent-20261015T000000-abcdef"
SOLO = "warp-2026-10-20-solo-20261020T000000-abcdef"
PLAN = "docs/plans/p.md"


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=str(repo), check=True, capture_output=True, text=True,
        **no_console_creationflags(),
    )
    return done.stdout.strip()


def _commit_all(repo: Path, msg: str = "c") -> str:
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
    return _git(repo, "rev-parse", "HEAD")


def _write(repo: Path, rel: str, body: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")


@pytest.fixture(autouse=True)
def launches(monkeypatch):
    calls: list = []
    monkeypatch.setattr(op, "_launch", lambda root, run_id: calls.append(run_id) or True)
    return calls


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _write(tmp_path, "base.txt", "base")
    _commit_all(tmp_path, "base")
    return tmp_path


def _marker(root: Path, solo: bool, withheld: tuple) -> tuple:
    """``(run_id, marker_path)`` for a halted solo or sibling run withholding *withheld*."""
    if solo:
        rid, d = SOLO, root / "state" / "warp" / SOLO
        body = {"status": "halted", "halted": "x", "sizing_rel": None}
    else:
        rid, d = "A", root / "state" / "warp" / "A"
        rec = root / c.parent_record_rel(PARENT)
        rec.parent.mkdir(parents=True, exist_ok=True)
        run = c.SiblingRun("A", "A", (), "state/sizings/A.yaml", c.STATUS_READY)
        rec.write_text(json.dumps(c.ParentRecord(PARENT, (run,)).to_json()), encoding="utf-8")
        st = root / c.status_rel("A")
        st.parent.mkdir(parents=True, exist_ok=True)
        st.write_text(json.dumps({"parent_id": PARENT, "status": "halted"}), encoding="utf-8")
        body = {"status": "halted", "halted": "x", "plan": PLAN, "blocking": ["R1"], "run_base_sha": "c" * 40, "sizing_rel": None}
    d.mkdir(parents=True, exist_ok=True)
    marker = d / "halted.json"
    body["run_artifacts"] = {"withheld": {p: "r" for p in withheld}}
    marker.write_text(json.dumps(body), encoding="utf-8")
    return rid, marker


def _call(root, **params):
    return op._handler(params, root)


@pytest.mark.parametrize("solo", [True, False])
def test_withheld_name_never_existing_at_its_case_is_covered_by_the_lowercase_add(repo, solo):
    rid, _ = _marker(repo, solo, ("docs/T/A-HEAVY.md",))
    sha = (_write(repo, "docs/T/a-heavy.md", "x"), _commit_all(repo))[1]
    out = _call(repo, run_id=rid, sha=sha)
    assert out["ok"] and out["written"] is True and out["sha"] == sha
    assert out["covers"] == {sha: ["docs/T/A-HEAVY.md"]}


@pytest.mark.parametrize("solo", [True, False])
def test_case_only_rename_covers_both_spellings(repo, solo):
    _write(repo, "docs/T/Note.md", "same blob")
    _commit_all(repo, "add")
    _git(repo, "mv", "docs/T/Note.md", "docs/T/tmp-note")
    _git(repo, "mv", "docs/T/tmp-note", "docs/T/note.md")
    sha = _commit_all(repo, "rename")
    rid, _ = _marker(repo, solo, ("docs/T/Note.md", "docs/T/note.md"))
    out = _call(repo, run_id=rid, sha=sha)
    assert out["ok"] and out["sha"] == sha
    assert out["covers"] == {sha: ["docs/T/Note.md", "docs/T/note.md"]}


@pytest.mark.parametrize("solo", [True, False])
def test_directory_case_difference_is_covered(repo, solo):
    rid, _ = _marker(repo, solo, ("Docs/T/x.md",))
    _write(repo, "docs/t/x.md", "x")
    sha = _commit_all(repo)
    out = _call(repo, run_id=rid, sha=sha)
    assert out["ok"] and out["covers"] == {sha: ["Docs/T/x.md"]}


@pytest.mark.parametrize("solo", [True, False])
def test_unrelated_commit_still_refuses_and_leaves_marker_untouched(repo, solo):
    rid, marker = _marker(repo, solo, ("docs/T/A-HEAVY.md",))
    _write(repo, "docs/T/a-heavy.md", "x")
    _commit_all(repo)
    _write(repo, "other.txt", "o")
    unrelated = _commit_all(repo)
    before = marker.read_bytes()
    out = _call(repo, run_id=rid, sha=unrelated)
    assert not out["ok"] and "did not change withheld" in out["error"]
    assert marker.read_bytes() == before
