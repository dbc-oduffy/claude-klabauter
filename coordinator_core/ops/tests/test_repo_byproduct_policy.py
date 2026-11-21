"""Tests for the pure byproduct retention policy."""
from __future__ import annotations

from pathlib import Path

from coordinator_core.ops.repo_byproduct_policy import (
    CATEGORIES,
    Category,
    Unit,
    select_victims,
)

DAY = 86400.0
NOW = 1_000_000_000.0
_REPO = Path(__file__).resolve().parents[3]


def _cat(**kw) -> Category:
    base = dict(name="t", relpath="scratch", unit_glob="*", kind="transient",
                max_age_days=7, keep_newest=2, max_total_bytes=10**9,
                gitignore_line="scratch/")
    base.update(kw)
    return Category(**base)


def _u(name: str, age_days: float, size: int = 1) -> Unit:
    return Unit(name, NOW - age_days * DAY, size)


def test_all_older_than_cutoff_still_keeps_newest_n():
    units = [_u("a", 100), _u("b", 200), _u("c", 300), _u("d", 400)]
    victims = select_victims(units, _cat(keep_newest=2), NOW)
    assert {v.path for v in victims} == {"c", "d"}


def test_fresh_units_beyond_keep_survive():
    units = [_u("a", 1), _u("b", 2), _u("c", 3)]
    assert select_victims(units, _cat(keep_newest=1), NOW) == []


def test_old_unit_beyond_keep_goes():
    units = [_u("a", 1), _u("b", 30)]
    assert [v.path for v in select_victims(units, _cat(keep_newest=1), NOW)] == ["b"]


def test_size_cap_drops_older_fresh_units_but_not_kept_newest():
    units = [_u("a", 1, 80), _u("b", 2, 80), _u("c", 3, 10)]
    victims = select_victims(units, _cat(keep_newest=1, max_total_bytes=100), NOW)
    assert [v.path for v in victims] == ["b"]


def test_size_cap_keeps_what_fits_after_an_oversized_unit():
    units = [_u("a", 1, 10), _u("b", 2, 500), _u("c", 3, 10)]
    victims = select_victims(units, _cat(keep_newest=1, max_total_bytes=100), NOW)
    assert [v.path for v in victims] == ["b"]


def test_kept_newest_may_exceed_size_cap():
    units = [_u("a", 1, 10**6), _u("b", 2, 10**6)]
    victims = select_victims(units, _cat(keep_newest=2, max_total_bytes=1), NOW)
    assert victims == []


def test_input_order_irrelevant_and_input_unmutated():
    units = [_u("old", 50), _u("new", 1), _u("mid", 20)]
    snapshot = list(units)
    victims = select_victims(units, _cat(keep_newest=1), NOW)
    assert units == snapshot
    assert [v.path for v in victims] == ["mid", "old"]


def test_empty_units():
    assert select_victims([], _cat(), NOW) == []


def test_excluded_roots_absent_from_table():
    banned = (
        ".coordinator-local/coordinator-sessions",
        "state/plan-sidecars",
        "state/ledgers",
        "state/kill-ledger.md",
        "state/memo-outbox",
        ".project-rag",
    )
    for cat in CATEGORIES:
        for b in banned:
            assert not cat.relpath.startswith(b), (cat.name, b)
        assert "coordinator-sessions" not in cat.relpath


def test_table_shape():
    names = [c.name for c in CATEGORIES]
    assert len(names) == len(set(names))
    for c in CATEGORIES:
        assert c.kind in ("transient", "record")
        assert "\\" not in c.relpath and not c.relpath.startswith("/")
        assert not Path(c.relpath).is_absolute() and ":" not in c.relpath
        assert c.keep_newest >= 1 and c.max_age_days > 0 and c.max_total_bytes > 0
        if c.kind == "transient":
            assert c.max_age_days == 7


def test_every_gitignore_line_is_a_line_of_this_repo_gitignore():
    lines = {ln.strip() for ln in (_REPO / ".gitignore").read_text(encoding="utf-8").splitlines()}
    for c in CATEGORIES:
        assert c.gitignore_line in lines, (c.name, c.gitignore_line)
