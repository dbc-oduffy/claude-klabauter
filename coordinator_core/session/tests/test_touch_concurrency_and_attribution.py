"""
coordinator_core.session.tests.test_touch_concurrency_and_attribution — the
durable guard for docs/plans/2026-08-14-cli-authored-writes-get-claimed.md
chunk C3.

Purpose: the plan's spike used throwaway scaffolding (uncommitted, unrepeatable)
to measure a ~3% same-session `touched.txt` append loss on Windows and to
demonstrate CLI-boundary write attribution once, live. This file replaces both
with re-runnable, DETERMINISTIC tests:

  - POST-AC17 CONCURRENCY (was AC2): two concurrent same-session declarations
    BOTH land, unlocked, because `touch_record.append_event` ->
    `atomic_append.append_line` performs one O(1) atomic append per event.
    See `TestPostAC17AtomicAppendNeedsNoLock`, which replaced three
    `test_ac2_*` cases BY NAME on 2026-08-26 -- they asserted properties of an
    application-level lock `scope.touch` no longer takes (`del lock`), after
    AC17 deleted the dedup-scan-then-append region that lock existed to
    serialize.
  - AC1: a file written by a `coordinator/bin/`-style CLI (the
    `cli_entry.run_op_main` in-process seam every real trampoline uses)
    appears in the writing session's `touched.txt`, on a real spawned
    process — turning the spike's one-off Leg A observation into a
    re-runnable guard.
  - AC3: a peer session's write is never recorded into this session's
    `touched.txt`, under concurrent writers sharing one tree.

Retired mechanism, recorded because its absence is the point: this file used
to carry a rendezvous harness that shadowed `scope.open` in each writer
subprocess to intercept the append-mode open and force the historical Windows
lseek-once-then-write-anywhere race deterministically. It is gone with the
three tests it served. `touch()` no longer opens the sink through
`scope.open` at all, so the harness could not fire; and there is no longer a
two-step region for it to straddle, so there is nothing left to force. Do NOT
resurrect it against `touch_record.open` -- that would make a deleted lock
look guarded. `scope._ATOMIC_APPEND_LOCK_TIMEOUT_SECS` (renamed 2026-08-27
from `_TOUCH_LOCK_TIMEOUT_SECS`, since nothing about it concerns `touch()`
any more) is likewise not exercised by `touch()` and is no longer bumped
anywhere in this file.

Also retired here, 2026-08-27: `scope.touch`'s `lock=` parameter, and the
`test_touch_ignores_the_vestigial_lock_parameter` test whose only subject
was that the parameter did nothing. The parameter is GONE rather than
inert, so there is no longer an inertness to pin; a test asserting that an
absent parameter has no effect is the same "looks guarded" shape this
file's own docstring warns about, one level up.

Marker: `cadence` + `spawns_process` (Rule 2/4,
`coordinator_core/tests/test_no_new_spawning_tests.py`) — every test here
spawns at least one real `python` child process (git init/config/commit for
the fixture repo, plus the writer/CLI subprocesses under test), which is
inherently a multi-hundred-ms-per-process operation unsuitable for the fast
per-commit tier. Measured on this box: this file's four tests together run
in ~1.0s serial — well inside a `cadence`-tier budget, nowhere near the
fast tier's target. (Prior docstring/commit-message figures of ~3.1s/~4s
disagreed and predated the P1/P2 strengthening below; this is the
re-measured number, not a re-derivation of either stale one. Review:
code-reviewer nit, 2026-08-14.)

Spec backlink: docs/plans/2026-08-14-cli-authored-writes-get-claimed.md § C3
"""

from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.session import core, scope
from coordinator_core.win_portability import no_console_creationflags

# held). The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[3]

_SESSION_ENV_VARS = ("CLAUDE_SESSION_ID", "CLAUDE_CODE_SESSION_ID", "COORDINATOR_SESSION_ID")


def _make_repo(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, **no_console_creationflags())
    subprocess.run(
        ["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True, **no_console_creationflags()
    )
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True, **no_console_creationflags())
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, **no_console_creationflags())
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True, **no_console_creationflags())


def _clean_env(extra: Optional[dict] = None) -> dict:
    env = dict(os.environ)
    for key in _SESSION_ENV_VARS:
        env.pop(key, None)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(_REPO_ROOT)] + ([env["PYTHONPATH"]] if env.get("PYTHONPATH") else [])
    )
    if extra:
        env.update(extra)
    return env


def _sink_as_legacy_text(session_dir) -> str:
    sink = Path(session_dir) / scope._TOUCH_RECORD_FILENAME
    lines, _degraded = scope._read_touch_record_as_legacy_lines(sink)
    return "\n".join(lines) + ("\n" if lines else "")


_PLAIN_WRITER_SCRIPT_TEMPLATE = textwrap.dedent(
    """
    import os, sys, time

    sys.path.insert(0, {repo_root!r})
    from coordinator_core.session import scope

    repo, sid, path, sent_open, sent_done, sent_ready = sys.argv[1:7]
    if sent_ready:
        # Signal the instant this writer is about to attempt the real
        # cross-process acquire -- NOT at process spawn, and NOT after
        # interpreter startup/imports have already elapsed. This lets a
        # caller measure acquire-blocked time specifically instead of
        # startup-plus-acquire time. Review: code-reviewer P1 (2026-08-14).
        _real_held_lock = scope.held_lock

        def _signaling_held_lock(*a, **kw):
            with open(sent_ready, "w") as fh:
                fh.write("1")
            return _real_held_lock(*a, **kw)

        scope.held_lock = _signaling_held_lock
    deadline = time.time() + 10
    while not os.path.exists(sent_open) and time.time() < deadline:
        time.sleep(0.01)
    scope.touch(sid, path, cwd=repo, root=repo)
    with open(sent_done, "w") as fh:
        fh.write("1")
    """
)


