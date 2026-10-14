"""Branch coverage for the in-process workday-start probes."""
from __future__ import annotations

import yaml

from coordinator_core.workday_start import probes


def _write(tmp_path, data, raw=None):
    d = tmp_path / "state" / "test-red"
    d.mkdir(parents=True)
    (d / "m1.yaml").write_text(raw if raw is not None else yaml.safe_dump(data), encoding="utf-8")


def _run(tmp_path):
    return probes.test_red_delta(tmp_path, machine="m1")


def test_absent_is_silent(tmp_path):
    assert _run(tmp_path) == (0, "")


def test_malformed_is_silent(tmp_path):
    _write(tmp_path, None, raw=": : [\n")
    assert _run(tmp_path) == (0, "")


def test_non_mapping_is_silent(tmp_path):
    _write(tmp_path, ["a"])
    assert _run(tmp_path) == (0, "")


def test_green_is_silent(tmp_path):
    _write(tmp_path, {"tiers": {"fast": {"outcome": "green", "failing": []}}})
    assert _run(tmp_path) == (0, "")


def test_null_failing_is_undeterminable_not_green(tmp_path):
    _write(tmp_path, {"tiers": {"fast": {"outcome": "build-failure", "failing": None}}})
    code, text = _run(tmp_path)
    assert code == 0 and "undeterminable" in text and text.count("\n") == 0


def test_new_failures_counted_against_previous_and_baseline(tmp_path):
    _write(tmp_path, {"tiers": {"fast": {
        "outcome": "test-failures",
        "failing": ["a", "b", "c"],
        "previous": {"failing": ["a"]},
        "acknowledged": {"baseline": ["b"]},
    }}})
    assert "3 failing, 1 new" in _run(tmp_path)[1]


def test_one_line_per_red_tier(tmp_path):
    _write(tmp_path, {"tiers": {
        "fast": {"outcome": "test-failures", "failing": ["a"]},
        "full": {"outcome": "test-failures", "failing": ["b"]},
        "ok": {"outcome": "green", "failing": []},
    }})
    assert len(_run(tmp_path)[1].splitlines()) == 2


def _home_with_config(tmp_path, monkeypatch, text=None):
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path))
    if text is not None:
        (tmp_path / ".claude.json").write_text(text, encoding="utf-8")


def test_mcp_registration_counts_entries(tmp_path, monkeypatch):
    _home_with_config(tmp_path, monkeypatch, '{"mcpServers": {"a": {}, "b": {}}, "x": 1}')
    assert probes.mcp_registration(tmp_path) == (0, "- MCP servers registered: 2")


def test_mcp_registration_no_key_is_zero(tmp_path, monkeypatch):
    _home_with_config(tmp_path, monkeypatch, "{}")
    assert probes.mcp_registration(tmp_path) == (0, "- MCP servers registered: 0")


def test_mcp_registration_missing_file_one_line_exit_0(tmp_path, monkeypatch):
    _home_with_config(tmp_path, monkeypatch)
    code, text = probes.mcp_registration(tmp_path)
    assert code == 0 and "not found" in text and "\n" not in text


def test_mcp_registration_unparseable_one_line_exit_0(tmp_path, monkeypatch):
    _home_with_config(tmp_path, monkeypatch, "{not json")
    code, text = probes.mcp_registration(tmp_path)
    assert code == 0 and "not valid JSON" in text and "\n" not in text


def test_mcp_registration_non_object_servers(tmp_path, monkeypatch):
    _home_with_config(tmp_path, monkeypatch, '{"mcpServers": ["a"]}')
    code, text = probes.mcp_registration(tmp_path)
    assert code == 0 and "not an object" in text


def test_fixture_sync_absent_is_silent(tmp_path):
    assert probes.fixture_sync_presence(tmp_path) == (0, "")


def test_fixture_sync_present_reports_not_run_and_never_spawns(tmp_path, monkeypatch):
    (tmp_path / "bin").mkdir()
    (tmp_path / "bin" / "check-fixture-sync.sh").write_text("exit 1\n", encoding="utf-8")
    import subprocess

    def _boom(*a, **k):
        raise AssertionError("spawned")

    monkeypatch.setattr(subprocess, "Popen", _boom)
    code, text = probes.fixture_sync_presence(tmp_path)
    assert code == 0 and "bin/check-fixture-sync.sh" in text and "not run" in text


def test_tool_availability_silent_when_both_resolve(monkeypatch, tmp_path):
    monkeypatch.setattr(probes.shutil, "which", lambda t: "/bin/" + t)
    assert probes.tool_availability(tmp_path) == (0, "")


def test_tool_availability_names_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(probes.shutil, "which", lambda t: None if t == "scc" else "/bin/" + t)
    assert probes.tool_availability(tmp_path) == (0, "- scc: not on PATH")
