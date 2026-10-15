"""Op-level pins: plan and inventory routes compose the review wave; every
code-landing route refuses, writing no script, when review inputs are absent.
Queue-route presence is pinned by the queue composition tests."""

from __future__ import annotations

import copy

import pytest

from coordinator_core.ops.dispatch_emit import op as op_mod
from coordinator_core.ops.dispatch_emit.op import NoReviewStageError, _dispatch_emit
from coordinator_core.ops.dispatch_emit.tests.test_emit_wake_digest import (
    _V5_FRAGMENT,
    _V5_STAGE_SCHEMAS,
)
from coordinator_core.ops.dispatch_emit.tests.test_inventory_mint import (
    _WRITE_OVERLAP_INVENTORY,
    _write_inventory,
)
from coordinator_core.ops.dispatch_emit.tests.test_queue_emit import (
    _FIXTURE_PROFILE_DIR,
    _setup_repo,
)

_PLAN = (
    "---\n---\n\n# Plan\n\n## Tasks\n\n"
    "```yaml plan-tasks\n"
    "- id: C1\n"
    "  title: Row\n"
    "  change_kind: script-edit\n"
    "  surface: pkg/row.py\n"
    "  writes:\n"
    "    - pkg/row.py\n"
    "```\n"
)


def _patch_loaders(monkeypatch, fragment):
    def _load_fragment():
        if isinstance(fragment, Exception):
            raise fragment
        return fragment

    monkeypatch.setattr(op_mod.review_mint_op, "load_fragment", _load_fragment)
    monkeypatch.setattr(
        op_mod.review_mint_op, "load_stage_schemas", lambda: _V5_STAGE_SCHEMAS
    )


def _without_route(route):
    fragment = copy.deepcopy(_V5_FRAGMENT)
    fragment["execute_review"]["required_for_emit"].remove(route)
    return fragment


_REFUSAL_FRAGMENTS = [
    pytest.param(lambda route: FileNotFoundError("no sibling root"), id="unloadable"),
    pytest.param(_without_route, id="route-not-required"),
]


def _plan_params(tmp_path):
    plan_path = tmp_path / "plan.md"
    plan_path.write_text(_PLAN, encoding="utf-8")
    output_path = tmp_path / "out.mjs"
    return {"plan_path": str(plan_path), "output_path": str(output_path)}, output_path


def _inventory_params(tmp_path):
    inventory_path = _write_inventory(tmp_path, _WRITE_OVERLAP_INVENTORY)
    output_path = tmp_path / "fixture.workflow.mjs"
    return (
        {
            "inventory_path": str(inventory_path),
            "output_path": str(output_path),
            "target_root": str(tmp_path),
        },
        output_path,
    )


def _queue_params(tmp_path):
    repo_root, queue_dir, _run_dir = _setup_repo(tmp_path)
    output_path = tmp_path / "queue-grind" / "emitted.mjs"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    return (
        {
            "queue": [str(queue_dir)],
            "profile": "fixture",
            "profile_dir": str(_FIXTURE_PROFILE_DIR),
            "output_path": str(output_path),
        },
        output_path,
        repo_root,
    )


def test_plan_route_composes_execute_review(tmp_path, monkeypatch):
    _patch_loaders(monkeypatch, _V5_FRAGMENT)
    params, output_path = _plan_params(tmp_path)

    result = _dispatch_emit(params, repo_root=tmp_path)

    assert result["ok"] is True, result["findings"]
    script = output_path.read_text(encoding="utf-8")
    assert "_reviewPrep" in script and "_reviewWave" in script


def test_inventory_route_composes_execute_review(tmp_path, monkeypatch):
    _patch_loaders(monkeypatch, _V5_FRAGMENT)
    params, output_path = _inventory_params(tmp_path)

    result = _dispatch_emit(params)

    assert result["ok"] is True, result["findings"]
    script = output_path.read_text(encoding="utf-8")
    assert "_reviewPrep" in script and "_reviewWave" in script


@pytest.mark.parametrize("make_fragment", _REFUSAL_FRAGMENTS)
def test_plan_route_refuses_without_review(tmp_path, monkeypatch, make_fragment):
    _patch_loaders(monkeypatch, make_fragment("plan"))
    params, output_path = _plan_params(tmp_path)

    with pytest.raises(NoReviewStageError):
        _dispatch_emit(params, repo_root=tmp_path)

    assert not output_path.exists()


@pytest.mark.parametrize("make_fragment", _REFUSAL_FRAGMENTS)
def test_inventory_route_refuses_without_review(tmp_path, monkeypatch, make_fragment):
    _patch_loaders(monkeypatch, make_fragment("inventory"))
    params, output_path = _inventory_params(tmp_path)

    with pytest.raises(NoReviewStageError):
        _dispatch_emit(params)

    assert not output_path.exists()


@pytest.mark.parametrize("make_fragment", _REFUSAL_FRAGMENTS)
def test_queue_route_refuses_without_review(tmp_path, monkeypatch, make_fragment):
    _patch_loaders(monkeypatch, make_fragment("queue"))
    params, output_path, repo_root = _queue_params(tmp_path)

    with pytest.raises(NoReviewStageError):
        _dispatch_emit(params, repo_root=repo_root)

    assert not output_path.exists()
