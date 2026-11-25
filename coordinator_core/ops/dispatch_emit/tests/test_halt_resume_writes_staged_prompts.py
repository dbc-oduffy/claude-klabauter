"""halt_resume_build: every staged prompt the recomposed resume script names exists under <record_dir>/prompts/."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose, halt_resume_build as hrb, halted_run
from coordinator_core.ops.dispatch_emit.ask_contract import record_dir
from coordinator_core.ops.dispatch_emit.ask_staged_prompts import PROMPTS_DIR

REL = "state/sizings/2026-10-16-refire-c2.yaml"
RUN = "warp-2026-10-16-refire-c2-20260101T000000-aaaaaa"
BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
BASE = "a" * 40


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: BLITZ)
    doc = {
        "schema": "sizing-object", "intent": "build the op", "estimate": {"tshirt": "XS", "provisional": True},
        "route": "dispatch", "detents": [], "fork": None, "xl_exit": None, "status": "sized",
        "premise": {"provenance": "not-applicable", "evidence": "fixture"},
        "pm_verbatims": [{"turn": 1, "text": "build the op"}],
        "interaction_mode": "pm", "deliverable_id": "dlv-refire-abc123",
        "exit_criterion": {"statement": "done", "accepted": {"pm_quote": "yes", "on": "2026-10-16", "mode": "pm"}},
    }
    (tmp_path / REL).write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    monkeypatch.setattr(halted_run, "_RESUME_SPAWNER", lambda root, run_id: None)
    halted_run.write_halt_marker(record_dir(tmp_path, RUN), halted="criterion", sizing_rel=REL, run_base_sha=BASE)
    return tmp_path


def test_the_resume_build_writes_each_staged_prompt_under_the_record_dir(repo: Path) -> None:
    record = hrb.build(repo, RUN)
    fragments = record["hoisted_fragments"]
    assert fragments, "the composed resume script stages at least one prompt body"
    staged = record_dir(repo, RUN) / PROMPTS_DIR
    for name, text in fragments.items():
        assert (staged / f"{name}.md").read_text(encoding="utf-8") == text


def test_every_prompt_file_the_recomposed_script_names_exists_after_the_build(repo: Path) -> None:
    record = hrb.build(repo, RUN)
    script = (repo / record["composed_script_rel"]).read_text(encoding="utf-8")
    named = set(re.findall(rf"{PROMPTS_DIR}/([\w.\-]+)\.md", script))
    assert named, "the recomposed script references staged prompt files"
    staged = record_dir(repo, RUN) / PROMPTS_DIR
    assert [n for n in sorted(named) if not (staged / f"{n}.md").is_file()] == []


def test_a_rebuild_over_existing_prompt_files_keeps_them_current(repo: Path) -> None:
    hrb.build(repo, RUN)
    staged = record_dir(repo, RUN) / PROMPTS_DIR
    victim = next(staged.glob("*.md"))
    victim.write_text("stale", encoding="utf-8")
    record = hrb.build(repo, RUN)
    assert victim.read_text(encoding="utf-8") == record["hoisted_fragments"][victim.stem]
