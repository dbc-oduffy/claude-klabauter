"""Pins run_ownership.owns_path on both sides of each verdict, including the release conditions."""

from coordinator_core.ops.dispatch_emit.run_ownership import (
    FOREIGN_CLAIM,
    FOREIGN_WITHHELD,
    OWN,
    RELEASED_CLAIM,
    LiveRun,
    blocks,
    holders_from_held,
    owns_path,
)


def test_holders_from_held_marks_held_path_foreign_withheld_and_others_own():
    runs = holders_from_held({"x/y.py": "b", "x/z.py": "b", "q.py": "c"})
    assert set(runs) == {"b", "c"}
    assert runs["b"].withheld == frozenset({"x/y.py", "x/z.py"})
    assert owns_path("a", "x/y.py", runs) == FOREIGN_WITHHELD
    assert owns_path("a", "other.py", runs) == OWN
    assert owns_path("b", "x/y.py", runs) == OWN


def test_holders_from_held_empty_is_empty():
    assert holders_from_held({}) == {}

P = frozenset({"x/y.py"})


def test_unclaimed_path_is_own():
    assert owns_path("a", "x/y.py", {"b": LiveRun(withheld=frozenset({"z.py"}))}) == OWN


def test_foreign_withheld_blocks():
    v = owns_path("a", "x/y.py", {"b": LiveRun(withheld=P)})
    assert v == FOREIGN_WITHHELD and blocks(v)


def test_foreign_claim_blocks():
    v = owns_path("a", "x/y.py", {"b": LiveRun(claims=P)})
    assert v == FOREIGN_CLAIM and blocks(v)


def test_halted_peer_on_different_plan_still_blocks():
    runs = {"a": LiveRun(plan="p1"), "b": LiveRun(plan="p2", claims=P, withheld=P)}
    v = owns_path("a", "x/y.py", runs)
    assert v == FOREIGN_WITHHELD and blocks(v)
    runs = {"a": LiveRun(plan="p1"), "b": LiveRun(plan="p2", claims=P)}
    assert owns_path("a", "x/y.py", runs) == FOREIGN_CLAIM


def test_own_superseded_predecessor_releases():
    runs = {"b": LiveRun(superseded_by="a", claims=P)}
    v = owns_path("a", "x/y.py", runs)
    assert v == RELEASED_CLAIM and not blocks(v)
    assert owns_path("c", "x/y.py", runs) == FOREIGN_CLAIM


def test_same_plan_sibling_releases():
    runs = {"a": LiveRun(plan="p1"), "b": LiveRun(plan="p1", claims=P)}
    assert owns_path("a", "x/y.py", runs) == RELEASED_CLAIM


def test_empty_plan_never_matches():
    runs = {"a": LiveRun(), "b": LiveRun(claims=P)}
    assert owns_path("a", "x/y.py", runs) == FOREIGN_CLAIM


def test_landed_successor_releases_withheld_too():
    runs = {"b": LiveRun(plan="p2", successor_landed=True, withheld=P)}
    v = owns_path("a", "x/y.py", runs, run_plan="p1")
    assert v == RELEASED_CLAIM and not blocks(v)


def test_own_run_entries_never_foreign():
    runs = {"a": LiveRun(withheld=P, claims=P)}
    assert owns_path("a", "x/y.py", runs) == OWN


def test_unreleased_block_outranks_released_claim():
    runs = {
        "h": LiveRun(superseded_by="a", claims=frozenset({"p"})),
        "b": LiveRun(claims=frozenset({"p"})),
    }
    assert owns_path("a", "p", runs) == FOREIGN_CLAIM


def test_withheld_outranks_claim():
    runs = {"c": LiveRun(claims=frozenset({"p"})), "b": LiveRun(withheld=frozenset({"p"}))}
    assert owns_path("a", "p", runs) == FOREIGN_WITHHELD


def test_directory_entry_covers_children_not_siblings():
    runs = {"b": LiveRun(claims=frozenset({"d/"}))}
    assert owns_path("a", "d/f.py", runs) == FOREIGN_CLAIM
    assert owns_path("a", "dd/f.py", runs) == OWN


def test_path_separators_normalised():
    runs = {"b": LiveRun(claims=frozenset({"d/f.py"}))}
    assert owns_path("a", ".\\d\\f.py", runs) == FOREIGN_CLAIM


def test_covers_any_matches_the_pairwise_cover_on_both_sides():
    from coordinator_core.ops.dispatch_emit import run_ownership as ro

    entries = ["a/b/", ".\\c\\d.py", "e", "", "/", "g//h"]
    paths = ["a/b", "a/b/x/y.py", "a/bb/x", "a", "c/d.py", "c/d.py/z", "c/d", "e/f", "ee", "g//h/i", "g/h", "", "x/"]
    for path in paths:
        assert ro.covers_any(entries, [path]) == any(ro._covers(e, path) for e in entries), path
        assert ro.covers_any(frozenset(entries), [path]) == any(ro._covers(e, path) for e in entries), path
    assert ro.covers_any(entries, paths) is True
    assert ro.covers_any(entries, ["zz", "a/bb"]) is False
    assert ro.covers_any([], paths) is False
    assert ro.covers_any(["", "/"], paths) is False


def test_owns_path_over_many_entries_and_paths_agrees_with_pairwise_cover():
    from coordinator_core.ops.dispatch_emit import run_ownership as ro

    withheld = frozenset(f"d{i}/f{j}.py" for i in range(20) for j in range(4)) | {"dir/"}
    runs = {"b": LiveRun(withheld=withheld)}
    for path in ("d3/f2.py", "d3/f9.py", "dir/x/y.py", "dirx/y.py", "d3"):
        want = FOREIGN_WITHHELD if any(ro._covers(w, path) for w in withheld) else OWN
        assert owns_path("a", path, runs) == want, path


def test_foreign_withheld_holders_matches_owns_path_per_run():
    from coordinator_core.ops.dispatch_emit import run_ownership as ro

    live = {
        "a": LiveRun(withheld=frozenset({"x/own.py"})),
        "b": LiveRun(withheld=frozenset({"x/", "y/f.py"})),
        "c": LiveRun(withheld=frozenset({"y/f.py"}), successor_landed=True),
        "d": LiveRun(withheld=frozenset({"x/q.py"}), superseded_by="a"),
        "e": LiveRun(withheld=frozenset({"x/q.py", "z"}), claims=frozenset({"w.py"})),
        "f": LiveRun(),
    }
    paths = ["x/own.py", "x/q.py", "y/f.py", ".\\y\\f.py", "z/k", "w.py", "none.py", ""]
    got = ro.foreign_withheld_holders("a", paths, live)
    for path in paths:
        want = sorted(rid for rid in live if owns_path("a", path, {rid: live[rid]}) == FOREIGN_WITHHELD)
        assert got.get(path, []) == want, path
    assert got["x/q.py"] == ["b", "e"] and "w.py" not in got
