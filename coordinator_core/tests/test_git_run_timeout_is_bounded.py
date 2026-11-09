"""A git spawn that times out must RETURN, on Windows as on POSIX.

Purpose: `run_git`'s bound used to be `subprocess.run(timeout=)`, which does
not bound the call on Windows. CPython's `run()` catches `TimeoutExpired`,
kills the child, and then — Windows-only, `Lib/subprocess.py:556` under
`if _mswindows:` — calls `process.communicate()` a SECOND time with NO
timeout, to drain the reader threads by joining them. If a reader never
reaches EOF the join never returns, and `kill()` does not close a pipe whose
write end a grandchild inherited. The bound became advisory on the platform
this repo calls first-class, on the PreToolUse(Bash) chain.

MEASURED, not reasoned: caught live 2026-08-31 with
`faulthandler.dump_traceback_later(60)`, main thread parked at
`subprocess.py:556` beneath `subagent_sandbox.engine ::
_resolve_git_root_uncached` (`timeout=2.0`). An external `timeout 600` could
not reap it either — SIGTERM cannot land while the main thread sits in
`Thread.join()`. Record:
`state/bug-backlog/2026-08-31-subprocess-run-s-timeout-does-not-bound-466bceff0ba5.yaml`.

WHY THE DOUBLE LOOKS LIKE THIS. The defect is a HANG, so a test that
reproduced it faithfully would hang the suite — the one thing this file must
never do. The double therefore models the hazard rather than staging it: its
`communicate()` raises `TimeoutExpired` on the bounded first call, and a
SECOND call raises `AssertionError` naming the defect. The second call is
where the real thing hangs; raising instead is what keeps this suite finite
while still failing loudly on the exact re-entry.

What a regression looks like from here, stated precisely rather than
dramatically: revert `run_git` to `subprocess.run` and these tests fail on
`timed_out`, not on the re-entry assertion — the double patches `Popen`, so a
reverted seam bypasses it entirely, runs real `git`, and returns success.
That is still a genuine red, and it is fast. The re-entry assertion covers the
other regression: a future edit that keeps `Popen` but re-adds a drain after
the kill.

The doubles remain non-spawning. Windows now performs no communicate call or
Popen context exit at all: its first bounded wait expires instead. One explicit
Windows regression below uses a harmless Python tree under the test launcher's
owned containment job; no real Git command or repository is involved.
"""

from __future__ import annotations

import subprocess
import sys

from coordinator_core.git import run as git_run


class _NeverDrainingProc:

    returncode = -9

    def __init__(self) -> None:
        self.kill_calls = 0
        self.wait_calls = []
        self.communicate_calls = 0
        self.closed = False
        self.files = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        assert sys.platform != 'win32', 'Windows must never enter Popen.__exit__'
        self.closed = True
        return False

    def communicate(self, input=None, timeout=None):
        self.communicate_calls += 1
        if self.communicate_calls == 1:
            raise subprocess.TimeoutExpired(cmd=["git"], timeout=timeout or 0)
        raise AssertionError(
            "run_git re-entered communicate() after the kill -- that is the "
            "unbounded Windows re-drain, and in a real process it does not "
            "raise, it hangs forever"
        )

    def kill(self):
        self.kill_calls += 1

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if sys.platform == 'win32' and len(self.wait_calls) == 1:
            raise subprocess.TimeoutExpired(cmd=['git'], timeout=timeout)
        return self.returncode


def _install(monkeypatch) -> _NeverDrainingProc:
    proc = _NeverDrainingProc()
    def spawn(*args, **kwargs):
        proc.files = [kwargs[key] for key in ('stdout', 'stderr') if kwargs.get(key) != subprocess.PIPE]
        return proc
    monkeypatch.setattr(subprocess, "Popen", spawn)
    return proc


def test_a_timed_out_git_call_returns_instead_of_re_draining(monkeypatch) -> None:
    proc = _install(monkeypatch)

    result = git_run.run_git(["rev-parse", "--show-toplevel"], cwd=".")

    assert result.timed_out is True
    assert result.returncode == -1
    assert proc.communicate_calls == (0 if sys.platform == 'win32' else 1)


def test_the_timed_out_child_is_killed_and_reaped_under_a_bound(monkeypatch) -> None:
    proc = _install(monkeypatch)

    git_run.run_git(["status", "--porcelain"], cwd=".")

    assert proc.kill_calls == 1
    assert proc.wait_calls, "the killed child must be reaped, not abandoned"
    assert all(t is not None for t in proc.wait_calls), "every reap is bounded"


def test_the_pipes_are_closed_on_the_timeout_path(monkeypatch) -> None:
    proc = _install(monkeypatch)

    git_run.run_git(["rev-parse", "HEAD"], cwd=".")

    if sys.platform == 'win32':
        assert not proc.closed and len(proc.files) == 2
        assert all(stream.closed for stream in proc.files)
    else:
        assert proc.closed is True


def test_a_timed_out_call_yields_no_bytes(monkeypatch) -> None:
    _install(monkeypatch)

    result = git_run.run_git(["log", "--oneline"], cwd=".")

    assert result.stdout == ""
    assert result.stderr == ""


