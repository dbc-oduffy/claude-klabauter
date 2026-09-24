"""Tests for coordinator_core.ops.deliverable_carry's session-chain tier.

The `resolve_deliverable_and_initiative` cascade is covered by
`coordinator_core/ops/test_deliverable_carry.py`.
"""
from __future__ import annotations

from coordinator_core.ops.read_frontmatter_field import read_frontmatter_field


def _write_frontmatter(path, **fields):
    lines = ["---"]
    for key, value in fields.items():
        lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append("# body")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# --- session-chain tier: the liveness gate (2026-08-28) ---------------------
#
# Spec backlink: state/bug-backlog/2026-08-28-the-session-chain-tier-carries-
#                off-a-shipped-baton.yaml

from coordinator_core.lifecycle_constants import HANDOFF_TERMINAL_DEPLOYMENT
from coordinator_core.ops.deliverable_carry import (
    resolve_session_chain_deliverable_id,
)

import pytest


def test_live_chain_carries_the_positive_control(tmp_path):
    """The tier's whole reason to exist still fires: a held handoff that has
    not shipped is the chain being authored into, and its id carries."""
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
    """A session goes on holding its claim after the chain ships — the held
    handoff outlives the work, so a terminal `deployment_state` must NOT
    carry, or the session's next deliverable joins its last one's spine.

    Parametrised over the canonical four-member set rather than a literal:
    both three-member copies in the tree omit `abandoned`, and this gate must
    not silently narrow if one of them is ever substituted."""
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
    """Unset is the pre-terminal default; treating absence as terminal would
    break the ordinary live chain. Direction stated so a later 'fail closed'
    edit has to argue with a test."""
    handoff = tmp_path / "handoff.md"
    _write_frontmatter(handoff, deliverable_id="dlv-no-deployment-state-ddd444")

    assert (
        resolve_session_chain_deliverable_id(read_frontmatter_field, str(handoff))
        == "dlv-no-deployment-state-ddd444"
    )


def test_terminal_does_not_mean_the_chain_is_finished_and_the_gate_still_mints(tmp_path):
    """Reviewer finding (2026-08-28, S1-code): the gate's docstring originally
    asserted that a terminal handoff implies a finished deliverable chain. It
    does not. `shipped` means "terminal with resolvable commit evidence", not
    "released", and `continued` says by name that a successor exists. Measured
    on this corpus: of 7 distinct deliverable_ids carried by terminal handoffs,
    3 have a non-handoff artifact newer than the handoff.

    So this pins the gate as a chosen DEFAULT rather than an invariant: given a
    terminal handoff on a chain that is still being authored into, the tier
    mints rather than carries. That is the deliberate direction -- a spurious
    second id is visible at a rollup and mergeable, while joining new work onto
    a closed spine corrupts shared history -- and the author's escape is to pass
    the id explicitly at the flag tier, which precedes this one.

    If this assertion is ever inverted, the docstring's cost argument must be
    re-made, not just the constant swapped."""
    handoff = tmp_path / "handoff.md"
    _write_frontmatter(
        handoff,
        status="claimed",
        deployment_state="continued",
        deliverable_id="dlv-chain-still-open-eee555",
    )

    assert resolve_session_chain_deliverable_id(read_frontmatter_field, str(handoff)) is None


# --- session-chain tier: the sizing-object gate (2026-08-30) ----------------
#
# Spec backlink: docs/plans/2026-08-30-drop-releases-a-claim-it-never-held.md
#                chunk C4; docs/wiki/deliverable-id.md § "Sizing-object
#                negative spec"


def test_sizing_object_doc_type_declines_a_live_chain(tmp_path):
    """A sizing object is the front door for a NOVEL ask — co-membership in
    whatever baton the session happens to be holding is not evidence the
    sizing belongs to that chain. Even a held handoff that would otherwise
    carry (live, non-terminal, has a deliverable_id) must be declined when
    the artifact being scaffolded is a sizing object."""
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
    """The gate must decline on `doc_type` alone, before any read of
    `chain_artifact_path` — proven by passing a path that does not exist at
    all (a real chain-path check would degrade to None too, but for the
    wrong reason; this pins the ORDER, not just the outcome)."""
    missing_path = str(tmp_path / "does-not-exist.md")

    assert (
        resolve_session_chain_deliverable_id(
            read_frontmatter_field, missing_path, doc_type="sizing-object"
        )
        is None
    )


def test_non_sizing_doc_type_is_unaffected_by_the_gate(tmp_path):
    """Negative control: any other `doc_type` (including the default `None`,
    the pre-existing call sites' behaviour) leaves the tier's normal
    carry-on-a-live-chain outcome untouched."""
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


# --- caller-level: the wrapper must thread doc_type through (2026-08-30) ----
#
# Spec backlink: coordinator/bin/coordinator-doc-new.py ::
#                _resolve_session_chain_deliverable_id
#
# The engine-level tests above call `resolve_session_chain_deliverable_id`
# directly with `doc_type="sizing-object"` handed in -- they stayed green
# even while the ONLY production caller, the CLI wrapper of the same name in
# `coordinator/bin/coordinator-doc-new.py`, silently dropped `doc_type` on
# the floor at its call site and always defaulted the engine's gate to
# `None`. This test exercises that wrapper function (imported via
# importlib, since the module's hyphenated filename is not import-safe),
# not the engine function called directly, so a regression on the
# thread-through breaks it even though the tests above stay green.

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
    """Caller-level regression: `coordinator-doc-new.py`'s own
    `_resolve_session_chain_deliverable_id` wrapper must pass its `doc_type`
    argument through to the engine call, so a `sizing-object` scaffold does
    NOT inherit a live held-handoff's `deliverable_id`. Before the
    thread-through fix, the wrapper always called the engine with the
    default `doc_type=None`, so this held-handoff carry-on would have fired
    regardless of the caller's actual doc type."""
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
