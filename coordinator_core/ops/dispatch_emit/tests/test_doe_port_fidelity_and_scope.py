"""coordinator_core/ops/dispatch_emit/tests/test_doe_port_fidelity_and_scope.py

DoE parity for two of the three legs ported into this engine's emitter (plan
`2026-09-21-bug-blitz-emitter-engine-leg.md` follow-on port):

  - leg 1: coordinator-content-repo ``emit-dispatch-workflow.py ::
    _check_fidelity_bar`` -> ``emit._check_fidelity_bar`` (in-process
    ``coordinator_core.roadmap.prep_gate`` call, never a file-path load).
    DoE parity source:
    ``coordinator-content-repo/coordinator/bin/tests/test_emit_dispatch_workflow_fidelity_gate.py``.
  - leg 2: coordinator-content-repo ``emit-dispatch-workflow.py ::
    _append_declared_scope`` -> ``emit._declared_scope_block``, appended
    into ``_row_return_contract``'s own rendered prompt (this engine has no
    on-disk brief file/subprocess CLI to append to -- DoE parity source:
    ``coordinator-content-repo/coordinator/tests/test_emit_dispatch_workflow.py`` (the
    ``_ScopeRow``/``_rendered_scope`` tests).

Leg 3 (``_guard_against_fired_drift`` -> ``op.guard_against_fired_drift``)
has no DoE unit test targeting the function by name (grepped DoE's whole
tests/ corpus) -- covered directly here instead, matching the DoE function's
own behavior contract (module docstring narration in ``op.py``).
"""

from __future__ import annotations
from .conftest import REVIEW_KW

import textwrap

import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit import op as dispatch_op
from coordinator_core.ops.dispatch_emit.spine_read import UNDECLARED
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow


def _wave_row(id_, writes, surface="dispatch_emit"):
    return WaveRow(
        id=id_,
        title=f"title-{id_}",
        surface=surface,
        writes=writes,
        reads=[],
        depends_on=[],
    )


# ---------------------------------------------------------------------------
# leg 1: the in-session fidelity bar
# ---------------------------------------------------------------------------

_FIDELITY_MARKER_ENV = emit._FIDELITY_MARKER_ENV

# Same one-row spine shape DoE's own fixture used
# (test_emit_dispatch_workflow_fidelity_gate.py::_SPINE_ONE_ROW).
_SPINE_ONE_ROW = """
## Tasks

```yaml plan-tasks
- id: C1
  title: ship it
  body: ship the thing
  change_kind: code-edit
  surface: coordinator_core/thing.py
  writes: [coordinator_core/thing.py]
  queue_scope: project
  disposition: open
```
"""

# Fails the mise-prep bar: no prime_exit_criterion, no census.
_FAILING_FM = """title: fixture
created: 2026-09-07
author: test
status: draft
"""

# Clears the mise-prep bar.
_PASSING_FM = """title: fixture
created: 2026-09-07
author: test
status: draft
prime_exit_criterion:
  statement: the tree carries the thing
  derived_from: state/sizings/2026-09-07-x.yaml
census: []
"""


