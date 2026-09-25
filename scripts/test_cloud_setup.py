"""scripts/test_cloud_setup.py — the prime exit criterion's falsifier.

Covers the three dispositions that break scripts/cloud_setup.py (docs/plans/
2026-09-06-a-deterministic-cloud-install-for-the-engine.md § C4): a closed stdin,
every network step failing, and a scratch HOME that disagrees with the work
directory. Also pins the argv `cloud_setup.py` builds for `scripts/setup.py` and the
durable JSON install report.

Hermeticity, since this file lands in the repo's fast tier the moment it exists and
~50 concurrent sessions share this box: no real network call in any arm, no
un-stubbed subprocess, every filesystem write under `tmp_path`. `main()` is invoked
in-process (never as a real subprocess) so that `subprocess.run` itself can be
monkeypatched to a no-op stub rather than actually shelling out to `git`/`python3`.

`test_stubbed_run_produces_no_prompt_shaped_output` pins that a clean `main()` run
under fully-stubbed network steps writes no prompt-shaped text. It does not exercise
stdin -- this module never calls `input()` itself, and nothing on the stubbed path
reads `sys.stdin` -- so it no longer simulates a closed stdin (Finding 3,
`code-reviewer` 2026-09-06: the prior `_ClosedStdin` patch was provably vacuous,
since deleting it changed no assertion's outcome). The real closed-stdin contract on
the one subprocess this module hands a live stdin to is pinned directly instead, by
`test_run_claude_klabauter_setup_argv_names_coordinator_root`'s assertion that
`stdin=subprocess.DEVNULL` is present in the real `subprocess.run` kwargs.

Run: python3 -m pytest scripts/test_cloud_setup.py -q
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_CLOUD_SETUP_PY_PATH = Path(__file__).resolve().parent / "cloud_setup.py"


def _load_cloud_setup_module():
    spec = importlib.util.spec_from_file_location(
        "_scripts_cloud_setup_under_test", _CLOUD_SETUP_PY_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def cloud_mod():
    # Fresh module per test (not module-scoped): tests monkeypatch module-level
    # globals (CLONES, INSTALL_REPORT_PATH) and must not leak state across arms.
    return _load_cloud_setup_module()


@pytest.fixture(autouse=True)
def _isolate_host_paths(cloud_mod, tmp_path, monkeypatch):
    """Keep the engine CLI shims and the `/root/engine-current` link under
    `tmp_path`. Unisolated, any arm that runs `main()` rewrites the operator's
    live shims and re-points the live engine link at a pytest temp dir, which
    breaks every engine CLI on the box."""
    monkeypatch.setattr(cloud_mod, "SHIM_DIR", tmp_path / "host-shims")
    monkeypatch.setattr(cloud_mod, "ENGINE_CURRENT_LINK", tmp_path / "host-engine-current")


@pytest.fixture(autouse=True)
def _restore_process_env():
    """Undo `cloud_setup.set_engine_env`'s writes to the REAL process environment.

    The `cloud_mod` fixture isolates module globals but not `os.environ`, and
    `set_engine_env` deliberately mutates the live environment — that is its whole
    job (fact 2: the env-var block is unreadable from the setup script, so the
    script exports the values itself). Under pytest that write outlives the test.

    Measured 2026-09-06: without this,
    `test_setup.py::test_resolve_claude_klabauter_root_repo_root_default` fails roughly one
    run in three, resolving the engine root to THIS file's tmp_path — because
    `resolve_claude_klabauter_root` reads `COORDINATOR_ENGINE_ROOT` and a cloud arm had left
    one behind. It only fails when random ordering happens to run that test after
    this module, which is what made it look like a flake in a file this module does
    not touch. A cross-file leak from an autouse-free fixture, not flakiness.
    """
    import os

    snapshot = dict(os.environ)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(snapshot)


@pytest.fixture(autouse=True)
def _isolate_claude_home(tmp_path, monkeypatch):
    """Keep every `_claude_home()` write under `tmp_path`, for every arm.

    This module's hermeticity contract is "every filesystem write under
    `tmp_path`", and `main()` reaches two functions that write into the resolved
    `.claude` directory — `install_global_doctrine` and `write_session_verdict`.
    Autouse rather than per-test on purpose: the arm that leaked was the one
    asserting a non-Linux refusal, which — before it simulated the host rather
    than reading it — ran the FULL pipeline on a Linux host instead of refusing,
    so the arms that most need isolation are exactly the ones whose author did
    not expect to need it.

    Measured: without this, a run on a Linux box writes a real
    `cloud-preboot-verdict.md` into the operator's own `~/.claude/rules/`, where
    the harness then loads a pytest tmp_path's verdict into every later session
    as standing instruction.
    """
    monkeypatch.setenv("CLAUDE_HOME", str(tmp_path / "claude-home"))
    # Belt and braces: `_claude_home()` falls back to `Path.home()` (USERPROFILE on
    # Windows, HOME on POSIX) when CLAUDE_HOME is unset, so point those at tmp too.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "home"))


#: The operator's real `.claude` settings, resolved at import -- before any fixture
#: redirects the home -- so the tripwire below watches the file sessions actually load.
_REAL_CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"


def _real_settings_fingerprint():
    try:
        return _REAL_CLAUDE_SETTINGS.read_bytes()
    except OSError:
        return None


@pytest.fixture(autouse=True)
def _real_claude_home_tripwire():
    """Fail the arm that changes the operator's real `~/.claude/settings.json`.

    Measured 2026-09-23 on machine-a: that file carried a marketplace source under a
    pytest tmp dir (`...\\test_every_network_step_failin0\\coordinator-claude`)
    plus `enabledPlugins["coordinator@coordinator-claude"]` -- a plugin
    registration the PM forbids on a working box, written by this module's arms."""
    before = _real_settings_fingerprint()
    yield
    assert _real_settings_fingerprint() == before, (
        f"this test wrote the operator's real {_REAL_CLAUDE_SETTINGS}"
    )


