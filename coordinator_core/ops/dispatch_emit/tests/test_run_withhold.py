"""Pure tests for run_withhold: validate, record, read."""

import json

import pytest

from coordinator_core.ops.dispatch_emit import run_withhold as rw


def test_validate_normalises():
    out = rw.validate([{"path": ".\\a\\b.py", "reason": "  peer hunks "}, {"path": "./c.py", "reason": "x"}])
    assert out == [{"path": "a/b.py", "reason": "peer hunks"}, {"path": "c.py", "reason": "x"}]


@pytest.mark.parametrize(
    "bad",
    [
        "nope",
        {"path": "a", "reason": "r"},
        ["a"],
        [{"path": "", "reason": "r"}],
        [{"path": "/abs/a.py", "reason": "r"}],
        [{"path": "C:/a.py", "reason": "r"}],  # abs-path-ok: refusal fixture
        [{"path": "../a.py", "reason": "r"}],
        [{"path": "a/../../b", "reason": "r"}],
        [{"path": "a.py", "reason": "  "}],
        [{"path": "a.py"}],
        [{"path": "state/warp/x/run.json", "reason": "r"}],
        [{"path": f"f{i}.py", "reason": "r"} for i in range(rw.MAX_ENTRIES + 1)],
    ],
)
def test_validate_refuses(bad):
    with pytest.raises(ValueError):
        rw.validate(bad)


def test_validate_names_first_bad_entry():
    with pytest.raises(ValueError, match=r"withhold_paths\[1\]"):
        rw.validate([{"path": "a", "reason": "r"}, {"path": "..", "reason": "r"}])


def test_reason_capped():
    out = rw.validate([{"path": "a", "reason": "x" * (rw.REASON_CAP + 50)}])
    assert len(out[0]["reason"]) == rw.REASON_CAP


def test_record_read_round_trip(tmp_path):
    d = tmp_path / "run"
    sink = rw.record(d, [{"path": "a.py", "reason": "r1"}], "2026-01-01T00:00:00Z")
    assert sink == d / rw.WITHHOLD_FILE
    assert json.loads(sink.read_text().splitlines()[0]) == {"path": "a.py", "reason": "r1", "at": "2026-01-01T00:00:00Z"}
    assert rw.read([d]) == {"a.py": "r1"}


def test_later_line_wins(tmp_path):
    rw.record(tmp_path, [{"path": "a.py", "reason": "old"}], "t1")
    rw.record(tmp_path, [{"path": "a.py", "reason": "new"}], "t2")
    assert rw.read([tmp_path]) == {"a.py": "new"}


def test_read_merges_dirs(tmp_path):
    d1, d2 = tmp_path / "one", tmp_path / "two"
    rw.record(d1, [{"path": "a", "reason": "r1"}, {"path": "b", "reason": "r1"}], "t")
    rw.record(d2, [{"path": "b", "reason": "r2"}], "t")
    assert rw.read([d1, d2]) == {"a": "r1", "b": "r2"}


def test_read_never_raises(tmp_path):
    assert rw.read([tmp_path / "missing"]) == {}
    (tmp_path / rw.WITHHOLD_FILE).write_text(
        'not json\n[1]\n{"path": "ok", "reason": "r"}\n{"path": 3, "reason": "r"}\n{"path": "x"}\n',
        encoding="utf-8",
    )
    assert rw.read([tmp_path]) == {"ok": "r"}
    bad = tmp_path / "bin"
    bad.mkdir()
    (bad / rw.WITHHOLD_FILE).write_bytes(b"\xff\xfe\x00")
    assert rw.read([bad]) == {}
