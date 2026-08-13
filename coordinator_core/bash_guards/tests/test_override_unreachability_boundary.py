"""Pins a SECURITY BOUNDARY: no subagent can populate any
``COORDINATOR_OVERRIDE_*`` / ``COORDINATOR_ALLOW_*`` environment variable
into the bash-guard process.

Why it holds -- the mechanism, not just the assertion. The PreToolUse(Bash)
hook (``preuse-bash-dispatch.py``) is invoked by the Claude Code harness as a
FRESH subprocess for every single tool-call event. That subprocess:

  1. receives the tool-call payload on **stdin only** (``sys.stdin.read()``);
  2. never shell-executes the candidate command -- ``evaluate_payload_json``
     (this module) and every check in ``dispatch_checks`` do pure string /
     token analysis on the command text, they do not run it;
  3. therefore has an ``os.environ`` that is whatever the HARNESS handed this
     subprocess at spawn time -- a leading ``VAR=1`` prefix, an ``export`` in
     a prior Bash call, or an ``env VAR=1`` wrapper inside the CANDIDATE
     command string are just characters in that string. Nothing ever
     interprets them as shell syntax, so they never reach ``os.environ`` of
     the process that ``_override()`` (dispatch_checks.py) reads from.

The guard process's environment and the candidate command's (would-be, never
executed) environment are disjoint by construction. This is what makes every
``COORDINATOR_OVERRIDE_*``/``COORDINATOR_ALLOW_*`` advisory unreachable by a
subagent -- those overrides are for a human operator to set in the shell
that LAUNCHES the harness/hook process itself, not for anything inside a
candidate Bash command to trigger.

**This boundary is a side effect of a performance decision, not a designed
security feature, and that is exactly why it needs a test.** Fresh-
subprocess-per-event is also a real cost -- a Python interpreter cold-start
on every Bash call, worst on Windows. A future change that pools or reuses
the hook process across events (to cut that cost) would silently delete
this property: a long-lived process retains whatever environment mutations
accumulate across calls, and stdin-only payload delivery stops being the
only channel in. **If this test starts failing (or has to be deleted) after
a hook-process-lifecycle change, that change has broken a security boundary,
not merely a performance one -- do not delete this test to unblock a pooling
optimization without re-deriving the confinement guarantee some other way
first.**

Spec backlink: coordinator_core/bash_guards/dispatch_checks.py (``_override``)
and coordinator/hooks/scripts/preuse-bash-dispatch.py (coordinator-claude repo,
stdin-only / in-process-no-shell-exec hook entry point).
"""

from __future__ import annotations

import os

import pytest

from coordinator_core.bash_guards import dispatch_checks as guard
from coordinator_core.doe_root_pointer import read_doe_root_pointer


@pytest.fixture(autouse=True)
def _no_override_env_leaks_into_this_test(monkeypatch):
    """Belt-and-suspenders: guarantee the REAL test process itself never
    carries one of these vars (e.g. from the operator's own shell), so a
    pass here can't be masked by an accidental ambient export."""
    for name in (
        "COORDINATOR_OVERRIDE_NO_VERIFY",
        "COORDINATOR_ALLOW_RM",
        "COORDINATOR_OVERRIDE_GIT_CLEAN",
    ):
        monkeypatch.delenv(name, raising=False)