def _make_scratch_clones(cloud_mod, tmp_path: Path) -> dict:
    # Derived from the real
    # CLONES dict rather than hand-copied, so a URL change in cloud_setup.py
    # cannot silently drift out of sync with what these tests exercise.
    return {
        name: {**spec, "dest": str(tmp_path / name)}
        for name, spec in cloud_mod.CLONES.items()
    }


def _patch_linux_root(monkeypatch, cloud_mod):
    monkeypatch.setattr(cloud_mod.platform, "system", lambda: "Linux")
    monkeypatch.setattr(cloud_mod.os, "geteuid", lambda: 0, raising=False)


def _patch_network_steps_ok(monkeypatch, cloud_mod):
    """Stub every network/subprocess-touching step to a hermetic no-op success."""
    monkeypatch.setattr(cloud_mod, "clone_repo", lambda name: None)

    def _fake_run_claude_klabauter_setup(report):
        report.container_optin_requested = True
        report.setup_exit_code = 0

    monkeypatch.setattr(cloud_mod, "run_claude_klabauter_setup", _fake_run_claude_klabauter_setup)
    monkeypatch.setattr(cloud_mod, "apply_settings_manifest_env", lambda report: None)


def _patch_network_steps_all_raise(monkeypatch, cloud_mod):
    def _raise_clone(name):
        raise RuntimeError(f"stubbed network failure: clone {name}")

    def _raise_setup(report):
        raise RuntimeError("stubbed network failure: run scripts/setup.py")

    monkeypatch.setattr(cloud_mod, "clone_repo", _raise_clone)
    monkeypatch.setattr(cloud_mod, "run_claude_klabauter_setup", _raise_setup)


# ---------------------------------------------------------------------------
# Arm: closed stdin (0<&-) -- the load-bearing arm
# ---------------------------------------------------------------------------


def test_stubbed_run_produces_no_prompt_shaped_output(monkeypatch, tmp_path, capsys, cloud_mod):
    _patch_linux_root(monkeypatch, cloud_mod)
    _patch_network_steps_ok(monkeypatch, cloud_mod)
    monkeypatch.setattr(cloud_mod, "CLONES", _make_scratch_clones(cloud_mod, tmp_path))
    monkeypatch.setattr(cloud_mod, "INSTALL_REPORT_PATH", tmp_path / "cloud-setup-report.json")

    rc = cloud_mod.main()

    assert rc == 0
    out = capsys.readouterr().out
    assert "?" not in out  # no prompt text was written to stdout
    assert "input(" not in out


