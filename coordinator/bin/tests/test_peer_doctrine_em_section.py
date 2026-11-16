"""The non-Claude EM section of the unimatrix peer doctrine (installed as ~/.codex/AGENTS.md).

This template, beside unimatrix-install, is the only copy: the installer renders it (G-EM ruling
2026-11-16; the DoE copy and its pins moved here).

A Codex EM submitted a run, never watched it, hand-did the same fix in parallel and committed with
plain git: these pin the three rules that close each step of that.
"""
import re
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[2] / "templates" / "unimatrix" / "peer-doctrine.md"


def _em_section() -> str:
    text = TEMPLATE.read_text(encoding="utf-8")
    start = text.index("## When the PM addresses you as EM")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def test_em_watches_a_submitted_run_to_exit_and_never_hand_does_it_in_parallel():
    em = _em_section()
    assert "coordinator-invoke workflow.fire_status '{\"fire_id\": \"<fire_id>\"}'" in em
    assert "`state` is `exited`" in em
    assert "never hand-done in parallel" in em


def test_em_hand_does_only_a_confident_xs_and_never_beside_a_live_run():
    em = _em_section()
    assert "coordinator/contract/xs-stop-rule.json" in em
    assert re.search(r"never while a run of\s+the same ask is live", em)


def test_em_commits_only_through_safe_commit_and_escalates_when_it_cannot_run():
    em = _em_section()
    assert '`coordinator-safe-commit "<subject>" -- <path> [<path>...]`' in em
    assert "never a bare `git add` and `git commit`" in em
    assert "claude-klabauter-44" in em and "do not fall back to plain git" in em


def test_a_worker_role_prompt_is_never_a_pm_ask():
    text = TEMPLATE.read_text(encoding="utf-8")
    assert "A prompt that begins `WORKER ROLE` is a stage brief from a workflow, never a PM ask" in text


def test_the_em_section_opens_with_the_worker_condition_before_any_em_rule():
    # A Codex stage worker obeyed the EM rules before it reached a trailing WORKER ROLE line (06aacf C2).
    em = _em_section()
    worker = em.index("A prompt that begins `WORKER ROLE`")
    assert worker < em.index("engine_ask.submit")
    assert "a bounded list of files to change, with or without that" in em
    assert em.count("WORKER ROLE") == 1
