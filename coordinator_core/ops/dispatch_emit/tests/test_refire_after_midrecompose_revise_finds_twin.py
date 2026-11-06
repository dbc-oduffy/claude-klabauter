"""A refire that names a staged twin finds it on disk after a revise recomposed the run's manifest.

The refire hands the launcher the twin path derived from the manifest handle it was given
(``nested_run_contract.staged_twin_rel``). A revise landing mid-recompose stages the changed manifest
under a new sha; the twin the pending refire names must not be unlinked by that staging.
"""

from __future__ import annotations

import hashlib

import pytest

from coordinator_core.ops.dispatch_emit import launcher_staged_twin as twin
from coordinator_core.ops.dispatch_emit import nested_run_contract as nrc

_MEMO = "2026-11-06-example-game-repo-em-engine-refire-staged-script-missing.md"
RUN = "run1"
REL = f"state/warp/{RUN}/manifest.json"
SCRIPT = f"export const meta = {{ name: 'x' }}\n{nrc.STAGED_INLINE_SLOT}\nreturn 1;\n"
CAP = 524288
RAW_BEFORE = b'{"rows":[{"id":"C1"}]}'
RAW_REVISED = b'{"rows":[{"id":"C1"},{"id":"C2"}]}'


def _stage(root, raw: bytes) -> str:
    handle = {"path": REL, "sha256": hashlib.sha256(raw).hexdigest(), "bytes": len(raw)}
    got = twin.ensure_twin(root, REL, raw, handle, max_bytes=CAP, refuse=ValueError)
    assert got == nrc.staged_twin_rel(RUN, handle["sha256"])
    return got


@pytest.fixture
def root(tmp_path):
    script = tmp_path / "scratch" / "warp" / f"{RUN}.workflow.mjs"
    script.parent.mkdir(parents=True)
    script.write_text(SCRIPT, encoding="utf-8")
    return tmp_path


def test_the_twin_a_pending_refire_names_survives_a_recompose_under_a_revise(root):
    named_by_refire = _stage(root, RAW_BEFORE)

    _stage(root, RAW_REVISED)

    assert (root / named_by_refire).is_file()


def test_the_twin_of_the_current_manifest_exists_after_the_recompose(root):
    _stage(root, RAW_BEFORE)

    current = _stage(root, RAW_REVISED)

    assert (root / current).is_file()


def test_twins_of_a_run_stay_bounded_across_many_recomposes(root):
    for n in range(8):
        _stage(root, b'{"rows":[{"id":"C%d"}]}' % n)

    left = list((root / "scratch" / "warp").glob(f"{RUN}.staged-*.workflow.mjs"))

    assert 1 <= len(left) <= 3
