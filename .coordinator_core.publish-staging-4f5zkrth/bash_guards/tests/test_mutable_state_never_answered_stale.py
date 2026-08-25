"""Mutable worktree/index state is never answered from anything that can be wrong.

Two seams in this package short-circuit a Bash call rather than letting it spawn:

  - `guard_inprocess_search` / `coordinator_core.search` ANSWER a command in the
    hook process and substitute a no-op for it. Whatever it prints is the whole
    answer the agent ever sees.
  - `dispatch_checks.check_multiprobe_banner_rewrite` REWRITES a session-facts
    banner chain into one `python3 -c` payload that batches every git fact into a
    single `git status --porcelain=v2 --branch`.

Both are right to exist -- `docs/decisions/DR-344-the-brightline-process-budget-
for-makima.md` makes process creation the cost to eliminate. Neither is allowed to
turn a cheap correct answer into a fast wrong one about state eleven peers are
mutating underneath it.

Observed 2026-08-21, which is why these are pinned rather than left to review: the
batched payload read `.stdout` off `subprocess.run(...)` with the return code
discarded, so a `git status` that failed -- `.git/index.lock` held by a concurrent
peer is the ordinary case at this repo's load norm -- rendered as an empty status,
a blank branch, and exit 0. Three agents read that as their working tree having
been wiped; one proposed a `git stash pop` recovery over a 144-file older snapshot.
A real `git status --porcelain` run at the same moment showed 697 modified paths.

The contracts asserted here:
  1. The in-process ANSWER path serves read-only content search and nothing else.
     A command whose answer is worktree or index state must fall through (`None`),
     and no state-reading verb may be enrolled in the answerable vocabulary.
  2. A file the search process cannot open refuses the whole answer. It is a hole
     in the result set, not an absence of matches, and under concurrent peers the
     holes land on the files being edited.
  3. The batched git payload never renders a failed `git status` as a clean one:
     it checks the return code, propagates the failure, and does not take the
     index lock it was failing on.

Spec backlink: coordinator_core/search/engine.py,
coordinator_core/bash_guards/dispatch_checks.py :: check_multiprobe_banner_rewrite
"""

from __future__ import annotations

import io
import sys
from contextlib import redirect_stdout

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.bash_guards import guard_inprocess_search as guard
from coordinator_core.search import engine
from coordinator_core.search.answer import answer, plan_for


#: Every one of these reports state a concurrent peer can change between the hook
#: process reading it and the agent acting on it. None is answerable in-process.
MUTABLE_STATE_COMMANDS = (
    "git status",
    "git status --short",
    "git status --porcelain",
    "git diff",
    "git diff --stat",
    "git diff --cached --name-only",
    "git stash list",
    "git ls-files -m",
    "git log --oneline -5",
    "cat coordinator_core/search/engine.py",
    "head -50 coordinator_core/search/engine.py",
    "tail -20 coordinator_core/search/engine.py",
    "ls coordinator_core",
    "find . -name '*.py'",
    "wc -l coordinator_core/search/engine.py",
)


class TestOnlyReadOnlySearchIsAnswerable:
    """Contract 1 -- the answerable vocabulary, pinned by membership."""

    @pytest.mark.parametrize("command", MUTABLE_STATE_COMMANDS)
    def test_mutable_state_command_is_never_answered(self, command, tmp_path):
        assert plan_for(command) is None
        assert answer(command, cwd=str(tmp_path)) is None

    @pytest.mark.parametrize("command", MUTABLE_STATE_COMMANDS)
    def test_mutable_state_command_falls_through_the_guard(self, command, tmp_path):
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "cwd": str(tmp_path),
        }
        assert guard.check(payload) is None

    def test_answerable_binaries_are_content_search_only(self):
        """`GREP_FAMILY` is the entire set of leading binaries `plan_for` will
        accept. Enrolling a verb that reports state -- `git`, `ls`, `stat` -- puts
        that state on the substituted path, where a wrong answer is the only
        answer the agent gets."""
        assert set(engine.GREP_FAMILY) == {"grep", "egrep", "fgrep", "rg"}

    def test_absorbed_pipeline_stages_read_no_state(self):
        """Downstream stages are pure functions over lines the search already
        produced. A stage that reads the filesystem or a repository would be
        answering a second, unrelated question inside a search's answer."""
        assert set(engine._STAGE_BUILDERS) == {
            "head", "tail", "wc", "sort", "uniq", "cut", "grep", "egrep", "fgrep",
        }


