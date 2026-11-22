"""The flat (non-group) re-verify prompt carries the verify-scope targets, not only the group one."""
import pytest

from coordinator_core.ops.dispatch_emit import emit
from coordinator_core.ops.dispatch_emit.tests.conftest import REVIEW_KW
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import _row
from coordinator_core.ops.review_mint.execute_review import _verify_scope_clause

_TARGET = "tests/test_sentinel_target_zz.py"
_PATH = "coordinator_core/ops/dispatch_emit/wave_map.py"


def test_the_clause_names_every_target_per_path():
    out = _verify_scope_clause({"a/one.py": ["tests/test_one.py"], "b/two.py": ["tests/t2.py"]})
    assert "tests/test_one.py" in out and "tests/t2.py" in out


def test_no_targets_adds_nothing():
    assert _verify_scope_clause({}) == "" and _verify_scope_clause(None) == ""


@pytest.mark.parametrize("variant", ["review_only", "writes_under"])
def test_emit_passes_the_verify_scope_targets_off_the_pipelined_groups_path(monkeypatch, variant):
    seen = []

    def _fake(rows, repo_root):
        seen.append(rows)
        return {_PATH: [_TARGET]}

    monkeypatch.setattr(emit, "_verify_scope_by_path", _fake)
    kwargs = dict(name="wf", description="d", plan_path="docs/plans/p.md", run_base_sha="a" * 40, **REVIEW_KW)
    if variant == "review_only":
        waves = [[_row("C1", [_PATH])]]
        kwargs.update(review_only=True, review_terminal_sha="b" * 40)
    else:
        waves = [[_row("C1", [_PATH])._replace(writes=[], writes_under=("coordinator_core/ops/dispatch_emit/",))]]
    script = emit.compose_script(waves, **kwargs)
    assert seen and _TARGET in script
