"""rows_landed: fully-landed spines report coded rows and sha; anything else is None."""

from coordinator_core.roadmap.baton_rows_landed import rows_landed


def _plan(*rows):
    body = "\n".join(
        f"- id: {rid}\n  title: t\n  disposition: {disp}"
        + (f"\n  disposition_ref: {ref}" if ref else "")
        for rid, disp, ref in rows
    )
    return f"---\nstatus: executing\n---\n\n## Tasks\n\n```yaml plan-tasks\n{body}\n```\n"


def test_all_coded_returns_last_sha_and_ids():
    out = rows_landed(_plan(("C0", "coded", "aaa111"), ("C1", "coded", "bbb222")))
    assert out == {"coded_sha": "bbb222", "rows": ["C0", "C1"]}


def test_done_alias_counts_as_coded():
    out = rows_landed(_plan(("C0", "done", "aaa111")))
    assert out == {"coded_sha": "aaa111", "rows": ["C0"]}


def test_closed_non_coded_rows_alongside_coded_still_landed():
    out = rows_landed(_plan(("C0", "coded", "aaa111"), ("C1", "wont_do", None)))
    assert out == {"coded_sha": "aaa111", "rows": ["C0"]}


def test_open_row_is_not_landed():
    assert rows_landed(_plan(("C0", "coded", "aaa111"), ("C1", "open", None))) is None


def test_no_coded_row_is_not_landed():
    assert rows_landed(_plan(("C0", "wont_do", None))) is None


def test_coded_without_ref_has_none_sha():
    assert rows_landed(_plan(("C0", "coded", None))) == {"coded_sha": None, "rows": ["C0"]}


def test_no_spine_is_none():
    assert rows_landed("---\nstatus: x\n---\n\nno spine here\n") is None
