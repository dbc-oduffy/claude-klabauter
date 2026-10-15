"""Pins the research trail record contract (field set, refusals, determinism)."""

from __future__ import annotations

import json

import pytest

from coordinator_core.ops import _research_trail as rt


def _repo(**kw):
    base = dict(
        run_id="r-1", sizing="state/sizings/x.yaml", value_class="corpus", tier="corpus",
        destination="repo", paths=["docs/research/2026-10-15-x/a.md", "docs/research/2026-10-15-x/b.md"],
    )
    base.update(kw)
    return rt.record(**base)


def test_trail_rel():
    assert rt.trail_rel("r-1") == "docs/research/trail/r-1.yaml"


@pytest.mark.parametrize("bad", ["", "a/b", "..", "a b", "a\\b"])
def test_trail_rel_refuses_unsafe_id(bad):
    with pytest.raises(ValueError):
        rt.trail_rel(bad)


def test_repo_record_field_set_and_count():
    rec = _repo()
    assert list(rec) == ["run_id", "sizing", "value_class", "tier", "destination", "output_count", "paths"]
    assert rec["output_count"] == 2


def test_repo_ignores_caller_count():
    assert _repo(output_count=99)["output_count"] == 2


def test_bare_ask_sizing_null_and_spike_class_accepted():
    rec = _repo(sizing=None, value_class="spike", tier="scouts")
    assert rec["sizing"] is None and rec["value_class"] == "spike"
    assert "sizing: null" in rt.render(rec)


@pytest.mark.parametrize("bad", ["/abs/x.md", "Z:" + "/x.md", "a/../b.md", "a\\b.md", ""])
def test_refuses_bad_paths(bad):
    with pytest.raises(ValueError):
        _repo(paths=[bad])


def test_refuses_bad_sizing():
    with pytest.raises(ValueError):
        _repo(sizing="/abs/s.yaml")


def test_refuses_bad_destination_and_tier():
    with pytest.raises(ValueError):
        _repo(destination="elsewhere")
    with pytest.raises(ValueError):
        _repo(tier="nope")


def test_local_only_record_has_no_paths_and_no_absolute():
    rec = rt.record(
        run_id="r-2", sizing=None, value_class="deep", tier="deep",
        destination="local-only", paths=None, output_count=3,
    )
    assert "paths" not in rec and rec["output_count"] == 3
    text = rt.render(rec)
    assert "paths" not in text
    assert "/" not in text.replace("null", "")


def test_local_only_refuses_paths_and_missing_count():
    kw = dict(run_id="r-2", sizing=None, value_class="deep", tier="deep", destination="local-only")
    with pytest.raises(ValueError):
        rt.record(paths=["docs/x.md"], output_count=1, **kw)
    with pytest.raises(ValueError):
        rt.record(paths=None, **kw)


def test_render_deterministic_lf_and_valid_json_scalars():
    rec = _repo()
    a = rt.render(rec)
    assert a == rt.render(dict(reversed(list(rec.items()))))
    assert "\r" not in a and a.endswith("\n")
    assert a.splitlines()[0] == 'run_id: "r-1"'
    assert a.splitlines()[-1] == '  - ' + json.dumps(rec["paths"][-1])
