
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
    assert round_module._not_carried_prose([]) == ""


def test_every_dropped_path_is_named(round_module):
    dropped = [("MODIFY", "lib/a.py"), ("MODIFY", "bin/b.py"), ("DELETE", "docs/c.md")]

    body = round_module._not_carried_prose(dropped)

    assert "Reported but not carried by this commit:" in body
    for path in ("lib/a.py", "bin/b.py", "docs/c.md"):
        assert path in body
    assert "and 0 more" not in body


def test_a_large_drop_names_the_cap_and_counts_the_rest(round_module):
    cap = round_module._NOT_CARRIED_NAME_CAP
    dropped = [("MODIFY", f"pkg/mod{i:03d}.py") for i in range(cap + 7)]

    body = round_module._not_carried_prose(dropped)

    assert body.count("\n  pkg/") == cap
    assert "... and 7 more" in body


def test_duplicate_change_lines_for_one_path_name_it_once(round_module):
    dropped = [("MODIFY", "lib/a.py"), ("DELETE", "lib/a.py")]

    assert round_module._not_carried_prose(dropped).count("lib/a.py") == 1


def test_identical_to_head_rewrites_are_not_reported_as_dropped(round_module):
    dropped = [("MODIFY", "bin/shim.py"), ("MODIFY", "lib/same.py"), ("MODIFY", "lib/lost.py")]
    unchanged = ["bin/shim.py", "lib/same.py"]

    body = round_module._not_carried_prose(dropped, unchanged)

    assert "lib/lost.py" in body
    assert "bin/shim.py" not in body
    assert "2 reported change(s) unchanged" in body


def test_subject_counts_only_real_drops(round_module):
    real_changes = [("MODIFY", "a.py"), ("MODIFY", "b.py"), ("MODIFY", "c.py")]

    all_unchanged = round_module._build_commit_subject(
        "row", real_changes, ["a.py"], unchanged_paths=["b.py", "c.py"], source_sha="0" * 12
    )
    one_lost = round_module._build_commit_subject(
        "row", real_changes, ["a.py"], unchanged_paths=["b.py"], source_sha="0" * 12
    )

    assert "not carried" not in all_unchanged
    assert "1 reported change(s) not carried" in one_lost
