"""A composed script records its scoped test set where `retest_targeted` reads it."""
from __future__ import annotations

import json
import re

from coordinator_core.ops.dispatch_emit.emit import compose_script
from coordinator_core.ops.dispatch_emit.wake_digest import next_action_parts
from coordinator_core.ops.dispatch_emit.wave_map import WaveRow

from .conftest import REVIEW_KW


def _row(id_, writes):
    return WaveRow(id=id_, title=f"title {id_}", surface="dispatch_emit", writes=writes, reads=[], depends_on=[])


def test_composed_script_carries_the_scoped_set_in_terminal_commit_params():
    waves = [[_row("C1", ["coordinator_core/ops/dispatch_emit/wave_map.py"])]]
    script = compose_script(waves, name="wf", description="scoped", **REVIEW_KW)
    match = re.search(r"targeted_test_paths: (\[[^\]]*\])", script)
    assert match, "terminal_commit params omit targeted_test_paths"
    assert json.loads(match.group(1).replace("'", '"'))


def test_script_without_a_test_scope_omits_the_key():
    waves = [[_row("C1", ["coordinator_core/subagent_sandbox/CONTRACT.md"])]]
    script = compose_script(waves, name="wf", description="prose only", **REVIEW_KW)
    assert "targeted_test_paths" not in script


def test_params_key_is_last_and_omitted_when_empty():
    kw = dict(
        has_commit_request=True, review_vars=None, test_var="_t", falsifier_var=None,
        verification_var="_v", test_absent_status="not_run",
    )
    with_paths = next_action_parts(**kw, targeted_test_paths=["test_a.py"])[2]
    tail = with_paths[with_paths.index("targeted_test_paths"):]
    assert tail.startswith('targeted_test_paths: ["test_a.py"] }')
    for empty in (None, []):
        assert "targeted_test_paths" not in next_action_parts(**kw, targeted_test_paths=empty)[2]
