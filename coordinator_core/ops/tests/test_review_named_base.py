"""named_review_base: the commit a live revise note names, as a per-path review base."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import review_named_base as rnb
from coordinator_core.ops.review_unreviewed_bases import EMPTY_TREE_SHA
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

RUN = "ask-own"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True,
        **no_console_creationflags(),
    ).stdout.strip()


def _commit(repo: Path, name: str, text: str) -> str:
    p = repo / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", f"edit {name}")
    return _git(repo, "rev-parse", "HEAD")


def _notes(repo: Path, run: str, *lines: dict) -> None:
    d = repo / "state" / "warp" / run
    d.mkdir(parents=True, exist_ok=True)
    (d / "notes.jsonl").write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")


def _note(text: str, at: str = "2026-11-16T10:00:00Z") -> dict:
    return {"note": text, "at": at}


@pytest.fixture
def repo(tmp_path):
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "T")
    return tmp_path


def test_named_commit_that_touched_path_maps_to_first_parent(repo):
    before = _commit(repo, "seed.py", "s = 1\n")
    x = _commit(repo, "pkg/a.py", "a = 1\n")
    _notes(repo, RUN, _note(f"review against {x} please"))
    assert rnb.named_review_base(repo, RUN, ["pkg/a.py"]) == {"pkg/a.py": before}


def test_named_commit_that_did_not_touch_path_is_the_base_itself(repo):
    _commit(repo, "pkg/a.py", "a = 1\n")
    x = _commit(repo, "other.py", "o = 1\n")
    _notes(repo, RUN, _note(f"base {x}"))
    assert rnb.named_review_base(repo, RUN, ["pkg/a.py"]) == {"pkg/a.py": x}


def test_root_named_commit_maps_to_empty_tree(repo):
    x = _commit(repo, "pkg/a.py", "a = 1\n")
    _notes(repo, RUN, _note(x))
    assert rnb.named_review_base(repo, RUN, ["pkg/a.py"]) == {"pkg/a.py": EMPTY_TREE_SHA}


def test_per_path_touched_and_untouched(repo):
    before = _commit(repo, "seed.py", "s = 1\n")
    x = _commit(repo, "pkg/a.py", "a = 1\n")
    _notes(repo, RUN, _note(x))
    assert rnb.named_review_base(repo, RUN, ["pkg/a.py", "seed.py"]) == {"pkg/a.py": before, "seed.py": x}


def test_newest_note_wins(repo):
    old = _commit(repo, "other.py", "o = 1\n")
    new = _commit(repo, "other2.py", "o = 2\n")
    _notes(repo, RUN, _note(f"use {old}", "2026-11-16T09:00:00Z"), _note(f"no, use {new}", "2026-11-16T10:00:00Z"))
    assert rnb.named_review_base(repo, RUN, ["pkg/a.py"]) == {"pkg/a.py": new}


def test_retracted_note_is_ignored(repo):
    x = _commit(repo, "other.py", "o = 1\n")
    _notes(repo, RUN, _note(f"use {x}"), {"retracts": "N1", "reason": "wrong", "at": "2026-11-16T11:00:00Z"})
    assert rnb.named_review_base(repo, RUN, ["pkg/a.py"]) == {}


def test_unresolvable_token_is_skipped_for_the_next(repo):
    x = _commit(repo, "other.py", "o = 1\n")
    _notes(repo, RUN, _note(f"not {'deadbeef' * 5} but {x}"))
    assert rnb.named_review_base(repo, RUN, ["pkg/a.py"]) == {"pkg/a.py": x}


def test_abbreviation_resolves_only_against_known_shas(repo):
    x = _commit(repo, "other.py", "o = 1\n")
    _notes(repo, RUN, _note(f"use {x[:10]}"))
    assert rnb.named_review_base(repo, RUN, ["pkg/a.py"]) == {}
    assert rnb.named_review_base(repo, RUN, ["pkg/a.py"], known_shas=[x]) == {"pkg/a.py": x}


def test_no_note_or_no_token_returns_empty(repo):
    _commit(repo, "other.py", "o = 1\n")
    assert rnb.named_review_base(repo, RUN, ["pkg/a.py"]) == {}
    _notes(repo, RUN, _note("nothing hex here, deploy later"))
    assert rnb.named_review_base(repo, RUN, ["pkg/a.py"]) == {}


def test_predecessor_notes_are_read_through_resumed_from(repo):
    x = _commit(repo, "other.py", "o = 1\n")
    _notes(repo, "ask-prior", _note(f"use {x}"))
    _notes(repo, RUN)
    (repo / "state" / "warp" / RUN / "run.json").write_text(json.dumps({"resumed_from": "ask-prior"}), encoding="utf-8")
    assert rnb.named_review_base(repo, RUN, ["pkg/a.py"]) == {"pkg/a.py": x}


def test_empty_paths_or_unsafe_run_id_returns_empty(repo):
    x = _commit(repo, "other.py", "o = 1\n")
    _notes(repo, RUN, _note(x))
    assert rnb.named_review_base(repo, RUN, []) == {}
    assert rnb.named_review_base(repo, "../escape", ["pkg/a.py"]) == {}


def test_spawns_nothing(repo, monkeypatch):
    x = _commit(repo, "other.py", "o = 1\n")
    _notes(repo, RUN, _note(x))
    import subprocess as sp

    def boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(sp, "Popen", boom)
    assert rnb.named_review_base(repo, RUN, ["pkg/a.py"]) == {"pkg/a.py": x}
