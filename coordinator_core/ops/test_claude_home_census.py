"""Tests for coordinator_core.ops.claude_home_census (tmp_path only, sparse fixtures)."""

from __future__ import annotations

import os
import time

from coordinator_core.ops import claude_home_census as cen


def _write(path, size, age_hours=0.0):
    cen._sparse(path, size, age_hours)


def test_measure_sums_bytes_and_files(tmp_path):
    _write(tmp_path / "a" / "x.bin", 100)
    _write(tmp_path / "a" / "b" / "y.bin", 50)
    walk = cen.measure(tmp_path, None)
    assert (walk.bytes, walk.files, walk.complete) == (150, 2, True)


def test_measure_missing_path_is_empty_and_complete(tmp_path):
    walk = cen.measure(tmp_path / "nope")
    assert (walk.bytes, walk.files, walk.complete) == (0, 0, True)


def test_measure_budget_stops_and_resumes_to_same_total(tmp_path):
    for i in range(20):
        _write(tmp_path / f"d{i}" / "f.bin", 10)
    partial = cen.measure(tmp_path, budget_seconds=0.0)
    assert not partial.complete
    finished = cen.measure(tmp_path, budget_seconds=None, resume=partial)
    assert finished.complete
    assert (finished.bytes, finished.files) == (200, 20)


def test_projects_breakdown_separates_subagents(tmp_path):
    proj = tmp_path / "projects"
    _write(proj / "p" / "s1.jsonl", 100)
    _write(proj / "p" / "s1" / "subagents" / "a.jsonl", 40)
    _write(proj / "p" / "notes.txt", 7)
    out = cen.projects_breakdown(proj, None)
    assert out["transcripts_bytes"] == 100
    assert out["subagents_bytes"] == 40 and out["subagents_files"] == 1
    assert out["other_bytes"] == 7
    assert out["complete"] is True


def test_settings_home_rows_name_writer_and_flag_unmapped(tmp_path):
    _write(tmp_path / "project-rag" / "x", 10)
    _write(tmp_path / "mystery" / "x", 5)
    rows = {r["name"]: r for r in cen.settings_home_rows(tmp_path, None)}
    assert rows["project-rag"]["regenerable"] is False
    assert "project-rag" in rows["project-rag"]["writer"]
    assert rows["mystery"]["writer"] == "unmapped" and rows["mystery"]["regenerable"] is False


def test_thresholds_default_and_override():
    assert cen.transcript_max_bytes(lambda k: None) == 250 * cen.MB
    assert cen.transcript_max_age_seconds(lambda k: None) == 48 * 3600
    vals = {cen.KEY_MAX_MB: "10", cen.KEY_MAX_AGE_HOURS: "2"}
    assert cen.transcript_max_bytes(vals.get) == 10 * cen.MB
    assert cen.transcript_max_age_seconds(vals.get) == 2 * 3600


def test_thresholds_reject_garbage_and_nonpositive():
    for bad in ("abc", "0", "-5", ""):
        assert cen.transcript_max_bytes(lambda k, b=bad: b) == 250 * cen.MB


def test_cache_cap_key_shape():
    assert cen.cache_cap_key("debug", "max_mb") == "cache_caps.debug.max_mb"


def test_census_reports_every_axis(tmp_path):
    home = tmp_path / "home"
    _write(home / "projects" / "p" / "s.jsonl", 10)
    _write(home / "debug" / "d.log", 20)
    temp_root = tmp_path / "tmp" / "claude"
    _write(temp_root / "p" / "sess" / "f", 30)
    settings = tmp_path / "settings"
    _write(settings / "state" / "f", 5)
    out = cen.census(home, temp_root, settings, budget=None)
    assert out["projects"]["transcripts_bytes"] == 10
    assert {f["name"]: f["bytes"] for f in out["claude_home_folders"]}["debug"] == 20
    assert out["temp_claude"]["bytes"] == 30 and out["temp_claude"]["files"] == 1
    assert out["settings_home"][0]["name"] == "state"


def test_fixture_ratchet_baseline_without_entry_point_is_equal(tmp_path):
    result = cen.fixture_ratchet(tmp_path, entry_point=lambda **_: None)
    assert result["bytes_after_total"] == result["bytes_before_total"]
    assert all(result["survives"].values())


def test_fixture_ratchet_with_entry_point_deletes_and_keeps_live(tmp_path):
    cap = 250 * cen.MB
    cutoff = 48 * 3600

    def retention(claude_home, temp_root, live_session_ids, apply):
        now = time.time()
        live_dirs = set(live_session_ids)
        for root in (claude_home / "projects", temp_root):
            for dirpath, _dirs, files in os.walk(root):
                parts = set(os.path.normpath(dirpath).split(os.sep))
                for name in files:
                    full = os.path.join(dirpath, name)
                    stem = name.split(".")[0]
                    if live_dirs & (parts | {stem}):
                        continue
                    st = os.stat(full)
                    if st.st_size > cap or now - st.st_mtime > cutoff:
                        os.remove(full)
        for cache in (claude_home / "debug").glob("*"):
            cache.unlink()

    result = cen.fixture_ratchet(tmp_path, entry_point=retention)
    s = result["survives"]
    assert s["big_ended"] is False and s["old_subagent"] is False
    assert s["live_big"] is True and s["temp_live"] is True
    assert s["temp_dead"] is False and s["cache"] is False
    assert result["bytes_after_total"] < result["bytes_before_total"]


def test_cli_fixture_ratchet_prints_json(tmp_path, capsys):
    assert cen.main(["--fixture-ratchet", "--root", str(tmp_path)]) == 0
    import json

    data = json.loads(capsys.readouterr().out)
    assert data["bytes_before_total"] > 0