class TestInlineAndEnvWrapperPrefixesCannotDisarmAGuard:
    """The core reachability claim: text that LOOKS like it sets
    ``COORDINATOR_OVERRIDE_NO_VERIFY=1`` inside the candidate command is
    never interpreted as shell syntax by anything in this pipeline, so it
    never reaches ``os.environ`` -- the guard must still deny."""

    def test_inline_var_assignment_prefix_does_not_disarm_no_verify(self):
        cmd = "COORDINATOR_OVERRIDE_NO_VERIFY=1 git commit --no-verify -m x"
        assert guard.check_no_verify(cmd) is not None

    def test_env_wrapper_does_not_disarm_no_verify(self):
        cmd = "env COORDINATOR_OVERRIDE_NO_VERIFY=1 git commit --no-verify -m x"
        assert guard.check_no_verify(cmd) is not None

    def test_export_then_semicolon_does_not_disarm_no_verify(self):
        """An `export FOO=1;` fragment inside ONE candidate command string is
        still just string content in a process that never shells out to
        interpret it -- the export never lands in this process's environ,
        so the guard on the same (post-`;`) segment still denies."""
        cmd = "export COORDINATOR_OVERRIDE_NO_VERIFY=1; git commit --no-verify -m x"
        assert guard.check_no_verify(cmd) is not None

    def test_real_env_var_set_in_this_process_DOES_disarm(self, monkeypatch):
        """Control case, proving the two prior tests are a meaningful
        negative and not a guard that ignores the override entirely: when
        the override is set in THIS process's actual os.environ (the only
        channel that matters), the guard honors it."""
        monkeypatch.setenv("COORDINATOR_OVERRIDE_NO_VERIFY", "1")
        cmd = "git commit --no-verify -m x"
        assert guard.check_no_verify(cmd) is None


@pytest.mark.real_home
class TestHookEntryPointStdinOnlyNoShellExec:
    """Structural properties of the hook entry point itself
    (``preuse-bash-dispatch.py``, coordinator-claude repo) that back the boundary --
    read from source since the entry point isn't importable as a module
    (it's a `python3 -c ... runpy.run_path(...)` hooks.json registration,
    not a package).

    ``real_home`` because these are live-tree read-only oracles: the coordinator-claude
    checkout is resolved through the machine-local registry rung of
    ``read_doe_root_pointer``, and conftest's home quarantine points that
    rung at a throwaway dir, which would turn every method here into a
    permanent skip. Nothing in this class writes."""

    def _source(self) -> str:
        # Two-repo layout: hook script lives in coordinator-claude, engine in
        # claude-klabauter (this repo). The sibling checkout is resolved
        # through the canonical registry-first ladder
        # (``doe_root_pointer.read_doe_root_pointer``, DR-071), never by
        # climbing out of this repo and guessing a flat-sibling directory
        # name -- that guess is the exact shape
        # ``coordinator_core/tests/test_no_hardcoded_paths.py``'s Tooth 2
        # exists to deny, and it degrades a test into silently asserting
        # nothing when the layout differs.
        doe_root = read_doe_root_pointer()
        candidate = (
            os.path.join(doe_root, "coordinator", "hooks", "scripts", "preuse-bash-dispatch.py")
            if doe_root
            else ""
        )
        if not candidate or not os.path.isfile(candidate):
            pytest.skip(
                "coordinator-claude checkout not resolvable via the doe_root_pointer "
                "ladder (registry `repos.example_doctrine_repo`, durable `.doe-root`, "
                "legacy `.doe-root`); resolved root was %r, hook candidate "
                "%r." % (doe_root, candidate)
            )
        with open(candidate, "r", encoding="utf-8") as fh:
            return fh.read()

    def test_hook_reads_stdin_only(self):
        src = self._source()
        assert "sys.stdin.read()" in src

    def test_hook_never_shell_execs_the_candidate_command(self):
        src = self._source()
        for banned in ("subprocess.run", "subprocess.Popen", "os.system", "shell=True"):
            assert banned not in src, (
                "preuse-bash-dispatch.py must never shell-execute the "
                "candidate command -- found %r" % banned
            )

    def test_dispatch_module_never_shell_execs_the_candidate_command(self):
        """The in-process engine call this hook makes
        (``evaluate_payload_json``) must also never shell out for the
        candidate command -- only pure string/token analysis."""
        import inspect

        from coordinator_core.bash_guards import dispatch as dispatch_mod

        src = inspect.getsource(dispatch_mod)
        assert "shell=True" not in src
