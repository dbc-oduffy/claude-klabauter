from coordinator_core.ops.dispatch_emit.run_leftovers import run_touched_left


def test_left_is_touched_and_changed_minus_landing_and_contested():
    out = run_touched_left(
        changed=["b.py", "a.py", "c.py", "d.py", "x.py"],
        touched=frozenset({"a.py", "b.py", "c.py", "d.py"}),
        landing=["a.py"],
        contested={"c.py": ["s1"]},
    )
    assert out == ["b.py", "d.py"]


def test_untouched_dirt_is_not_a_leftover():
    assert run_touched_left(["peer.py"], frozenset({"mine.py"}), [], {}) == []


def test_fully_landed_is_empty():
    assert run_touched_left(["a.py"], frozenset({"a.py"}), ["a.py"], {}) == []


def test_backslash_paths_normalise():
    assert run_touched_left(["a\\b.py"], frozenset({"a/b.py"}), [], {}) == ["a/b.py"]
    assert run_touched_left(["a/b.py"], frozenset({"a/b.py"}), ["a\\b.py"], {}) == []
