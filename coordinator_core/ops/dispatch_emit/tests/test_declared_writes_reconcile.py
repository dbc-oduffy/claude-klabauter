from coordinator_core.ops.dispatch_emit.declared_writes_reconcile import reconcile


def test_exact_match_is_clean():
    assert reconcile(["a/x.md"], ["./a/x.md"]) == {
        "undelivered": [], "undeclared": [], "renamed": []}


def test_same_dir_single_pair_is_rename():
    r = reconcile(["runs/census.md"], ["runs/crosstab.md"])
    assert r == {"undelivered": [], "undeclared": [],
                 "renamed": [{"declared": "runs/census.md", "delivered": "runs/crosstab.md"}]}


def test_different_dir_not_paired():
    r = reconcile(["a/x.md"], ["b/y.md"])
    assert r == {"undelivered": ["a/x.md"], "undeclared": ["b/y.md"], "renamed": []}


def test_ambiguous_same_dir_not_paired():
    r = reconcile(["d/a.md", "d/b.md"], ["d/c.md"])
    assert r["renamed"] == []
    assert r["undelivered"] == ["d/a.md", "d/b.md"]
    assert r["undeclared"] == ["d/c.md"]


def test_backslash_normalised_and_extra_delivered():
    r = reconcile(["a\\x.md"], ["a/x.md", "a/z/q.md"])
    assert r == {"undelivered": [], "undeclared": ["a/z/q.md"], "renamed": []}
