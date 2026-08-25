"""
coordinator_core.session.tests.test_touch_concurrency_and_attribution — the
durable guard for docs/plans/2026-08-14-cli-authored-writes-get-claimed.md
chunk C3.

Purpose: the plan's spike used throwaway scaffolding (uncommitted, unrepeatable)
to measure a ~3% same-session `touched.txt` append loss on Windows and to
demonstrate CLI-boundary write attribution once, live. This file replaces both
with re-runnable, DETERMINISTIC tests:

  - AC2: a same-session concurrent-declaration data loss, reproduced 100% of
    the time (not a statistical wave count) via rendezvous injection — hold
    writer A between resolving its append offset and performing the write
    while writer B completes its own append. Pre-C2 code (``touch(...,
    lock=False)``, the exact unlocked path C2 replaced) loses B's line every
    time; post-C2 code (``lock=True``, the default) never does, and a second
    writer genuinely observes the lock held cross-process (real
    ``held_lock``/msvcrt contention, not a mocked wait).
  - AC1: a file written by a `coordinator/bin/`-style CLI (the
    `cli_entry.run_op_main` in-process seam every real trampoline uses)
    appears in the writing session's `touched.txt`, on a real spawned
    process — turning the spike's one-off Leg A observation into a
    re-runnable guard.
  - AC3: a peer session's write is never recorded into this session's
    `touched.txt`, under concurrent writers sharing one tree.

Rendezvous mechanism (RAG-bait — the seam a future reader needs, not just the
assertion): each writer subprocess installs `scope.open = <wrapper>` (module-
global shadowing of the builtin `open` name — Python resolves an unqualified
`open()` call via the module globals dict before falling through to
builtins, so this shadow is invisible to every other module). The wrapper
intercepts ONLY the append-mode open of THIS test's `touched.txt`, replacing
it with `_FakeAppendHandle`: it captures the append offset once (mirroring
the historical Windows lseek-once-at-open-then-write-anywhere hazard C2's
docstring records), signals a sentinel FILE (cross-process — these are real
subprocesses, not threads; a `threading.Event` cannot cross a process
boundary) that it has resolved its offset, then blocks on a second sentinel
file before performing an explicit `seek(offset)` + `write()` against a
freshly reopened handle. This is not a mock of the defect — it is the exact
non-atomic seek-then-write shape `touch()`'s docstring names, driven
deterministically instead of raced statistically.

`_TOUCH_LOCK_TIMEOUT_SECS` is bumped to a generous constant, module-locally
in the spawned subprocess only (never on the production default), for the
lock-held test: the production 0.2s acquire timeout exists to bound a real
tool call's latency, not a test's rendezvous window, and a synthetic pause
longer than 0.2s would otherwise trip the SAME fail-open degrade this file's
tests exist to hold pre-C2 and rule out post-C2 — see
:func:`_write_writer_script` and its `bump_timeout` parameter.

Marker: `cadence` + `spawns_process` (Rule 2/4,
`coordinator_core/tests/test_no_new_spawning_tests.py`) — every test here
spawns at least one real `python` child process (git init/config/commit for
the fixture repo, plus the writer/CLI subprocesses under test), which is
inherently a multi-hundred-ms-per-process operation unsuitable for the fast
per-commit tier. Measured on this box: this file's five tests together run
in ~4.1-4.7s serial — well inside a `cadence`-tier budget, nowhere near the
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
import tempfile
import textwrap
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.session import core
from coordinator_core.win_portability import no_console_creationflags

# Every test in this file spawns real subprocesses (git init/config/commit
# for the fixture repo, plus writer/CLI child processes under test) --
# genuine process-boundary behaviour (Windows append-mode file-handle
# semantics, cross-process `held_lock` contention) that no in-process mock
# or thread stands in for; two threads in ONE process do not contend for the
# same OS-level advisory lock the way two independent processes do (verified
# during authoring -- a same-process thread pair never observed the lock as
# held). The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue
# and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Ambient session-identity env vars that would let the pytest-hosting
# session's own identity leak into a spawned writer/CLI subprocess.
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


# ---------------------------------------------------------------------------
# Rendezvous writer script (AC2) — a real subprocess, not a thread, so the
# induced pause genuinely straddles a process boundary the way the historic
# Windows append-race does.
# ---------------------------------------------------------------------------

_WRITER_SCRIPT_TEMPLATE = textwrap.dedent(
    """
    import os, sys, time

    sys.path.insert(0, {repo_root!r})
    from coordinator_core.session import core, scope

    repo, sid, path, sent_open, sent_release, lock_flag, bump_timeout = sys.argv[1:8]
    lock = lock_flag == "1"
    if bump_timeout == "1":
        scope._TOUCH_LOCK_TIMEOUT_SECS = 5.0

    touched = os.path.join(core.session_dir(sid, cwd=repo), "touched.txt")
    real_open = open

    class _FakeAppendHandle:
        \"\"\"Emulates the historic non-atomic Windows append hazard
        ``touch()``'s own docstring names (offset resolved once, then a
        SEPARATE write at that captured offset) -- deterministically, via a
        cross-process rendezvous, rather than by racing real OS timing.\"\"\"

        def __init__(self, target_path):
            self.target_path = target_path
            self.offset = os.path.getsize(target_path) if os.path.exists(target_path) else 0
            with real_open(sent_open, "w") as fh:
                fh.write("1")
            deadline = time.time() + 10
            while not os.path.exists(sent_release) and time.time() < deadline:
                time.sleep(0.01)

        def write(self, text):
            with real_open(self.target_path, "r+b") as fh:
                fh.seek(self.offset)
                fh.write(text.encode("utf-8"))

        def __enter__(self):
            return self

        def __exit__(self, *exc_info):
            return False

    def _patched_open(file, mode="r", encoding=None, **kwargs):
        if mode == "a" and os.path.abspath(str(file)) == os.path.abspath(touched):
            return _FakeAppendHandle(file)
        if encoding is not None:
            return real_open(file, mode, encoding=encoding, **kwargs)
        return real_open(file, mode, **kwargs)

    scope.open = _patched_open
    scope.touch(sid, path, cwd=repo, root=repo, lock=lock)
    """
)

_PLAIN_WRITER_SCRIPT_TEMPLATE = textwrap.dedent(
    """
    import os, sys, time

    sys.path.insert(0, {repo_root!r})
    from coordinator_core.session import scope

    repo, sid, path, sent_open, sent_done, lock_flag, bump_timeout, sent_ready = sys.argv[1:9]
    lock = lock_flag == "1"
    if bump_timeout == "1":
        scope._TOUCH_LOCK_TIMEOUT_SECS = 5.0
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
    scope.touch(sid, path, cwd=repo, root=repo, lock=lock)
    with open(sent_done, "w") as fh:
        fh.write("1")
    """
)


def _write_writer_script(tmp_path: Path, name: str, template: str) -> Path:
    script = tmp_path / name
    script.write_text(template.format(repo_root=str(_REPO_ROOT)), encoding="utf-8")
    return script


def _run_rendezvous(tmp_path: Path, repo: Path, sid: str, *, lock: bool, bump_timeout: bool):
    """Drive writer A (held via rendezvous) and writer B against ``repo``'s
    session ``sid``. Returns ``(touched_content,
    b_still_running_after_pause, b_returncode)``.

    Both spawned children are terminated on ANY exit path (assertion
    failure, timeout, exception) -- an unguarded early return here would
    otherwise leave a rendezvous-blocked child running for up to its own
    internal 10s deadline. Review: code-reviewer P2 (2026-08-14).
    """
    held_script = _write_writer_script(tmp_path, "writer_held.py", _WRITER_SCRIPT_TEMPLATE)
    plain_script = _write_writer_script(tmp_path, "writer_plain.py", _PLAIN_WRITER_SCRIPT_TEMPLATE)

    sent_open = tmp_path / "opened.flag"
    sent_release = tmp_path / "release.flag"
    sent_ready = tmp_path / "b_ready.flag"

    lock_arg = "1" if lock else "0"
    bump_arg = "1" if bump_timeout else "0"
    # Only requested (and only meaningful) for the deterministic
    # acquire-blocked-time measurement below.
    ready_arg = str(sent_ready) if (lock and bump_timeout) else ""

    import time as _time

    proc_a: Optional[subprocess.Popen] = None
    proc_b: Optional[subprocess.Popen] = None
    try:
        proc_a = subprocess.Popen(
            [
                sys.executable, str(held_script), str(repo), sid, "pathA.txt",
                str(sent_open), str(sent_release), lock_arg, bump_arg,
            ],
            **no_console_creationflags(),
        )
        deadline_open = 10
        start = _time.time()
        while not sent_open.exists() and _time.time() - start < deadline_open:
            _time.sleep(0.01)
        assert sent_open.exists(), "writer A never signalled it opened its append handle"

        sent_done = tmp_path / "done.flag"
        proc_b = subprocess.Popen(
            [
                sys.executable, str(plain_script), str(repo), sid, "pathB.txt",
                str(sent_open), str(sent_done), lock_arg, bump_arg, ready_arg,
            ],
            **no_console_creationflags(),
        )

        if lock and bump_timeout:
            # Wait for B to signal it is about to attempt the real acquire
            # -- NOT a fixed post-spawn delay, which would have to cover
            # interpreter startup plus `import coordinator_core.session.scope`
            # before B ever reaches `held_lock`, and could go green on a
            # silently no-op lock simply because B hadn't gotten there yet.
            # Review: code-reviewer P1 (2026-08-14).
            deadline_ready = _time.time() + 10
            while not sent_ready.exists() and _time.time() < deadline_ready:
                _time.sleep(0.01)
            assert sent_ready.exists(), "writer B never signalled it reached the lock acquire call"
            # NOW give B a deterministic window to be denied the lock A
            # holds, measured from the acquire attempt itself, well inside
            # the bumped 5s acquire timeout.
            _time.sleep(0.3)
            b_still_running = proc_b.poll() is None
        else:
            proc_b.wait(timeout=10)
            b_still_running = False

        sent_release.write_text("1")
        proc_a.wait(timeout=10)
        proc_b.wait(timeout=10)

        touched = Path(core.session_dir(sid, cwd=str(repo))) / "touched.txt"
        content = touched.read_text(encoding="utf-8") if touched.exists() else ""
        return content, b_still_running, proc_b.returncode
    finally:
        for proc in (proc_a, proc_b):
            if proc is not None and proc.poll() is None:
                proc.kill()
                proc.wait(timeout=10)


def test_ac2_concurrent_declarations_lost_deterministically_without_lock(tmp_path):
    """RED: pre-C2 semantics (``lock=False``, the exact unlocked path C2
    replaced). Writer A's offset-resolution/write are forced apart by the
    rendezvous while writer B fully completes its append; A's later write at
    its stale offset clobbers B's line every time this runs -- 100%, not a
    wave-count probability.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    sid = "sid-ac2-red"
    core.init(sid, cwd=str(repo))

    content, _, _ = _run_rendezvous(tmp_path, repo, sid, lock=False, bump_timeout=False)

    assert "pathA.txt" in content, "writer A's own declaration should still land"
    assert "pathB.txt" not in content, (
        "AC2 RED: writer B's declaration must be lost deterministically under the "
        "pre-C2 unlocked append path -- if this starts passing, the rendezvous "
        "seam has stopped forcing the race and this test is no longer a guard"
    )


