"""halt_strand_census: classification, green-code, population sum and the Mini key row."""

import json
from pathlib import Path

from coordinator_core.ops.dispatch_emit import halt_strand_census as hsc


def _run(root, name, halted=None, stage=None, files=None):
    d = root / name
    d.mkdir(parents=True)
    if halted is not None:
        (d / "halted.json").write_text(json.dumps(halted), encoding="utf-8")
    if stage is not None:
        (d / "run-stage.json").write_text(json.dumps(stage), encoding="utf-8")
    for fname, body in (files or {}).items():
        (d / fname).write_text(json.dumps(body), encoding="utf-8")
    return d


GREEN_LEG = {"status": "pass", "tests_run": 5, "tests_failed": 0}


def test_classifies_r1_causes_and_green(tmp_path):
    d = _run(tmp_path, "a", {"status": "halted", "halted": "test_verdict_empty", "reason": "x"},
             files={"test-leg.json": GREEN_LEG})
    r = hsc.classify(d)
    assert r["cause"] == "empty-verify-leg" and r["green"] is True and r["evidence"]["handled"] is True
    d = _run(tmp_path, "b", {"status": "halted", "halted": "commit", "reason": "land_byte_gate.admit stalled at land-deadline"},
             files={"overlay-verdict.json": {"verdict": "green"}})
    assert hsc.classify(d)["cause"] == "land-deadline-byte-gate"


def test_red_signal_blocks_green_and_base_moved_never_green(tmp_path):
    d = _run(tmp_path, "r", {"status": "halted", "halted": "commit"},
             files={"overlay-verdict.json": {"verdict": "green"}, "test-leg.json": {"status": "fail", "tests_failed": 2}})
    assert hsc.classify(d)["green"] is False
    d = _run(tmp_path, "m", {"status": "halted", "halted": "commit", "reason": "base moved under the run"},
             files={"test-leg.json": GREEN_LEG})
    r = hsc.classify(d)
    assert r["cause"] == "base-moved" and r["green"] is False


def test_unregistered_kind_and_registered_kind_evidence(tmp_path):
    d = _run(tmp_path, "k", {"status": "halted", "halted": "refusal"})
    ev = hsc.classify(d)["evidence"]
    assert ev["registered"] is True and ev["remedy_class"] == "mechanical"
    assert hsc.classify(_run(tmp_path, "u", {"status": "halted", "halted": "weird-kind"}))["cause"] == "weird-kind"


def test_population_sum_mixes_halted_abandoned_parked_and_non_halted(tmp_path):
    _run(tmp_path, "h1", {"status": "halted", "halted": "commit"})
    _run(tmp_path, "h2", {"status": "resumed", "halted": "commit"})
    _run(tmp_path, "ab", stage={"stage": "abandoned"})
    _run(tmp_path, "pk", stage={"status": "parked"})
    _run(tmp_path, "both", {"status": "halted", "halted": "parked"}, stage={"stage": "parked"})
    _run(tmp_path, "live", stage={"stage": "staged"})
    result = hsc.census(tmp_path)
    assert result["population"] == 5
    assert sum(r["runs"] for r in result["rows"]) == result["population"]
    causes = {r["cause"] for r in result["rows"]}
    assert {"abandoned", "parked", "resolved", "commit"} <= causes


def test_census_opens_a_bounded_number_of_files_per_run_dir(tmp_path, monkeypatch):
    green = {"overlay-verdict.json": {"verdict": "green"}, "test-leg.json": GREEN_LEG,
             "run-outcome.json": {"ledger_verdict": {"items": [{"status": "met"}]}},
             "withheld-prints.json": {"a": 1}}
    _run(tmp_path, "open", {"status": "halted", "halted": "commit"}, stage={"stage": "x"}, files=green)
    _run(tmp_path, "resolved", {"status": "resumed", "halted": "commit"}, stage={"stage": "x"}, files=green)
    _run(tmp_path, "moved", {"status": "halted", "halted": "commit", "reason": "base moved"}, files=green)
    _run(tmp_path, "parked", stage={"stage": "parked"}, files=green)
    _run(tmp_path, "live", stage={"stage": "staged"}, files=green)
    opened: list = []
    real = hsc._read_bytes
    monkeypatch.setattr(hsc, "_read_bytes", lambda p: (opened.append(p), real(p))[1])
    result = hsc.census(tmp_path)
    per_dir: dict = {}
    opened = [Path(p) for p in opened]
    for p in opened:
        per_dir[p.parent.name] = per_dir.get(p.parent.name, 0) + 1
    assert result["population"] == 4
    assert max(per_dir.values()) <= hsc.MAX_OPENS_PER_RUN_DIR
    assert per_dir["resolved"] == 1 and per_dir["moved"] == 1
    assert not any(p.name == hsc.WITHHELD_PRINTS_FILE for p in opened)


def test_census_rows_match_classify_per_run(tmp_path):
    _run(tmp_path, "g", {"status": "halted", "halted": "test_verdict_empty"}, files={"test-leg.json": GREEN_LEG})
    _run(tmp_path, "r", {"status": "halted", "halted": "commit"},
         files={"overlay-verdict.json": {"verdict": "green"}, "test-leg.json": {"status": "fail", "tests_failed": 1}})
    _run(tmp_path, "p", stage={"stage": "parked"}, files={"test-leg.json": GREEN_LEG})
    _run(tmp_path, "z", {"status": "resumed", "halted": "commit"})
    expected: dict = {}
    for d in tmp_path.iterdir():
        c = hsc.classify(d)
        row = expected.setdefault(c["cause"], [0, 0])
        row[0] += 1
        row[1] += 1 if c["green"] else 0
    got = {r["cause"]: [r["runs"], r["green"]] for r in hsc.census(tmp_path)["rows"]}
    assert got == expected


def test_mini_key_absent_yields_typed_unreadable_row(tmp_path):
    out = hsc.mini_census(tmp_path)
    assert out["unreadable"] == hsc.MINI_WARP_ROOT_KEY
    assert out["rows"][0]["cause"] == "unreadable" and out["rows"][0]["key"] == hsc.MINI_WARP_ROOT_KEY


def test_mini_key_present_reads_root(tmp_path):
    mini = tmp_path / "miniwarp"
    _run(mini, "x", {"status": "halted", "halted": "commit"})
    (tmp_path / "machine-local").mkdir()
    (tmp_path / "machine-local" / "census-roots.json").write_text(
        json.dumps({hsc.MINI_WARP_ROOT_KEY: str(mini)}), encoding="utf-8")
    assert hsc.mini_census(tmp_path)["population"] == 1


def test_main_prints_json(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "nohome"))
    _run(tmp_path / "warp", "h", {"status": "halted", "halted": "commit"})
    assert hsc.main([str(tmp_path / "warp")]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["box"]["population"] == 1 and out["mini"]["unreadable"] == hsc.MINI_WARP_ROOT_KEY
