"""review.prep, driven through _handler at default arguments, reviews an already-landed declared path
against the commit that introduced it unreviewed (or a live revise note's named commit)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_prep as rp
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-own"
DECLARED = "pkg/a.py"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _commit(repo: Path, name: str, text: str, message: str = "") -> str:
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message or f"edit {name}")
    return _git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nplan_id: pln-test-1\n---\n\n# p\n", encoding="utf-8")
    _commit(tmp_path, "seed.py", "x = 1\n", "init")
    return tmp_path


def _stage(repo: Path, declared: list) -> None:
    d = record_dir(repo, RUN)
    d.mkdir(parents=True, exist_ok=True)
    (d / "manifest.json").write_text(json.dumps({"review_declared_paths": declared}), encoding="utf-8")


def _note(repo: Path, text: str) -> None:
    d = repo / "state" / "warp" / RUN
    d.mkdir(parents=True, exist_ok=True)
    (d / "notes.jsonl").write_text(json.dumps({"note": text, "at": "2026-11-16T10:00:00Z"}) + "\n", encoding="utf-8")


def _handler(repo: Path, base: str = "", head: str = "") -> dict:
    tip = _git(repo, "rev-parse", "HEAD")
    params = {
        "base": base or tip, "head": head or tip, "slice_prefix": "prep-z",
        "plan_path": "docs/plans/p.md", "session_id": "sess-own", "run_id": RUN,
    }
    return rp._handler(params, repo)


def _files(reply: dict) -> list:
    return [f for s in reply["slices"] for f in s["files"]]


def _diff(reply: dict) -> str:
    return "".join(Path(s["diff_path"]).read_text(encoding="utf-8") for s in reply["slices"])


def _landed_then_unrelated(repo: Path, introducer_message: str) -> tuple:
    """Introducer commit X lands ``DECLARED``; a later unrelated commit leaves it clean at HEAD."""
    before = _git(repo, "rev-parse", "HEAD")
    x = _commit(repo, DECLARED, "a = 1\n", introducer_message)
    _commit(repo, "later.py", "l = 1\n")
    return before, x


def test_a_unstamped_introducer_yields_slices_not_no_delta(repo):
    before, x = _landed_then_unrelated(repo, "sweep everything in")
    _stage(repo, [DECLARED])
    reply = _handler(repo)
    assert reply["error"] is None
    assert _files(reply) == [DECLARED]
    assert reply["excluded"] == []
    assert "+a = 1" in _diff(reply)


def test_b_stamped_introducer_stays_no_delta(repo):
    _landed_then_unrelated(repo, "land a\n\nInline-Review: applies review-abc123")
    _stage(repo, [DECLARED])
    reply = _handler(repo)
    assert reply["slices"] == [] and _files(reply) == []
    assert reply["error"], "an empty range for the only declared path still halts"


def test_c_live_revise_note_names_the_base_and_wins_over_a_stamped_introducer(repo):
    before, x = _landed_then_unrelated(repo, "land a\n\nInline-Review: applies review-abc123")
    _stage(repo, [DECLARED])
    _note(repo, f"review {DECLARED} against {x[:10]}")
    reply = _handler(repo)
    assert reply["error"] is None
    assert _files(reply) == [DECLARED]
    assert reply["excluded"] == []
    assert before in (repo / reply["sidecar_path"]).read_text(encoding="utf-8")
    assert "+a = 1" in _diff(reply)


def test_c2_a_note_naming_a_commit_that_did_not_touch_the_path_is_the_base_itself(repo):
    _landed_then_unrelated(repo, "land a\n\nInline-Review: applies review-abc123")
    mid = _commit(repo, "mid.py", "m = 1\n")
    _commit(repo, "tail.py", "t = 1\n")
    _stage(repo, [DECLARED])
    _note(repo, f"review against {mid}")
    reply = _handler(repo)
    assert reply["slices"] == []
    assert reply["excluded"] == [{"path": DECLARED, "reason": "no-delta"}]


def test_d_ordinary_run_with_a_delta_is_unchanged(repo):
    base = _git(repo, "rev-parse", "HEAD")
    _commit(repo, DECLARED, "a = 1\n", "work")
    head = _git(repo, "rev-parse", "HEAD")
    _stage(repo, [DECLARED])
    reply = _handler(repo, base=base, head=head)
    assert reply["error"] is None
    assert _files(reply) == [DECLARED]
    assert reply["excluded"] == []
    assert base in (repo / reply["sidecar_path"]).read_text(encoding="utf-8")


def test_e_mixed_run_keeps_the_untouched_unstamped_path_no_delta(repo):
    _commit(repo, "old.py", "o = 1\n", "old unstamped sweep")
    (repo / DECLARED).parent.mkdir(parents=True, exist_ok=True)
    (repo / DECLARED).write_text("a = 1\n", encoding="utf-8")
    _stage(repo, [DECLARED, "old.py"])
    reply = _handler(repo)
    assert reply["error"] is None
    assert _files(reply) == [DECLARED]
    assert reply["excluded"] == [{"path": "old.py", "reason": "no-delta"}]
    assert "o = 1" not in _diff(reply)


def test_f_an_uncommitted_edit_on_a_committed_unstamped_file_is_diffed_from_the_run_base(repo):
    _commit(repo, DECLARED, "a = 1\nb = 2\n", "unstamped landing")
    (repo / DECLARED).write_text("a = 1\nb = 3\n", encoding="utf-8")
    _stage(repo, [DECLARED])
    reply = _handler(repo)
    assert reply["error"] is None
    assert _files(reply) == [DECLARED]
    diff = _diff(reply)
    assert "-b = 2" in diff and "+b = 3" in diff
    assert "new file mode" not in diff