def test_ac2_concurrent_declarations_serialize_with_lock_and_peer_observes_it_held(tmp_path):
    """GREEN: post-C2 semantics (``lock=True``, the default). The SAME
    rendezvous holds writer A mid-operation; writer B's own ``touch()`` call
    contends for the SAME `held_lock` anchor and is genuinely denied --
    observed directly as "still running" after a deterministic pause well
    inside its acquire timeout (the plan's alternative acceptable shape: a
    second writer observes the lock held). Releasing A lets both complete
    with zero loss, in the write order the lock enforced.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    sid = "sid-ac2-green"
    core.init(sid, cwd=str(repo))

    content, b_still_running, _ = _run_rendezvous(tmp_path, repo, sid, lock=True, bump_timeout=True)

    assert b_still_running, (
        "AC2 GREEN: writer B must still be blocked on the lock A holds, 0.3s "
        "into a 5s acquire timeout, measured from B's own signalled acquire "
        "attempt (not from process spawn) -- if B has already finished, it "
        "degraded to the fail-open unlocked path instead of genuinely "
        "contending"
    )
    assert "pathA.txt" in content
    assert "pathB.txt" in content, (
        "AC2 GREEN: zero loss under the lock -- both declarations must land"
    )
    # Serialization order: A held the lock and wrote first; B could only
    # acquire (and append) after A released.
    assert content.index("pathA.txt") < content.index("pathB.txt")


def test_ac2_lock_contention_past_production_timeout_degrades_to_unlocked_append(tmp_path):
    """PRODUCTION-TIMEOUT COVERAGE: every other lock-path test in this file
    bumps ``_TOUCH_LOCK_TIMEOUT_SECS`` to 5.0s for determinism, which leaves
    the SHIPPED ``_TOUCH_LOCK_TIMEOUT_SECS = 0.2`` fail-open degrade path --
    the one path where this defect can still occur in production, when real
    contention exceeds the bound -- with no coverage anywhere. This test
    drives B against the SAME rendezvous-held lock A holds, but at B's own
    unmodified production 0.2s acquire timeout: A is held via the rendezvous
    (proven still holding the real lock, since ``sent_release`` is only
    written AFTER B has already finished waiting), so B's ``held_lock`` call
    is deterministically denied past its bound. ``touch()`` must degrade to
    an unlocked append rather than raising, and the writer process must
    still exit 0.

    Note: after B degrades and appends, the release below lets A perform
    its OWN deferred write at A's stale pre-contention offset -- the exact
    same clobber
    ``test_ac2_concurrent_declarations_lost_deterministically_without_lock``
    already covers -- so B's line is not guaranteed to survive A's later
    write, and this test does not assert it does. The fail-open contract
    this test targets (no raise, exit 0) is observable from B's own
    returncode regardless of what A does afterward. Review: code-reviewer
    P2 (2026-08-14).
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    sid = "sid-ac2-prod-timeout"
    core.init(sid, cwd=str(repo))

    content, b_still_running, b_returncode = _run_rendezvous(
        tmp_path, repo, sid, lock=True, bump_timeout=False
    )

    assert b_returncode == 0, (
        "touch() must never raise for an operational lock-acquire timeout -- "
        "the fail-open contract -- but writer B exited non-zero"
    )
    assert not b_still_running, "writer B is awaited synchronously on this path; sanity check"
    assert "pathA.txt" in content, "writer A's own declaration should still land once released"


