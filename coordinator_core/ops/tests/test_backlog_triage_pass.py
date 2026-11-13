"""Tests for backlog.triage_pass: changelog-first walk, rag gap fallback, output home, budget."""

from __future__ import annotations

import json
import time
import types
from pathlib import Path

import pytest

from coordinator_core.authz.classification import OP_CLASSIFICATION, OpClass
from coordinator_core.backlog import rag_reads
from coordinator_core.op_scopes import _OP_KEY_SCOPE
from coordinator_core.ops import backlog_triage_pass as op


def _w(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


def _daily(root: Path, day: str, bullets: list[str]) -> None:
    body = "\n".join(f"- {b}" for b in bullets)
    _w(root, f"archive/daily-summaries/{day}.md",
       f"---\nrollup_schema: 1\ngrain: day\nperiod: {day}\n---\n\n# Daily\n\n## Work Completed\n\n{body}\n")


PLAN = ("---\nstatus: open\n---\n# Flagged plan\n\n## Decision\n\n**Decision:** use the walker\n\n"
        "```yaml plan-tasks\n- id: C0\n  title: first\n  disposition: open\n```\n")


@pytest.fixture
def source(tmp_path: Path) -> Path:
    src = tmp_path / "source"
    _w(src, "docs/plans/2026-05/flagged-plan.md", PLAN)
    _w(src, "docs/plans/2026-05/unflagged-plan.md", "# Never cited\n")
    _w(src, "archive/week-changelogs/2026-05-25/WEEK-SUMMARY.md",
       "---\nrollup_schema: 1\ngrain: week\nweek_start: 2026-05-25\nweek_end: 2026-05-31\nnarrative: null\n---\n\n## Days\n")
    _daily(src, "2026-05-27", [
        "**Landed**: see docs/plans/2026-05/flagged-plan.md (`abc1234`)",
        "**Quiet**: no artifact cited (`bbb2222`)",
        "**Gone**: cites docs/plans/2026-05/missing-plan.md (`ccc3333`)",
    ])
    _daily(src, "2026-06-10", ["**Later**: another bullet (`ddd4444`)"])
    return src


@pytest.fixture
def invoking(tmp_path: Path) -> Path:
    repo = tmp_path / "invoking"
    (repo / ".git").mkdir(parents=True)
    return repo


@pytest.fixture(autouse=True)
def _no_read_contract(monkeypatch):
    def missing():
        raise ImportError("no read contract")

    monkeypatch.setattr(rag_reads, "_default_loader", missing)


def _run(invoking: Path, source: Path, run_id: str = "r1") -> dict:
    return op._handler({"run_id": run_id, "source_repo": str(source)}, repo_root=invoking / ".git")


def test_registered_mutating_common_dir():
    assert OP_CLASSIFICATION["backlog.triage_pass"] is OpClass.MUTATING
    assert _OP_KEY_SCOPE["backlog.triage_pass"] == "common_dir"


def test_every_week_walked_and_only_cited_artifact_opened(source, invoking):
    result = _run(invoking, source)
    digests = json.loads((invoking / result["digests_path"]).read_text())
    ledger = json.loads((invoking / result["ledger_path"]).read_text())

    assert digests["weeks_walked"] == digests["weeks_total"] == len(ledger["weeks"]) > 2
    assert digests["artifacts_cited"] == 2 and digests["artifacts_opened"] == 1
    paths = [p for t in digests["topics"] for p in t["paths"]]
    assert paths == ["docs/plans/2026-05/flagged-plan.md"]
    assert "unflagged-plan" not in json.dumps(ledger) + json.dumps(digests)
    assert digests["total_tokens"] == sum(t["tokens"] for t in digests["topics"]) > 0
    assert "C0" in digests["topics"][0]["text"]


def test_ledger_records_each_flagged_change_fate(source, invoking):
    ledger = json.loads((invoking / _run(invoking, source)["ledger_path"]).read_text())
    fates = {c["title"]: c["fate"] for c in ledger["changes"]}
    assert fates == {"Landed": "opened", "Quiet": "log_only", "Gone": "missing", "Later": "log_only"}
    assert {a["path"]: a["how"] for a in ledger["artifacts"]} == {
        "docs/plans/2026-05/flagged-plan.md": "opened",
        "docs/plans/2026-05/missing-plan.md": "missing",
    }


def test_missing_read_contract_records_gaps_and_falls_back(source, invoking):
    result = _run(invoking, source)
    forms = sorted(g["form"] for g in result["rag_gaps"])
    assert forms == ["changelog_resolve", "doc_outline"]
    assert all(g["reason"] for g in result["rag_gaps"])
    assert result["artifacts_opened"] == 1


def test_served_read_contract_supplies_paths_and_outlines(source, invoking, monkeypatch):
    _w(source, "docs/decisions/DR-900-served.md", "# should not be read\n")
    calls = []

    def changelog_resolve(entry):
        calls.append(entry["title"])
        if entry["title"] != "Quiet":
            return []
        return [{"path": "docs/decisions/DR-900-served.md",
                 "outline": ["# DR 900", "**Decision:** from the outline"]}]

    module = types.SimpleNamespace(changelog_resolve=changelog_resolve, doc_outline=lambda paths: [])
    monkeypatch.setattr(rag_reads, "_default_loader", lambda: module)

    result = _run(invoking, source)
    ledger = json.loads((invoking / result["ledger_path"]).read_text())
    digests = json.loads((invoking / result["digests_path"]).read_text())

    assert result["rag_gaps"] == []
    assert calls == ["Landed", "Quiet", "Gone", "Later"]
    assert {a["path"]: a["how"] for a in ledger["artifacts"]}["docs/decisions/DR-900-served.md"] == "outline"
    text = "\n".join(t["text"] for t in digests["topics"])
    assert "from the outline" in text and "should not be read" not in text


def test_cited_path_escaping_source_root_is_not_opened(source, invoking, tmp_path):
    _w(tmp_path, "outside.md", "# outside secret\n")
    _daily(source, "2026-05-28", ["**Sneaky**: see ../outside.md and docs/plans/../../../outside.md (`eee5555`)"])
    result = _run(invoking, source)
    digests = json.loads((invoking / result["digests_path"]).read_text())
    assert "outside secret" not in json.dumps(digests)


def test_output_lands_under_invoking_repo_only(source, invoking):
    before = sorted(p.relative_to(source).as_posix() for p in source.rglob("*"))
    result = _run(invoking, source, run_id="2026-11-13.run_1")
    after = sorted(p.relative_to(source).as_posix() for p in source.rglob("*"))

    assert before == after
    assert result["digests_path"] == "state/scratch/backlog-pass/2026-11-13.run_1/digests.json"
    assert (invoking / "state/scratch/backlog-pass/2026-11-13.run_1/triage-ledger.json").is_file()


def test_source_repo_defaults_to_invoking_repo(invoking):
    _daily(invoking, "2026-05-27", ["**Own**: change (`fff6666`)"])
    result = op._handler({"run_id": "r1"}, repo_root=invoking / ".git")
    assert result["weeks_walked"] == result["weeks_total"] >= 1


def test_rerun_rewrites_in_full(source, invoking):
    first = _run(invoking, source)
    path = invoking / first["digests_path"]
    path.write_text("stale", encoding="utf-8")
    _run(invoking, source)
    assert json.loads(path.read_text())["schema_version"] == op.SCHEMA_VERSION


@pytest.mark.parametrize("run_id", [None, "", "../x", "a/b", "..", "a..b", ".hidden", 7])
def test_bad_run_id_refused(source, invoking, run_id):
    with pytest.raises(ValueError):
        op._handler({"run_id": run_id, "source_repo": str(source)}, repo_root=invoking / ".git")


def test_repo_root_none_refused(source):
    with pytest.raises(ValueError):
        op._handler({"run_id": "r1", "source_repo": str(source)}, repo_root=None)


def test_source_repo_must_be_a_directory(invoking, tmp_path):
    with pytest.raises(ValueError):
        op._handler({"run_id": "r1", "source_repo": str(tmp_path / "nope")}, repo_root=invoking / ".git")


def test_process_time_under_500ms_on_fixture_corpus(source, invoking):
    for i in range(1, 29):
        _daily(source, f"2026-07-{i:02d}", [f"**Day {i}**: change (`{i:07x}`)"])
    start = time.process_time()
    result = _run(invoking, source)
    elapsed = time.process_time() - start
    assert result["weeks_walked"] == result["weeks_total"]
    assert elapsed < 0.5, f"{elapsed:.3f}s process time"
