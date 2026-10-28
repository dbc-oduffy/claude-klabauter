"""`coordinator/bin/publish-repo-bundle-at-ceremony.py` -- the detached sequencer
spawned by `scip-rebuild-at-ceremony.py` that waits on `scip-rebuild --ceremony NAME`
then publishes via example-retrieval-repo's `publish-repo-bundle` verb.

WHAT THESE PIN (docs/plans/2026-09-26-wire-repo-index-bundle-verbs.md, row C2):
`run()` never raises and always returns a single reportable line; a rebuild that
does not exit 0 never reaches the publish call; a publish exit 4 is mapped to a
named fix and is never blind-retried at the same HEAD; every subprocess call
passes `no_console_creationflags()`. `_run_and_wait` is monkeypatched throughout
so no real example-retrieval-repo process is ever launched from this test.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_TOOL = Path(__file__).resolve().parents[1] / "publish-repo-bundle-at-ceremony.py"


def _load():
    """`spec_from_file_location`, not `import_module` -- the filename is
    hyphenated and not a valid dotted identifier."""
    spec = importlib.util.spec_from_file_location("_publish_repo_bundle_at_ceremony", _TOOL)
    assert spec and spec.loader, f"unloadable: {_TOOL}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def tool():
    return _load()


@pytest.fixture()
def fake_cli(tmp_path):
    exe = tmp_path / "example-retrieval-repo-cli.exe"
    exe.write_text("", encoding="utf-8")
    return exe


def test_tool_exists_and_is_executable_python():
    assert _TOOL.is_file(), f"missing {_TOOL}"
    compile(_TOOL.read_text(encoding="utf-8"), str(_TOOL), "exec")


def test_bad_ceremony_name_rejected(tool):
    result = tool.run("Bad Name", Path("."))
    assert result.startswith("publish-repo-bundle: skipped -- invalid --ceremony name")


def test_cli_override_missing_file_skips(tool, tmp_path):
    result = tool.run("handoff", tmp_path, cli_override=str(tmp_path / "nope.exe"))
    assert result.startswith("publish-repo-bundle: skipped -- --cli names a file that does not exist")


def test_registry_key_unresolved_skips_where_example_retrieval_repo_is_absent(tool, monkeypatch, tmp_path):
    monkeypatch.setattr(tool, "_resolve_registry_key", lambda key: (None, "registry key unset"))
    result = tool.run("handoff", tmp_path)
    assert result == "publish-repo-bundle: skipped -- registry key unset"


def test_registry_key_unresolved_is_a_defect_where_example_retrieval_repo_is_registered(tool, monkeypatch, tmp_path):
    registry = {"repos.project_rag": str(tmp_path)}
    monkeypatch.setattr(
        tool, "_resolve_registry_key", lambda key: (registry.get(key), None if key in registry else "unset")
    )
    result = tool.run("handoff", tmp_path)
    assert result == "publish-repo-bundle: defect -- unset, but repos.project_rag is registered"


def _calls_recorder(monkeypatch, tool, results):
    """Feeds successive `_run_and_wait` calls the given (code, stdout, stderr)
    tuples in order, and records the argv each call was made with."""
    calls: list[list[str]] = []
    iterator = iter(results)

    def fake(argv, cwd, timeout):
        calls.append(argv)
        return next(iterator)

    monkeypatch.setattr(tool, "_run_and_wait", fake)
    return calls


def test_rebuild_exit3_skips_no_publish(tool, monkeypatch, tmp_path, fake_cli):
    calls = _calls_recorder(monkeypatch, tool, [(3, "", "lock held")])
    monkeypatch.setattr(tool, "_git_common_dir", lambda repo_root: None)

    result = tool.run("handoff", tmp_path, cli_override=str(fake_cli))

    assert result == "publish-repo-bundle: skipped -- scip-rebuild exited 3 (rebuild lock held elsewhere), no publish"
    assert len(calls) == 1  # only the rebuild call, publish never invoked


def test_rebuild_exit1_skips_no_publish(tool, monkeypatch, tmp_path, fake_cli):
    calls = _calls_recorder(monkeypatch, tool, [(1, "", "boom")])
    monkeypatch.setattr(tool, "_git_common_dir", lambda repo_root: None)

    result = tool.run("handoff", tmp_path, cli_override=str(fake_cli))

    assert result.startswith("publish-repo-bundle: skipped -- scip-rebuild exited 1")
    assert len(calls) == 1


def test_rebuild_timeout_skips_no_publish(tool, monkeypatch, tmp_path, fake_cli):
    calls = _calls_recorder(monkeypatch, tool, [(None, "", "")])
    monkeypatch.setattr(tool, "_git_common_dir", lambda repo_root: None)

    result = tool.run("handoff", tmp_path, cli_override=str(fake_cli))

    assert result == "publish-repo-bundle: skipped -- scip-rebuild timed out, no publish"
    assert len(calls) == 1


def test_happy_path_runs_rebuild_then_publish_with_right_argv(tool, monkeypatch, tmp_path, fake_cli):
    repo_root = tmp_path / "target-repo"
    repo_root.mkdir()
    calls = _calls_recorder(monkeypatch, tool, [(0, "", ""), (0, '{"land": {}}', "")])
    monkeypatch.setattr(tool, "_git_common_dir", lambda root: tmp_path / "common")
    monkeypatch.setattr(tool, "_current_head", lambda root: "deadbeef")

    result = tool.run("handoff", repo_root, cli_override=str(fake_cli))

    assert calls[0] == [str(fake_cli), "scip-rebuild", "--ceremony", "handoff"]
    assert calls[1] == [
        str(fake_cli),
        "publish-repo-bundle",
        "--project-root",
        str(repo_root),
        "--json",
    ]
    assert result == "publish-repo-bundle: published"

    record_path = tmp_path / "common" / "coordinator-sessions" / "publish-repo-bundle-last.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["exit_code"] == 0
    assert record["head"] == "deadbeef"
    assert record["line"] == "publish-repo-bundle: published"


def test_repo_slug_passed_through_only_when_given(tool, monkeypatch, tmp_path, fake_cli):
    repo_root = tmp_path / "target-repo"
    repo_root.mkdir()
    calls = _calls_recorder(monkeypatch, tool, [(0, "", ""), (0, "", "")])
    monkeypatch.setattr(tool, "_git_common_dir", lambda root: tmp_path / "common")
    monkeypatch.setattr(tool, "_current_head", lambda root: "deadbeef")

    tool.run("handoff", repo_root, cli_override=str(fake_cli), repo_slug="acme/widgets")

    assert calls[1][-2:] == ["--repo", "acme/widgets"]


@pytest.mark.parametrize(
    ("stderr", "expected_fix"),
    [
        ("refusal: index older than HEAD by 3 commits", "reindex --incremental"),
        ("refusal: HEAD not reachable from any remote-tracking ref", "push HEAD"),
        ("refusal: working tree is dirty", "commit or stash"),
        ("refusal: no target GitHub repo resolved for this project", "register repos.<slug>.publish_repo"),
    ],
)
def test_publish_exit4_maps_named_fix(tool, monkeypatch, tmp_path, fake_cli, stderr, expected_fix):
    repo_root = tmp_path / "target-repo"
    repo_root.mkdir()
    _calls_recorder(monkeypatch, tool, [(0, "", ""), (4, "", stderr)])
    monkeypatch.setattr(tool, "_git_common_dir", lambda root: tmp_path / "common")
    monkeypatch.setattr(tool, "_current_head", lambda root: "deadbeef")

    result = tool.run("handoff", repo_root, cli_override=str(fake_cli))

    assert result.startswith(f"publish-repo-bundle: refused -- {expected_fix}")
    assert stderr.strip().splitlines()[0] in result


def test_publish_exit4_unmatched_cause_echoed_verbatim(tool, monkeypatch, tmp_path, fake_cli):
    repo_root = tmp_path / "target-repo"
    repo_root.mkdir()
    _calls_recorder(monkeypatch, tool, [(0, "", ""), (4, "", "refusal: something never seen before")])
    monkeypatch.setattr(tool, "_git_common_dir", lambda root: tmp_path / "common")
    monkeypatch.setattr(tool, "_current_head", lambda root: "deadbeef")

    result = tool.run("handoff", repo_root, cli_override=str(fake_cli))

    assert "refusal: something never seen before" in result


def test_publish_exit4_same_head_not_retried(tool, monkeypatch, tmp_path, fake_cli):
    repo_root = tmp_path / "target-repo"
    repo_root.mkdir()
    common_dir = tmp_path / "common"
    record_path = common_dir / "coordinator-sessions" / "publish-repo-bundle-last.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        json.dumps({"exit_code": 4, "head": "deadbeef", "line": "prior refusal", "finished_at": "x"}),
        encoding="utf-8",
    )
    calls = _calls_recorder(monkeypatch, tool, [(0, "", "")])
    monkeypatch.setattr(tool, "_git_common_dir", lambda root: common_dir)
    monkeypatch.setattr(tool, "_current_head", lambda root: "deadbeef")

    result = tool.run("handoff", repo_root, cli_override=str(fake_cli))

    assert result == "publish-repo-bundle: skipped -- last publish exited 4 at this HEAD, not retried"
    assert len(calls) == 1  # rebuild ran, publish did not


def test_publish_exit4_different_head_is_retried(tool, monkeypatch, tmp_path, fake_cli):
    repo_root = tmp_path / "target-repo"
    repo_root.mkdir()
    common_dir = tmp_path / "common"
    record_path = common_dir / "coordinator-sessions" / "publish-repo-bundle-last.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(
        json.dumps({"exit_code": 4, "head": "oldhead", "line": "prior refusal", "finished_at": "x"}),
        encoding="utf-8",
    )
    calls = _calls_recorder(monkeypatch, tool, [(0, "", ""), (0, "", "")])
    monkeypatch.setattr(tool, "_git_common_dir", lambda root: common_dir)
    monkeypatch.setattr(tool, "_current_head", lambda root: "newhead")

    tool.run("handoff", repo_root, cli_override=str(fake_cli))

    assert len(calls) == 2  # both rebuild and publish ran


def test_publish_verb_absent_advisory_line(tool, monkeypatch, tmp_path, fake_cli):
    repo_root = tmp_path / "target-repo"
    repo_root.mkdir()
    _calls_recorder(
        monkeypatch,
        tool,
        [(0, "", ""), (2, "", "argument verb: invalid choice: 'publish-repo-bundle'")],
    )
    monkeypatch.setattr(tool, "_git_common_dir", lambda root: tmp_path / "common")
    monkeypatch.setattr(tool, "_current_head", lambda root: "deadbeef")

    result = tool.run("handoff", repo_root, cli_override=str(fake_cli))

    assert result == "publish-repo-bundle: skipped -- verb absent (installed CLI lacks publish-repo-bundle)"


def test_publish_generic_failure_skips(tool, monkeypatch, tmp_path, fake_cli):
    repo_root = tmp_path / "target-repo"
    repo_root.mkdir()
    _calls_recorder(monkeypatch, tool, [(0, "", ""), (1, "", "some generic failure")])
    monkeypatch.setattr(tool, "_git_common_dir", lambda root: tmp_path / "common")
    monkeypatch.setattr(tool, "_current_head", lambda root: "deadbeef")

    result = tool.run("handoff", repo_root, cli_override=str(fake_cli))

    assert result.startswith("publish-repo-bundle: skipped -- exited 1")
    assert "some generic failure" in result


def test_no_console_creationflags_used_by_run_and_wait(tool, monkeypatch, tmp_path):
    """Assert the real `_run_and_wait` (not the monkeypatched fake used above)
    passes `no_console_creationflags()` kwargs into `subprocess.Popen`."""
    captured = {}

    class _FakeProc:
        returncode = 0

        def communicate(self, timeout=None):
            return "", ""

    def fake_popen(argv, **kwargs):
        captured.update(kwargs)
        return _FakeProc()

    monkeypatch.setattr(tool.subprocess, "Popen", fake_popen)
    tool._run_and_wait(["irrelevant"], tmp_path, 5)

    expected = tool.no_console_creationflags()
    for key, value in expected.items():
        assert captured.get(key) == value


def _lease(tmp_path):
    """Where `_run_and_wait` puts the single-rebuild lease for a cwd with no `.git`."""
    return tmp_path / "coordinator-sessions" / "scip-rebuild-lease.json"


def _forbid_popen(tool, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("Popen reached")

    monkeypatch.setattr(tool.subprocess, "Popen", boom)


def _patch_admission(monkeypatch, admitted):
    import coordinator_core.bash_guards._heavy_command_class as hcc

    verdict = hcc.SpawnAdmission(admitted, "HOLD scip-rebuild: test" if not admitted else "ADMIT", "scip-rebuild", 4.0)
    monkeypatch.setattr(hcc, "admit_before_spawn", lambda key, peak: verdict)
    monkeypatch.setattr(hcc, "register_spawn", lambda *a, **k: None)


def _fake_pids(tool, monkeypatch, alive, started=None):
    killed = []
    monkeypatch.setattr(tool, "_pid_alive", lambda pid: pid in alive)
    monkeypatch.setattr(tool, "_proc_started", lambda pid: (started or {}).get(pid))
    monkeypatch.setattr(tool, "_kill_tree", killed.append)
    return killed


def test_second_concurrent_rebuild_is_deduped_without_spawning(tool, monkeypatch, tmp_path):
    _fake_pids(tool, monkeypatch, alive={9001})
    lease = _lease(tmp_path)
    lease.parent.mkdir(parents=True)
    lease.write_text(json.dumps({"sequencer_pid": 9001, "child_pid": None}), encoding="utf-8")
    _forbid_popen(tool, monkeypatch)
    code, _out, err = tool._run_and_wait(["cli", "scip-rebuild"], tmp_path, 5)
    assert code is None and err.startswith("dedupe: ")
    assert lease.is_file(), "the live holder's lease must survive a deduped caller"


def test_dead_sequencers_orphaned_rebuild_is_reaped_and_lease_retaken(tool, monkeypatch, tmp_path):
    import os

    killed = _fake_pids(tool, monkeypatch, alive={9002})
    lease = _lease(tmp_path)
    lease.parent.mkdir(parents=True)
    lease.write_text(
        json.dumps({"sequencer_pid": 9001, "child_pid": 9002, "child_started": None}), encoding="utf-8"
    )
    assert tool._claim_lease(lease) is None
    assert killed == [9002]
    assert json.loads(lease.read_text(encoding="utf-8"))["sequencer_pid"] == os.getpid()


def test_recycled_child_pid_is_not_killed(tool, monkeypatch, tmp_path):
    killed = _fake_pids(tool, monkeypatch, alive={9002}, started={9002: 500.0})
    lease = _lease(tmp_path)
    lease.parent.mkdir(parents=True)
    lease.write_text(
        json.dumps({"sequencer_pid": 9001, "child_pid": 9002, "child_started": 1.0}), encoding="utf-8"
    )
    assert tool._claim_lease(lease) is None
    assert killed == []


def test_refused_admission_spawns_nothing_and_frees_the_lease(tool, monkeypatch, tmp_path):
    _patch_admission(monkeypatch, admitted=False)
    _forbid_popen(tool, monkeypatch)
    code, _out, err = tool._run_and_wait(["cli", "scip-rebuild"], tmp_path, 5)
    assert code is None and err.startswith("admission: HOLD")
    assert not _lease(tmp_path).exists()


def test_timeout_kills_the_rebuild_tree_and_frees_the_lease(tool, monkeypatch, tmp_path):
    import subprocess

    _patch_admission(monkeypatch, admitted=True)
    killed = []

    class _Hung:
        pid = 4242

        def communicate(self, timeout=None):
            raise subprocess.TimeoutExpired("cli", timeout)

        def poll(self):
            return None

    monkeypatch.setattr(tool.subprocess, "Popen", lambda argv, **kw: _Hung())
    monkeypatch.setattr(tool, "_kill_tree", killed.append)
    monkeypatch.setattr(tool, "_bind_child_to_caller", lambda proc: None)
    code, _out, err = tool._run_and_wait(["cli", "scip-rebuild"], tmp_path, 1)
    assert (code, err) == (None, "")
    assert killed == [4242]
    assert not _lease(tmp_path).exists()


def test_admitted_rebuild_is_leased_while_running_then_released(tool, monkeypatch, tmp_path):
    _patch_admission(monkeypatch, admitted=True)
    seen = {}

    class _Proc:
        pid = 5151
        returncode = 0

        def communicate(self, timeout=None):
            seen["lease"] = json.loads(_lease(tmp_path).read_text(encoding="utf-8"))
            return "", ""

        def poll(self):
            return 0

    monkeypatch.setattr(tool.subprocess, "Popen", lambda argv, **kw: _Proc())
    monkeypatch.setattr(tool, "_bind_child_to_caller", lambda proc: None)
    assert tool._run_and_wait(["cli", "scip-rebuild"], tmp_path, 5)[0] == 0
    assert seen["lease"]["child_pid"] == 5151
    assert not _lease(tmp_path).exists()


def test_run_reports_dedupe_and_does_not_publish(tool, monkeypatch, tmp_path, fake_cli):
    monkeypatch.setattr(tool, "_git_common_dir", lambda root: None)
    calls = _calls_recorder(monkeypatch, tool, [(None, "", "dedupe: a rebuild is already running (sequencer pid 7)")])
    result = tool.run("handoff", tmp_path, cli_override=str(fake_cli))
    assert result.startswith("publish-repo-bundle: skipped -- scip-rebuild deduped")
    assert len(calls) == 1


def test_main_always_exits_zero_on_registry_failure(tool, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(tool, "_resolve_registry_key", lambda key: (None, "registry key unset"))
    exit_code = tool.main(["--ceremony", "handoff", "--repo-root", str(tmp_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "skipped" in out


def test_main_exits_zero_even_on_unexpected_exception(tool, monkeypatch, tmp_path, capsys):
    def _boom(ceremony, repo_root, cli_override=None, repo_slug=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(tool, "run", _boom)
    exit_code = tool.main(["--ceremony", "handoff", "--repo-root", str(tmp_path)])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert "unexpected error" in out


def test_main_passes_cli_and_repo_through(tool, monkeypatch, tmp_path):
    captured = {}

    def fake_run(ceremony, repo_root, cli_override=None, repo_slug=None):
        captured["ceremony"] = ceremony
        captured["repo_root"] = repo_root
        captured["cli_override"] = cli_override
        captured["repo_slug"] = repo_slug
        return "publish-repo-bundle: published"

    monkeypatch.setattr(tool, "run", fake_run)
    exit_code = tool.main(
        [
            "--ceremony",
            "handoff",
            "--repo-root",
            str(tmp_path),
            "--cli",
            "/path/to/cli",
            "--repo",
            "acme/widgets",
        ]
    )
    assert exit_code == 0
    assert captured["cli_override"] == "/path/to/cli"
    assert captured["repo_slug"] == "acme/widgets"
