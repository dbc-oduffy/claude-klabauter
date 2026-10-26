"""Pins that the plan-review roster forwarder is live against the real plugin root.

`resolve()` forwards the DoE-owned `plan_review` block of review-roster-fragment.json as
`parallelReview`; an absent block makes the forwarder silently inert, so the live test fails
rather than skips.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from coordinator_core import machine_resolver
from coordinator_core.ops.dispatch_emit import plan_blitz_args, review_wave
from coordinator_core.trusted_root_guard import PLUGIN_MIRROR_LIVE_PATH_KEY

_FRAGMENT_RELS = (
    Path("coordinator") / "contract" / "review-roster-fragment.json",
    Path("contract") / "review-roster-fragment.json",
)

_SHAPE = (
    "tiers{XS..XXL:{reviewers[{agentType, model}]}}, signal_reviewers{signal:{agentType, model}}, "
    "lead_round_cap, revise{step:'merged', after}"
)


def _fragment_path(plugin_root: Path) -> Path | None:
    for rel in _FRAGMENT_RELS:
        if (plugin_root / rel).is_file():
            return plugin_root / rel
    return None


def _registered_plugin_root() -> Path | None:
    """Plugin root as the installed engine resolves it outside a harness session: the registered
    coordinator-claude checkout, then the settings-home plugin mirror."""
    checkout = machine_resolver.registry_get("repos.coordinator_claude")
    if checkout and (Path(checkout) / "coordinator").is_dir():
        return Path(checkout) / "coordinator"
    mirror = machine_resolver.registry_get(PLUGIN_MIRROR_LIVE_PATH_KEY)
    if mirror and Path(mirror).is_dir():
        return Path(mirror)
    return None


@pytest.mark.real_home
def test_real_plugin_root_fragment_carries_valid_plan_review_block():
    plugin_root = _registered_plugin_root()
    if plugin_root is None:
        pytest.skip("neither repos.coordinator_claude nor the plugin mirror is registered")
    fragment = _fragment_path(plugin_root)
    where = fragment or (plugin_root / _FRAGMENT_RELS[0])
    block = plan_blitz_args._plan_review_roster(plugin_root)
    assert block is not None, (
        f"{where} has no `plan_review` block (or the file is absent/unparseable); "
        f"M4's parallelReview forwarder is inert. Missing keys: plan_review with {_SHAPE}"
    )
    assert review_wave.review_wave_args(block) is not None, (
        f"{where} `plan_review` block is not usable by review_wave.review_wave_args; "
        f"required keys: {_SHAPE}. Got: {json.dumps(block, sort_keys=True)[:300]}"
    )


def test_resolve_emits_parallel_review_for_valid_block(tmp_path):
    contract = tmp_path / "contract"
    contract.mkdir()
    block = {
        "tiers": {
            "L": {"reviewers": [{"agentType": "reviewer-a", "model": "m-a"}]},
            "XL": {"reviewers": [{"agentType": "reviewer-a", "model": "m-a"}]},
        },
        "signal_reviewers": {"ux-flow": {"agentType": "reviewer-b", "model": "m-b"}},
        "lead_round_cap": None,
        "revise": {"step": "merged", "after": "parallel-review-wave"},
    }
    (contract / "review-roster-fragment.json").write_text(
        json.dumps({"plan_review": block}), encoding="utf-8"
    )
    out = plan_blitz_args.resolve(plugin_root=tmp_path, engine_root=None, sizing_abs="s")
    assert out[review_wave.PARALLEL_REVIEW_ARG] == {
        "models": {"reviewer-a": "m-a", "reviewer-b": "m-b"},
        "tiers": {"reviewer-a": ["L", "XL"]},
        "revise": {
            "agent": review_wave.REVISE_AGENT_TYPE,
            "model": review_wave.REVISE_MODEL,
        },
    }


def test_resolve_omits_parallel_review_for_absent_block(tmp_path):
    contract = tmp_path / "contract"
    contract.mkdir()
    (contract / "review-roster-fragment.json").write_text("{}", encoding="utf-8")
    out = plan_blitz_args.resolve(plugin_root=tmp_path, engine_root=None, sizing_abs="s")
    assert review_wave.PARALLEL_REVIEW_ARG not in out
