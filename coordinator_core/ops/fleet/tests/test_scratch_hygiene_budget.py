"""fleet.scratch_hygiene is bounded by time: past the budget an entry is reported, not walked."""

from __future__ import annotations

import json
import os
import time

from coordinator_core.ops.fleet import scratch_hygiene as op
from coordinator_core.ops.fleet import scratch_hygiene_plan as plan_module
from coordinator_core.ops.fleet.scratch_hygiene_plan import classify_entry


def _old_tree(root, name, files=3):
    d = root / "scratch" / name
    d.mkdir(parents=True)
    old = time.time() - 30 * 86400
    for i in range(files):
        f = d / f"f{i}.txt"
        f.write_text("x", encoding="utf-8")
        os.utime(f, (old, old))
    os.utime(d, (old, old))
    return d


def test_entry_past_the_deadline_is_reported_unwalked(tmp_path):
    entry = _old_tree(tmp_path, "a")
    rec = classify_entry(entry, repo_root=tmp_path, deadline=time.monotonic() - 1)
    assert (rec["action"], rec["reason"], rec["files"]) == ("skipped-budget", "budget-exhausted", 0)
    assert entry.exists()


def test_zero_budget_apply_deletes_nothing_and_exits_one(tmp_path):
    entry = _old_tree(tmp_path, "a")
    out = op.run_hygiene({"repo_root": str(tmp_path), "cadence": "distill", "apply": True, "budget_secs": 0})
    assert [r["action"] for r in out["records"] if r.get("kind") == "purge" and r["path"] == "scratch/a"] == ["skipped-budget"]
    assert out["contract_exit"] == 1
    assert entry.exists()


def test_apply_within_budget_deletes_the_gated_entry(tmp_path):
    entry = _old_tree(tmp_path, "a")
    out = op.run_hygiene({"repo_root": str(tmp_path), "cadence": "distill", "apply": True, "budget_secs": 30})
    assert [r["action"] for r in out["records"] if r.get("kind") == "purge" and r["path"] == "scratch/a"] == ["deleted"]
    assert not entry.exists()


def test_negative_budget_is_a_bad_argument(tmp_path):
    out = op.run_hygiene({"repo_root": str(tmp_path), "cadence": "distill", "budget_secs": -1})
    assert out["exit_code"] == 2


def _limited(monkeypatch, per_call):
    """Make each run_hygiene call gate only ``per_call`` entries; the rest report skipped-budget."""
    real = plan_module.classify_entry
    seen: list[str] = []

    def limited(entry, **kw):
        if len(seen) >= per_call:
            kw["deadline"] = time.monotonic() - 1
        else:
            seen.append(os.path.basename(entry))
        return real(entry, **kw)

    monkeypatch.setattr(op, "classify_entry", limited)
    monkeypatch.setattr(plan_module, "classify_entry", limited)
    return seen


def _fixture(tmp_path, kept=5, old=15):
    for i in range(kept):
        d = tmp_path / "scratch" / f"a{i:02d}-kept"
        d.mkdir(parents=True)
        (d / "f.txt").write_text("x", encoding="utf-8")
    for i in range(old):
        _old_tree(tmp_path, f"b{i:02d}-old", files=1)


def _call(tmp_path, apply):
    return op.run_hygiene({"repo_root": str(tmp_path), "cadence": "distill", "apply": apply, "budget_secs": 30})


def test_successive_apply_calls_drain_past_kept_leading_entries(tmp_path, monkeypatch):
    _fixture(tmp_path)
    seen = _limited(monkeypatch, 4)
    walked_per_call = []
    for _ in range(8):
        seen.clear()
        out = _call(tmp_path, True)
        walked_per_call.append(list(seen))
        if not any((tmp_path / "scratch").glob("b*-old")):
            break
    assert not list((tmp_path / "scratch").glob("b*-old"))
    assert len(list((tmp_path / "scratch").glob("a*-kept"))) == 5
    flat = [n for call in walked_per_call for n in call]
    assert len(flat) == len(set(flat)) == 20


def test_no_entry_is_walked_twice_before_all_are_walked_once(tmp_path, monkeypatch):
    _fixture(tmp_path, old=0)
    seen = _limited(monkeypatch, 2)
    order = []
    for _ in range(3):
        seen.clear()
        _call(tmp_path, True)
        order.extend(seen)
    assert order == [f"a{i:02d}-kept" for i in range(5)] + ["a00-kept"]


def test_cursor_names_an_entry_so_deletions_do_not_shift_the_resume_point(tmp_path, monkeypatch):
    _fixture(tmp_path, kept=0, old=6)
    seen = _limited(monkeypatch, 3)
    _call(tmp_path, True)
    assert seen == ["b00-old", "b01-old", "b02-old"]
    seen.clear()
    _call(tmp_path, True)
    assert seen == ["b03-old", "b04-old", "b05-old"]
    assert not list((tmp_path / "scratch").glob("b*-old"))


def test_missing_or_garbled_cursor_starts_from_the_top(tmp_path, monkeypatch):
    _fixture(tmp_path, kept=3, old=0)
    seen = _limited(monkeypatch, 1)
    (tmp_path / "scratch" / ".scratch-hygiene-cursor").write_text("{not json", encoding="utf-8")
    _call(tmp_path, True)
    assert seen == ["a00-kept"]


