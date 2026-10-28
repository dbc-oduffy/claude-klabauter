"""Ratchet: the guard census and deny_registry agree, plane by plane."""

from __future__ import annotations

import pytest

from coordinator_core import deny_registry as dr
from coordinator_core import guard_census as gc


def test_reconcile_is_clean_over_the_present_planes():
    assert gc.reconcile() == []


def test_enumerators_see_real_guards():
    assert "destructive-rm" in gc.enumerate_bash_guards()
    assert gc.enumerate_write_guards()
    assert gc.enumerate_hooks()
    assert gc.op_refusal_sites()


def _with_plane(monkeypatch, plane, rows):
    real = gc.plane_rows
    monkeypatch.setattr(gc, "plane_rows", lambda p: list(rows) if p == plane else real(p))


def test_unfated_guard_is_a_problem(monkeypatch):
    _with_plane(monkeypatch, "bash_guards", [gc.Row("destructive-rm", gc.GUIDE)])
    assert any("unfated" in p for p in gc.reconcile())


def test_deleted_row_whose_module_survives_is_a_problem(monkeypatch):
    _with_plane(monkeypatch, "bash_guards", [gc.Row("destructive-rm", gc.DELETED)])
    assert any("deleted but the module is still present" in p for p in gc.reconcile())


def test_block_row_needs_a_redirect_and_a_registry_entry(monkeypatch):
    rows = [gc.Row("destructive-rm", gc.BLOCK), gc.Row("destructive-git-clean", gc.BLOCK, "x")]
    monkeypatch.delitem(dr.GUARD_DENY_CLASS, "destructive-git-clean")
    _with_plane(monkeypatch, "bash_guards", rows)
    problems = gc.reconcile()
    assert any("destructive-rm" in p and "no redirect" in p for p in problems)
    assert any("destructive-git-clean" in p and "not in the deny registry" in p for p in problems)


def test_bad_fate_is_a_problem(monkeypatch):
    _with_plane(monkeypatch, "hooks", [gc.Row("x", "maybe")])
    assert any("not block|guide|deleted" in p for p in gc.reconcile())


def test_registered_guard_without_a_block_row_is_a_problem(monkeypatch):
    # Registry coverage is enforced once every guard plane has a census module.
    real = gc.plane_rows
    monkeypatch.setattr(gc, "plane_rows", lambda p: [] if p in gc.PLANES else real(p))
    problems = gc.reconcile()
    assert any(p.startswith("registry:blanket-git-add") for p in problems)


@pytest.mark.parametrize("name", sorted(dr.GUARD_DENY_CLASS) + sorted(dr.SUBAGENT_DENY_CLASS))
def test_every_registry_entry_has_a_class_in_the_closed_list(name):
    assert dr.guard_deny_class(name, subagent=True) in dr.GUARD_CLASSES


def test_op_refusal_row_must_name_a_real_site_and_a_redirect(monkeypatch):
    _with_plane(monkeypatch, "op_refusals", [gc.Row("coordinator_core.ops.nope:GoneRefused", gc.BLOCK)])
    problems = gc.reconcile()
    assert any("names no op refusal site" in p for p in problems)
    assert any("must be a block with a redirect" in p for p in problems)
