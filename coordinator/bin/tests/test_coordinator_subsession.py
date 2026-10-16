"""Tier T tests for `coordinator/bin/coordinator-subsession.py` -- the
refusing launcher's preflight and spawn behavior. All tmp_path fixtures; the
engine and guard-plane resolvers are monkeypatched to keep this tier-T, and
`claude` is stood in for by a Python stub that records its own argv/env."""

from __future__ import annotations

import importlib.util
import json
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

# C6's own tests git-init a scratch `tmp_path` project (`_git_init`) to give
# the pre/post-spawn git snapshot something real to diff -- the module under
# test's own `_git_snapshot` shells out to `git`, so this whole file spawns.
pytestmark = [pytest.mark.spawns_process]

_MODULE_PATH = Path(__file__).resolve().parents[1] / "coordinator-subsession.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("coordinator_subsession", _MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


launcher = _load_module()


class _FakeEngineRootOK:
    def __init__(self, root: Path) -> None:
        self._root = root

    def resolve_claude_klabauter_root(self):
        return str(self._root)


class _FakeEngineRootMissing:
    def resolve_claude_klabauter_root(self):
        return None


def _stub_claude(tmp_path: Path) -> Path:
    """A `claude` stand-in that records its own argv and env to a JSON file
    named by the RECORD_PATH env var, optionally writes fixture files named
    by the STUB_ACTIONS env var (a JSON list of `[abs_path, content]` pairs,
    simulating the child authoring plans/sizing objects), echoes a fixed
    marker to stdout, and exits with the code named by EXIT_CODE (default 0)."""
    stub = tmp_path / "stub_claude.py"
    body = textwrap.dedent(
        """\
        import json
        import os
        import sys

        actions = os.environ.get("STUB_ACTIONS")
        if actions:
            for path, content in json.loads(actions):
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(content)

        record_path = os.environ["RECORD_PATH"]
        with open(record_path, "w", encoding="utf-8") as fh:
            json.dump({"argv": sys.argv[1:], "env": dict(os.environ)}, fh)
        sys.stdout.buffer.write(b"stub-claude-stdout-marker\\n")
        sys.stdout.flush()
        sys.exit(int(os.environ.get("EXIT_CODE", "0")))
        """
    )
    stub.write_text(f"#!{sys.executable}\n" + body, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    if sys.platform == "win32":
        shim = tmp_path / "stub_claude.cmd"
        shim.write_text(f'@"{sys.executable}" "{stub}" %*\r\n', encoding="utf-8", newline="")
        return shim
    return stub


def _armed_config(config_dir: Path) -> None:
    """A minimal settings-delivery guard plane, armed -- mirrors
    `test_hook_plane_probe.py::test_settings_only_armed`'s fixture."""
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {"hooks": [{"command": "python3 coordinator/hooks/scripts/guard-foo.py"}]}
                    ]
                }
            }
        ),
        encoding="utf-8",
    )


def _engine_root_dir(tmp_path: Path) -> Path:
    root = tmp_path / "engine-root"
    (root / "coordinator_core").mkdir(parents=True, exist_ok=True)
    return root


def test_unarmed_config_root_refuses_and_stub_never_runs(tmp_path, monkeypatch, capfd):
    config_dir = tmp_path / "config"
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    # deliberately empty config_dir -- no settings, no plugin

    stub = _stub_claude(tmp_path)
    record_path = tmp_path / "record.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_BIN", str(stub))
    monkeypatch.setenv("RECORD_PATH", str(record_path))

    rc = launcher.main(["--cwd", str(project_dir), "--", "-p", "hello"])

    assert rc == 3
    assert not record_path.exists()
    captured = capfd.readouterr()
    assert "refused:" in captured.err


def test_missing_hook_boot_names_the_file(tmp_path, monkeypatch, capfd):
    config_dir = tmp_path / "config"
    project_dir = tmp_path / "project"
    install_dir = tmp_path / "install"
    project_dir.mkdir(parents=True, exist_ok=True)

    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(
        json.dumps({"enabledPlugins": {"coordinator@some-market": True}}), encoding="utf-8"
    )
    (config_dir / "plugins").mkdir(parents=True, exist_ok=True)
    (config_dir / "plugins" / "installed_plugins.json").write_text(
        json.dumps({"plugins": {"coordinator@some-market": [{"installPath": str(install_dir)}]}}),
        encoding="utf-8",
    )
    (install_dir / "hooks").mkdir(parents=True, exist_ok=True)
    (install_dir / "hooks" / "hooks.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "${CLAUDE_PLUGIN_ROOT}/hooks/_hook_boot.py",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    # deliberately never create install_dir/hooks/_hook_boot.py

    stub = _stub_claude(tmp_path)
    record_path = tmp_path / "record.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_BIN", str(stub))
    monkeypatch.setenv("RECORD_PATH", str(record_path))

    rc = launcher.main(["--cwd", str(project_dir), "--", "-p", "hello"])

    assert rc == 3
    assert not record_path.exists()
    captured = capfd.readouterr()
    assert "_hook_boot.py" in captured.err


