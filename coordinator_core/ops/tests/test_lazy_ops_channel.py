"""test_lazy_ops_channel.py — the two lazy-op-registration channels and their
precedence, plus the engine-side pytest-child env strip.

Subject: `coordinator_core/ops/__init__.py`'s gate on `_eager_import_all()`.

Two channels, and the precedence between them is the whole point:

  * `COORDINATOR_CORE_LAZY_OPS` in the environment is the OPERATOR override.
    It is authoritative in BOTH directions — "1" forces lazy, any other value
    forces eager. `coordinator/tests/test_install_substrate.sh` Test 15 depends
    on the eager direction: it compares a trampoline's output under
    `LAZY_OPS=1` against a `LAZY_OPS=0` baseline, and if the in-process channel
    could override `0` that test would silently compare lazy against lazy and
    stop detecting anything.
  * `sys._coordinator_core_lazy_ops` is the IN-PROCESS channel, set by the two
    writers that only ever need the flag for their own process
    (`coordinator_core/invoke/__main__.py`, `coordinator/bin/lib/cc_invoke.py`).

WHY THE IN-PROCESS CHANNEL IS NOT AN ENV VAR (2026-07-28). Both writers used to
write `os.environ`, which every child spawned without an explicit `env=`
inherits. 59 test modules in this tree assert the op registry at import time, so
any pytest child of such a process failed collection on a green tree. A `sys`
attribute is not inherited by anything, so the leak is structurally impossible
rather than patched per spawn site. Scoping study:
`docs/research/2026-07-28-lazy-ops-import-side-effect-scope.md` § 6 (c).

Negative-spec:
  - Does NOT test the ~108ms latency saving the flag exists for; that is
    `test_install_substrate.sh` Test 15's output-identity smoke plus the
    `-X importtime` check, not an assertion available in-process.
  - Does NOT cover the in-process ordering hazard (a worker importing a
    cc_invoke consumer before its first `coordinator_core.ops` import still
    gets an empty registry). That is knowingly out of scope — see
    `state/bug-backlog/2026-07-28-lazy-ops-testpaths-order-fragility.yaml`.
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]

_LAZY_OPS_ENV_KEY = "COORDINATOR_CORE_LAZY_OPS"

# Print the live registry size after importing the package under whichever
# channel the caller armed. Registry size, not the flag's value, is the
# behaviour under test — the flag is only interesting via what it does.
_REGISTRY_SIZE_SCRIPT = textwrap.dedent(
    """
    import coordinator_core.ops  # noqa: F401
    import coordinator_core.ipc as ipc
    print(len(ipc._REGISTRY))
    """
)

_ARM_SYS_ATTRIBUTE = "import sys; sys._coordinator_core_lazy_ops = True\n"


def _clean_env(**overrides: str) -> dict[str, str]:
    """This test process's env with the operator override removed.

    The pytest process running this file may itself carry the variable (an
    operator export, or a peer test's leftover); stripping it here is what makes
    each case below assert its own armed channel rather than an ambient one.
    """
    env = dict(os.environ)
    env.pop(_LAZY_OPS_ENV_KEY, None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), env["PYTHONPATH"]] if env.get("PYTHONPATH") else [str(REPO_ROOT)]
    )
    env.update(overrides)
    return env


def _registry_size(script_prefix: str = "", **env_overrides: str) -> int:
    proc = subprocess.run(
        [sys.executable, "-c", script_prefix + _REGISTRY_SIZE_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=str(REPO_ROOT),
        env=_clean_env(**env_overrides),
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, f"probe failed: {proc.stderr}"
    return int(proc.stdout.strip())


class TestLazyOpsChannelPrecedence:
    def test_neither_channel_set_registers_eagerly(self) -> None:
        assert _registry_size() > 0

    def test_sys_attribute_alone_suppresses_eager_registration(self) -> None:
        assert _registry_size(_ARM_SYS_ATTRIBUTE) == 0

    def test_env_var_alone_suppresses_eager_registration(self) -> None:
        """The operator override, read from outside the process — the path
        `test_install_substrate.sh` Test 15 exercises."""
        assert _registry_size(**{_LAZY_OPS_ENV_KEY: "1"}) == 0

    def test_operator_env_zero_beats_the_in_process_channel(self) -> None:
        """`LAZY_OPS=0` must force eager even when the in-process channel is
        armed, or Test 15's eager baseline degenerates into a second lazy run."""
        assert _registry_size(_ARM_SYS_ATTRIBUTE, **{_LAZY_OPS_ENV_KEY: "0"}) > 0

    def test_invoke_dispatch_arms_the_channel_without_touching_os_environ(self) -> None:
        """`coordinator_core/invoke/__main__.py:main()` needs the flag for its OWN
        process only. Writing it to `os.environ` handed it to every child the
        dispatched op later spawned — three op modules spawn pytest.

        Driven through `--dump-op-timeouts`, which short-circuits immediately
        after the arming statement, so this observes the writer and nothing else.
        `main()` ends in `os._exit()`, which would take the observation down with
        it, so the child swaps that for a `SystemExit` it can catch.
        """
        script = textwrap.dedent(
            """
            import contextlib, io, os, sys

            def _raise_instead_of_exit(code):
                raise SystemExit(code)

            os._exit = _raise_instead_of_exit
            from coordinator_core.invoke.__main__ import main

            sys.argv = ["invoke", "--dump-op-timeouts"]
            with contextlib.redirect_stdout(io.StringIO()):
                with contextlib.suppress(SystemExit):
                    main()
            print("ENV:", os.environ.get("COORDINATOR_CORE_LAZY_OPS"))
            print("SYS:", getattr(sys, "_coordinator_core_lazy_ops", None))
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=120,
            cwd=str(REPO_ROOT), env=_clean_env(),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        assert proc.returncode == 0, proc.stderr
        assert "ENV: None" in proc.stdout, f"invoke leaked the flag into os.environ: {proc.stdout}"
        assert "SYS: True" in proc.stdout, f"invoke did not arm the in-process channel: {proc.stdout}"


class TestEngineSpawnedPytestChildren:
    """The three `coordinator_core` op modules that spawn a pytest run.

    They cannot use `cc_invoke.child_env()` (wrong layer), and before
    2026-07-28 they passed no `env=` at all, so a pytest child inherited
    whatever the dispatch process carried.
    """

    def test_cutover_gate_reverify_strips_the_flag_from_its_pytest_child(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """End-to-end: the re-run must PASS against a test node that asserts the
        flag is absent, with the flag set in the dispatch process's own env."""
        from coordinator_core.ops import cutover_gate

        test_file = tmp_path / "test_flag_absent.py"
        test_file.write_text(
            "import os\n\n\n"
            "def test_lazy_ops_flag_absent():\n"
            "    assert os.environ.get('COORDINATOR_CORE_LAZY_OPS') is None\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(_LAZY_OPS_ENV_KEY, "1")

        ok, detail = cutover_gate._reverify_test_node_id(
            "test_flag_absent.py::test_lazy_ops_flag_absent", {"r": tmp_path}
        )
        assert ok, detail

    def test_copy_plugin_template_run_pytest_strips_the_flag(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from coordinator_core.ops import copy_plugin_template

        test_file = tmp_path / "test_flag_absent.py"
        test_file.write_text(
            "import os\n\n\n"
            "def test_lazy_ops_flag_absent():\n"
            "    assert os.environ.get('COORDINATOR_CORE_LAZY_OPS') is None\n",
            encoding="utf-8",
        )
        monkeypatch.setenv(_LAZY_OPS_ENV_KEY, "1")

        assert copy_plugin_template._run_pytest(test_file, tmp_path) is True

    def test_whoami_run_tests_passes_a_stripped_env_to_pytest(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Capture-based rather than end-to-end: reaching the pytest spawn for
        real means provisioning a venv and pip-installing a package, which is
        not what this assertion is about."""
        from coordinator_core.ops import whoami_run_tests

        venv_bin = tmp_path / ".venv" / "bin"
        venv_bin.mkdir(parents=True)
        (venv_bin / "python").touch()
        (tmp_path / ".venv" / ".deps-installed").touch()

        captured: dict[str, object] = {}

        def _fake_run(cmd, **kwargs):  # noqa: ANN001, ANN003
            captured["cmd"] = cmd
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(cmd, 0)

        monkeypatch.setattr(whoami_run_tests.subprocess, "run", _fake_run)
        monkeypatch.setenv(_LAZY_OPS_ENV_KEY, "1")

        assert whoami_run_tests.main([], base_dir=str(tmp_path)) == 0
        env = captured["env"]
        assert isinstance(env, dict), "pytest spawn passed no explicit env="
        assert _LAZY_OPS_ENV_KEY not in env
