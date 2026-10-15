"""Tests for the M arm spine mint."""

from __future__ import annotations

import copy

import pytest

from coordinator_core.ops.dispatch_emit.sizing_m_mint import concrete_acceptance, mint_m_spine
from coordinator_core.ops.dispatch_emit.spine_read import read_spine

SIZING_REL = "state/sizings/2026-10-01-m-thing.yaml"


def _rows() -> list[dict]:
    return [
        {"statement": "A parses", "writes": ["pkg/a.py", "pkg/tests/test_a.py"], "check": "pytest pkg/tests/test_a.py -q"},
        {"statement": "B wires A", "writes": ["pkg/b.py"], "check": "pytest pkg/tests/test_b.py -q"},
        {"statement": "C documents", "writes": ["docs/c.md"], "check": "grep -q wired docs/c.md"},
    ]


def _sizing(rows=None) -> dict:
    return {
        "deliverable_id": "dlv-m-thing-abc123",
        "intent": "Do the M thing",
        "exit_criterion": {"statement": "the M thing works"},
        "acceptance_rows": _rows() if rows is None else rows,
    }


def test_concrete_three_rows_mint_three_rows_with_writes_and_check(tmp_path):
    sizing = _sizing()
    rows = concrete_acceptance(sizing)
    assert rows == _rows()
    text, path = mint_m_spine(sizing, sizing_rel=SIZING_REL, rows=rows)
    assert path.as_posix() == "docs/plans/2026-10-01-m-thing.md"
    out = tmp_path / "p.md"
    out.write_text(text, encoding="utf-8", newline="\n")
    spine = read_spine(out)
    assert [r.id for r in spine] == ["M1", "M2", "M3"]
    assert [list(r.writes) for r in spine] == [r["writes"] for r in _rows()]
    for r, acc in zip(spine, _rows()):
        assert acc["statement"] in r.body
        assert f"DONE only when this holds): {acc['check']}" in r.body
        assert SIZING_REL in r.body
    assert "the M thing works" in text


@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r[0].pop("check"),
        lambda r: r[0].update(check="  "),
        lambda r: r[0].update(statement=""),
        lambda r: r[0].update(writes=[]),
        lambda r: r[0].pop("writes"),
        lambda r: r[1].update(writes=["pkg/*.py"]),
        lambda r: r[1].update(writes=["pkg/"]),
        lambda r: r.append("not a mapping"),
    ],
)
def test_any_non_concrete_row_returns_none(mutate):
    rows = copy.deepcopy(_rows())
    mutate(rows)
    assert concrete_acceptance(_sizing(rows)) is None


def test_empty_list_and_absent_key_return_none():
    assert concrete_acceptance(_sizing([])) is None
    sizing = _sizing()
    del sizing["acceptance_rows"]
    assert concrete_acceptance(sizing) is None
    assert concrete_acceptance({"acceptance": _rows()}) is None


def test_mint_refuses_glob_and_empty_rows():
    from coordinator_core.ops.dispatch_emit.inventory_mint import GlobFootprintError

    bad = _rows()
    bad[0]["writes"] = ["pkg/*.py"]
    with pytest.raises((GlobFootprintError, ValueError)):
        mint_m_spine(_sizing(bad), sizing_rel=SIZING_REL, rows=bad)
    with pytest.raises(ValueError):
        mint_m_spine(_sizing(), sizing_rel=SIZING_REL, rows=[])


def test_plan_body_beside_code_routes_to_sidecar():
    rows = [{"statement": "s", "writes": ["src/x.py", "docs/plans/2026-10-14-p.md"], "check": "c"}]
    text, _ = mint_m_spine(_sizing(rows), sizing_rel=SIZING_REL, rows=rows)
    assert "docs/plans/2026-10-14-p.evidence.yaml" in text


def test_repo_root_refuses_an_existing_directory_write(tmp_path):
    import pytest
    from coordinator_core.ops.dispatch_emit.inventory_mint import DirectoryShapedFootprintError
    from coordinator_core.ops.dispatch_emit.sizing_m_mint import mint_m_spine

    (tmp_path / "pkg").mkdir()
    rows = [{"statement": "s", "writes": ["pkg"], "check": "true"}]
    with pytest.raises(DirectoryShapedFootprintError):
        mint_m_spine({}, sizing_rel="state/sizings/x.yaml", rows=rows, repo_root=tmp_path)
    mint_m_spine({}, sizing_rel="state/sizings/x.yaml", rows=rows)


def test_spine_mismatch_names_changed_rows_and_flags_unreadable():
    from coordinator_core.ops.dispatch_emit.sizing_m_mint import mint_m_spine, spine_mismatch

    mk = lambda c: mint_m_spine({}, sizing_rel="state/sizings/x.yaml", rows=[{"statement": "s", "writes": ["a.py"], "check": c}])[0]
    assert spine_mismatch(mk("true"), mk("true")) == []
    assert spine_mismatch(mk("true"), mk("false")) == ["M1"]
    assert spine_mismatch("prose only", mk("true")) == ["<unparseable>"]