def test_engine_unresolvable_refuses(tmp_path, monkeypatch, capfd):
    config_dir = tmp_path / "config"
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _armed_config(config_dir)

    monkeypatch.setattr(launcher, "_load_engine_root", lambda: _FakeEngineRootMissing())

    stub = _stub_claude(tmp_path)
    record_path = tmp_path / "record.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_BIN", str(stub))
    monkeypatch.setenv("RECORD_PATH", str(record_path))

    rc = launcher.main(["--cwd", str(project_dir), "--", "-p", "hello"])

    assert rc == 3
    assert not record_path.exists()
    captured = capfd.readouterr()
    assert "refused:" in captured.err
    assert "engine" in captured.err


def test_armed_spawns_stub_with_marker_env_and_propagates_exit_code(tmp_path, monkeypatch, capfd):
    config_dir = tmp_path / "config"
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _armed_config(config_dir)

    engine_root = _engine_root_dir(tmp_path)
    monkeypatch.setattr(launcher, "_load_engine_root", lambda: _FakeEngineRootOK(engine_root))

    stub = _stub_claude(tmp_path)
    record_path = tmp_path / "record.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_BIN", str(stub))
    monkeypatch.setenv("RECORD_PATH", str(record_path))
    monkeypatch.setenv("EXIT_CODE", "7")
    monkeypatch.setattr(sys, "executable", sys.executable)

    rc = launcher.main(["--cwd", str(project_dir), "--", "-p", "hello", "world"])

    assert rc == 7
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["argv"] == ["-p", "hello", "world"]
    assert record["env"].get("COORDINATOR_SUBSESSION_FIDELITY") == "1"

    captured = capfd.readouterr()
    assert captured.out == "stub-claude-stdout-marker\n"


class _FakeSizingDirModule:
    """Stands in for `coordinator-doc-new.py`: `_default_output_path` returns
    a fixed `state/sizings/...` relative path regardless of arguments."""

    @staticmethod
    def _default_output_path(doc_type, title, topic, **kwargs):
        assert doc_type == "sizing-object"
        return "state/sizings/2026-09-26-probe.yaml"


class _FakeMisePrepGate:
    """Stands in for `mise-prep-gate.py`: verdicts are keyed by plan filename
    via VERDICT_BY_NAME, defaulting to PREPPED."""

    PREPPED = "PREPPED"
    NOT_PREPPED = "NOT-PREPPED"
    ENGINE_ERROR = "ENGINE-ERROR"
    VERDICT_BY_NAME: dict = {}

    @staticmethod
    def corpus_inputs(repo_root):
        return object()

    @classmethod
    def prep_gate(cls, plan_path, corpus):
        verdict = cls.VERDICT_BY_NAME.get(Path(plan_path).name, cls.PREPPED)
        return {"verdict": verdict}

    @staticmethod
    def _frontmatter(text):
        return ({}, None)


def _git_init(repo_root: Path) -> None:
    no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(["git", "init", "-q", str(repo_root)], check=True, creationflags=no_window)
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.email", "t@example.com"],
        check=True, creationflags=no_window,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.name", "t"],
        check=True, creationflags=no_window,
    )
    (repo_root / "README.md").write_text("x", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo_root), "add", "README.md"],
        check=True, creationflags=no_window,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "commit", "-q", "-m", "init"],
        check=True, creationflags=no_window,
    )


def _armed_run(tmp_path, monkeypatch, project_dir, fidelity_report, extra_flags=None, exit_code="0", actions=None):
    config_dir = tmp_path / "config"
    _armed_config(config_dir)
    engine_root = _engine_root_dir(tmp_path)
    monkeypatch.setattr(launcher, "_load_engine_root", lambda: _FakeEngineRootOK(engine_root))
    monkeypatch.setattr(launcher, "_load_coordinator_doc_new", lambda root: _FakeSizingDirModule)
    monkeypatch.setattr(launcher, "_load_mise_prep_gate", lambda: _FakeMisePrepGate)

    stub = _stub_claude(tmp_path)
    record_path = tmp_path / "record.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_BIN", str(stub))
    monkeypatch.setenv("RECORD_PATH", str(record_path))
    monkeypatch.setenv("EXIT_CODE", exit_code)
    if actions is not None:
        monkeypatch.setenv("STUB_ACTIONS", json.dumps(actions))

    argv = ["--cwd", str(project_dir)]
    if fidelity_report is not None:
        argv += ["--fidelity-report", str(fidelity_report)]
    if extra_flags:
        argv += extra_flags
    argv += ["--", "-p", "hello"]

    rc = launcher.main(argv)
    return rc