# ---------------------------------------------------------------------------
# Arm: every network step stubbed to raise -- must still exit 0 and name each
# failed step in both the printed summary and the durable JSON report.
# ---------------------------------------------------------------------------


def test_every_network_step_failing_is_named_not_silent(monkeypatch, tmp_path, capsys, cloud_mod):
    _patch_linux_root(monkeypatch, cloud_mod)
    _patch_network_steps_all_raise(monkeypatch, cloud_mod)
    monkeypatch.setattr(cloud_mod, "CLONES", _make_scratch_clones(cloud_mod, tmp_path))
    report_path = tmp_path / "cloud-setup-report.json"
    monkeypatch.setattr(cloud_mod, "INSTALL_REPORT_PATH", report_path)

    rc = cloud_mod.main()

    assert rc == 0
    out = capsys.readouterr().out
    for step_name in (
        "clone coordinator-claude",
        "clone klabauter",
        "run scripts/setup.py",
    ):
        assert step_name in out
        assert "FAILED" in out

    assert report_path.exists()
    report = json.loads(report_path.read_text())
    steps_by_name = {s["name"]: s for s in report["steps"]}
    for step_name in (
        "clone coordinator-claude",
        "clone klabauter",
        "run scripts/setup.py",
    ):
        assert step_name in steps_by_name, f"{step_name} missing from report"
        assert steps_by_name[step_name]["ok"] is False
        assert steps_by_name[step_name]["detail"]  # non-empty diagnostic


# No scratch-HOME arm for `set_engine_env`: it reads only `CLONES`. Every OTHER
# `main()` step that writes the `.claude` directory resolves it through
# `_claude_home()`, which DOES fall back to the real home -- the autouse
# `_isolate_claude_home` and `_real_claude_home_tripwire` fixtures cover that.


# ---------------------------------------------------------------------------
# Arm: the argv cloud_setup.py builds for scripts/setup.py names
# --coordinator-root pointing at the CLONES coordinator-claude destination.
# ---------------------------------------------------------------------------


def test_run_claude_klabauter_setup_argv_names_coordinator_root(monkeypatch, tmp_path, cloud_mod):
    scratch_clones = _make_scratch_clones(cloud_mod, tmp_path)
    monkeypatch.setattr(cloud_mod, "CLONES", scratch_clones)

    captured_argv = {}
    captured_kwargs = {}

    class _FakeCompletedProcess:
        returncode = 0
        stdout = "--i-assert-no-other-consumer"
        stderr = ""

    def _fake_run(argv, **kwargs):
        captured_argv["argv"] = argv
        captured_kwargs.update(kwargs)
        return _FakeCompletedProcess()

    monkeypatch.setattr(cloud_mod.subprocess, "run", _fake_run)

    report = cloud_mod.Report()
    cloud_mod.run_claude_klabauter_setup(report)

    argv = captured_argv["argv"]
    assert "--coordinator-root" in argv
    idx = argv.index("--coordinator-root")
    assert argv[idx + 1] == scratch_clones["coordinator-claude"]["dest"]
    assert "--with-test-deps" in argv
    assert report.container_optin_requested is True
    assert report.setup_exit_code == 0
    # stdin=subprocess.DEVNULL
    # had zero coverage: this test's own _fake_run previously discarded kwargs,
    # so deleting the argument would not have failed anything.
    assert captured_kwargs.get("stdin") is cloud_mod.subprocess.DEVNULL


def test_run_claude_klabauter_setup_records_nonzero_exit_code(monkeypatch, tmp_path, cloud_mod):
    """DR-411's audit case: a PEP-668 refusal (exit 96) must be recoverable from
    the report, not just the raised exception's message."""
    scratch_clones = _make_scratch_clones(cloud_mod, tmp_path)
    monkeypatch.setattr(cloud_mod, "CLONES", scratch_clones)

    class _FakeCompletedProcess:
        returncode = 96
        stdout = ""
        stderr = "externally-managed-environment"

    monkeypatch.setattr(
        cloud_mod.subprocess, "run", lambda argv, **kwargs: _FakeCompletedProcess()
    )

    report = cloud_mod.Report()
    with pytest.raises(RuntimeError):
        cloud_mod.run_claude_klabauter_setup(report)

    assert report.container_optin_requested is True
    assert report.setup_exit_code == 96


