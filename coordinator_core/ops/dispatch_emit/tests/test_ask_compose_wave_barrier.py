"""The ask execute loop commits each wave through emit's `_commitWave` before launching the next wave."""

from __future__ import annotations

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW

_BLITZ_FN = "  async function planBlitz(args) {\n    return { ready: [] };\n  }"
_ROADMAP_TEXT = (
    "export const meta = { name: 'rb', phases: [{ title: 'Synthesize', detail: 'x' }] };\n"
    "return { gate_report_path: 'g.json' };"
)


def _script() -> str:
    return ask_compose.compose_ask_script(
        repo_root="REPO", prompt="add a thing", sizing_rel=None, run_id="run-1", session_id=None,
        wrap_stage=lambda text: (_BLITZ_FN, ["Size", "Plan"]), plan_blitz_text="stub",
        roadmap_blitz_text=_ROADMAP_TEXT, **REVIEW_KW,
    )


def _loop(script: str) -> str:
    start = script.rindex("for (const w of _waves) {", 0, script.index("const _waveIds"))
    return script[start: script.index("await Promise.all(_verifications);", start)]


def test_wave_commit_awaited_before_the_next_wave_launches():
    loop = _loop(_script())
    assert loop.index("_runRow(") < loop.index("await Promise.allSettled(_waveIds") < loop.index("await _commitWave(w, _waveIds)")
    assert loop.rindex("await _commitWave(w, _waveIds)") < loop.index("\n  }\n", loop.index("await _commitWave("))


def test_row_commit_descriptor_fills_landed():
    loop = _loop(_script())
    assert "{ title: r.id, paths: r.writes ?? [], declared: r.writes ?? [] }" in loop
    work = next(ln for ln in loop.splitlines() if "stallMs:" in ln)
    assert work.rstrip().endswith("{ title: r.id, paths: r.writes ?? [], declared: r.writes ?? [] });")


def test_commit_wave_label_present():
    assert "'commit:wave-' + n" in _script()


def test_failed_commit_records_failure_and_does_not_halt():
    script = _script()
    body = script[script.index("async function _commitWave("): script.index("\n  }\n", script.index("async function _commitWave("))]
    assert "_commitFailures.push(" in body
    assert "_halted" not in body
    assert "_commitFailures.length) log(" in script
