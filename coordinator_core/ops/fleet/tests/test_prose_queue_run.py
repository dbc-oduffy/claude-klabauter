"""Tests for the fleet.migrate_prose_queue run driver: skip chain, emission, report, confinement."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from coordinator_core.ops import queue_append
from coordinator_core.ops.fleet import _prose_queue_run as run

PIPE = (
    "# Improvement queue\n\n"
    "- 2026-06-02 | self | skills/x/SKILL.md | Fix the thing. More detail. | proposed target: do x\n"
    "- 2026-06-03 | self | scripts/y.sh | Second item here. | proposed target: do y\n"
)
CLOSED_TABLE = (
    "| ID | System | Severity | Description | Status | Found |\n"
    "|---|---|---|---|---|---|\n"
    "| B-1 | core | P1 | Broken thing. Details. | open | 2026-05-01 |\n"
    "| B-2 | core | P2 | Old thing. Details. | RESOLVED | 2026-05-02 |\n"
)
PROSE = "# Debt\n\nJust some heading prose with no rows.\n"


def _repo(tmp_path: Path, name: str, text: str) -> Path:
    (tmp_path / "state").mkdir(exist_ok=True)
    (tmp_path / "state" / f"{name}.md").write_text(text, encoding="utf-8")
    return tmp_path


def _counts(rep: dict) -> dict:
    return {k: v for k, v in rep.items() if k != "examples"}


def _created(tmp_path: Path) -> set:
    return {p for p in tmp_path.rglob("*") if p.is_file() and p.suffix == ".yaml"}


def test_dry_run_and_act_have_equal_counts_and_dry_run_writes_nothing(tmp_path):
    root = _repo(tmp_path, "improvement-queue", PIPE)
    dry = run.run_migration(root, ["improvement-queue"], True)
    assert _created(root) == set()
    act = run.run_migration(root, ["improvement-queue"], False)
    d, a = dry["files"]["improvement-queue"], act["files"]["improvement-queue"]
    assert _counts(d) == _counts(a)
    assert d["to-migrate"] == 2 and len(_created(root)) == 2
    assert d["migrated-incomplete"] == 0 and d["outcome"] == "parsed"


def test_second_run_skips_already_migrated(tmp_path):
    root = _repo(tmp_path, "improvement-queue", PIPE)
    run.run_migration(root, ["improvement-queue"], False)
    before = _created(root)
    again = run.run_migration(root, ["improvement-queue"], False)["files"]["improvement-queue"]
    assert _created(root) == before
    assert again["to-migrate"] == 0 and again["skipped"]["already-migrated"] == 2


def test_closed_in_source_is_skipped_and_not_emitted(tmp_path):
    root = _repo(tmp_path, "bug-backlog", CLOSED_TABLE)
    rep = run.run_migration(root, ["bug-backlog"], False)["files"]["bug-backlog"]
    assert rep["to-migrate"] == 1 and rep["skipped"]["closed-in-source"] == 1
    assert len(_created(root)) == 1


def test_duplicate_source_segment(tmp_path):
    line = "- 2026-06-02 | self | a/b.py | Same thing. Detail. | proposed target: z\n"
    root = _repo(tmp_path, "improvement-queue", line + line)
    rep = run.run_migration(root, ["improvement-queue"], False)["files"]["improvement-queue"]
    assert rep["to-migrate"] == 1 and rep["skipped"]["duplicate-source-segment"] == 1


def test_title_match_unmarked_lists_path(tmp_path):
    root = _repo(tmp_path, "improvement-queue", PIPE)
    fam = root / "state" / "improvement-queue"
    fam.mkdir()
    (fam / "2026-01-01-fix-the-thing-aaaaaaaaaaaa.yaml").write_text(
        "title: '  FIX   the thing.'\n", encoding="utf-8"
    )
    rep = run.run_migration(root, ["improvement-queue"], False)["files"]["improvement-queue"]
    assert rep["skipped"]["title-match-unmarked"] == 1 and rep["to-migrate"] == 1
    ex = rep["examples"]["skipped:title-match-unmarked"][0]
    assert ex["path"] == "state/improvement-queue/2026-01-01-fix-the-thing-aaaaaaaaaaaa.yaml"


def test_debt_row_gets_constants_and_incomplete_count(tmp_path):
    text = "- 2026-06-02 | self | a/b.py | Debt thing. Detail. | proposed target: z\n"
    root = _repo(tmp_path, "debt-backlog", text)
    rep = run.run_migration(root, ["debt-backlog"], False)["files"]["debt-backlog"]
    assert rep["to-migrate"] == 1 and rep["migrated-incomplete"] == 1
    (out,) = _created(root)
    content = out.read_text(encoding="utf-8")
    assert "(not recorded in legacy source)" in content
    assert "proposed_action: z" in content


def test_prose_file_is_unparseable_on_both_runs(tmp_path):
    root = _repo(tmp_path, "debt-backlog", PROSE)
    for dry in (True, False):
        rep = run.run_migration(root, ["debt-backlog"], dry)["files"]["debt-backlog"]
        assert rep["outcome"] == "file-unparseable" and rep["to-migrate"] == 0
    assert _created(root) == set()


def test_absent_source_is_reported_without_error(tmp_path):
    rep = run.run_migration(tmp_path, ["bug-backlog"], False)["files"]["bug-backlog"]
    assert rep["outcome"] == "source-absent" and rep["to-migrate"] == 0
    assert not (tmp_path / "state").exists()


def test_validation_failure_is_parse_error_in_both_modes(tmp_path, monkeypatch):
    root = _repo(tmp_path, "improvement-queue", PIPE)

    def boom(*a, **k):
        raise ValueError("queue.append: invalid")

    monkeypatch.setattr(run, "prepare_queue_entry", boom)
    for dry in (True, False):
        rep = run.run_migration(root, ["improvement-queue"], dry)["files"]["improvement-queue"]
        assert rep["unmigrated-parse-error"] == 2 and rep["to-migrate"] == 0
    assert _created(root) == set()


def test_source_bytes_unchanged_and_writes_confined(tmp_path):
    root = _repo(tmp_path, "improvement-queue", PIPE)
    src = root / "state" / "improvement-queue.md"
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    run.run_migration(root, ["improvement-queue"], False)
    assert hashlib.sha256(src.read_bytes()).hexdigest() == digest
    for p in tmp_path.rglob("*"):
        if p.is_file() and p != src:
            assert p.parent == root / "state" / "improvement-queue"


def test_confinement_refusal_writes_nothing(tmp_path, monkeypatch):
    root = _repo(tmp_path, "improvement-queue", PIPE)
    outside = tmp_path / "elsewhere"
    real = run.prepare_queue_entry

    def redirected(schema, **kw):
        p = real(schema, **kw)
        return queue_append.PreparedEntry(
            p.fields, p.digest12, p.slug, str(outside / "x.yaml"), p.yaml_content, p.schema, p.title
        )

    monkeypatch.setattr(run, "prepare_queue_entry", redirected)
    res = run.run_migration(root, ["improvement-queue"], False)
    assert res["exit_code"] == 1 and "outside" in res["error"]
    assert _created(root) == set() and not outside.exists()
    assert run.run_migration(root, ["improvement-queue"], True).get("exit_code") is None


def test_crlf_source_equals_lf_source(tmp_path):
    (tmp_path / "a").mkdir()
    a = _repo(tmp_path / "a", "improvement-queue", PIPE)
    (tmp_path / "b").mkdir()
    (tmp_path / "b" / "state").mkdir()
    (tmp_path / "b" / "state" / "improvement-queue.md").write_bytes(PIPE.replace("\n", "\r\n").encode())
    ra = run.run_migration(a, ["improvement-queue"], True)["files"]["improvement-queue"]
    rb = run.run_migration(tmp_path / "b", ["improvement-queue"], True)["files"]["improvement-queue"]
    assert _counts(ra) == _counts(rb)


def test_example_cap(tmp_path):
    rows = "".join(f"- 2026-06-02 | self | a/b.py | Item {i}. Detail. | proposed target: z\n" for i in range(25))
    root = _repo(tmp_path, "improvement-queue", rows)
    run.run_migration(root, ["improvement-queue"], False)
    rep = run.run_migration(root, ["improvement-queue"], True)["files"]["improvement-queue"]
    assert rep["skipped"]["already-migrated"] == 25
    assert len(rep["examples"]["skipped:already-migrated"]) == 20