# ---------------------------------------------------------------------------
# Non-Linux / non-root host precondition -- the arm that runs natively here.
# ---------------------------------------------------------------------------


def _patch_non_linux_host(monkeypatch, cloud_mod):
    """Simulate a non-Linux host for `host_precondition_met`.

    `cloud_setup.py` is loaded from file into a private module object, but its
    `platform` global is still the ONE shared `platform` module, so patching the
    attribute there is what the code under test reads. What does not work is
    patching `sys.platform` (nothing here reads it) or leaving the host
    unpatched: this arm used to do the latter and was therefore inert the moment
    it ran on a Linux container, silently exercising the whole pipeline instead
    of the refusal it names.
    """
    monkeypatch.setattr(cloud_mod.platform, "system", lambda: "Darwin")


def test_host_precondition_refuses_on_non_linux_and_records_nothing_executed(
    monkeypatch, tmp_path, cloud_mod
):
    _patch_non_linux_host(monkeypatch, cloud_mod)
    assert cloud_mod.host_precondition_met()[0] is False, (
        "the non-Linux simulation must reach the code under test, or this arm "
        "asserts nothing about the refusal path"
    )
    report_path = tmp_path / "cloud-setup-report.json"
    monkeypatch.setattr(cloud_mod, "INSTALL_REPORT_PATH", report_path)

    def _fail_if_called(*a, **kw):
        raise AssertionError("no step may run when the host precondition fails")

    monkeypatch.setattr(cloud_mod, "clone_repo", _fail_if_called)
    monkeypatch.setattr(cloud_mod, "run_claude_klabauter_setup", _fail_if_called)

    rc = cloud_mod.main()

    assert rc == 0
    assert report_path.exists()
    report = json.loads(report_path.read_text())
    assert len(report["steps"]) == 1
    assert report["steps"][0]["name"] == "host precondition"
    assert report["steps"][0]["ok"] is False


def test_host_precondition_refusal_still_writes_the_session_verdict_surface(
    monkeypatch, tmp_path, cloud_mod
):
    """F1 (code-reviewer 2026-09-17) -- the refusal branch used to call only
    `_write_report_best_effort`, never `_record_session_surfaces_best_effort`,
    so the single most severe pre-boot outcome left no `cloud-preboot-verdict.md`
    for a session that has no other way to learn nothing ran. A separate test
    from the non-Linux arm above, which simulates the host and exercises the
    real `host_precondition_met`: here the predicate itself is stubbed, so the
    verdict surface is pinned independently of how the refusal was reached.
    """
    monkeypatch.setattr(
        cloud_mod, "host_precondition_met", lambda: (False, "host precondition failed: stubbed")
    )
    report_path = tmp_path / "cloud-setup-report.json"
    monkeypatch.setattr(cloud_mod, "INSTALL_REPORT_PATH", report_path)

    rc = cloud_mod.main()

    assert rc == 0
    claude_home = Path(cloud_mod.os.environ["CLAUDE_HOME"]) / ".claude"
    verdict_path = claude_home / "rules" / cloud_mod.SESSION_VERDICT_RULE
    assert verdict_path.exists(), "the refusal is the case that most needs the session surface"
    body = verdict_path.read_text()
    assert "host precondition" in body


# ---------------------------------------------------------------------------
# Arm: a non-ASCII byte in a step's recorded detail must not raise past
# `_print_summary`, which `run_step`'s exception net does not wrap (Finding 1,
# `code-reviewer` 2026-09-06).
# ---------------------------------------------------------------------------


class _AsciiOnlyStdout:
    """A stand-in for a stdout stream on a minimal-locale host (LANG=C, no
    UTF-8): `.encoding` reports `ascii`, and writing a non-ASCII character
    raises `UnicodeEncodeError`, matching what a real `print()` would do
    there."""

    encoding = "ascii"

    def __init__(self):
        self.written: list[str] = []

    def write(self, s: str) -> int:
        s.encode("ascii")  # raises UnicodeEncodeError on any non-ASCII byte
        self.written.append(s)
        return len(s)

    def flush(self) -> None:
        pass


