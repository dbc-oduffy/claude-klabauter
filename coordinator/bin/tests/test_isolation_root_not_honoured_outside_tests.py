from __future__ import annotations
"""
test_isolation_root_not_honoured_outside_tests.py — regression net for the
inherited-test-knob misroute.

`QUEUE_APPEND_OUTPUT_ROOT` / `LESSON_PROMOTE_OUTBOX_ROOT` redirect a queue
write's root. They are a property of a CALLING process that means to redirect
its own writes; nothing confined them to one. A live session that inherited one
-- from a shell descended from a test run, or from a warm engine spawned by one
-- silently wrote real improvement-queue entries into
`Temp/harvest-test-*/state/improvement-queue/` while the CLI printed a
plausible path and exited 0, twice, on 2026-08-31
(state/bug-backlog/2026-08-31-coordinator-queue-append-writes-into-a-s-1236db3da983.yaml).

The gate: honour the override only when `PYTEST_CURRENT_TEST` is also present,
i.e. only when this process is genuinely under test. Every in-repo setter of
these vars either uses `monkeypatch.setenv` (in-process) or hands a subprocess
`dict(os.environ)` / `os.environ.copy()`, both of which carry
`PYTEST_CURRENT_TEST` -- so no legitimate caller loses the redirect.

The end-to-end leg spawns the real CLI with the override set and
`PYTEST_CURRENT_TEST` STRIPPED, reproducing an inheriting session exactly, and
asserts the entry does NOT land under the override root.
"""

import os
import subprocess
import sys
import tempfile

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_BIN_DIR = os.path.dirname(_THIS_DIR)
_LIB_DIR = os.path.join(_BIN_DIR, "lib")
_REPO_ROOT = os.path.dirname(os.path.dirname(_BIN_DIR))

_QUEUE_APPEND_CLI = os.path.join(_BIN_DIR, "coordinator-queue-append.py")
_SUBPROCESS_TIMEOUT_SECS = 60


def _cli_shared():
    if _LIB_DIR not in sys.path:
        sys.path.insert(0, _LIB_DIR)
    import cli_shared

    return cli_shared


def test_override_honoured_when_under_test(monkeypatch, tmp_path) -> None:
    cli_shared = _cli_shared()
    monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.setenv(cli_shared.UNDER_TEST_ENV, "some::test (call)")
    resolved = cli_shared.isolation_root_if_under_test(
        "QUEUE_APPEND_OUTPUT_ROOT", caller_name="unit"
    )
    assert resolved == str(tmp_path)


def test_override_dropped_when_not_under_test(monkeypatch, tmp_path, capsys) -> None:
    cli_shared = _cli_shared()
    # A fresh warn-set per case: the helper warns once per var per process, so a
    # sibling case in the same worker would otherwise swallow the stderr line.
    monkeypatch.setattr(cli_shared, "_ISOLATION_ROOT_WARNED", set())
    monkeypatch.setenv("QUEUE_APPEND_OUTPUT_ROOT", str(tmp_path))
    monkeypatch.delenv(cli_shared.UNDER_TEST_ENV, raising=False)
    resolved = cli_shared.isolation_root_if_under_test(
        "QUEUE_APPEND_OUTPUT_ROOT", caller_name="unit"
    )
    assert resolved is None
    err = capsys.readouterr().err
    assert "QUEUE_APPEND_OUTPUT_ROOT" in err, (
        "dropping the override must not be silent -- silence is the defect this "
        f"gate exists to close. stderr={err!r}"
    )


def test_unset_override_is_silent(monkeypatch) -> None:
    cli_shared = _cli_shared()
    monkeypatch.setattr(cli_shared, "_ISOLATION_ROOT_WARNED", set())
    monkeypatch.delenv("QUEUE_APPEND_OUTPUT_ROOT", raising=False)
    assert (
        cli_shared.isolation_root_if_under_test(
            "QUEUE_APPEND_OUTPUT_ROOT", caller_name="unit"
        )
        is None
    )


def test_inherited_override_does_not_capture_a_real_write(stamped_engine_env: str) -> None:
    """The whole P1, end to end: a session that merely INHERITED the knob must
    not have its queue entry diverted into the override root.
    """
    name = "test_inherited_override_does_not_capture_a_real_write"
    override_root = tempfile.mkdtemp(prefix="inherited-knob-")
    repo_dir = tempfile.mkdtemp(prefix="inherited-knob-repo-")
    try:
        subprocess.run(
            ["git", "init", "-q"], cwd=repo_dir, capture_output=True, text=True, timeout=10
        )
        env = dict(os.environ)
        env["QUEUE_APPEND_OUTPUT_ROOT"] = override_root
        # This is the reproduction: an inheriting session is NOT under pytest.
        env.pop("PYTEST_CURRENT_TEST", None)
        # The stamped engine the box actually dispatches to — the source
        # checkout carries no build stamp and the stamp gate refuses it.
        env["COORDINATOR_ENGINE_ROOT"] = stamped_engine_env

        result = subprocess.run(
            [
                sys.executable,
                _QUEUE_APPEND_CLI,
                "--schema",
                "bug-backlog",
                "--title",
                "inherited knob must not divert this write",
                "--body",
                "regression fixture for the inherited-test-knob misroute",
                "--surface",
                "coordinator/bin/coordinator-queue-append.py",
                "--severity",
                "P3",
                "--status",
                "open",
                "--from-repo",
                "claude-klabauter-em",
            ],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            env=env,
            timeout=_SUBPROCESS_TIMEOUT_SECS,
        )

        diverted = os.path.join(override_root, "state", "bug-backlog")
        if os.path.isdir(diverted) and os.listdir(diverted):
            raise AssertionError(
                f"{name}: the inherited override captured the write -- entry landed in "
                f"{diverted} ({os.listdir(diverted)}). stdout={result.stdout!r}"
            )
        if "QUEUE_APPEND_OUTPUT_ROOT" not in result.stderr:
            raise AssertionError(
                f"{name}: dropping an inherited override must name it on stderr. "
                f"stderr={result.stderr!r}"
            )
    finally:
        import shutil

        shutil.rmtree(override_root, ignore_errors=True)
        shutil.rmtree(repo_dir, ignore_errors=True)
