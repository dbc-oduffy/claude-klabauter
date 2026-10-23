"""publish.py's duplicated round-derived names stay equal to percolate.round_derived's.

Run: python -m pytest coordinator/bin/tests/test_publish_round_derived_pinned.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_publish_stamp_names_the_copied_tree as base  # noqa: E402

publish = base.publish


def _round_derived():
    base_lib = Path(__file__).resolve().parents[2] / "lib"
    if str(base_lib) not in sys.path:
        sys.path.insert(0, str(base_lib))
    from percolate import round_derived

    return round_derived


def test_expected_manifest_rel_pinned():
    rd = _round_derived()
    assert publish._EXPECTED_MANIFEST_REL == rd.EXPECTED_MANIFEST_REL, (
        "coordinator/bin/publish.py _EXPECTED_MANIFEST_REL diverged from "
        "coordinator/lib/percolate/round_derived.py EXPECTED_MANIFEST_REL"
    )


def test_round_derived_basenames_pinned():
    rd = _round_derived()
    assert publish._ROUND_DERIVED_BASENAMES == rd.REGENERATED_BASENAMES, (
        "coordinator/bin/publish.py _ROUND_DERIVED_BASENAMES diverged from "
        "coordinator/lib/percolate/round_derived.py REGENERATED_BASENAMES"
    )
