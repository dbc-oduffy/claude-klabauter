import json

from coordinator_core.ops.dispatch_emit import row_returns as r


def test_round_trip_utf8_lf_sorted(tmp_path):
    assert r.write(tmp_path, {}) is None
    assert not (tmp_path / r.RETURNS_FILE).exists()
    path = r.write(tmp_path, {"C2": "DONE: 960449 B µ", "C1": "DONE: a\nb"})
    raw = path.read_bytes()
    assert b"\r" not in raw
    assert list(json.loads(raw)) == ["C1", "C2"]
    got = r.read(tmp_path)
    assert got["C2"]["text"] == "DONE: 960449 B µ"
    assert got["C1"]["sha256"] == r.digest("DONE: a\nb")


def test_cap_marker_names_cut_and_report(tmp_path):
    r.write(tmp_path, {"C1": "x" * (r.RETURN_CAP_CHARS + 5)}, {"C1": ".coordinator-local/rep/C1.md"})
    text = r.read(tmp_path)["C1"]["text"]
    assert text.startswith("x" * r.RETURN_CAP_CHARS)
    last = text.splitlines()[-1]
    assert str(r.RETURN_CAP_CHARS) in last and ".coordinator-local/rep/C1.md" in last
    r.write(tmp_path, {"C1": "x" * r.RETURN_CAP_CHARS})
    assert r.read(tmp_path)["C1"]["text"] == "x" * r.RETURN_CAP_CHARS


def test_sha_mismatch_and_malformed_dropped(tmp_path):
    assert r.read(tmp_path) == {}
    f = tmp_path / r.RETURNS_FILE
    f.write_text(
        json.dumps({"C1": {"text": "DONE", "sha256": "bad"}, "C2": {"text": "DONE", "sha256": r.digest("DONE")}, "C3": 1}),
        encoding="utf-8",
    )
    assert list(r.read(tmp_path)) == ["C2"]
    f.write_text("{nope", encoding="utf-8")
    assert r.read(tmp_path) == {}


def test_is_done_and_persisted_path():
    assert r.is_done("DONE: x") and r.is_done("  done")
    assert not r.is_done("BLOCKED: x") and not r.is_done(None)
    assert r.persisted_path("state/runs/a\\b/") == "state/runs/a/b/returns.json"