def test_ac3_peer_session_write_never_recorded_into_this_sessions_touched(tmp_path):
    """AC3: two concurrent sessions writing into the same tree at once --
    each session's `touched.txt` must contain ONLY its own declared paths,
    never the peer's.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _make_repo(repo)
    sid_mine = "sid-ac3-mine"
    sid_peer = "sid-ac3-peer"
    core.init(sid_mine, cwd=str(repo))
    core.init(sid_peer, cwd=str(repo))

    script = _write_writer_script(tmp_path, "writer_ac3.py", _PLAIN_WRITER_SCRIPT_TEMPLATE)
    sent_open = tmp_path / "ac3.flag"
    sent_open.write_text("1")  # no rendezvous needed here -- run both immediately

    proc_mine: Optional[subprocess.Popen] = None
    proc_peer: Optional[subprocess.Popen] = None
    try:
        proc_mine = subprocess.Popen(
            [
                sys.executable, str(script), str(repo), sid_mine, "mine.txt",
                str(sent_open), str(tmp_path / "mine_done.flag"), "1", "0", "",
            ],
            **no_console_creationflags(),
        )
        proc_peer = subprocess.Popen(
            [
                sys.executable, str(script), str(repo), sid_peer, "peer.txt",
                str(sent_open), str(tmp_path / "peer_done.flag"), "1", "0", "",
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

    mine_touched = Path(core.session_dir(sid_mine, cwd=str(repo))) / "touched.txt"
    peer_touched = Path(core.session_dir(sid_peer, cwd=str(repo))) / "touched.txt"

    mine_content = mine_touched.read_text(encoding="utf-8") if mine_touched.exists() else ""
    peer_content = peer_touched.read_text(encoding="utf-8") if peer_touched.exists() else ""

    assert "mine.txt" in mine_content
    assert "peer.txt" not in mine_content, "a peer session's write must never appear in THIS session's touched.txt"
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
    """AC1, durable: a file written by a `coordinator/bin/`-style CLI --
    routed through the SAME `cli_entry.run_op_main` in-process seam every
    real trampoline uses, and run as a real spawned `python` process, not an
    in-process call -- appears in the writing session's `touched.txt`.
    Turns the spike's one-off Leg A observation
    (docs/research/spike-verdicts/2026-08-14-cli-process-boundary-write-attribution.md,
    recorded event `T 2026-08-14T20:05:18.591426Z artifacts/leg_a.txt`) into
    a re-runnable guard against the shipped seam.
    """
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

    touched = Path(core.session_dir(sid, cwd=str(repo))) / "touched.txt"
    assert touched.is_file(), "the writing session's touched.txt must exist after the CLI ran"
    content = touched.read_text(encoding="utf-8")
    assert "artifact.txt" in content