def _write_plan(tmp_path, *, frontmatter, name="fixture-plan.md"):
    path = tmp_path / name
    path.write_text(f"---\n{frontmatter}---\n\n# fixture\n{_SPINE_ONE_ROW}", encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clean_marker(monkeypatch):
    monkeypatch.delenv(_FIDELITY_MARKER_ENV, raising=False)


def test_marker_unset_with_a_failing_plan_emits_as_today(tmp_path, monkeypatch):
    monkeypatch.delenv(_FIDELITY_MARKER_ENV, raising=False)
    plan_path = _write_plan(tmp_path, frontmatter=_FAILING_FM, name="marker-unset-failing.md")
    (tmp_path / "coordinator_core").mkdir()

    script = emit.emit_script(plan_path, repo_root=tmp_path, **REVIEW_KW)

    assert script


def test_marker_set_with_a_failing_plan_refuses(tmp_path, monkeypatch):
    monkeypatch.setenv(_FIDELITY_MARKER_ENV, "1")
    plan_path = _write_plan(tmp_path, frontmatter=_FAILING_FM, name="marker-set-failing.md")
    (tmp_path / "coordinator_core").mkdir()

    with pytest.raises(emit.FidelityBarRefusalError):
        emit.emit_script(plan_path, repo_root=tmp_path, **REVIEW_KW)


def test_marker_set_with_a_passing_plan_emits(tmp_path, monkeypatch):
    monkeypatch.setenv(_FIDELITY_MARKER_ENV, "1")
    plan_path = _write_plan(tmp_path, frontmatter=_PASSING_FM, name="marker-set-passing.md")
    (tmp_path / "coordinator_core").mkdir()

    script = emit.emit_script(plan_path, repo_root=tmp_path, **REVIEW_KW)

    assert script


def test_fidelity_bar_calls_prep_gate_in_process_not_by_file_path():
    """CSF-C5's own instruction: call ``coordinator_core.roadmap.prep_gate``
    directly, never load a hyphenated CLI script by file path the way DoE's
    ``_load_mise_prep_gate`` does (there is no such indirection to load in
    this repo)."""
    import inspect

    source = inspect.getsource(emit._check_fidelity_bar)
    assert "prep_gate" in source
    assert "spec_from_file_location" not in source
    assert "exec_module" not in source


# ---------------------------------------------------------------------------
# leg 2: declared writes scope, inlined into the row prompt
# ---------------------------------------------------------------------------


def test_declared_writes_are_rendered_with_the_test_path_in_scope():
    row = _wave_row(
        "C1",
        ["coordinator_core/ops/foo.py", "coordinator_core/ops/tests/test_foo.py"],
    )
    out = emit._declared_scope_block(row)
    assert "coordinator_core/ops/foo.py" in out
    assert "coordinator_core/ops/tests/test_foo.py" in out, (
        "the declared test path is missing -- this is the exact half executors "
        "dropped in the DoE-reported defect this leg ports the fix for"
    )
    assert "IN SCOPE and is expected to be written" in out


def test_brief_requires_examined_and_changed_as_separate_counts():
    row = _wave_row("C1", ["a/one.py", "a/two.py", "a/three.py"])
    out = emit._declared_scope_block(row)
    assert "examined and changed as two separate counts" in out
    assert "over this list of 3" in out
    assert "A path you never opened is an omission" in out


def test_empty_writes_renders_as_a_positive_no_files_claim():
    row = _wave_row("C1", [])
    out = emit._declared_scope_block(row)
    assert "writes: []" in out and "NO files" in out
    assert "- `" not in out.split("## Files you may write")[1]


def test_undeclared_writes_instructs_stop_and_report_blocked():
    row = _wave_row("C1", UNDECLARED)
    out = emit._declared_scope_block(row)
    assert "UNDECLARED" in out
    assert "stop and report BLOCKED" in out.lower() or "Stop and report BLOCKED" in out


def test_row_prompt_carries_the_declared_scope_block():
    """The block reaches the composed row prompt (the engine's own
    inlined-prompt shape, replacing DoE's on-disk brief append)."""
    row = _wave_row("C1", ["coordinator_core/ops/foo.py"])
    contract = emit._row_return_contract(row, "docs/plans/p.md")
    assert "## Files you may write (declared `writes:` scope)" in contract
    assert "coordinator_core/ops/foo.py" in contract


# ---------------------------------------------------------------------------
# leg 3: refuse to fire drifted bytes
# ---------------------------------------------------------------------------


def _sha256(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_guard_against_fired_drift_passes_on_matching_sha256(tmp_path):
    script_path = tmp_path / "plan.workflow.mjs"
    script_path.write_bytes(b"console.log(1);\n")
    dispatch_op.guard_against_fired_drift(script_path, _sha256("console.log(1);\n"))


def test_guard_against_fired_drift_refuses_on_a_peer_overwrite(tmp_path):
    script_path = tmp_path / "plan.workflow.mjs"
    script_path.write_text("console.log(1);\n", encoding="utf-8")
    with pytest.raises(dispatch_op.FiredDriftError):
        dispatch_op.guard_against_fired_drift(script_path, _sha256("console.log(2);\n"))


def test_guard_against_fired_drift_is_a_noop_on_a_missing_script(tmp_path):
    """A missing script is not this guard's refusal -- fire_workflow's own
    ScriptNotFoundError owns it."""
    script_path = tmp_path / "does-not-exist.mjs"
    dispatch_op.guard_against_fired_drift(script_path, _sha256("anything"))


# ---------------------------------------------------------------------------
# foreign_overwrite verdict: live in the production path, not test-only
# ---------------------------------------------------------------------------


def test_refuse_foreign_emission_is_called_from_the_registered_op():
    """Confirms ``_refuse_foreign_emission`` is wired into ``_dispatch_emit``
    itself (the ``@register_op("dispatch.emit")`` production handler), not
    exercised only by test_queue_emit.py's direct unit calls."""
    import inspect

    source = inspect.getsource(dispatch_op._dispatch_emit)
    assert "_refuse_foreign_emission(" in source