class TestUnreadableFileRefusesRatherThanReadsEmpty:
    """Contract 2 -- a sharing violation from a peer mid-write must not render as
    `(no matches)`."""

    def test_read_text_raises_on_oserror(self, tmp_path):
        missing = tmp_path / "does-not-exist.py"
        with pytest.raises(engine.Unanswerable):
            engine._read_text(str(missing))

    def test_read_text_skips_binary_without_refusing(self, tmp_path):
        binary = tmp_path / "blob.bin"
        binary.write_bytes(b"alpha\x00beta")
        assert engine._read_text(str(binary)) is None

    def test_unreadable_file_declines_the_whole_search(self, tmp_path, monkeypatch):
        (tmp_path / "a.py").write_text("alpha\n", encoding="utf-8")
        (tmp_path / "b.py").write_text("alpha\n", encoding="utf-8")

        real_read = engine._read_text

        def _locked_b(path: str):
            if path.endswith("b.py"):
                raise engine.Unanswerable("cannot read %r: sharing violation" % path)
            return real_read(path)

        monkeypatch.setattr(engine, "_read_text", _locked_b)
        assert answer("grep -rn alpha .", cwd=str(tmp_path)) is None

    def test_readable_tree_still_answers(self, tmp_path):
        (tmp_path / "a.py").write_text("alpha\n", encoding="utf-8")
        rendered = answer("grep -rn alpha .", cwd=str(tmp_path))
        assert rendered is not None
        assert "alpha" in rendered


def _payload_script(command: str) -> str:
    """Strip the `<resolved-interpreter> -c '` wrapper off a rewrite payload.

    The prefix is computed from `_bt_python3_invocation()` rather than hardcoded --
    a bare `python3` is frequently absent on Windows, so the resolution result is
    not a constant a test may assume (same posture as
    `test_bx16_multiprobe_and_headtail_rewrite._payload_prefix`)."""
    prefix = dc._bt_python3_invocation() + " -c '"
    assert command.startswith(prefix) and command.endswith("'"), command
    return command[len(prefix) : -1]


def _banner_rewrite_script(cmd: str) -> str:
    out = dc.check_multiprobe_banner_rewrite(cmd)
    assert out is not None, "expected a rewrite for %r" % cmd
    return _payload_script(out["hookSpecificOutput"]["updatedInput"]["command"])


class TestBatchedGitStatusNeverFabricatesACleanTree:
    """Contract 3 -- the defect observed live on 2026-08-21."""

    _CMD = 'echo "=== SESSION FACTS ==="; git status --porcelain; git rev-parse HEAD'

    def test_payload_checks_the_return_code(self):
        script = _banner_rewrite_script(self._CMD)
        assert "returncode" in script, script

    def test_payload_does_not_take_the_index_lock(self):
        """`--no-optional-locks` is output-identical and lock-free
        (`guard_no_optional_locks.py`'s measured evidence). Batching every git
        fact into one call is worthless if that one call is the one contending
        with eleven peers for `.git/index.lock`."""
        script = _banner_rewrite_script(self._CMD)
        assert '"--no-optional-locks"' in script, script

    def test_failed_git_status_propagates_instead_of_printing_a_clean_tree(self):
        """Exec the real generated payload with `git status` faked to the exact
        shape index-lock contention produces: non-zero exit, empty stdout, a
        diagnostic on stderr. It must exit non-zero and print no git fact."""
        script = _banner_rewrite_script(self._CMD)

        class _FailedResult:
            stdout = ""
            stderr = (
                "fatal: Unable to create '.git/index.lock': File exists.\n"
            )
            returncode = 128

        class _FakeSubprocess:
            @staticmethod
            def run(*_a, **_k):
                return _FailedResult()

        real_subprocess = sys.modules.get("subprocess")
        sys.modules["subprocess"] = _FakeSubprocess
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                with pytest.raises(SystemExit) as excinfo:
                    exec(compile(script, "<banner-rewrite-failed-git>", "exec"), {})
        finally:
            if real_subprocess is not None:
                sys.modules["subprocess"] = real_subprocess

        assert excinfo.value.code == 128
        printed = buf.getvalue()
        assert "=== SESSION FACTS ===" not in printed, printed
        assert printed.strip() == "", printed
