"""Contract tests for fleet.migrate_prose_queue driven through _handler over pipe, table, DSR and prose fixtures."""

from __future__ import annotations

import asyncio
import hashlib
import subprocess
import time
from pathlib import Path

import pytest

from coordinator_core.ops.fleet import migrate_prose_queue

PIPE = (
    "# Improvement queue\n\n"
    "- 2026-06-02 | self | skills/x/SKILL.md | Fix the thing. More detail. | proposed target: do x\n"
    "- 2026-06-03 | self | scripts/y.sh | Second item here. | proposed target: do y\n"
)
TABLE = (
    "| ID | System | Severity | Description | Status | Found |\n"
    "|---|---|---|---|---|---|\n"
    "| B-1 | core | P1 | Broken thing. Details. | open | 2026-05-01 |\n"
    "| B-2 | core | P2 | Other broken thing. Details. | open | 2026-05-02 |\n"
)
CLOSED_TABLE = TABLE + "| B-3 | core | P2 | Old thing. Details. | RESOLVED | 2026-05-03 |\n"
DSR = (
    "Schema: `DSR-YYYY-MM-DD-N | file:line | one-line description | severity`\n\n"
    "- DSR-2026-05-27-1 | core/x.py | Description here | P2\n"
    "- **DSR-2026-06-16-1** Bold text rotted. More.\n"
)
PROSE = "# Improvement queue\n\nJust some heading prose with no rows.\n"

GRAMMAR_CASES = [
    ("improvement-queue", PIPE, 2),
    ("bug-backlog", TABLE, 2),
    ("debt-backlog", DSR, 2),
]


def _repo(root: Path, family: str, text: str) -> Path:
    (root / "state").mkdir(exist_ok=True)
    (root / "state" / f"{family}.md").write_text(text, encoding="utf-8")
    return root


def _call(root: Path, family: str, dry_run: bool) -> dict:
    params = {"repo_root": str(root), "family": [family], "dry_run": dry_run}
    result = asyncio.run(migrate_prose_queue._handler(params, root))
    assert "exit_code" not in result, result
    return result["files"][family]


def _yamls(root: Path) -> set:
    return {p for p in root.rglob("*.yaml") if p.is_file()}


def _counts(rep: dict) -> dict:
    return {k: v for k, v in rep.items() if k != "examples"}


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.mark.parametrize("family,text,expected", GRAMMAR_CASES)
def test_one_conversion_per_grammar(tmp_path, family, text, expected):
    root = _repo(tmp_path, family, text)
    rep = _call(root, family, False)
    assert rep["outcome"] == "parsed"
    assert rep["to-migrate"] == expected
    assert len(_yamls(root)) == expected


@pytest.mark.parametrize("family,text,expected", GRAMMAR_CASES)
def test_dry_run_and_act_buckets_equal_and_dry_run_writes_nothing(tmp_path, family, text, expected):
    root = _repo(tmp_path, family, text)
    dry = _call(root, family, True)
    assert _yamls(root) == set()
    act = _call(root, family, False)
    assert _counts(dry) == _counts(act)
    assert len(_yamls(root)) == expected


@pytest.mark.parametrize(
    "family,text", [("improvement-queue", PIPE), ("bug-backlog", TABLE), ("improvement-queue", PROSE)]
)
def test_second_run_emits_no_new_files(tmp_path, family, text):
    root = _repo(tmp_path, family, text)
    first = _call(root, family, False)
    created = _yamls(root)
    second = _call(root, family, False)
    assert _yamls(root) == created
    if text == PROSE:
        assert created == set()
        for rep in (first, second):
            assert rep["outcome"] == "file-unparseable" and rep["to-migrate"] == 0
    else:
        assert second["to-migrate"] == 0
        assert second["skipped"]["already-migrated"] == first["to-migrate"]


def test_duplicate_segment(tmp_path):
    line = "- 2026-06-02 | self | a/b.py | Same thing. Detail. | proposed target: z\n"
    root = _repo(tmp_path, "improvement-queue", line + line)
    rep = _call(root, "improvement-queue", False)
    assert rep["to-migrate"] == 1 and rep["skipped"]["duplicate-source-segment"] == 1


def test_title_match_unmarked(tmp_path):
    root = _repo(tmp_path, "improvement-queue", PIPE)
    fam = root / "state" / "improvement-queue"
    fam.mkdir()
    existing = fam / "2026-01-01-fix-the-thing-aaaaaaaaaaaa.yaml"
    existing.write_text("title: '  FIX   the thing.'\n", encoding="utf-8")
    rep = _call(root, "improvement-queue", False)
    assert rep["skipped"]["title-match-unmarked"] == 1 and rep["to-migrate"] == 1
    assert rep["examples"]["skipped:title-match-unmarked"][0]["path"] == (
        "state/improvement-queue/" + existing.name
    )


@pytest.mark.parametrize("family,text,_n", GRAMMAR_CASES)
def test_source_unchanged_and_writes_confined_to_family_dir(tmp_path, family, text, _n):
    root = _repo(tmp_path, family, text)
    src = root / "state" / f"{family}.md"
    before = _sha(src)
    _call(root, family, False)
    assert _sha(src) == before
    created = {p for p in root.rglob("*") if p.is_file() and p != src}
    assert created
    for p in created:
        assert p.parent == root / "state" / family


def test_closed_in_source_row_is_not_emitted(tmp_path):
    root = _repo(tmp_path, "bug-backlog", CLOSED_TABLE)
    rep = _call(root, "bug-backlog", False)
    assert rep["skipped"]["closed-in-source"] == 1 and rep["to-migrate"] == 2
    assert len(_yamls(root)) == 2
    assert not any("old-thing" in p.name for p in _yamls(root))


def test_debt_row_gets_not_recorded_and_migrated_incomplete(tmp_path):
    root = _repo(tmp_path, "debt-backlog", DSR)
    rep = _call(root, "debt-backlog", False)
    assert rep["migrated-incomplete"] == rep["to-migrate"] >= 1
    for out in _yamls(root):
        assert "(not recorded in legacy source)" in out.read_text(encoding="utf-8")


def test_handler_refuses_candidate_ids_and_foreign_repo_root(tmp_path):
    (tmp_path / "a").mkdir()
    root = _repo(tmp_path / "a", "improvement-queue", PIPE)
    other = tmp_path / "b"
    other.mkdir()
    base = {"repo_root": str(root), "family": ["improvement-queue"], "dry_run": False}
    refused = asyncio.run(migrate_prose_queue._handler({**base, "candidate_ids": ["x"]}, root))
    assert refused["exit_code"] == 2
    foreign = asyncio.run(migrate_prose_queue._handler({**base, "repo_root": str(other)}, root))
    assert foreign["exit_code"] == 1
    assert _yamls(tmp_path) == set()


def test_perf_200_row_pipe_no_subprocess(tmp_path, monkeypatch):
    rows = "".join(
        f"- 2026-06-02 | self | a/b{i}.py | Perf item number {i}. Detail. | proposed target: z\n"
        for i in range(200)
    )
    root = _repo(tmp_path, "improvement-queue", rows)

    def _no_spawn(*a, **k):
        raise AssertionError("subprocess.Popen spawned")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    start = time.process_time()
    rep = _call(root, "improvement-queue", False)
    elapsed = time.process_time() - start
    assert rep["to-migrate"] == 200 and len(_yamls(root)) == 200
    assert elapsed < 0.3, f"{elapsed:.3f}s process time"
