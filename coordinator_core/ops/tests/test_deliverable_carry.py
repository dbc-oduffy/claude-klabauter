from __future__ import annotations

from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field


def _write_frontmatter(path, **fields):
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("# body")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops.deliverable_carry import (
    resolve_session_chain_deliverable_id,
)

import pytest


def test_live_chain_carries_the_positive_control(tmp_path):
    handoff = tmp_path / "handoff.md"
    _write_frontmatter(
        handoff,
        status="claimed",
        deployment_state="in-progress",
        deliverable_id="dlv-live-chain-aaa111",
    )

    assert (
        resolve_session_chain_deliverable_id(read_frontmatter_field, str(handoff))
        == "dlv-live-chain-aaa111"
    )


@pytest.mark.parametrize("terminal_state", sorted(HANDOFF_TERMINAL_DEPLOYMENT))
def test_terminal_chain_falls_through_to_mint(tmp_path, terminal_state):
    handoff = tmp_path / "handoff.md"
    _write_frontmatter(
        handoff,
        status="claimed",
        deployment_state=terminal_state,
        deliverable_id="dlv-shipped-chain-bbb222",
    )

    assert resolve_session_chain_deliverable_id(read_frontmatter_field, str(handoff)) is None


def test_claimed_status_alone_does_not_block_the_carry(tmp_path):
    """`status` is the wrong axis and this pins it. `claimed` is a member of
    HANDOFF_TERMINAL_STATUS *and* the status of every actively-worked
    handoff, so a gate written against `status` would disable the tier
    outright. Only `deployment_state` separates the two."""
    handoff = tmp_path / "handoff.md"
    _write_frontmatter(
        handoff, status="claimed", deliverable_id="dlv-claimed-but-live-ccc333"
    )

    assert (
        resolve_session_chain_deliverable_id(read_frontmatter_field, str(handoff))
        == "dlv-claimed-but-live-ccc333"
    )


def test_absent_deployment_state_carries(tmp_path):
    handoff = tmp_path / "handoff.md"
    _write_frontmatter(handoff, deliverable_id="dlv-no-deployment-state-ddd444")

    assert (
        resolve_session_chain_deliverable_id(read_frontmatter_field, str(handoff))
        == "dlv-no-deployment-state-ddd444"
    )


def test_terminal_does_not_mean_the_chain_is_finished_and_the_gate_still_mints(tmp_path):
    handoff = tmp_path / "handoff.md"
    _write_frontmatter(
        handoff,
        status="claimed",
        deployment_state="continued",
        deliverable_id="dlv-chain-still-open-eee555",
    )

    assert resolve_session_chain_deliverable_id(read_frontmatter_field, str(handoff)) is None


def test_sizing_object_doc_type_declines_a_live_chain(tmp_path):
    handoff = tmp_path / "handoff.md"
    _write_frontmatter(
        handoff,
        status="claimed",
        deployment_state="in-progress",
        deliverable_id="dlv-unrelated-baton-fff666",
    )

    assert (
        resolve_session_chain_deliverable_id(
            read_frontmatter_field, str(handoff), doc_type="sizing-object"
        )
        is None
    )


def test_sizing_object_gate_fires_before_touching_the_chain_path(tmp_path):
    missing_path = str(tmp_path / "does-not-exist.md")

    assert (
        resolve_session_chain_deliverable_id(
            read_frontmatter_field, missing_path, doc_type="sizing-object"
        )
        is None
    )


def test_non_sizing_doc_type_is_unaffected_by_the_gate(tmp_path):
    handoff = tmp_path / "handoff.md"
    _write_frontmatter(
        handoff,
        status="claimed",
        deployment_state="in-progress",
        deliverable_id="dlv-live-chain-ggg777",
    )

    assert (
        resolve_session_chain_deliverable_id(
            read_frontmatter_field, str(handoff), doc_type="handoff"
        )
        == "dlv-live-chain-ggg777"
    )
    assert (
        resolve_session_chain_deliverable_id(read_frontmatter_field, str(handoff))
        == "dlv-live-chain-ggg777"
    )


import importlib.util as _importlib_util  # noqa: E402
from pathlib import Path as _Path  # noqa: E402


def _load_coordinator_doc_new_module():
    _repo_root = _Path(__file__).resolve().parents[3]
    _module_path = _repo_root / "coordinator" / "bin" / "coordinator-doc-new.py"
    _spec = _importlib_util.spec_from_file_location(
        "_coordinator_doc_new_under_test", _module_path
    )
    _module = _importlib_util.module_from_spec(_spec)
    _spec.loader.exec_module(_module)
    return _module


def test_wrapper_threads_doc_type_into_the_engine_sizing_object_gate(
    tmp_path, monkeypatch
):
    _module = _load_coordinator_doc_new_module()

    handoff = tmp_path / "handoff.md"
    _write_frontmatter(
        handoff,
        status="claimed",
        deployment_state="in-progress",
        deliverable_id="dlv-unrelated-baton-fff666",
    )

    monkeypatch.setattr(_module, "_NEW_CHAIN_REQUESTED", False)
    monkeypatch.setattr(
        _module,
        "_resolve_session_held_handoff_path",
        lambda repo_root: str(handoff),
    )

    assert (
        _module._resolve_session_chain_deliverable_id(
            "sizing-object", str(tmp_path)
        )
        is None
    )
