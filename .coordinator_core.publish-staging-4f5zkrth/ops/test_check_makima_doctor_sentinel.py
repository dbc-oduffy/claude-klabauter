"""
Tests for coordinator_core.ops.check_makima_doctor_sentinel.

Port of: check-makima-doctor-sentinel.sh (DoE b5a4192c, 2026-07-20)
Golden-oracle corpus captured against the original bash script (positive +
negative cases) before authoring this module; assertions below mirror that
oracle's stdout+exit-code behavior line-for-line.
"""

from __future__ import annotations

import json
import time

import pytest

from coordinator_core.ops import check_makima_doctor_sentinel as mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("COORDINATOR_MAKIMA_DOCTOR_STALE_SEC", raising=False)


def _write_sentinel(tmp_path, payload):
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    sentinel = state_dir / "doctor-last-run.json"
    if isinstance(payload, str):
        sentinel.write_text(payload, encoding="utf-8")
    else:
        sentinel.write_text(json.dumps(payload), encoding="utf-8")
    return sentinel


def test_sentinel_absent(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sentinel absent (doctor never run on this machine)" in out


def test_green_fresh_is_silent(tmp_path, monkeypatch, capsys):
    _write_sentinel(tmp_path, {"verdict": "GREEN", "red_probes": [], "hint": "", "ts": int(time.time())})
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


def test_green_stale(tmp_path, monkeypatch, capsys):
    _write_sentinel(
        tmp_path,
        {"verdict": "GREEN", "red_probes": [], "hint": "", "ts": int(time.time()) - 999999},
    )
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "stale (last run 11d ago). Run python bin/makima-doctor-probe.py --triage." in out


def test_amber(tmp_path, monkeypatch, capsys):
    _write_sentinel(
        tmp_path,
        {"verdict": "AMBER", "red_probes": ["p1"], "hint": "fix p1", "ts": int(time.time())},
    )
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "[health] makima-doctor: AMBER — fix p1. Run python bin/makima-doctor-probe.py --triage to re-probe."


def test_red(tmp_path, monkeypatch, capsys):
    _write_sentinel(
        tmp_path,
        {"verdict": "RED", "red_probes": ["p1", "p2"], "hint": "run doctor", "ts": int(time.time())},
    )
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "[health] makima-doctor: RED (p1,p2) — run doctor. Run python bin/makima-doctor-probe.py --triage for details."


def test_malformed_json(tmp_path, monkeypatch, capsys):
    _write_sentinel(tmp_path, "not json")
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sentinel unreadable" in out
    assert "malformed JSON?" in out


def test_unknown_verdict(tmp_path, monkeypatch, capsys):
    _write_sentinel(tmp_path, {"verdict": "PURPLE", "red_probes": [], "hint": "", "ts": int(time.time())})
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "[health] makima-doctor: unknown verdict 'PURPLE'. Run python bin/makima-doctor-probe.py --triage."


def test_missing_ts(tmp_path, monkeypatch, capsys):
    _write_sentinel(tmp_path, {"verdict": "GREEN", "red_probes": [], "hint": ""})
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "[health] makima-doctor: sentinel ts unparseable. Run python bin/makima-doctor-probe.py --triage."


def test_custom_stale_sec_env(tmp_path, monkeypatch, capsys):
    _write_sentinel(
        tmp_path,
        {"verdict": "GREEN", "red_probes": [], "hint": "", "ts": int(time.time()) - 999999},
    )
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    monkeypatch.setenv("COORDINATOR_MAKIMA_DOCTOR_STALE_SEC", "99999999")
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


def test_non_dict_json_is_treated_as_malformed(tmp_path, monkeypatch, capsys):
    _write_sentinel(tmp_path, [1, 2, 3])
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert "sentinel unreadable" in out


def test_tolerant_of_sentinel_lacking_vendor_drift_key(tmp_path, monkeypatch, capsys):
    """Old-shape sentinel (pre-2026-07-26, no `vendor_drift` key at all) parses
    identically to today's — this module never requires the additive key.

    Spec backlink: cross-repo/inbox/2026-07-26-doe-claude-em-schema-drift-watch-seam-and-tolerance-ratification.md
    """
    _write_sentinel(
        tmp_path,
        {
            "verdict": "GREEN",
            "red_probes": [],
            "hint": "",
            "ts": int(time.time()),
            "schema_version": 1,
            "plugin": "project-makima",
        },
    )
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


def test_tolerant_of_sentinel_carrying_vendor_drift_key(tmp_path, monkeypatch, capsys):
    """New-shape sentinel (carrying the additive `vendor_drift` key) parses
    identically to the pre-2026-07-26 shape — this module reads only the four
    fields it has always read (verdict, hint, red_probes, ts) and ignores the rest."""
    _write_sentinel(
        tmp_path,
        {
            "verdict": "AMBER",
            "red_probes": [],
            "hint": "re-vendor improvement-queue.schema.json",
            "ts": int(time.time()),
            "schema_version": 1,
            "plugin": "project-makima",
            "vendor_drift": {
                "status": "DRIFT",
                "checked": 12,
                "drifted": [
                    {
                        "schema": "improvement-queue.schema.json",
                        "direction": "we-are-behind",
                        "local_version": "1.0.0",
                        "doe_version": "1.1.0",
                    }
                ],
                "indeterminate": [],
            },
        },
    )
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == (
        "[health] makima-doctor: AMBER — re-vendor improvement-queue.schema.json. "
        "Run python bin/makima-doctor-probe.py --triage to re-probe."
    )


def test_amber_advisory_only_renders_advisory_not_amber(tmp_path, monkeypatch, capsys):
    """A non-required-only degradation (advisory_only=True) must render as
    ADVISORY, not the bare AMBER band a real prerequisite failure gets.

    Root-cause regression case: makima.schema.vendor_drift is required=False,
    but `_local_reduce_overall`/`reduce_overall` drag `envelope.overall` to
    DEGRADED regardless of `required`, so the sentinel's AMBER verdict was
    visually identical to a required-probe failure until `advisory_only` was
    threaded through.
    """
    _write_sentinel(
        tmp_path,
        {
            "verdict": "AMBER",
            "red_probes": [],
            "hint": "makima.schema.vendor_drift — verify the DoE clone",
            "ts": int(time.time()),
            "schema_version": 1,
            "plugin": "project-makima",
            "advisory_only": True,
        },
    )
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == (
        "[health] makima-doctor: ADVISORY (non-gating) — makima.schema.vendor_drift — "
        "verify the DoE clone. Run python bin/makima-doctor-probe.py --triage to re-probe."
    )
    assert "AMBER" not in out


def test_red_advisory_only_renders_advisory_not_red(tmp_path, monkeypatch, capsys):
    """Same distinction for RED/BROKEN: a required=False probe can also drive
    `overall` to BROKEN (e.g. makima.invoke.smoke's timeout case); advisory_only
    must suppress the RED band there too."""
    _write_sentinel(
        tmp_path,
        {
            "verdict": "RED",
            "red_probes": ["makima.invoke.smoke"],
            "hint": "re-run the smoke probe",
            "ts": int(time.time()),
            "schema_version": 1,
            "plugin": "project-makima",
            "advisory_only": True,
        },
    )
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == (
        "[health] makima-doctor: ADVISORY (non-gating) (makima.invoke.smoke) — "
        "re-run the smoke probe. Run python bin/makima-doctor-probe.py --triage for details."
    )
    assert "RED" not in out


def test_amber_required_failure_still_renders_amber(tmp_path, monkeypatch, capsys):
    """A real required-probe failure (advisory_only absent/False) must render
    exactly as before this change — no regression to the gating case."""
    _write_sentinel(
        tmp_path,
        {"verdict": "AMBER", "red_probes": ["p1"], "hint": "fix p1", "ts": int(time.time())},
    )
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "[health] makima-doctor: AMBER — fix p1. Run python bin/makima-doctor-probe.py --triage to re-probe."
    assert "ADVISORY" not in out


def test_red_required_failure_still_renders_red(tmp_path, monkeypatch, capsys):
    """A real required-probe RED failure with advisory_only explicitly False
    must render exactly as before."""
    _write_sentinel(
        tmp_path,
        {
            "verdict": "RED",
            "red_probes": ["p1", "p2"],
            "hint": "run doctor",
            "ts": int(time.time()),
            "advisory_only": False,
        },
    )
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == "[health] makima-doctor: RED (p1,p2) — run doctor. Run python bin/makima-doctor-probe.py --triage for details."
    assert "ADVISORY" not in out


def test_old_sentinel_lacking_advisory_only_key_renders_as_before(tmp_path, monkeypatch, capsys):
    """An OLD sentinel written before `advisory_only` existed (key wholly
    absent) must behave identically to today's pre-change behavior — no
    crash, no new ADVISORY band, plain AMBER."""
    _write_sentinel(
        tmp_path,
        {
            "verdict": "AMBER",
            "red_probes": [],
            "hint": "makima.schema.vendor_drift — verify the DoE clone",
            "ts": int(time.time()),
            "schema_version": 1,
            "plugin": "project-makima",
            "vendor_drift": {"status": "INDETERMINATE", "checked": 12, "drifted": [], "indeterminate": ["x.schema.json"]},
        },
    )
    monkeypatch.setattr(mod, "_makima_root", lambda: tmp_path)
    rc = mod.main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert out.strip() == (
        "[health] makima-doctor: AMBER — makima.schema.vendor_drift — verify the DoE clone. "
        "Run python bin/makima-doctor-probe.py --triage to re-probe."
    )
    assert "ADVISORY" not in out