def test_dry_run_advances_the_cursor_and_deletes_nothing(tmp_path, monkeypatch):
    _fixture(tmp_path, kept=2, old=4)
    seen = _limited(monkeypatch, 4)
    dry = _call(tmp_path, False)
    assert (tmp_path / "scratch" / ".scratch-hygiene-cursor").exists()
    assert seen == ["a00-kept", "a01-kept", "b00-old", "b01-old"]
    assert dry["walk"] == {"eligible": 2, "walked": 4, "skipped_budget": 2}
    assert len(list((tmp_path / "scratch").glob("b*-old"))) == 4
    seen.clear()
    wet = _call(tmp_path, True)
    assert seen == ["b02-old", "b03-old", "a00-kept", "a01-kept"]
    assert wet["walk"]["eligible"] == wet["walk"]["deleted"] == 2
    assert wet["walk"]["recheck_kept"] == 0


def _default_call(tmp_path, apply):
    return op.run_hygiene({"repo_root": str(tmp_path), "cadence": "distill", "apply": apply})


def test_default_call_walks_a_slow_first_entry_and_moves_the_cursor(tmp_path, monkeypatch):
    for apply in (False, True):
        root = tmp_path / str(apply)
        root.mkdir()
        _fixture(root, kept=0, old=3)
        real = plan_module._walk
        monkeypatch.setattr(op, "DEFAULT_BUDGET_SECS", 0.01)

        def slow(top, cutoff, deadline=None, _real=real):
            time.sleep(0.05)
            return _real(top, cutoff, deadline)

        monkeypatch.setattr(plan_module, "_walk", slow)
        out = _default_call(root, apply)
        assert out["walk"]["walked"] >= 1
        assert out["walk"]["skipped_budget"] == 2
        assert json.loads((root / "scratch" / ".scratch-hygiene-cursor").read_text(encoding="utf-8"))["after"] == str(
            root / "scratch" / "b00-old"
        )
        assert (root / "scratch" / "b00-old").exists() is (not apply)


def test_default_calls_cover_the_whole_backlog_and_wrap(tmp_path, monkeypatch):
    _fixture(tmp_path, kept=4, old=0)
    real = plan_module._walk
    order: list[str] = []

    def slow(top, cutoff, deadline=None):
        order.append(os.path.basename(top))
        time.sleep(0.05)
        return real(top, cutoff, deadline)

    monkeypatch.setattr(op, "DEFAULT_BUDGET_SECS", 0.01)
    monkeypatch.setattr(plan_module, "_walk", slow)
    for _ in range(5):
        _default_call(tmp_path, False)
    assert order == [f"a{i:02d}-kept" for i in range(4)] + ["a00-kept"]


def test_an_entry_cut_off_by_the_deadline_is_never_deleted(tmp_path, monkeypatch):
    _fixture(tmp_path, kept=0, old=3)
    real = plan_module._walk

    def cut(top, cutoff, deadline=None):
        w = real(top, cutoff, deadline)
        if deadline is not None:
            w.out_of_time = True
        return w

    monkeypatch.setattr(plan_module, "_walk", cut)
    out = _default_call(tmp_path, True)
    assert out["walk"]["deleted"] == 1
    assert [p.name for p in (tmp_path / "scratch").glob("b*-old")] == ["b01-old", "b02-old"]


def test_progressing_budget_deferred_call_exits_zero_and_other_skips_still_exit_one(tmp_path, monkeypatch):
    _fixture(tmp_path, kept=0, old=4)
    seen = _limited(monkeypatch, 2)
    for apply in (False, True):
        seen.clear()
        out = _call(tmp_path, apply)
        assert out["walk"]["walked"] >= 1 and out["walk"]["skipped_budget"] >= 1
        assert any(r.get("action") == "skipped-budget" for r in out["records"])
        assert out["contract_exit"] == 0
    # A progressing call with a non-budget skip still exits 1.
    real = plan_module.classify_entry

    def active(entry, **kw):
        rec = real(entry, **kw)
        rec["action"], rec["reason"] = "skipped-active", "active"
        return rec

    monkeypatch.setattr(op, "classify_entry", active)
    monkeypatch.setattr(plan_module, "classify_entry", active)
    seen.clear()
    out = _call(tmp_path, False)
    assert out["walk"]["walked"] >= 1
    assert out["contract_exit"] == 1


def test_default_call_on_a_large_backlog_with_slow_first_entry_exits_zero(tmp_path, monkeypatch):
    _fixture(tmp_path, kept=0, old=60)
    real = plan_module._walk
    monkeypatch.setattr(op, "DEFAULT_BUDGET_SECS", 0.01)

    def slow(top, cutoff, deadline=None):
        time.sleep(0.05)
        return real(top, cutoff, deadline)

    monkeypatch.setattr(plan_module, "_walk", slow)
    out = _default_call(tmp_path, False)
    assert out["walk"]["walked"] >= 1
    assert out["walk"]["skipped_budget"] > 0
    assert out["contract_exit"] == 0


def test_cursor_file_is_never_a_purge_candidate(tmp_path):
    _fixture(tmp_path, kept=0, old=1)
    _call(tmp_path, True)
    cursor = tmp_path / "scratch" / ".scratch-hygiene-cursor"
    assert cursor.exists()
    old = time.time() - 30 * 86400
    os.utime(cursor, (old, old))
    out = _call(tmp_path, True)
    assert cursor.exists()
    assert all(r["path"] != "scratch/.scratch-hygiene-cursor" for r in out["records"] if r.get("kind") == "purge")


def test_a_top_level_file_entry_is_deleted(tmp_path):
    (tmp_path / "scratch").mkdir()
    f = tmp_path / "scratch" / "probe.txt"
    f.write_text("x", encoding="utf-8")
    old = time.time() - 30 * 86400
    os.utime(f, (old, old))
    out = op.run_hygiene({"repo_root": str(tmp_path), "cadence": "distill", "apply": True, "budget_secs": 30})
    assert [r["action"] for r in out["records"] if r.get("kind") == "purge"] == ["deleted"]
    assert not f.exists()
