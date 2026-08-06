"""
coordinator_core.session.tests.test_cli_transport — transport-fidelity tests
for coordinator_core.session.js_bridge_cli, spawned as claude-klabauter's OWN
subprocess (``python3 -m coordinator_core.session.js_bridge_cli <verb> ...``).

NEGATIVE-SPEC — READ BEFORE TOUCHING THIS FILE:
    - This is NOT a differential parity test. It does NOT compare against
      any bash or JS oracle, retired or otherwise.
    - It asserts TRANSPORT FIDELITY ONLY: that the spawn -> argv -> exit-code
      -> stdout boundary does not corrupt or drop the verdict the in-process
      ``coordinator_core.session.{liveness,claims}`` functions already
      produce for the identical fixture. The comparison target is this
      repo's OWN in-process implementation, never an external artifact.
    - Do NOT re-label any test/class here "parity". Do NOT add a example-doctrine-repo oracle,
      a example-doctrine-repo path, or a example-doctrine-repo env var to this file. There is deliberately no
      external dependency, and therefore no skip-on-missing-oracle hazard —
      that hazard (28 permanently-skipping assertions across four suites) is
      exactly what the 2026-07-22 parity-retire-fold plan removed; this file
      exists to close the one *residual* coverage gap that retirement opened
      (see plan file below, § 6/§ 7 C5), not to reopen it.
    - Semantic correctness of liveness/claim logic is covered by
      ``test_liveness.py`` / ``test_claims.py``. In-process CLI dispatch
      (argv validation, usage text, dedup-append behavior) is covered by
      ``test_js_bridge_cli.py`` via ``capsys`` — that file calls
      ``js_bridge_cli.main(argv)`` directly and therefore can never observe a
      spawn-boundary defect. THIS file is the only one in the repo that
      actually spawns the session CLI as an external process.

Why this file exists: the four real defects the 2026-07-22 session surfaced
(two ``real_home`` fixture gaps, a path-containment fixture violation, and an
``is_backfill`` caller-contract gap) all lived at a process/argv/env
boundary, not in pure function logic. A direct in-process handler call
cannot find that class of bug by construction. This file spawns the real CLI
entrypoint so that class of bug has somewhere to be caught.

Spec backlink: state/review-trail/findings/2026-07-22-parity-retire-fold-plan.md § 6, § 7 C5
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.session import claims
from coordinator_core.session import core
from coordinator_core.session import liveness
from coordinator_core.session import scope

_CLI_MODULE = "coordinator_core.session.js_bridge_cli"

# Repo root (four parents up from this file: tests/ -> session/ ->
# coordinator_core/ -> repo root), so the spawned ``-m`` invocation can
# resolve ``coordinator_core`` regardless of the CLI's own cwd (which is
# deliberately set to the FIXTURE repo, not this repo, in every test below).
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Env vars that would let the ambient test-harness session (this pytest
# process is itself very likely running inside a live Claude Code session)
# leak into a spawned CLI's session resolution. Stripped by default so every
# test's live/dead session set is attributable ONLY to the fixtures it wrote.
_SESSION_ENV_VARS = ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "COORDINATOR_SESSION_ID")


def _run_cli(repo, args, env=None):
    """Spawn the real ``js_bridge_cli`` entrypoint as a subprocess, cwd'd at
    ``repo``, with ambient session env vars stripped unless explicitly
    supplied via ``env``. This is the ONE seam in this file that actually
    crosses a process boundary — every assertion downstream of this call is
    exercising transport, not logic."""
    full_env = dict(os.environ)
    for key in _SESSION_ENV_VARS:
        full_env.pop(key, None)
    full_env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT)] + ([full_env["PYTHONPATH"]] if full_env.get("PYTHONPATH") else [])
    )
    if env:
        full_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", _CLI_MODULE, *args],
        cwd=str(repo),
        env=full_env,
        capture_output=True,
        text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _write_session(repo, sid, meta: dict):
    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
    return sdir


def _self_lstart_meta():
    """Build a live-session meta fixture off THIS test process's own birth
    instant, portable across POSIX (``ps -o lstart=``) and Windows
    (``core._win_create_time_epoch``) — mirrors ``test_js_bridge_cli.py``'s
    ``TestLiveSessionIds`` fixture so the in-process/subprocess comparison
    below exercises a genuinely live PID on both platforms, not a mock."""
    if core._IS_WINDOWS:
        epoch = core._win_create_time_epoch(os.getpid())
        assert epoch, "psutil create_time() must succeed on a live test process"
        return {
            "stable_pid": str(os.getpid()),
            "stable_pid_lstart": str(epoch),
            "stable_pid_start_epoch": str(epoch),
        }
    result = subprocess.run(
        ["ps", "-p", str(os.getpid()), "-o", "lstart="],
        capture_output=True,
        text=True,
    )
    lstart = result.stdout.strip()
    assert lstart, "ps -p <self> -o lstart= must succeed on a live test process"
    return {"stable_pid": str(os.getpid()), "stable_pid_lstart": lstart}


# ---------------------------------------------------------------------------
# live-session-ids
# ---------------------------------------------------------------------------


class TestLiveSessionIdsTransport:
    def test_no_sessions_matches_in_process_empty_result(self, tmp_path):
        repo = _make_repo(tmp_path)
        expected = liveness.live_session_ids(cwd=str(repo))
        assert expected == frozenset()

        result = _run_cli(repo, ["live-session-ids"])
        assert result.returncode == 0
        assert result.stdout == ""

    def test_stdout_payload_matches_in_process_result_sorted(self, tmp_path):
        """Two sessions (one live, one long-dead) exercise both the
        multi-line stdout shape and the sort-for-determinism divergence the
        CLI module docstring documents against the (unsorted) JS original."""
        repo = _make_repo(tmp_path)
        _write_session(repo, "sidLive", _self_lstart_meta())
        _write_session(
            repo,
            "sidDead",
            {"pid": "999999", "last_activity": "2026-01-01T00:00:00Z"},
        )

        expected = sorted(liveness.live_session_ids(cwd=str(repo)))
        assert expected == ["sidLive"]  # sanity: fixture actually discriminates

        result = _run_cli(repo, ["live-session-ids"])
        assert result.returncode == 0
        assert result.stdout.splitlines() == expected


# ---------------------------------------------------------------------------
# claim-path
# ---------------------------------------------------------------------------


class TestClaimPathTransport:
    def test_appends_entry_matches_in_process_dedup_append(self, tmp_path):
        repo = _make_repo(tmp_path)
        entry = "coordinator/foo.py"
        touched_inprocess = repo / "touched-inprocess.txt"
        touched_subprocess = repo / "touched-subprocess.txt"

        # In-process oracle for THIS input (not an external oracle — the
        # library function this CLI is a thin adapter over).
        claims.atomic_dedup_append(str(touched_inprocess), entry)
        claims.atomic_dedup_append(str(touched_inprocess), entry)  # dedup

        result1 = _run_cli(repo, ["claim-path", str(touched_subprocess), entry])
        result2 = _run_cli(repo, ["claim-path", str(touched_subprocess), entry])

        assert result1.returncode == 0
        assert result2.returncode == 0
        # Review: coordinatorcode-reviewer-7ca5d82a Finding 1 — event-line format
        # (scope.format_touch_event), not a bare path. Keep this as an equivalence
        # assertion between the CLI transport and the in-process oracle (the point
        # of this test) — parse both sides via scope.parse_touch_event rather than
        # pinning either to a literal (timestamped) line.
        lines_subprocess = touched_subprocess.read_text(encoding="utf-8").splitlines()
        lines_inprocess = touched_inprocess.read_text(encoding="utf-8").splitlines()
        assert len(lines_subprocess) == len(lines_inprocess) == 1
        parsed_subprocess = scope.parse_touch_event(lines_subprocess[0])
        parsed_inprocess = scope.parse_touch_event(lines_inprocess[0])
        assert (parsed_subprocess[0], parsed_subprocess[2]) == (
            parsed_inprocess[0],
            parsed_inprocess[2],
        ) == ("T", entry)

    def test_wrong_arg_count_exit_zero_and_stderr_message(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = _run_cli(repo, ["claim-path", "only-one-arg"])
        assert result.returncode == 0
        assert "requires exactly 2 args" in result.stderr


# ---------------------------------------------------------------------------
# self-claim
# ---------------------------------------------------------------------------


class TestSelfClaimTransport:
    def test_touched_file_matches_in_process_result(self, tmp_path):
        repo = _make_repo(tmp_path)
        _write_session(
            repo,
            "sidA",
            {"pid": "999999", "last_activity": "2026-01-01T00:00:00Z"},
        )
        path = "coordinator/bar.py"

        result = _run_cli(
            repo, ["self-claim", path], env={"CLAUDE_CODE_SESSION_ID": "sidA"}
        )
        assert result.returncode == 0

        touched = repo / ".git" / "coordinator-sessions" / "sidA" / "touched.txt"
        assert touched.is_file()
        # Review: coordinatorcode-reviewer-7ca5d82a Finding 1 — event-line format,
        # parse rather than assert exact-membership of the bare path.
        lines = touched.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1
        verb, _ts, parsed_path = scope.parse_touch_event(lines[0])
        assert (verb, parsed_path) == ("T", path)

    def test_wrong_arg_count_exit_zero_and_stderr_message(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = _run_cli(repo, ["self-claim"])
        assert result.returncode == 0
        assert "requires exactly 1 arg" in result.stderr

    def test_no_session_never_exits_nonzero(self, tmp_path):
        """No live session to attribute to (fail-open, best-effort contract)
        must still map to exit 0 — never a raised exception surfaced as a
        non-zero process exit."""
        repo = _make_repo(tmp_path)
        result = _run_cli(repo, ["self-claim", "coordinator/baz.py"])
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# exit-code mapping — explicit, across every reachable error/edge argv shape
# ---------------------------------------------------------------------------


class TestExitCodeMapping:
    """``js_bridge_cli.main`` documents (module docstring, Negative-spec
    bullet 3) that it NEVER raises and NEVER exits non-zero, even on a
    resolution failure. This class asserts that contract explicitly, as a
    spawned process exit code (``subprocess.CompletedProcess.returncode``),
    for every argv shape reachable without a live session. This is the
    assertion the mandatory exit-code-inversion sensitivity check (see the
    chunk report) flips locally to prove the suite actually watches it."""

    @pytest.mark.parametrize(
        "argv,expect_stderr_substring",
        [
            ([], "usage"),
            (["bogus-command"], "unknown subcommand"),
            (["claim-path", "only-one-arg"], "requires exactly 2 args"),
            (["self-claim"], "requires exactly 1 arg"),
        ],
    )
    def test_error_and_edge_argv_all_map_to_exit_zero(
        self, tmp_path, argv, expect_stderr_substring
    ):
        repo = _make_repo(tmp_path)
        result = _run_cli(repo, argv)
        assert result.returncode == 0
        assert expect_stderr_substring in result.stderr

    def test_success_argv_also_maps_to_exit_zero(self, tmp_path):
        """Same mapping, success arm: there is exactly one exit code in this
        CLI's contract, and it applies uniformly across both arms."""
        repo = _make_repo(tmp_path)
        result = _run_cli(repo, ["live-session-ids"])
        assert result.returncode == 0