def test_a_reaping_wait_that_itself_times_out_still_returns(monkeypatch) -> None:
    proc = _install(monkeypatch)

    def _wait(timeout=None):
        proc.wait_calls.append(timeout)
        raise subprocess.TimeoutExpired(cmd=["git"], timeout=timeout or 0)

    monkeypatch.setattr(proc, "wait", _wait)

    result = git_run.run_git(["fsck"], cwd=".")

    assert result.timed_out is True
    assert result.returncode == -1


def test_windows_file_capture_preserves_binary_and_text_contract(monkeypatch):
    import pytest
    if sys.platform != 'win32':pytest.skip('Windows file-backed capture')
    recorded=[]
    class Proc:
        returncode=3
        def __init__(self,kwargs):self.kwargs=kwargs
        def wait(self,timeout=None):
            assert timeout is not None
            self.kwargs['stdout'].write(b'a\r\nb\rc\xff\0')
            self.kwargs['stderr'].write(b'err\r\n')
            return self.returncode
        def communicate(self,*args,**kwargs):raise AssertionError('PIPE drain forbidden')
        def __enter__(self):raise AssertionError('Popen context forbidden')
    def spawn(*args,**kwargs):recorded.append(kwargs);return Proc(kwargs)
    monkeypatch.setattr(subprocess,'Popen',spawn)
    text=git_run.run_git(['status'])
    binary=git_run.run_git(['status','-z'],binary=True)
    assert (text.returncode,text.stdout,text.stderr,text.stdout_bytes)==(3,'a\nb\nc\ufffd\0','err\n',b'')
    assert (binary.stdout,binary.stderr,binary.stdout_bytes)==('a\r\nb\rc\ufffd\0','err\r\n',b'a\r\nb\rc\xff\0')
    assert all(item['stdout'].closed and item['stderr'].closed for item in recorded)


def test_windows_job_accounting_closes_before_owned_capture_files(monkeypatch):
    import pytest
    if sys.platform != 'win32':pytest.skip('Windows file-backed cleanup')
    recorded={};charged=[]
    class Proc:
        returncode=0
        def wait(self,timeout=None):return 0
    def spawn(*args,**kwargs):recorded.update(kwargs);return Proc()
    monkeypatch.setattr(subprocess,'Popen',spawn)
    monkeypatch.setattr(git_run,'_open_charging_job',lambda:object())
    monkeypatch.setattr(git_run,'_assign_and_resume',lambda *_:True)
    def charge(job):
        assert not recorded['stdout'].closed and not recorded['stderr'].closed
        charged.append(job)
    monkeypatch.setattr(git_run,'_charge_and_close_job',charge)
    assert git_run.run_git(['status']).ok
    assert len(charged)==1 and recorded['stdout'].closed and recorded['stderr'].closed


def test_windows_missing_spawn_closes_files_and_charges_once(monkeypatch):
    import pytest
    if sys.platform != 'win32':pytest.skip('Windows file-backed cleanup')
    files=[];charged=[]
    def missing(*args,**kwargs):
        files.extend([kwargs['stdin'],kwargs['stdout'],kwargs['stderr']]);raise FileNotFoundError('fake missing git')
    monkeypatch.setattr(subprocess,'Popen',missing)
    monkeypatch.setattr(git_run,'_open_charging_job',lambda:object())
    monkeypatch.setattr(git_run,'_charge_and_close_job',charged.append)
    result=git_run.run_git(['hash-object','--stdin'],input=b'x\n\0')
    assert result.returncode==127 and not result.timed_out
    assert len(charged)==1 and all(stream.closed for stream in files)


def test_windows_inherited_writer_returns_before_grandchild_eof(tmp_path,monkeypatch):
    """One harmless Python tree; the outer canonical test Job owns cleanup.

    run_git itself does not promise descendant containment. A surviving writer
    is deliberate evidence that this return no longer waits for pipe EOF.
    """
    import pytest,time,json,psutil
    if sys.platform != 'win32':pytest.skip('Windows inherited-handle regression')
    pid_file=tmp_path/'own-grandchild.json'
    program=('import subprocess,sys,time,json,pathlib; '
        'p=subprocess.Popen([sys.executable,"-I","-S","-c","import time;time.sleep(5)"]); '
        f'pathlib.Path({str(pid_file)!r}).write_text(json.dumps({{"pid":p.pid}})); '
        'time.sleep(30)')
    monkeypatch.setattr(git_run,'_git_exe',lambda:sys.executable)
    monkeypatch.setattr(git_run,'_wall_bound',lambda *_:.7)
    started=time.monotonic()
    result=git_run.run_git(['-I','-S','-c',program],cwd=str(tmp_path))
    elapsed=time.monotonic()-started
    assert result.timed_out and result.returncode==-1
    assert result.stdout=='' and result.stderr=='' and result.stdout_bytes==b''
    pid=json.loads(pid_file.read_text())['pid']
    assert psutil.pid_exists(pid), 'writer must still be alive to prove no EOF dependency'
    assert elapsed<4, 'return must precede the five-second inherited writer EOF'
