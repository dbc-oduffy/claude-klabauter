"""own_landing_set keeps declared, prefixed and artifact paths and reports the rest."""
from coordinator_core.ops.dispatch_emit.landing_scope import FOREIGN_EXCLUDED_KEY, own_landing_set


def _split(admitted, declared=(), prefixes=(), artifacts=()):
    return own_landing_set(
        admitted, declared=declared, declared_prefixes=prefixes, own_artifacts=artifacts
    )


def test_key_name():
    assert FOREIGN_EXCLUDED_KEY == "foreign_excluded"


def test_declared_path_kept():
    assert _split(["a.py", "b.py"], declared=["a.py"]) == (["a.py"], ["b.py"])


def test_own_artifact_kept():
    assert _split(["state/x.json"], artifacts=["state/x.json"]) == (["state/x.json"], [])


def test_prefix_file_kept():
    assert _split(["src/foo/x.py"], prefixes=["src/foo"]) == (["src/foo/x.py"], [])
    assert _split(["src/foo/x.py"], prefixes=["src/foo/"]) == (["src/foo/x.py"], [])


def test_prefix_is_segment_bounded():
    assert _split(["src/foobar/x.py"], prefixes=["src/foo"]) == ([], ["src/foobar/x.py"])


def test_backslash_and_dot_slash_normalised():
    kept, foreign = _split(["src\\a.py", "./b.py", "c.py"], declared=["src/a.py", "b.py"])
    assert kept == ["src\\a.py", "./b.py"]
    assert foreign == ["c.py"]


def test_order_preserved_and_foreign_sorted_deduped():
    kept, foreign = _split(["z", "k", "b", "k", "a", "b"], declared=["z", "a"])
    assert kept == ["z", "a"]
    assert foreign == ["b", "k"]


def test_empty_admitted():
    assert _split([], declared=["a"]) == ([], [])


def test_nothing_declared_all_foreign():
    assert _split(["a"]) == ([], ["a"])
