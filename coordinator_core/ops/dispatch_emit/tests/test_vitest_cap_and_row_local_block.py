"""The box's vitest worker cap reaches every emitted test-runner brief, and a
ROW-LOCAL-BLOCK reply never arms the stop-rule halt."""

from __future__ import annotations

from coordinator_core import machine_resolver
from coordinator_core.bash_guards._heavy_admission_contract import KEY_VITEST_MAX_WORKERS
from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from .conftest import REVIEW_KW


def _compose() -> str:
    row = WaveRow(
        id="C1", title="t", surface="dispatch_emit", writes=["a.py", "tests/test_a.py"], reads=[],
        depends_on=[], body="",
    )
    return compose_script(
        [[row]], name="n", description="d", plan_path="docs/plans/example.md", **REVIEW_KW
    )


def _set_cap(monkeypatch, value):
    monkeypatch.setattr(
        machine_resolver, "registry_get", lambda key: value if key == KEY_VITEST_MAX_WORKERS else None
    )


def test_cap_is_threaded_into_terminal_and_per_row_runner_briefs(monkeypatch):
    _set_cap(monkeypatch, "2")
    script = _compose()
    assert script.count("--maxWorkers=2") >= 2


def test_unset_cap_emits_nothing_new():
    assert "maxWorkers" not in _compose()
    assert emit._vitest_cap_clause() == ""


def test_non_positive_cap_is_ignored(monkeypatch):
    _set_cap(monkeypatch, "0")
    assert "maxWorkers" not in _compose()


def test_executor_brief_names_the_row_local_token():
    assert f"{emit._ROW_LOCAL_BLOCK_TOKEN}:" in emit._stop_rule_clause()


def test_halt_is_guarded_by_the_row_local_token():
    script = _compose()
    assert emit._ROW_LOCAL_BLOCK_JS_RE in script
    assert (
        f"if ({emit._STOP_RULE_JS_RE}.test(_text) && !{emit._ROW_LOCAL_BLOCK_JS_RE}.test(_text))"
        in script
    )
