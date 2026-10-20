"""Pure tests for run_credit: validate, record, read."""

import json

import pytest

from coordinator_core.ops.dispatch_emit import credit_gate
from coordinator_core.ops.dispatch_emit import run_credit as rc


def test_validate_normalises():
    out = rc.validate([{"chunk": "C1", "evidence": "  tests pass "}, {"chunk": "a.b-c_1", "evidence": "x"}])
    assert out == [{"chunk": "C1", "evidence": "tests pass"}, {"chunk": "a.b-c_1", "evidence": "x"}]


@pytest.mark.parametrize(
    "bad",
    [
        "nope",
        {"chunk": "C1", "evidence": "e"},
        ["C1"],
        [{"chunk": "", "evidence": "e"}],
        [{"chunk": "-C1", "evidence": "e"}],
        [{"chunk": "C 1", "evidence": "e"}],
        [{"chunk": 3, "evidence": "e"}],
        [{"chunk": "C1", "evidence": "  "}],
        [{"chunk": "C1"}],
        [{"chunk": sorted(credit_gate.BOOKKEEPING_CHUNK_IDS)[0], "evidence": "e"}],
        [{"chunk": f"C{i}", "evidence": "e"} for i in range(rc.MAX_ENTRIES + 1)],
    ],
)
def test_validate_refuses(bad):
    with pytest.raises(ValueError):
        rc.validate(bad)


def test_validate_names_first_bad_entry():
    with pytest.raises(ValueError, match=r"credit_chunks\[1\]"):
        rc.validate([{"chunk": "C1", "evidence": "e"}, {"chunk": "", "evidence": "e"}])


def test_evidence_capped():
    out = rc.validate([{"chunk": "C1", "evidence": "x" * (rc.EVIDENCE_CAP + 50)}])
    assert len(out[0]["evidence"]) == rc.EVIDENCE_CAP


def test_record_read_round_trip(tmp_path):
    d = tmp_path / "run"
    sink = rc.record(d, [{"chunk": "C1", "evidence": "e1"}], "2026-01-01T00:00:00Z")
    assert sink == d / rc.CREDIT_FILE
    assert json.loads(sink.read_text(encoding="utf-8").splitlines()[0]) == {
        "chunk": "C1",
        "evidence": "e1",
        "at": "2026-01-01T00:00:00Z",
    }
    assert rc.read([d]) == {"C1": "e1"}


def test_later_line_wins(tmp_path):
    rc.record(tmp_path, [{"chunk": "C1", "evidence": "old"}], "t1")
    rc.record(tmp_path, [{"chunk": "C1", "evidence": "new"}], "t2")
    assert rc.read([tmp_path]) == {"C1": "new"}


def test_later_dir_wins(tmp_path):
    d1, d2 = tmp_path / "one", tmp_path / "two"
    rc.record(d1, [{"chunk": "C1", "evidence": "r1"}, {"chunk": "C2", "evidence": "r1"}], "t")
    rc.record(d2, [{"chunk": "C2", "evidence": "r2"}], "t")
    assert rc.read([d1, d2]) == {"C1": "r1", "C2": "r2"}


SIZING = "state/sizings/2026-10-19-thing.yaml"


def _run(tmp_path, name, sizing=SIZING, credits=(("C1", "e"),)):
    d = tmp_path / rc.RECORD_DIR_ROOT / name
    d.mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({"sizing_rel": sizing}), encoding="utf-8")
    if credits:
        rc.record(d, [{"chunk": c, "evidence": e} for c, e in credits], "t")
    return d


def test_credits_for_sizing_newest_wins(tmp_path):
    _run(tmp_path, "ask-20261019T100000-aaaaaa", credits=(("C1", "old"), ("C2", "x")))
    _run(tmp_path, "ask-20261019T120000-bbbbbb", credits=(("C1", "new"),))
    assert rc.credits_for_sizing(tmp_path, SIZING) == {"C1": "new", "C2": "x"}


def test_credits_for_sizing_ignores_other_sizing(tmp_path):
    _run(tmp_path, "ask-20261019T100000-aaaaaa", sizing="state/sizings/2026-10-19-other.yaml")
    assert rc.credits_for_sizing(tmp_path, SIZING) == {}


def test_credits_for_sizing_ignores_dir_before_sizing_date(tmp_path):
    _run(tmp_path, "ask-20261018T230000-aaaaaa")
    assert rc.credits_for_sizing(tmp_path, SIZING) == {}


def test_credits_for_sizing_counts_dir_stamped_exactly_at_floor(tmp_path):
    _run(tmp_path, "ask-20261019T000000-aaaaaa", credits=(("C1", "at-floor"),))
    assert rc.credits_for_sizing(tmp_path, SIZING) == {"C1": "at-floor"}


def test_credits_for_sizing_counts_warp_dir_ordered_by_stamp(tmp_path):
    _run(tmp_path, "warp-2026-10-19-thing-20261019T130000-cccccc", credits=(("C1", "warp"),))
    _run(tmp_path, "ask-20261019T140000-dddddd", credits=(("C1", "ask-newer"),))
    assert rc.credits_for_sizing(tmp_path, SIZING) == {"C1": "ask-newer"}
    _run(tmp_path, "warp-2026-10-19-thing-20261019T150000-eeeeee", credits=(("C1", "warp-newest"),))
    assert rc.credits_for_sizing(tmp_path, SIZING) == {"C1": "warp-newest"}


def test_credits_for_sizing_undated_name_yields_empty(tmp_path):
    _run(tmp_path, "ask-20261019T100000-aaaaaa", sizing="state/sizings/undated.yaml")
    assert rc.credits_for_sizing(tmp_path, "state/sizings/undated.yaml") == {}


def test_credits_for_sizing_skips_malformed_line(tmp_path):
    d = _run(tmp_path, "ask-20261019T100000-aaaaaa", credits=())
    (d / rc.CREDIT_FILE).write_text('garbage\n{"chunk": "C1", "evidence": "ok"}\n', encoding="utf-8")
    assert rc.credits_for_sizing(tmp_path, SIZING) == {"C1": "ok"}


def test_credits_for_sizing_missing_root(tmp_path):
    assert rc.credits_for_sizing(tmp_path, SIZING) == {}


def test_read_never_raises(tmp_path):
    assert rc.read([tmp_path / "missing"]) == {}
    (tmp_path / rc.CREDIT_FILE).write_text(
        'not json\n[1]\n{"chunk": "ok", "evidence": "e"}\n{"chunk": 3, "evidence": "e"}\n{"chunk": "x"}\n',
        encoding="utf-8",
    )
    assert rc.read([tmp_path]) == {"ok": "e"}
    bad = tmp_path / "bin"
    bad.mkdir()
    (bad / rc.CREDIT_FILE).write_bytes(b"\xff\xfe\x00")
    assert rc.read([bad]) == {}
