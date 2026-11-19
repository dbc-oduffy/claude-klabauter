"""The emit op's `family` rides the composed gate legs and the gate stamps it on the minted sizing.

Run: python -m pytest coordinator_core/ops/dispatch_emit/tests/test_ask_gate_family_stamp.py -q
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.dispatch_emit import ask_compose
from coordinator_core.ops.dispatch_emit.ask_contract import RUN_DIR_ROOT
from coordinator_core.ops.dispatch_emit.ask_gate import _gate_body, gate
from coordinator_core.ops.dispatch_emit.op import _dispatch_emit
from coordinator_core.ops.dispatch_emit.tests.test_ask_compose_em_brief import _compose
from coordinator_core.ops.dispatch_emit.tests.test_ask_gate import REL, _put, repo as gate_repo  # noqa: F401

pytestmark = pytest.mark.complete

_BLITZ = "export const meta = { phases: [{ title: 'Plan' }] };\nreturn { ready: [] };\n"
_NAMES_AGY = "Make the change. Executing agent family: agy. Declared writes: a.py."


@pytest.fixture(autouse=True)
def _git_marker(gate_repo: Path) -> None:  # noqa: F811
    """`locked_rmw` resolves the lock dir from the repo's `.git`."""
    (gate_repo / ".git").mkdir(exist_ok=True)


def _stamped(repo: Path) -> object:
    return yaml.safe_load((repo / REL).read_text(encoding="utf-8")).get("family")


def _refusal(v) -> str:
    assert v.arm is None and v.halt is not None and v.halt["kind"] == "refusal", v
    return v.halt["reason"]


def test_gate_stamps_the_family_on_the_sizing(gate_repo) -> None:  # noqa: F811
    _put(gate_repo)
    v = gate(gate_repo, REL, family="codex")
    assert v.halt is None and v.arm == "s", v
    assert _stamped(gate_repo) == "codex"


def test_a_stamped_family_satisfies_the_directive_the_ask_text_names(gate_repo) -> None:  # noqa: F811
    _put(gate_repo, em_context=_NAMES_AGY)
    v = gate(gate_repo, REL, family="agy")
    assert v.halt is None and v.arm == "s", v
    assert _stamped(gate_repo) == "agy"


def test_the_same_family_again_is_a_noop(gate_repo) -> None:  # noqa: F811
    _put(gate_repo, family="codex")
    before = (gate_repo / REL).read_bytes()
    assert gate(gate_repo, REL, family="codex").halt is None
    assert (gate_repo / REL).read_bytes() == before


def test_unknown_family_is_refused_naming_it_and_writes_nothing(gate_repo) -> None:  # noqa: F811
    _put(gate_repo)
    before = (gate_repo / REL).read_bytes()
    reason = _refusal(gate(gate_repo, REL, family="gpt"))
    assert "gpt" in reason and "codex" in reason
    assert (gate_repo / REL).read_bytes() == before


def test_a_family_differing_from_the_sizings_own_is_refused_naming_both(gate_repo) -> None:  # noqa: F811
    _put(gate_repo, family="grok")
    reason = _refusal(gate(gate_repo, REL, family="codex"))
    assert "codex" in reason and "grok" in reason
    assert _stamped(gate_repo) == "grok"


def test_claude_stamps_nothing_but_still_conflicts_with_a_recorded_family(gate_repo) -> None:  # noqa: F811
    _put(gate_repo)
    assert gate(gate_repo, REL, family="claude").halt is None
    assert _stamped(gate_repo) is None
    _put(gate_repo, family="codex")
    assert "claude" in _refusal(gate(gate_repo, REL, family="claude"))


def test_no_family_leaves_the_sizing_untouched(gate_repo) -> None:  # noqa: F811
    _put(gate_repo)
    before = (gate_repo / REL).read_bytes()
    assert gate(gate_repo, REL).halt is None
    assert (gate_repo / REL).read_bytes() == before


def test_the_gate_op_body_takes_family_and_stamps(gate_repo) -> None:  # noqa: F811
    _put(gate_repo)
    out = _gate_body({"sizing_path": REL, "family": "codex"}, gate_repo)
    assert "error" not in out and out.get("halt") is None, out
    assert _stamped(gate_repo) == "codex"


@pytest.fixture
def emit_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir(exist_ok=True)
    (tmp_path / "state" / "sizings").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "11111111-2222-3333-4444-555555555555")
    monkeypatch.delenv("DELIVERABLE_ID", raising=False)
    monkeypatch.setattr(ask_compose, "_read_plan_blitz", lambda: _BLITZ)
    return tmp_path


def _emit(repo: Path, **extra) -> str:
    result = _dispatch_emit({"ask": "add a thing", "repo_root": str(repo), **extra}, repo_root=repo)
    assert "error" not in result, result
    (script,) = sorted((repo / RUN_DIR_ROOT).glob("*.workflow.mjs"))
    return script.read_text(encoding="utf-8")


def test_emit_family_rides_the_composed_gate_legs(emit_repo) -> None:
    script = _emit(emit_repo, family="codex")
    assert 'writes: _writes, family: "codex"' in script


def test_emit_without_family_composes_no_family_slot(emit_repo) -> None:
    script = _emit(emit_repo)
    assert 'family: "' not in script and '"family":"' not in script


def test_emit_unknown_family_is_refused_naming_it_and_composes_nothing(emit_repo) -> None:
    with pytest.raises(ValueError, match="gpt"):
        _emit(emit_repo, family="gpt")
    assert not list((emit_repo / RUN_DIR_ROOT).glob("*.workflow.mjs"))


def test_family_rides_the_scout_gate_template_and_is_absent_without_one() -> None:
    famed, _ = _compose(family="codex", accept_pending=True)
    assert 'family: "codex"' in famed and '"family":"codex"' in famed
    plain, _ = _compose(accept_pending=True)
    assert 'family: "' not in plain and '"family":"' not in plain


_RB = "export const meta = { phases: [{ title: 'Roadmap' }] };\nreturn { ok: true };\n"


def test_roadmap_arm_with_a_family_composes_in_the_body_that_carries_it(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ask_compose, "_known_arm", lambda *_: "roadmap")
    script, _ = _compose(prompt=None, sizing_rel="state/sizings/x.yaml", family="codex", roadmap_blitz_text=_RB)
    assert 'family: "codex"' in script and "phase('roadmap-blitz')" in script
