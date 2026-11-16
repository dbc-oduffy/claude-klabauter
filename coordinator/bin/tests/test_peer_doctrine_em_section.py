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
    assert "coordinator-invoke workflow.fire_wait '{\"fire_id\": \"<fire_id>\"}'" in em
    assert "workflow.fire_status" not in em
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


def test_em_revises_or_refires_a_failed_run_and_stops_with_a_report_when_no_pm_is_present():
    # 1b attempt 5: the EM saw exit 1, hand-edited and committed, then asked the absent PM a question.
    em = _em_section()
    flat = re.sub(r"\s+", " ", em)
    assert "exited non-zero or halted is steered with `workflow.revise` or refired by its `owed_next_action`" in flat
    assert "hand edits and a hand commit never replace it" in flat
    assert "With no PM present" in flat and "never end on a question: stop with a report" in flat


def test_a_repeated_halt_is_reported_with_both_runs_and_never_hand_landed():
    # 1b attempt 6: a revised refire also halted; the EM ran the test itself and hand-committed.
    flat = re.sub(r"\s+", " ", _em_section())
    assert "A repeated halt is reported, never hand-landed" in flat
    assert "once a refire or revised run also halts, stop with a report naming both runs and the `owed_next_action`" in flat
    assert "do not run the test or commit by hand" in flat


def test_the_em_section_states_the_launchers_open_run_exit_code():
    flat = re.sub(r"\s+", " ", _em_section())
    assert "Your launcher exits 3 if you end while a run you fired is neither landed nor halted." in flat


def test_the_em_section_applies_only_under_the_headless_em_token_or_an_interactive_pm():
    # Prose alone did not stop a codex sizing-scout obeying the EM rules (1b attempt 3, run 1712a5); the
    # gate names the env var only headless-em-launch sets, and the executor strips from agent children.
    em = _em_section()
    gate = em.index("`COORDINATOR_ALLOW_HEADLESS_EM_TOKEN` is set")
    assert gate < em.index("A prompt that begins `WORKER ROLE`") < em.index("engine_ask.submit")
    assert "Any other prompt is a worker's, and nothing below applies." in em
