from coordinator_core.ops.dispatch_emit import xs_diff_budget as m


def test_in_budget_is_none():
    assert m.verdict(m.parse_numstat("10\t5\ta.py\n8\t7\tb.py\n")) is None


def test_files_over():
    v = m.verdict([(1, 0, "a"), (1, 0, "b"), (1, 0, "c")])
    assert v == {"files": 3, "lines": 3, "budget_files": 2, "budget_lines": 30}


def test_lines_over():
    v = m.verdict(m.parse_numstat("20\t11\ta.py\n"))
    assert v["lines"] == 31 and v["files"] == 1


def test_boundary_exact_is_in_budget():
    assert m.verdict([(30, 0, "a"), (0, 0, "b")]) is None


def test_binary_row_counts_as_file():
    rows = m.parse_numstat("-\t-\timg.png\n1\t1\ta\n2\t2\tb\n")
    assert rows[0] == (0, 0, "img.png")
    assert m.verdict(rows)["files"] == 3


def test_empty_diff():
    assert m.parse_numstat("") == []
    assert m.verdict([]) is None


def test_product_rows_drops_bookkeeping_and_exempt_but_keeps_product():
    rows = [(1, 0, "a.py"), (9, 0, "state/x.md"), (9, 0, "scratch/s.mjs"), (3, 0, "docs/plan.md"),
            (2, 0, ".coordinator-local/r.md"), (1, 1, "pkg/b.py")]
    assert m.product_rows(rows, ["docs/plan.md"]) == [(1, 0, "a.py"), (1, 1, "pkg/b.py")]


def test_resize_next_action():
    v = m.verdict([(30, 1, "a")])
    s = m.resize_next_action({"files": 15, "lines": 621, "budget_files": 2, "budget_lines": 30}, "s.yaml")
    assert s == (
        "XS run diff is 15 files, 621 lines; XS budget is 2 files, 30 lines. "
        "Resize s.yaml to S or larger and refire."
    )
    assert v is not None
    assert "the sizing" in m.resize_next_action(v, None)
