"""A sizing emit that outruns the hook budget reports the run id it minted; the re-emit after an over-cap
failure keeps that id, so the deferred launcher's `dispatch.minted_run start` finds the run and no second run exists."""

from __future__ import annotations

import pytest

from coordinator_core.hooks import submit_engine_ask as mod
from coordinator_core.hooks.tests import test_submit_engine_ask_real_emit as real
from coordinator_core.ops.dispatch_emit import ask_compose, minted_run, op, prompt_fragments, run_token
from coordinator_core.ops.dispatch_emit.ask_contract import RECORD_DIR_ROOT

pytestmark = pytest.mark.complete

repo = real.repo
OVER_CAP = "composed ask script is over the Workflow runner's 100-byte cap: 200 bytes"


def _params() -> dict:
    return {"session_id": "sess-real-emit", "ask": True, "sizing_path": real.REL}


@pytest.fixture
def minted(monkeypatch: pytest.MonkeyPatch) -> list:
    """Every run id a token mint names; the first compose (hoisting off) is over the byte cap, as for a large sizing."""
    ids: list = []
    real_mint = run_token.mint
    real_compose = ask_compose.compose_ask_script

    def mint(root, run_id, *args, **kwargs):
        ids.append(run_id)
        return real_mint(root, run_id, *args, **kwargs)

    def compose(**kwargs):
        if not prompt_fragments.HOIST_ENABLED:
            raise ValueError(OVER_CAP)
        return real_compose(**kwargs)

    monkeypatch.setattr(run_token, "mint", mint)
    monkeypatch.setattr(ask_compose, "compose_ask_script", compose)
    return ids


def test_over_cap_re_emit_keeps_the_id_the_first_attempt_minted(repo, minted) -> None:
    real._put_sizing(repo)
    result = mod._default_emit(_params(), repo)

    assert len(set(minted)) == 1, minted  # one id reported by the first token, the re-emit mints the same one
    assert len(minted) == 2, minted
    assert result["run_id"] == minted[0]
    assert [p.name for p in (repo / RECORD_DIR_ROOT).iterdir() if p.is_dir()] == [minted[0]]
    started = minted_run._handler({"run_id": minted[0], "action": "start"}, repo)
    assert started["ok"] is True, started


def test_the_hook_emit_leaves_no_fresh_run_id_behind(repo, minted) -> None:
    real._put_sizing(repo)
    mod._default_emit(_params(), repo)
    assert op.FRESH_RUN_ID.get() is None


def test_a_direct_emit_publishes_the_id_it_minted_through_fresh_run_id(repo, minted) -> None:
    real._put_sizing(repo)
    prompt_fragments.HOIST_ENABLED = True
    try:
        first = op._dispatch_emit(_params(), repo)
    finally:
        prompt_fragments.HOIST_ENABLED = False
    assert first["run_id"] == minted[0]
    assert op.FRESH_RUN_ID.get() == first["run_id"]


def test_a_preminted_id_of_another_sizing_is_refused(repo, minted) -> None:
    real._put_sizing(repo)
    other = "warp-some-other-sizing-20261116T104247-1c98e2"
    with pytest.raises(ValueError, match="preminted_run_id"):
        op._dispatch_emit({**_params(), "preminted_run_id": other}, repo)
    assert minted == []
