"""plan_delivery: terminal status, open dispatchable rows, last coded ref, archived resolve."""

from __future__ import annotations

from coordinator_core.ops.baton_close_plan_delivery import PlanDeliveryCache, plan_delivery

SHA1 = "a" * 40
SHA2 = "b" * 40


def _row(rid, disposition="open", ref=None, deferred=False):
    s = f"- id: {rid}\n  title: t\n  disposition: {disposition}\n"
    if ref:
        s += f"  disposition_ref: {ref}\n"
    if deferred:
        s += "  deferred: true\n"
    return s


def _plan(status, rows):
    body = "".join(rows or [])
    spine = f"## Tasks\n\n```yaml plan-tasks\n{body}```\n" if rows is not None else "no spine\n"
    return f"---\ntitle: x\nstatus: {status}\n---\n\n{spine}"


def test_delivered_plan_takes_last_coded_ref():
    d = plan_delivery(_plan("implemented", [_row("C1", "coded", SHA1), _row("C2", "coded", SHA2)]))
    assert d.delivered and d.open_rows == () and d.ship_ref == SHA2


def test_open_row_blocks_delivery():
    d = plan_delivery(_plan("implemented", [_row("C1", "coded", SHA1), _row("C2")]))
    assert not d.delivered and d.open_rows == ("C2",)


def test_deferred_open_row_does_not_block():
    d = plan_delivery(_plan("implemented", [_row("C1", "coded", SHA1), _row("C2", deferred=True)]))
    assert d.delivered and d.open_rows == ()


def test_done_alias_folds_to_coded():
    d = plan_delivery(_plan("complete", [_row("C1", "done", SHA1)]))
    assert d.delivered and d.ship_ref == SHA1


def test_reviewed_plan_not_delivered():
    d = plan_delivery(_plan("reviewed", [_row("C1", "coded", SHA1)]))
    assert not d.delivered and d.status == "reviewed"


def test_malformed_spine_not_delivered():
    text = "---\nstatus: implemented\n---\n\n```yaml plan-tasks\n- id: [unclosed\n```\n"
    d = plan_delivery(text)
    assert not d.delivered


def test_absent_spine_implemented_delivered_without_ship_ref():
    d = plan_delivery(_plan("implemented", None))
    assert d.delivered and d.ship_ref is None


def test_non_sha_ref_ignored():
    d = plan_delivery(_plan("implemented", [_row("C1", "coded", "abc123")]))
    assert d.delivered and d.ship_ref is None


def test_cache_resolves_live_then_archive_and_none(tmp_path):
    live = tmp_path / "docs" / "plans"
    live.mkdir(parents=True)
    (live / "p.md").write_text(_plan("implemented", [_row("C1", "coded", SHA1)]), encoding="utf-8")
    old = tmp_path / "archive" / "specs" / "2026-09"
    new = tmp_path / "archive" / "specs" / "2026-10"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    (old / "q.md").write_text(_plan("reviewed", [_row("C1")]), encoding="utf-8")
    (new / "q.md").write_text(_plan("implemented", [_row("C1", "coded", SHA2)]), encoding="utf-8")
    cache = PlanDeliveryCache(str(tmp_path))
    assert cache.get("docs/plans/p.md").delivered
    arch = cache.get("docs/plans/q.md")
    assert arch.delivered and arch.ship_ref == SHA2
    assert cache.get("docs/plans/missing.md") is None
    assert cache.get("../escape.md") is None
    assert cache.get("docs/plans/p.md") is cache.get("docs/plans/p.md")
