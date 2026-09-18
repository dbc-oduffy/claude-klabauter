"""Every emitted script opens with a narration that `node --check` will
flag a top-level `return` in this file and that this is not a defect.

Spec backlink: claude-klabauter#21, part 2. The emitted `.workflow.mjs`
body runs a top-level `return` -- legal only because the Workflow runner
executes the body directly (module docstring § Top-level body, never a
defined-but-uninvoked wrapper). `node --check` has no way to know that and
reports `SyntaxError: Illegal return statement` for every script this
module emits; an operator running that check unprompted misreads it as a
real defect. This test pins the narration comment's presence and position,
and that it does not perturb the `export const meta` block `_workflow_
contract.extract_meta_block` and `workflow.validate` both key on.
"""

from coordinator_core.ops._workflow_contract import extract_meta_block, run_checks, Severity
from coordinator_core.ops.dispatch_emit.emit import (
    _NODE_CHECK_DOES_NOT_APPLY_COMMENT,
    compose_script,
)
from coordinator_core.ops.dispatch_emit.tests.test_emit import _two_wave_fixture


def test_emitted_script_opens_with_the_node_check_narration():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")
    assert script.startswith(_NODE_CHECK_DOES_NOT_APPLY_COMMENT)


def test_narration_names_the_illegal_return_statement_error():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")
    assert "Illegal return statement" in script
    assert "not a defect" in script


def test_narration_precedes_the_meta_block():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")
    assert script.index(_NODE_CHECK_DOES_NOT_APPLY_COMMENT) < script.index(
        "export const meta"
    )


def test_meta_block_still_extracts_cleanly_with_the_narration_present():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")
    block = extract_meta_block(script)
    assert block is not None
    assert block.startswith("{")
    assert block.endswith("}")


def test_narration_does_not_introduce_a_run_checks_error():
    waves = _two_wave_fixture()
    script = compose_script(waves, name="wf", description="two waves")
    findings = run_checks(script)
    errors = [f for f in findings if f.severity is Severity.ERROR]
    assert errors == []
