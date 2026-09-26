"""Behavioral tests for the registered-marker gate in
coordinator_core.write_guards.nudge_unmarked_spawning_test (Item 27,
docs/plans/2026-09-26-inbox-blitz-claude-klabauter-fixes-fyi-rest.md § R27).

Covers:
  1. A consumer repo with no `markers` list registered anywhere (no
     pyproject.toml `[tool.pytest.ini_options]`, no `pytest.ini`/
     `setup.cfg` `[pytest]` section) gets no offer, even for an unmarked
     spawning test fixture.
  2. A consumer repo whose `pyproject.toml` registers markers but NOT
     `spawns_process` gets no offer.
  3. A consumer repo that DOES register `spawns_process` in
     `pyproject.toml` gets the offer.
  4. The same, registered via an ini-shaped `pytest.ini` `[pytest]
     markers` instead of pyproject.toml.
  5. This repo (claude-klabauter), which registers `spawns_process` in its own
     `pyproject.toml`, gets the offer with no monkeypatching of repo
     resolution at all.

Grep anchors: THE-SPAWNING-TEST-NUDGE-OFFERS-ONLY-REGISTERED-MARKERS, R27
"""

from __future__ import annotations

from coordinator_core.write_guards import nudge_unmarked_spawning_test as guard

_UNMARKED_SPAWN_TEST = (
    "import subprocess\n"
    "def test_thing():\n"
    "    subprocess.run(['git', 'status'])\n"
)


def _payload(file_path: str, content: str) -> dict:
    return {"tool_name": "Write", "tool_input": {"file_path": file_path, "content": content}}


def test_no_markers_list_anywhere_stays_silent(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text('[tool.pytest.ini_options]\n', encoding="utf-8")
    monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd=None: str(tmp_path))

    result = guard.check(
        _payload(str(tmp_path / "tests" / "test_thing.py"), _UNMARKED_SPAWN_TEST)
    )

    assert result is None


def test_markers_registered_but_not_spawns_process_stays_silent(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'markers = [\n    "slow: marks tests as slow",\n]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd=None: str(tmp_path))

    result = guard.check(
        _payload(str(tmp_path / "tests" / "test_thing.py"), _UNMARKED_SPAWN_TEST)
    )

    assert result is None


def test_spawns_process_registered_in_pyproject_fires(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\n"
        'markers = [\n'
        '    "slow: marks tests as slow",\n'
        '    "spawns_process: spawns a real OS process",\n'
        "]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd=None: str(tmp_path))

    result = guard.check(
        _payload(str(tmp_path / "tests" / "test_thing.py"), _UNMARKED_SPAWN_TEST)
    )

    assert result is not None
    hso = result["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "additionalContext" in hso
    assert "permissionDecision" not in hso
    assert "spawns_process" in hso["additionalContext"]


def test_spawns_process_registered_in_ini_shaped_pytest_ini_fires(tmp_path, monkeypatch):
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\n"
        "markers =\n"
        "    slow: marks tests as slow\n"
        "    spawns_process: spawns a real OS process\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(guard, "resolve_repo_root", lambda cwd=None: str(tmp_path))

    result = guard.check(
        _payload(str(tmp_path / "tests" / "test_thing.py"), _UNMARKED_SPAWN_TEST)
    )

    assert result is not None
    assert "spawns_process" in result["hookSpecificOutput"]["additionalContext"]


def test_claude_klabauter_itself_registers_spawns_process_and_fires():
    # No monkeypatching of repo resolution: this test runs inside claude-klabauter's
    # own checkout, which registers `spawns_process` in its own
    # pyproject.toml (see the module docstring's REGISTERED-MARKER GATE).
    result = guard.check(
        _payload("/repo/coordinator_core/tests/test_thing.py", _UNMARKED_SPAWN_TEST)
    )

    assert result is not None
    assert "spawns_process" in result["hookSpecificOutput"]["additionalContext"]
