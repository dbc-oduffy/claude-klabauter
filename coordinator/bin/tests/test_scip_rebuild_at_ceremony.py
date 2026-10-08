"""`coordinator/bin/scip-rebuild-at-ceremony.py` -- the ceremony-triggered
SCIP rebuild trigger, wired to example-retrieval-repo's `scip-rebuild --ceremony NAME`.

WHAT THESE PIN. This tool is best-effort by contract (memo:
state/cross-repo/inbox/2026-09-24-example-retrieval-repo-em-scip-rebuild-ceremony-hook.md) --
it must never fail a ceremony. Every scenario below asserts `main()` returns 0
(never raises) alongside the specific `skipped`/`spawned` line, and the happy
path asserts the spawned argv/cwd rather than letting a real rebuild run --
`subprocess.Popen` is monkeypatched so no real example-retrieval-repo process is ever
launched from this test.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "scip-rebuild-at-ceremony.py"


def _load():
    """`spec_from_file_location`, not `import_module` -- the filename is
    hyphenated and not a valid dotted identifier."""
    spec = importlib.util.spec_from_file_location("_scip_rebuild_at_ceremony", _TOOL)
    assert spec and spec.loader, f"unloadable: {_TOOL}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool(monkeypatch):
    module = _load()
    # The host's real free memory must not decide a test's path; the floor has its own tests.
    monkeypatch.setattr(module, "available_memory_gb", lambda: 1024.0)
    monkeypatch.delenv(module._MIN_AVAILABLE_GB_ENV, raising=False)
    return module


def test_tool_exists_and_is_executable_python():
    assert _TOOL.is_file(), f"missing {_TOOL}"
    compile(_TOOL.read_text(encoding="utf-8"), str(_TOOL), "exec")


def test_bad_ceremony_name_rejected(tool):
    result = tool.run("Bad Name", Path("."))
    assert result.startswith("scip-rebuild: skipped -- invalid --ceremony name")


def test_bad_ceremony_name_rejected_leading_dash(tool):
    # `[a-z0-9][a-z0-9_-]*` requires an alnum first character.
    result = tool.run("-handoff", Path("."))
    assert result.startswith("scip-rebuild: skipped -- invalid --ceremony name")


def test_registry_key_missing_reader_skips(tool, monkeypatch, tmp_path):
    monkeypatch.setattr(tool, "_settings_home", lambda: tmp_path / "no-such-settings-home")
    result = tool.run("handoff", tmp_path)
    assert result.startswith("scip-rebuild: skipped -- machine-local reader not found")


def test_registry_key_points_at_missing_file_skips(tool, monkeypatch, tmp_path):
    monkeypatch.setattr(
        tool, "_resolve_registry_key", lambda key: (str(tmp_path / "does-not-exist.exe"), None)
    )
    result = tool.run("workstream-complete", tmp_path)
    assert result.startswith("scip-rebuild: skipped -- registry key names a file that does not exist")


def test_registry_key_unresolved_skips(tool, monkeypatch, tmp_path):
    monkeypatch.setattr(tool, "_resolve_registry_key", lambda key: (None, "registry key unset"))
    result = tool.run("merge-to-main", tmp_path)
    assert result == "scip-rebuild: skipped -- registry key unset"


def test_happy_path_spawns_with_right_argv_and_cwd(tool, monkeypatch, tmp_path):
    """Pins argv shape (the `publish-repo-bundle-at-ceremony.py` sequencer, run
    under `sys.executable`, with `--ceremony`/`--repo-root`/`--cli`) and cwd
    (the target repo root, not this script's own location) -- both load-bearing
    per the memo contract and the row-C3 chaining. `_spawn_detached` is
    monkeypatched wholesale so no real subprocess is ever created."""
    fake_exe = tmp_path / "example-retrieval-repo-cli.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(tool, "_resolve_registry_key", lambda key: (str(fake_exe), None))

    captured = {}

    def fake_spawn(argv, cwd, log_path):
        captured["argv"] = argv
        captured["cwd"] = cwd
        captured["log_path"] = log_path
        return True

    monkeypatch.setattr(tool, "_spawn_detached", fake_spawn)
    monkeypatch.setattr(tool, "_git_common_dir", lambda repo_root: None)

    repo_root = tmp_path / "target-repo"
    repo_root.mkdir()
    result = tool.run("handoff", repo_root)

    assert captured["argv"] == [
        sys.executable,
        str(tool._PUBLISH_SEQUENCER),
        "--ceremony",
        "handoff",
        "--repo-root",
        str(repo_root),
        "--cli",
        str(fake_exe),
    ]
    assert captured["cwd"] == repo_root
    assert result.startswith("scip-rebuild: spawned (handoff) -> ")
    assert str(captured["log_path"]) in result


def test_happy_path_never_waits_on_the_spawned_child(tool, monkeypatch, tmp_path):
    """`run()` returns immediately once `_spawn_detached` reports success --
    it never calls `.wait()`/`.communicate()` on anything, since `_spawn_detached`
    itself is the only subprocess entry point exercised on the happy path."""
    fake_exe = tmp_path / "example-retrieval-repo-cli.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(tool, "_resolve_registry_key", lambda key: (str(fake_exe), None))
    monkeypatch.setattr(tool, "_spawn_detached", lambda argv, cwd, log_path: True)
    monkeypatch.setattr(tool, "_git_common_dir", lambda repo_root: None)

    repo_root = tmp_path / "target-repo"
    repo_root.mkdir()
    result = tool.run("handoff", repo_root)
    assert result.startswith("scip-rebuild: spawned (handoff) -> ")


def test_prior_publish_line_surfaced_as_second_line(tool, monkeypatch, tmp_path):
    """When the sequencer's last-publish record is present, `run()` prints its
    `line` field as a second output line -- so an exit-4 fix reaches the next
    ceremony's own transcript, not only a log file."""
    fake_exe = tmp_path / "example-retrieval-repo-cli.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(tool, "_resolve_registry_key", lambda key: (str(fake_exe), None))
    monkeypatch.setattr(tool, "_spawn_detached", lambda argv, cwd, log_path: True)

    common_dir = tmp_path / "common"
    monkeypatch.setattr(tool, "_git_common_dir", lambda repo_root: common_dir)

    record_dir = common_dir / "coordinator-sessions"
    record_dir.mkdir(parents=True)
    record_path = record_dir / "publish-repo-bundle-last.json"
    record_path.write_text(
        json.dumps(
            {
                "exit_code": 4,
                "head": "deadbeef",
                "line": "publish-repo-bundle: refused -- reindex --incremental (older than HEAD)",
                "finished_at": "2026-09-26T00:00:00Z",
            }
        ),
        encoding="utf-8",
    )

    repo_root = tmp_path / "target-repo"
    repo_root.mkdir()
    result = tool.run("handoff", repo_root)

    lines = result.splitlines()
    assert lines[0].startswith("scip-rebuild: spawned (handoff) -> ")
    assert lines[1] == "publish-repo-bundle: refused -- reindex --incremental (older than HEAD)"


def test_no_prior_publish_record_means_no_second_line(tool, monkeypatch, tmp_path):
    fake_exe = tmp_path / "example-retrieval-repo-cli.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(tool, "_resolve_registry_key", lambda key: (str(fake_exe), None))
    monkeypatch.setattr(tool, "_spawn_detached", lambda argv, cwd, log_path: True)
    monkeypatch.setattr(tool, "_git_common_dir", lambda repo_root: tmp_path / "common-empty")

    repo_root = tmp_path / "target-repo"
    repo_root.mkdir()
    result = tool.run("handoff", repo_root)
    assert "\n" not in result
    assert result.startswith("scip-rebuild: spawned (handoff) -> ")


def test_spawn_failure_still_reports_and_never_raises(tool, monkeypatch, tmp_path):
    fake_exe = tmp_path / "example-retrieval-repo-cli.exe"
    fake_exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(tool, "_resolve_registry_key", lambda key: (str(fake_exe), None))
    monkeypatch.setattr(tool, "_spawn_detached", lambda argv, cwd, log_path: False)

    result = tool.run("handoff", tmp_path)
    assert result.startswith("scip-rebuild: skipped -- failed to spawn")


def test_main_always_exits_zero_on_skip(tool, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tool, "_settings_home", lambda: tmp_path / "no-such-settings-home")
    exit_code = tool.main(["--ceremony", "handoff", "--repo-root", str(tmp_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "skipped" in out


def test_main_exits_zero_even_on_unexpected_exception(tool, monkeypatch, tmp_path, capsys):
    def _boom(ceremony, repo_root):
        raise RuntimeError("boom")

    monkeypatch.setattr(tool, "run", _boom)
    exit_code = tool.main(["--ceremony", "handoff", "--repo-root", str(tmp_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "unexpected error" in out


def _spawnable(tool, monkeypatch, tmp_path, spawned):
    fake_exe = tmp_path / "example-retrieval-repo-cli.exe"
    fake_exe.write_text("")
    monkeypatch.setattr(tool, "_resolve_registry_key", lambda key: (str(fake_exe), None))
    monkeypatch.setattr(tool, "_spawn_detached", lambda argv, cwd, log_path: spawned.append(argv) or True)
    monkeypatch.setattr(tool, "_git_common_dir", lambda repo_root: None)


def test_low_available_memory_skips_without_spawning(tool, monkeypatch, tmp_path):
    spawned: list = []
    _spawnable(tool, monkeypatch, tmp_path, spawned)
    monkeypatch.setattr(tool, "available_memory_gb", lambda: 5.5)
    line = tool.run("merge-to-main", tmp_path)
    assert line.startswith("scip-rebuild: skipped -- 5.5 GB available")
    assert spawned == []


def test_memory_floor_is_overridable_by_env(tool, monkeypatch, tmp_path):
    spawned: list = []
    _spawnable(tool, monkeypatch, tmp_path, spawned)
    monkeypatch.setattr(tool, "available_memory_gb", lambda: 5.5)
    monkeypatch.setenv(tool._MIN_AVAILABLE_GB_ENV, "4")
    assert tool.run("merge-to-main", tmp_path).startswith("scip-rebuild: spawned")
    assert len(spawned) == 1


def test_unknown_available_memory_still_spawns(tool, monkeypatch, tmp_path):
    spawned: list = []
    _spawnable(tool, monkeypatch, tmp_path, spawned)
    monkeypatch.setattr(tool, "available_memory_gb", lambda: None)
    assert tool.run("merge-to-main", tmp_path).startswith("scip-rebuild: spawned")


def test_available_memory_reads_this_host():
    value = _load().available_memory_gb()
    assert value is None or value > 0
