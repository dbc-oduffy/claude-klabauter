"""A delivery that lives only in `prep.whole_diff_sidecars.delivery` is read through one
loader by the resolver, `prior_unbacked_claims` and `review_stamp.mint`'s fallback."""

from __future__ import annotations

import yaml

from coordinator_core.ops.dispatch_emit import reverify_delivery as rd

_FAIL = {"verdict": "FAIL", "unbacked": [{"claim": "c", "anchor": "a"}]}


def _write(path, fm):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml.safe_dump(fm)}---\nbody\n", encoding="utf-8")
    return path


def _sidecar_only_record(tmp_path, *, inline_prep):
    sidecar = "sidecars/delivery.md"
    _write(tmp_path / sidecar, _FAIL)
    prep = {"whole_diff_sidecars": {"delivery": sidecar}}
    fm = {"plan_id": "pln-x", "commit_range": {"head": "h"}}
    if inline_prep:
        fm["prep"] = prep
    else:
        fm["prep_sidecar"] = "sidecars/prep.md"
        _write(tmp_path / "sidecars/prep.md", prep)
    return _write(tmp_path / "records/run.md", fm)


def test_load_delivery_falls_back_to_the_prep_sidecar_named_delivery(tmp_path):
    for inline in (True, False):
        record = rd._frontmatter(_sidecar_only_record(tmp_path, inline_prep=inline))
        assert rd.load_delivery(tmp_path, record) == _FAIL
        assert rd.load_delivery(None, record) is None


def test_frontmatter_delivery_outranks_the_sidecar(tmp_path):
    record = rd._frontmatter(_sidecar_only_record(tmp_path, inline_prep=True))
    record["delivery"] = {"verdict": "PASS"}
    assert rd.load_delivery(tmp_path, record) == {"verdict": "PASS"}


def test_prior_unbacked_claims_reads_the_sidecar_delivery(tmp_path):
    path = _sidecar_only_record(tmp_path, inline_prep=True)
    _, claims = rd.prior_unbacked_claims(path, tmp_path)
    assert claims == [{"claim": "c", "anchor": "a"}]


def test_resolver_picks_a_record_whose_delivery_lives_only_in_the_sidecar(tmp_path):
    path = _sidecar_only_record(tmp_path, inline_prep=True)
    found, block = rd.resolve_delivery_in_force(tmp_path, None, None, str(path))
    assert found == path
    assert block == _FAIL
    assert rd._has_reverifiable(tmp_path, rd._frontmatter(path))