def test_non_ascii_step_detail_under_ascii_stdout_still_exits_zero(
    monkeypatch, tmp_path, cloud_mod
):
    _patch_linux_root(monkeypatch, cloud_mod)
    _patch_network_steps_ok(monkeypatch, cloud_mod)
    monkeypatch.setattr(cloud_mod, "CLONES", _make_scratch_clones(cloud_mod, tmp_path))
    monkeypatch.setattr(cloud_mod, "INSTALL_REPORT_PATH", tmp_path / "cloud-setup-report.json")

    def _raise_with_non_ascii_detail(report):
        raise RuntimeError("clone failed: dépôt introuvable")

    monkeypatch.setattr(cloud_mod, "run_claude_klabauter_setup", _raise_with_non_ascii_detail)

    fake_stdout = _AsciiOnlyStdout()
    monkeypatch.setattr(sys, "stdout", fake_stdout)

    rc = cloud_mod.main()

    assert rc == 0
    joined = "".join(fake_stdout.written)
    assert "run scripts/setup.py" in joined
    assert "FAILED" in joined


# ---------------------------------------------------------------------------
# publish.mirrors.* seeding — the key cloud never had
# ---------------------------------------------------------------------------


class _Ok:
    returncode = 0
    stdout = ""
    stderr = ""


def _publish_mirror_env(cloud_mod, monkeypatch, tmp_path, *, engine, mirror):
    """Pin the two things this step reads: where the engine clone is, and which
    roots a mirror checkout is searched under."""
    monkeypatch.setitem(cloud_mod.CLONES, "claude-klabauter", {"url": "u", "dest": str(engine)})
    monkeypatch.setattr(cloud_mod, "retrieval_search_roots", lambda: [tmp_path])
    monkeypatch.setattr(cloud_mod, "_machine_local_argv", lambda: ["machine-local"])
    calls: list = []

    def _run(argv, **kwargs):
        calls.append(argv)
        return _Ok()

    monkeypatch.setattr(cloud_mod.subprocess, "run", _run)
    return calls


def test_a_separate_mirror_checkout_is_registered(cloud_mod, monkeypatch, tmp_path):
    engine = tmp_path / "engine-clone"
    (engine / ".git").mkdir(parents=True)
    mirror = tmp_path / "claude-klabauter"
    (mirror / ".git").mkdir(parents=True)
    calls = _publish_mirror_env(cloud_mod, monkeypatch, tmp_path, engine=engine, mirror=mirror)

    report = cloud_mod.Report()
    cloud_mod.register_publish_mirror_keys(report)

    assert report.machine_local_keys["publish.mirrors.claude_klabauter.path"] == str(mirror)
    assert calls == [
        ["machine-local", "set", "publish.mirrors.claude_klabauter.path", str(mirror)]
    ]


def test_the_engine_clone_is_never_written_into_the_publish_key(
    cloud_mod, monkeypatch, tmp_path
):
    """The live trap, refused at the seeding layer: `repos.claude_klabauter` is
    the DEPLOYED ENGINE -- a real clone on `main` with a live push remote -- and
    a publish target resolving there pushes onto the published mirror's default
    branch with nothing warning. The two keys are different keys for exactly
    this reason, so the engine clone is rejected rather than registered even
    though it is a valid checkout of the right repository.
    """
    engine = tmp_path / "claude-klabauter"
    (engine / ".git").mkdir(parents=True)
    calls = _publish_mirror_env(cloud_mod, monkeypatch, tmp_path, engine=engine, mirror=None)

    report = cloud_mod.Report()
    cloud_mod.register_publish_mirror_keys(report)

    assert calls == []
    verdict = report.machine_local_keys["publish.mirrors.claude_klabauter.path"]
    assert verdict.startswith("skipped:")
    assert str(engine) in verdict


def test_a_container_with_no_mirror_checkout_records_why_and_does_not_fail(
    cloud_mod, monkeypatch, tmp_path
):
    """Non-fatal by construction: nothing else in the boot depends on this key,
    and failing the run over an absent publish mirror would break every
    container that has no reason to publish."""
    engine = tmp_path / "engine-clone"
    (engine / ".git").mkdir(parents=True)
    _publish_mirror_env(cloud_mod, monkeypatch, tmp_path, engine=engine, mirror=None)

    report = cloud_mod.Report()
    cloud_mod.register_publish_mirror_keys(report)

    assert "skipped" in report.machine_local_keys["publish.mirrors.claude_klabauter.path"]