def _write_writer_script(tmp_path: Path, name: str, template: str) -> Path:
    script = tmp_path / name
    script.write_text(template.format(repo_root=str(_REPO_ROOT)), encoding="utf-8")
    return script


class TestPostAC17AtomicAppendNeedsNoLock:

    def test_concurrent_same_session_declarations_both_land_unlocked(self, tmp_path):
        repo = tmp_path / "repo"
        repo.mkdir()
        _make_repo(repo)
        sid = "sid-atomic-concurrent"
        core.init(sid, cwd=str(repo))

        script = _write_writer_script(
            tmp_path, "plain_writer.py", _PLAIN_WRITER_SCRIPT_TEMPLATE
        )
        gun = tmp_path / "go.flag"
        procs = []
        for name in ("alpha.txt", "beta.txt"):
            procs.append(
                subprocess.Popen(
                    [
                        sys.executable, str(script), str(repo), sid, name,
                        str(gun), str(tmp_path / f"done-{name}.flag"), "",
                    ],
                    env=_clean_env(),
                    **no_console_creationflags(),
                )
            )
        try:
            gun.write_text("1")
            for proc in procs:
                assert proc.wait(timeout=30) == 0, "an unlocked concurrent declaration must not fail"
        finally:
            for proc in procs:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=10)

        content = _sink_as_legacy_text(core.session_dir(sid, cwd=str(repo)))
        assert "alpha.txt" in content, (
            "the atomic append must not lose a concurrent writer's declaration -- "
            "if this fails, a read-modify-write has been reintroduced onto the "
            "touch path that AC17 removed"
        )
        assert "beta.txt" in content, (
            "the atomic append must not lose a concurrent writer's declaration -- "
            "if this fails, a read-modify-write has been reintroduced onto the "
            "touch path that AC17 removed"
        )


def test_ac3_peer_session_write_never_recorded_into_this_sessions_touched(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    sid_mine = "sid-ac3-mine"
    sid_peer = "sid-ac3-peer"
    core.init(sid_mine, cwd=str(repo))
    core.init(sid_peer, cwd=str(repo))

    script = _write_writer_script(tmp_path, "writer_ac3.py", _PLAIN_WRITER_SCRIPT_TEMPLATE)
    sent_open = tmp_path / "ac3.flag"
    sent_open.write_text("1")

    proc_mine: Optional[subprocess.Popen] = None
    proc_peer: Optional[subprocess.Popen] = None
    try:
        proc_mine = subprocess.Popen(
            [
                sys.executable, str(script), str(repo), sid_mine, "mine.txt",
                str(sent_open), str(tmp_path / "mine_done.flag"), "",
            ],
            **no_console_creationflags(),
        )
        proc_peer = subprocess.Popen(
            [
                sys.executable, str(script), str(repo), sid_peer, "peer.txt",
                str(sent_open), str(tmp_path / "peer_done.flag"), "",
            ],
            **no_console_creationflags(),
        )
        proc_mine.wait(timeout=10)
        proc_peer.wait(timeout=10)
    finally:
        for proc in (proc_mine, proc_peer):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)

    mine_content = _sink_as_legacy_text(core.session_dir(sid_mine, cwd=str(repo)))
    peer_content = _sink_as_legacy_text(core.session_dir(sid_peer, cwd=str(repo)))

    assert "mine.txt" in mine_content
    assert "peer.txt" not in mine_content, "a peer session's write must never appear in THIS session's touch record"
    assert "peer.txt" in peer_content
    assert "mine.txt" not in peer_content


_AC1_CLI_OP_SCRIPT = textwrap.dedent(
    """
    from coordinator_core.session.declared_writes import declare_write


    def main(argv):
        path = argv[0]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("cli-written\\n")
        declare_write(path)
        return 0
    """
)


def test_ac1_file_written_by_cli_appears_in_writing_sessions_touched(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    sid = "sid-ac1"

    op_dir = tmp_path / "fake_op"
    op_dir.mkdir()
    (op_dir / "fake_cli_op.py").write_text(_AC1_CLI_OP_SCRIPT, encoding="utf-8")

    env = _clean_env({"COORDINATOR_SESSION_ID": sid})
    env["PYTHONPATH"] = os.pathsep.join([str(op_dir), env["PYTHONPATH"]])

    driver_code = (
        "import sys; from coordinator_core.cli_entry import run_op_main; "
        f"sys.exit(run_op_main('fake_cli_op', ['artifact.txt'], cwd={str(repo)!r}))"
    )
    result = subprocess.run(
        [sys.executable, "-c", driver_code],
        cwd=str(repo),
        env=env,
        capture_output=True,
        text=True,
        **no_console_creationflags(),
    )
    assert result.returncode == 0, result.stderr

    assert (repo / "artifact.txt").is_file(), "the CLI's own write must have actually happened"

    sink = Path(core.session_dir(sid, cwd=str(repo))) / scope._TOUCH_RECORD_FILENAME
    assert sink.is_file(), "the writing session's touch record must exist after the CLI ran"
    content = _sink_as_legacy_text(core.session_dir(sid, cwd=str(repo)))
    assert "artifact.txt" in content
