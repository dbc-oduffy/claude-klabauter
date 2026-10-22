"""copy-currency-refresh pulls the engine mirror before keying the cache and records engine_origin."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from types import SimpleNamespace

_SPEC = importlib.util.spec_from_file_location(
    "copy_currency_refresh_under_test", Path(__file__).resolve().parents[1] / "copy-currency-refresh.py"
)
ccr = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(ccr)


class FakePo:
    _COPIES_CACHE_SCHEMA = 1

    def __init__(self, events, stamp="abc"):
        self.events = events
        self.stamp = stamp

    def _copies_mod(self, name):
        return SimpleNamespace(COPY="engine-mirror", PUBLISH_LAG_SYMBOL="sym")

    def copies_engine_key(self, ctx):
        self.events.append("key")
        return {"stamp": self.stamp}

    def resolve_plugin_pair(self, ctx):
        return None, None, "no plugin"


def _ctx(live_tree=False):
    return SimpleNamespace(live_tree=live_tree, authoring_root="/a", engine_root="/e")


def _result(**kw):
    base = dict(upstream="origin/main", reason="", ahead=0, behind=0, fast_forwarded=False, dirty=False)
    base.update(kw)
    return SimpleNamespace(**base)


def _lag(*_):
    return None


def test_pull_runs_before_key_and_records_fast_forward():
    events = []

    def ff(root):
        events.append("pull")
        return _result(fast_forwarded=True)

    entry = ccr.compute_cache(FakePo(events), _ctx(), publish_lag=_lag, import_origin=lambda: {}, fast_forward=ff)["engine-mirror"]
    assert events == ["pull", "key"]
    eo = entry["engine_origin"]
    assert eo["fast_forwarded"] is True and eo["behind"] == 0 and eo["upstream"] == "origin/main"
    assert "fast-forwarded" in ccr._summary({"engine-mirror": entry}, Path("x"))


def test_unfetched_behind_is_recorded():
    reason = "could not fetch origin (timeout)"
    entry = ccr._engine_entry(FakePo([]), _ctx(), _lag, 0.0, lambda: {}, lambda r: _result(behind=3, reason=reason))
    assert entry["engine_origin"]["fetched"] is False and entry["engine_origin"]["behind"] == 3


def test_raising_pull_records_error_and_cache_still_built():
    def ff(root):
        raise RuntimeError("boom")

    cache = ccr.compute_cache(FakePo([]), _ctx(), publish_lag=_lag, import_origin=lambda: {}, fast_forward=ff)
    entry = cache["engine-mirror"]
    assert "boom" in entry["engine_origin_error"] and "engine_origin" not in entry
    assert "key" in entry and entry["import_origin"] == {}


def test_unmeasured_result_is_error():
    entry = ccr._engine_entry(FakePo([]), _ctx(), _lag, 0.0, lambda: {}, lambda r: _result(upstream=None, reason="detached"))
    assert entry["engine_origin_error"] == "detached"


def test_live_tree_no_pull_but_keyed():
    def ff(root):
        raise AssertionError("must not pull")

    events = []
    entry = ccr._engine_entry(FakePo(events), _ctx(live_tree=True), _lag, 0.0, lambda: {}, ff)
    assert "key" in entry and "engine_origin" not in entry and "engine_origin_error" not in entry
    assert events == ["key"]


def test_dirty_result_is_recorded_as_dirty_not_error():
    entry = ccr._engine_entry(FakePo([]), _ctx(), _lag, 0.0, lambda: {}, lambda r: _result(behind=2, dirty=True, reason="1 modified"))
    assert entry["engine_origin"]["dirty"] is True and entry["engine_origin"]["behind"] == 2
    assert "engine_origin_error" not in entry


def test_unmeasured_after_failed_fetch_is_error_not_zero_counts():
    entry = ccr._engine_entry(
        FakePo([]), _ctx(), _lag, 0.0, lambda: {}, lambda r: _result(reason="could not measure against origin/main: bad (could not fetch origin (x))")
    )
    assert "engine_origin" not in entry and "could not measure" in entry["engine_origin_error"]


def test_import_origin_still_recorded():
    entry = ccr._engine_entry(FakePo([]), _ctx(), _lag, 0.0, lambda: {"package_dir": "p"}, lambda r: _result())
    assert entry["import_origin"] == {"package_dir": "p"}
