"""Unit tests for atlas_freshness: plan_repair, apply_repair and the acted ledger."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.cartography import atlas_freshness as af
from coordinator_core.cartography.atlas_freshness import (
    ACTED_LEDGER_RELPATH,
    ActedFact,
    RepairPlan,
    apply_repair,
    plan_repair,
    read_acted,
    record_acted,
)

_FILE_INDEX = """\
---
last_mapped: 2026-08-06
---

# File index

## Directory → system

### cartography — atlas tooling

`coordinator_core/cartography/`
"""

_PAGE = "docs/architecture/systems/cartography.md"
_PAGE_TEXT = "---\nsystem: cartography\nfiles: 7\nlines: 10\n---\n\n# cartography\n\nprose 7 files\n"


def _write(root: Path, rel: str, text: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def _atlas(root: Path) -> None:
    _write(root, "docs/architecture/file-index.md", _FILE_INDEX)
    _write(root, _PAGE, _PAGE_TEXT)


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


@pytest.fixture
def no_spawn(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)


def test_added_covered_path_is_plus_one(tmp_path, no_spawn):
    _atlas(tmp_path)
    p = "coordinator_core/cartography/new_mod.py"
    plans, facts = plan_repair(tmp_path, [p], set(), {p})
    assert plans == [RepairPlan(_PAGE, 1)]
    assert facts == []


def test_deleted_covered_path_is_minus_one(tmp_path, no_spawn):
    _atlas(tmp_path)
    p = "coordinator_core/cartography/old.py"
    plans, facts = plan_repair(tmp_path, [p], {p})
    assert plans == [RepairPlan(_PAGE, -1)]
    assert facts == []


def test_add_and_delete_net_zero_emits_no_plan(tmp_path, no_spawn):
    _atlas(tmp_path)
    a, d = "coordinator_core/cartography/a.py", "coordinator_core/cartography/d.py"
    plans, _ = plan_repair(tmp_path, [a, d], {d}, {a})
    assert plans == []


def test_modified_covered_path_is_content_drift_not_a_plan(tmp_path, no_spawn):
    _atlas(tmp_path)
    p = "coordinator_core/cartography/tree.py"
    plans, facts = plan_repair(tmp_path, [p], set())
    assert plans == []
    assert [(f.page, f.kind) for f in facts] == [(_PAGE, "content_drift")]
    assert p in facts[0].detail and facts[0].at


def test_uncovered_source_candidate_is_flagged(tmp_path, no_spawn):
    _atlas(tmp_path)
    p = "coordinator_core/mystery_pkg/thing.py"
    plans, facts = plan_repair(tmp_path, [p], set(), {p})
    assert plans == []
    assert [f.kind for f in facts] == ["uncovered"]
    assert p in facts[0].detail


def test_non_candidates_are_ignored(tmp_path, no_spawn):
    _atlas(tmp_path)
    paths = [
        "coordinator_core/cartography/tests/test_x.py",
        "coordinator_core/cartography/readme.md",
        "state/handoffs/x.py",
    ]
    assert plan_repair(tmp_path, paths, set(), set(paths)) == ([], [])


def test_unreadable_atlas_is_empty(tmp_path, no_spawn):
    p = "coordinator_core/cartography/new_mod.py"
    assert plan_repair(tmp_path, [p], set(), {p}) == ([], [])


def test_missing_package_table_section_is_empty_and_never_uncovered(tmp_path, no_spawn):
    _write(tmp_path, "docs/architecture/file-index.md", "---\nlast_mapped: 2026-08-06\n---\n\n# File index\n")
    _write(tmp_path, _PAGE, _PAGE_TEXT)
    p = "coordinator_core/anything/new_mod.py"
    assert plan_repair(tmp_path, [p], set(), {p}) == ([], [])


def test_windows_separators_are_normalised(tmp_path, no_spawn):
    _atlas(tmp_path)
    p = "coordinator_core\\cartography\\new_mod.py"
    plans, _ = plan_repair(tmp_path, [p], set(), {p})
    assert plans == [RepairPlan(_PAGE, 1)]


def _tracked_atlas(root: Path) -> None:
    _atlas(root)
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "seed")


def test_apply_repair_rewrites_only_files_line(tmp_path):
    _tracked_atlas(tmp_path)
    written = apply_repair(tmp_path, [RepairPlan(_PAGE, 2)])
    assert written == [_PAGE]
    assert (tmp_path / _PAGE).read_text(encoding="utf-8") == _PAGE_TEXT.replace("files: 7", "files: 9")


def test_apply_repair_preserves_crlf(tmp_path):
    _atlas(tmp_path)
    (tmp_path / _PAGE).write_bytes(_PAGE_TEXT.replace("\n", "\r\n").encode())
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "-c", "core.autocrlf=false", "add", "-A")
    apply_repair(tmp_path, [RepairPlan(_PAGE, -1)])
    assert (tmp_path / _PAGE).read_bytes() == _PAGE_TEXT.replace("files: 7", "files: 6").replace("\n", "\r\n").encode()


def test_apply_repair_skips_dirty_page_and_records_page_dirty(tmp_path):
    _tracked_atlas(tmp_path)
    peer_edit = _PAGE_TEXT + "peer's uncommitted prose\n"
    (tmp_path / _PAGE).write_text(peer_edit, encoding="utf-8")
    assert apply_repair(tmp_path, [RepairPlan(_PAGE, 1)]) == []
    assert (tmp_path / _PAGE).read_text(encoding="utf-8") == peer_edit
    fact = read_acted(tmp_path)[_PAGE]
    assert fact.kind == "page_dirty"


def test_apply_repair_never_goes_negative(tmp_path):
    _tracked_atlas(tmp_path)
    apply_repair(tmp_path, [RepairPlan(_PAGE, -50)])
    assert "files: 0\n" in (tmp_path / _PAGE).read_text(encoding="utf-8")


def test_apply_repair_without_a_git_index_treats_page_as_dirty(tmp_path):
    _atlas(tmp_path)
    assert apply_repair(tmp_path, [RepairPlan(_PAGE, 1)]) == []
    assert (tmp_path / _PAGE).read_text(encoding="utf-8") == _PAGE_TEXT


def test_apply_repair_does_not_spawn(tmp_path, monkeypatch):
    _tracked_atlas(tmp_path)

    def _boom(*a, **k):
        raise AssertionError("subprocess spawned")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)
    assert apply_repair(tmp_path, [RepairPlan(_PAGE, 1)]) == [_PAGE]


def test_ledger_roundtrip_newest_per_page_wins(tmp_path):
    record_acted(tmp_path, [ActedFact("p1", "", "content_drift", "a", "2026-11-01")], "sha1")
    record_acted(tmp_path, [ActedFact("p1", "", "uncovered", "b"), ActedFact("p2", "", "content_drift", "c")], "sha2")
    got = read_acted(tmp_path)
    assert set(got) == {"p1", "p2"}
    assert (got["p1"].kind, got["p1"].sha) == ("uncovered", "sha2")
    assert got["p1"].at != ""
    assert (tmp_path / ACTED_LEDGER_RELPATH).read_text(encoding="utf-8").count("\n") == 3


def test_fact_with_own_sha_keeps_it(tmp_path):
    record_acted(tmp_path, [ActedFact("p", "own", "page_dirty", "")], "other")
    assert read_acted(tmp_path)["p"].sha == "own"


def test_read_acted_absent_ledger_is_empty(tmp_path):
    assert read_acted(tmp_path) == {}


def test_read_acted_skips_malformed_lines(tmp_path):
    ledger = tmp_path / ACTED_LEDGER_RELPATH
    ledger.parent.mkdir(parents=True)
    ledger.write_text('not json\n{"page": "p"}\n{"page":"q","sha":"s","kind":"k","detail":"d","at":"2026-11-01"}\n', encoding="utf-8")
    assert list(read_acted(tmp_path)) == ["q"]


def test_record_acted_empty_creates_nothing(tmp_path):
    record_acted(tmp_path, [], "sha")
    assert not (tmp_path / ACTED_LEDGER_RELPATH).exists()


def test_module_exports_contract():
    assert af.ACTED_LEDGER_RELPATH == ".coordinator-local/freshness/atlas-acted.jsonl"
