"""The shared `content_root_for` primitive, and twin parity with the bin/ side.

This is the convergence point for a systemic defect: ~45 call sites joined
`<resolved doe_root>/coordinator/...` as the ONLY layout, so every one of them
resolved correctly against the private authoring tree and produced a path that
cannot exist against a published flat mirror. The same fix was applied pointwise
in `data_root()` in August and nowhere else, which is what let it keep
reappearing. These arms pin the primitive's contract so the conversions have
something to converge on, and pin the two trees' copies against drift.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from coordinator_core.data_root import FLAT_CONTENT_ROOT_MARKER, content_root_for

_BIN_LIB = str(Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "lib")


def _private_tree(root: Path) -> Path:
    (root / "coordinator").mkdir(parents=True)
    return root


def _flat_mirror(root: Path) -> Path:
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text("{}")
    return root


def test_the_private_authoring_tree_resolves_to_its_coordinator_subdir(tmp_path):
    root = _private_tree(tmp_path / "DoE-claude")
    assert content_root_for(str(root)) == root / "coordinator"


def test_the_published_flat_mirror_resolves_to_its_own_root(tmp_path):
    # The arm that was missing everywhere. A cloud container registers exactly
    # this shape and sets `repos.doe_claude` to it.
    root = _flat_mirror(tmp_path / "coordinator-claude")
    assert content_root_for(str(root)) == root


def test_the_private_layout_wins_when_a_root_somehow_carries_both(tmp_path):
    # Order is load-bearing: probing private first is what makes this widen
    # nothing for the callers that already worked.
    root = _flat_mirror(_private_tree(tmp_path / "both"))
    assert content_root_for(str(root)) == root / "coordinator"


def test_a_bare_directory_is_not_a_content_root(tmp_path):
    (tmp_path / "bare").mkdir()
    assert content_root_for(str(tmp_path / "bare")) is None


def test_a_flat_directory_without_its_plugin_manifest_is_not_a_content_root(tmp_path):
    root = tmp_path / "unmarked"
    (root / ".claude-plugin").mkdir(parents=True)
    assert content_root_for(str(root)) is None


@pytest.mark.parametrize("empty", ["", None])
def test_an_unresolved_doe_root_is_none_never_a_raise(empty):
    assert content_root_for(empty) is None


def test_a_path_argument_is_accepted_as_well_as_a_string(tmp_path):
    root = _private_tree(tmp_path / "DoE-claude")
    assert content_root_for(root) == root / "coordinator"


def test_a_trailing_separator_does_not_defeat_the_probe(tmp_path):
    root = _private_tree(tmp_path / "DoE-claude")
    assert content_root_for(str(root) + os.sep) == root / "coordinator"


# --- twin parity -----------------------------------------------------------


def _bin_twin():
    if _BIN_LIB not in sys.path:
        sys.path.insert(0, _BIN_LIB)
    import coordinator_data_root  # noqa: PLC0415

    return coordinator_data_root


def test_the_bin_twin_agrees_on_every_layout(tmp_path):
    """Both trees must answer identically — the bin/ CLIs cannot import
    coordinator_core, which is why a twin exists; drift between them is the
    failure mode that constraint creates."""
    twin = _bin_twin()
    cases = [
        _private_tree(tmp_path / "private"),
        _flat_mirror(tmp_path / "flat"),
        _flat_mirror(_private_tree(tmp_path / "both")),
    ]
    (tmp_path / "bare").mkdir()
    cases.append(tmp_path / "bare")

    for root in cases:
        assert twin.content_root_for(str(root)) == content_root_for(str(root)), root
    assert twin.content_root_for("") is content_root_for("") is None


def test_both_twins_gate_the_flat_arm_on_the_same_marker():
    assert _bin_twin().FLAT_CONTENT_ROOT_MARKER == FLAT_CONTENT_ROOT_MARKER
    assert FLAT_CONTENT_ROOT_MARKER == (".claude-plugin", "plugin.json")