def test_fired_and_clean_reports_sizing_and_prepped_plan(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _git_init(project_dir)
    _FakeMisePrepGate.VERDICT_BY_NAME = {}

    sizing_path = project_dir / "state" / "sizings" / "2026-09-26-probe.yaml"
    plan_path = project_dir / "docs" / "plans" / "2026-09-26-new-plan.md"
    actions = [
        [str(sizing_path), "name: probe\n"],
        [str(plan_path), "---\ntitle: x\n---\nbody\n"],
    ]

    report_path = tmp_path / "fidelity-report.json"
    rc = _armed_run(tmp_path, monkeypatch, project_dir, report_path, actions=actions)

    assert rc == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["sizing_fired"] is True
    assert report["plans"] == [{"path": "docs/plans/2026-09-26-new-plan.md", "prep_gate": "PREPPED"}]
    # the stub's own new files leave the tree dirty (untracked) -- a genuinely
    # "fired and clean" child would commit them, which this fixture doesn't.
    assert report["diff_bearing"] is True
    assert report["terminal_gate"] == "pending-R8"


def test_no_sizing_reports_sizing_fired_false(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _git_init(project_dir)
    _FakeMisePrepGate.VERDICT_BY_NAME = {}

    report_path = tmp_path / "fidelity-report.json"
    rc = _armed_run(tmp_path, monkeypatch, project_dir, report_path)

    assert rc == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["sizing_fired"] is False
    assert report["plans"] == []


def test_a_failing_plan_is_reported_non_prepped(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _git_init(project_dir)
    _FakeMisePrepGate.VERDICT_BY_NAME = {"2026-09-26-bad-plan.md": "NOT-PREPPED"}

    plan_path = project_dir / "docs" / "plans" / "2026-09-26-bad-plan.md"
    actions = [[str(plan_path), "---\ntitle: x\n---\nbody\n"]]

    report_path = tmp_path / "fidelity-report.json"
    rc = _armed_run(tmp_path, monkeypatch, project_dir, report_path, actions=actions)

    assert rc == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["plans"] == [
        {"path": "docs/plans/2026-09-26-bad-plan.md", "prep_gate": "NOT-PREPPED"}
    ]


def test_require_fidelity_exits_4_on_a_fidelity_miss(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _git_init(project_dir)
    _FakeMisePrepGate.VERDICT_BY_NAME = {}

    report_path = tmp_path / "fidelity-report.json"
    rc = _armed_run(
        tmp_path,
        monkeypatch,
        project_dir,
        report_path,
        extra_flags=["--require-fidelity"],
        exit_code="0",
    )

    # no sizing fired -> a fidelity miss, on an otherwise-clean (exit 0) child
    assert rc == 4
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["sizing_fired"] is False


def test_no_git_repo_reports_diff_bearing_null(tmp_path, monkeypatch):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    # deliberately never git-init project_dir
    _FakeMisePrepGate.VERDICT_BY_NAME = {}

    report_path = tmp_path / "fidelity-report.json"
    rc = _armed_run(tmp_path, monkeypatch, project_dir, report_path)

    assert rc == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["diff_bearing"] is None


def test_no_fidelity_report_writes_nothing_and_stderr_is_only_the_stub(tmp_path, monkeypatch, capfd):
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    _git_init(project_dir)
    _FakeMisePrepGate.VERDICT_BY_NAME = {}

    rc = _armed_run(tmp_path, monkeypatch, project_dir, fidelity_report=None)

    assert rc == 0
    captured = capfd.readouterr()
    assert captured.err == ""
    for entry in tmp_path.iterdir():
        assert not entry.name.endswith(".json") or entry.name == "record.json"


def test_require_fidelity_without_report_is_a_usage_error(tmp_path, monkeypatch, capfd):
    config_dir = tmp_path / "config"
    project_dir = tmp_path / "project"
    project_dir.mkdir(parents=True, exist_ok=True)
    # deliberately never arm the guard plane -- this must fail BEFORE preflight

    stub = _stub_claude(tmp_path)
    record_path = tmp_path / "record.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.setenv("CLAUDE_BIN", str(stub))
    monkeypatch.setenv("RECORD_PATH", str(record_path))

    rc = launcher.main(
        ["--cwd", str(project_dir), "--require-fidelity", "--", "-p", "hello"]
    )

    assert rc == 2
    assert not record_path.exists()
    captured = capfd.readouterr()
    assert "require-fidelity" in captured.err
