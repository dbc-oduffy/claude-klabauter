"""coordinator_core.tests.test_engine_stamp_predicate_pin

Pin test: THREE independent copies of "is this root a stamped engine build"
now exist on this box, each duplicated deliberately for the same reason
(the shared implementation is too expensive to import on that copy's own
hot path) --

  1. coordinator_core.warm.engine_root.is_engine_root -- the original,
     shared definition.
  2. coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py::_is_stamped_engine_root
     -- standalone, installed bare into <settings-home>/bin/.
  3. coordinator_core.ipc._is_dispatch_engine_stamped -- inline in the
     dispatch chokepoint (state/handoffs/2026-08-21_103635_reaching-the-
     warm-engine.md; import cost measured at +21ms, see that function's own
     docstring).

Three copies of a correctness predicate with nothing asserting they agree
is exactly the defect class this box hit twice today already (publish.py's
unscoped stamp write; _ENGINE_TOUCHING_PATHS needing a pin -- see
coordinator/bin/tests/test_publish_engine_stamp.py::
test_publisher_and_skew_agree_on_engine_touching_paths, the pattern this
test follows). A silent divergence here does not fail loudly: it lets one
copy classify a root as stamped while another refuses it, invisibly, since
each stays green on its own tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import coordinator_core.ipc as ipc
from coordinator_core.engine_root import _load_shim
from coordinator_core.warm.engine_root import is_engine_root


def _all_three_verdicts(monkeypatch, root: Path) -> tuple:
    monkeypatch.setattr(ipc, "_DISPATCH_ENGINE_ROOT", root)
    ipc._reset_engine_stamped_verdict_for_test()

    shim = _load_shim()
    return (
        is_engine_root(root),
        shim._is_stamped_engine_root(root),
        ipc._is_dispatch_engine_stamped(),
    )


@pytest.mark.parametrize(
    "make_stamp",
    [
        pytest.param(lambda p: p.write_text("sha:deadbeef\n", encoding="utf-8"), id="valid_stamp"),
        pytest.param(lambda p: p.write_text("", encoding="utf-8"), id="empty_stamp"),
        pytest.param(None, id="missing_stamp"),
    ],
)
def test_all_three_stamp_predicates_agree(monkeypatch, tmp_path, make_stamp):
    coordinator_core_dir = tmp_path / "coordinator_core"
    coordinator_core_dir.mkdir()
    stamp_path = coordinator_core_dir / "_engine_stamp"
    if make_stamp is not None:
        make_stamp(stamp_path)

    verdicts = _all_three_verdicts(monkeypatch, tmp_path)
    assert len(set(verdicts)) == 1, (
        f"the three stamp-predicate copies disagree for {make_stamp!r}: "
        f"warm.engine_root.is_engine_root={verdicts[0]!r}, "
        f"_resolve_claude_klabauter._is_stamped_engine_root={verdicts[1]!r}, "
        f"ipc._is_dispatch_engine_stamped={verdicts[2]!r}"
    )


def test_all_three_agree_root_has_no_coordinator_core_dir_at_all(monkeypatch, tmp_path):
    """Distinct from the empty/missing-file cases above: the CONTAINING
    directory itself is absent, not just the stamp file -- a broken or
    half-installed checkout."""
    verdicts = _all_three_verdicts(monkeypatch, tmp_path)
    assert len(set(verdicts)) == 1, (
        f"disagree with no coordinator_core/ dir at all: {verdicts!r}"
    )
