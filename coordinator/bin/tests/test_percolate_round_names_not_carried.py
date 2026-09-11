"""The commit body names the paths a round reported and did not carry.

The subject has counted them for a while ("47 reported change(s) not carried"),
and on 2026-09-11 two sessions independently found that count unusable: it names
no members, the numbers run to 47 and 53 on quiet rounds, and a benign 47 — paths
rewritten with identical bytes — reads exactly like an alarming one. Both ended up
content-probing the mirror by hand, once per fix, to learn whether their own work
had shipped.

The stderr line already classifies the causes; this is membership, in the one
report that outlives the round.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_ROUND_PATH = Path(__file__).resolve().parent.parent / "percolate-round.py"


@pytest.fixture(scope="module")
def round_module():
    spec = importlib.util.spec_from_file_location("_percolate_round_probe", _ROUND_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_nothing_dropped_adds_no_body(round_module):
    """A clean round's message is byte-identical to what it was before."""
    assert round_module._not_carried_prose([]) == ""


def test_every_dropped_path_is_named(round_module):
    dropped = [("MODIFY", "lib/a.py"), ("MODIFY", "bin/b.py"), ("DELETE", "docs/c.md")]

    body = round_module._not_carried_prose(dropped)

    assert "Reported but not carried by this commit:" in body
    for path in ("lib/a.py", "bin/b.py", "docs/c.md"):
        assert path in body
    assert "and 0 more" not in body


def test_a_large_drop_names_the_cap_and_counts_the_rest(round_module):
    """A round can drop hundreds. Naming all of them is unreadable; naming none is
    the defect. The cap names what fits and states the remainder exactly."""
    cap = round_module._NOT_CARRIED_NAME_CAP
    dropped = [("MODIFY", f"pkg/mod{i:03d}.py") for i in range(cap + 7)]

    body = round_module._not_carried_prose(dropped)

    assert body.count("\n  pkg/") == cap
    assert "... and 7 more" in body


def test_duplicate_change_lines_for_one_path_name_it_once(round_module):
    """A path can carry more than one reported change line; membership is per path."""
    dropped = [("MODIFY", "lib/a.py"), ("DELETE", "lib/a.py")]

    assert round_module._not_carried_prose(dropped).count("lib/a.py") == 1
