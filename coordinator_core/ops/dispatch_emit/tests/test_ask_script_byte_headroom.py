"""Pins the production bare-ask script under 90% of the Workflow runner byte cap.

Measured on the production staged script (`staged_out` sink, as dispatch.emit always passes), with
hoisting on and every production default (no injected wrap_stage, blitz text, review roster or
REVIEW_KW). The m_plus shapes are pinned in test_m_plus_production_shapes_under_cap.
An unresolved plugin root raises AskPlanBlitzRefused and fails the test loudly; it is never skipped or stubbed.

coordinator_core/conftest.py dispatch_emit autouse fixtures:
- _unhoisted_ask_scripts: overridden here (HOIST_ENABLED back on); production hoists.
- _no_ambient_box_terms: kept; box terms are machine-local and would make the size vary per box.
- _no_ambient_plugin_root: kept for the agent-type host pin; its CLAUDE_PLUGIN_ROOT removal is
  undone per test with the root resolved at import, since plan-blitz.mjs is part of the script.
- _write_set_reads_clean: kept; it stubs the dirty-write-set guard on the plan route, which a
  bare ask does not take, so it does not change composed text.
"""

from pathlib import Path

from coordinator_core.ops.dispatch_emit import ask_compose, emit, prompt_fragments
from coordinator_core.ops.dispatch_emit.compose_size import ASK_SCRIPT_CEILING_BYTES
from coordinator_core.subagent_sandbox.provision_report import resolve_plugin_root
import pytest

_CEILING = ASK_SCRIPT_CEILING_BYTES
_REPO_ROOT = Path(__file__).resolve().parents[4]
# Resolved at import, before the per-test home quarantine hides the real plugin install.
_PLUGIN_ROOT = resolve_plugin_root()


def measured_bare_ask_bytes(monkeypatch) -> int:
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    assert _PLUGIN_ROOT, "plugin root did not resolve at import; plan-blitz.mjs is unreachable"
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)
    script = ask_compose.compose_ask_script(
        repo_root=str(_REPO_ROOT),
        prompt="probe ask",
        sizing_rel=None,
        run_id="ask-probe-000000",
        session_id="probe",
        script_path="scratch/warp/x.mjs",
        staged_out={},
    )
    n = len(script.encode("utf-8"))
    print(f"bare-ask bytes: {n}")
    return n


def test_bare_ask_stays_under_ninety_percent_of_the_runner_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    n = measured_bare_ask_bytes(monkeypatch)
    assert n <= _CEILING, f"bare ask is {n} bytes; ceiling is {_CEILING} (90% of the runner cap)"


_BLITZ_BYTES = 279005


def _padded_plan_blitz_text() -> str:
    from coordinator_core.ops.dispatch_emit.ask_plan_blitz import load_plan_blitz_text

    text = load_plan_blitz_text()
    short = _BLITZ_BYTES - len(text.encode("utf-8"))
    if short < 19:
        return text  # the real file already meets the 279 KB size; no padding needed
    # A non-comment line: _strip_comment_lines drops comments, which would shrink the pad.
    return text + "\nconst __pad = '" + "x" * (short - 19) + "';\n"


def _composed_bytes(monkeypatch, *, label) -> int:
    monkeypatch.setattr(prompt_fragments, "HOIST_ENABLED", True)
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", _PLUGIN_ROOT)
    monkeypatch.setattr(emit, "_WORKFLOW_SCRIPT_BYTE_CAP", 10**9)
    text = _padded_plan_blitz_text()
    script = ask_compose.compose_ask_script(
        repo_root=str(_REPO_ROOT),
        prompt="probe ask",
        sizing_rel=None,
        run_id="ask-probe-000000",
        session_id="probe",
        script_path="scratch/warp/x.mjs",
        plan_blitz_text=text,
        staged_out={},
    )
    n = len(script.encode("utf-8"))
    print(f"{label} bytes: {n}")
    return n


def test_raw_ask_with_279kb_plan_blitz_stays_under_ninety_percent(monkeypatch: pytest.MonkeyPatch) -> None:
    n = _composed_bytes(monkeypatch, label="raw-ask+279KB-blitz")
    assert n <= _CEILING, f"raw ask is {n} bytes; ceiling is {_CEILING} (90% of the runner cap)"


def test_runner_cap_constant_is_unchanged() -> None:
    assert emit._WORKFLOW_SCRIPT_BYTE_CAP == 524288


# Read at import: the per-test fixtures that lift the cap act after collection.
_RUNNER_CAP = emit._WORKFLOW_SCRIPT_BYTE_CAP
# Question: how far under the runner cap must the unhoisted, unstaged shape compose, so the next prompt addition
# does not turn every literal-text composer test into an AskComposeRefused again? 32 KiB, the growth of a few days.
_LITERAL_TEXT_HEADROOM_BYTES = 32768


def _split_bytes(monkeypatch, shape: str) -> int:
    from coordinator_core.benchmarks.ask_script_bytes_probe import compose_in_run_commit_split

    script = compose_in_run_commit_split(shape, _PLUGIN_ROOT, monkeypatch)
    n = len(script.encode("utf-8"))
    print(f"in-run-commit split ({shape}) bytes: {n}")
    return n


def test_in_run_commit_split_stays_under_ninety_percent_of_the_runner_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    assert _PLUGIN_ROOT, "plugin root did not resolve at import; plan-blitz.mjs is unreachable"
    n = _split_bytes(monkeypatch, "production")
    assert n <= _CEILING, f"in-run-commit split is {n} bytes; ceiling is {_CEILING} (90% of the runner cap)"


def test_literal_text_in_run_commit_split_keeps_headroom_under_the_runner_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    n = _split_bytes(monkeypatch, "literal_text")
    bar = _RUNNER_CAP - _LITERAL_TEXT_HEADROOM_BYTES
    assert n <= bar, (
        f"unhoisted, unstaged in-run-commit split is {n} bytes; bar is {bar} "
        f"({_RUNNER_CAP}-byte runner cap less {_LITERAL_TEXT_HEADROOM_BYTES})"
    )
