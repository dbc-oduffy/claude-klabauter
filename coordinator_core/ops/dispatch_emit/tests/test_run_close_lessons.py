"""Tests for the run-close lessons leg."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.frontmatter import schema_validate
from coordinator_core.frontmatter.schema_validate import validate_frontmatter_obj
from coordinator_core.ops.dispatch_emit.run_close_lessons import lessons_leg

CAND = {
    "title": "A lesson",
    "body": "Body text.",
    "change_kind": "wiki-append",
    "target_wiki": "coordinator/docs/wiki/x.md",
}


@pytest.fixture
def outbox(tmp_path, monkeypatch):
    out = tmp_path / "outbox"
    monkeypatch.setenv("LESSON_PROMOTE_OUTBOX_ROOT", str(out))
    monkeypatch.setattr(
        subprocess, "Popen", lambda *a, **k: pytest.fail("process spawned")
    )
    return out


def test_ran_writes_one_open_file_per_candidate(outbox, tmp_path):
    crit = {"lesson_candidates": [CAND, dict(CAND, title="Second")]}
    res = lessons_leg(tmp_path, "run-a", crit)
    assert res.status == "ran"
    files = sorted(outbox.glob("*.yaml"))
    assert len(files) == 2
    text = files[0].read_text(encoding="utf-8")
    assert "status: open" in text
    assert "authored_by: terminal-judge" in text
    assert "run_id: run-a" in text
    assert "lessons-outbox/" in res.detail and str(tmp_path) not in res.detail


def test_written_file_is_schema_valid(outbox, tmp_path):
    import yaml

    lessons_leg(tmp_path, "run-v", {"lesson_candidates": [CAND]})
    doc = next(iter(yaml.safe_load_all(next(outbox.glob("*.yaml")).read_text(encoding="utf-8"))))
    assert doc["title"] == "A lesson" and doc["status"] == "open"
    assert doc["id"] == "run-close-run-v-0"
    schema = json.loads(
        (Path(schema_validate.__file__).parent / "schemas" / "lessons-outbox.schema.json").read_text(encoding="utf-8")
    )
    assert validate_frontmatter_obj(doc, schema) == {"ok": True}


def test_rerun_is_discharged_without_duplicate(outbox, tmp_path):
    crit = {"lesson_candidates": [CAND]}
    assert lessons_leg(tmp_path, "run-b", crit).status == "ran"
    assert lessons_leg(tmp_path, "run-b", crit).status == "discharged"
    assert len(list(outbox.glob("*.yaml"))) == 1


@pytest.mark.parametrize("crit", [None, {}, {"lesson_candidates": []}, {"lesson_candidates": None}])
def test_not_applicable_without_candidates(outbox, tmp_path, crit):
    assert lessons_leg(tmp_path, "run-c", crit).status == "not-applicable"
    assert not outbox.exists()


def test_incomplete_candidate_fails(outbox, tmp_path):
    res = lessons_leg(tmp_path, "run-d", {"lesson_candidates": [{"title": "t"}]})
    assert res.status == "failed"
