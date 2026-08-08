"""Tests for the two BX-16 rewrite checks that close BX-7's and BX-8's
missing-rewrite-target gap (example-doctrine-repo docs/plans/2026-07-29-windows-viability-
stop-the-spawn-storms.md, row BX-16): ``check_multiprobe_banner_rewrite``
(MULTI_PROBE_BANNER, 40.1% of measured forks) and
``check_head_tail_plumbing_rewrite`` (HEAD_TAIL_PLUMBING, 25%).

Both checks share the two-tier BX-16 contract, never deny -- see
``dispatch_checks.py``'s BX-16 module comment. As of C4 (2026-08-01),
``check_multiprobe_banner_rewrite`` emits EITHER a concrete rewrite OR
nothing (``None``) for an unrecognized probe/stage -- the prior
prose-only-advisory fall-through was Axis A's exact failure mode and is
removed; ``check_head_tail_plumbing_rewrite`` still advises (out of this
chunk's scope). This file verifies output-equivalence for every
auto-rewrite, the silent fall-through on an unrecognized probe/stage for
the banner check, that a quoted separator token is never mis-split into a
false segment boundary, that a genuinely composed/piped command passes
through untouched (``None``), and that neither check can ever return a
``deny`` verdict for any input tried here.
"""
from __future__ import annotations

import io
import os
import platform
import re
import shutil
import subprocess
import sys
from contextlib import redirect_stdout

import pytest

from coordinator_core.bash_guards import dispatch_checks as dc
from coordinator_core.bash_guards import guard_head_tail_rewrite as ht


def _posix(p) -> str:
    """POSIX-slash string form of a path for embedding in a bash
    command-line string -- the tokenizer under test parses commands as
    real bash/POSIX-sh syntax (backslash is an escape character), so a
    native Windows ``str(Path)`` (backslash-separated) embedded directly
    into a ``cmd`` string is not a realistic Bash-tool payload and
    silently corrupts the path once tokenized (see bb48ce7's identical
    fixture-realism finding on the write-bump test suite). Accepts a
    ``Path`` or a plain ``str``."""
    return p.as_posix() if hasattr(p, "as_posix") else str(p).replace("\\", "/")


def _payload_prefix() -> str:
    """The dynamic ``<resolved-interpreter> -c '`` prefix every BX-16
    rewrite payload carries -- NOT a literal ``"python3 -c '"`` since
    ``_bt_python3_invocation()`` resolves the actual interpreter for this
    host (see that function's own docstring: a bare ``python3`` is
    frequently absent on Windows). Computed once per call so a test
    comparing prefixes doesn't itself hardcode the resolution result."""
    return dc._bt_python3_invocation() + " -c '"


def _run_python_c(command: str) -> str:
    """Execute a generated ``<interpreter> -c '<script>'`` rewrite command's
    embedded script directly via the CURRENT interpreter (no subprocess,
    no shell re-parsing) and capture its stdout -- isolates "does the
    generated script produce the right facts" from "does the outer shell
    quoting survive a real shell", which the fidelity tests below check
    separately via `bash -c`/`subprocess.run`.
    """
    prefix = _payload_prefix()
    assert command.startswith(prefix) and command.endswith("'")
    script = command[len(prefix) : -1]
    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(script, "<bx16-rewrite>", "exec"), {})
    return buf.getvalue()


def _run_shell(command: str) -> str:
    """Execute `command` through an actual bash-compatible shell. On
    Windows, plain `subprocess.run(shell=True)` launches cmd.exe -- a
    different tokenizer than the bash/POSIX-sh one `dispatch_checks`
    itself parses `command` as, so it does not corroborate anything about
    what a real Bash-tool invocation would do. Routed through Git Bash
    explicitly there; POSIX `shell=True` already invokes `/bin/sh`, which
    is bash-compatible for everything exercised in this module."""
    if platform.system() == "Windows":
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("git-bash not found on PATH")
        result = subprocess.run(
            [bash, "-c", command], capture_output=True, text=True, timeout=10
        )
    else:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=10
        )
    return result.stdout


#: `HH:MM:SS` as `date(1)` prints it -- the ONLY field that legitimately
#: differs between two runs of the same probe milliseconds apart.
_CLOCK_RE = re.compile(r"\b\d{2}:\d{2}:\d{2}\b")


def _mask_clock(text: str) -> str:
    """Blank the wall-clock second out of captured probe output so a
    shell-vs-rewrite equivalence assertion cannot fail on the two subprocesses
    landing either side of a tick."""
    return _CLOCK_RE.sub("HH:MM:SS", text)


# ---------------------------------------------------------------------------
# _bt_python3_invocation -- ImportError fallback (Task 3, 2026-07-29)
# ---------------------------------------------------------------------------


class TestPython3InvocationImportErrorFallback:
    """`_bt_python3_invocation` must fail open to the literal `"python3"` on
    ANY resolution failure, per its own docstring -- including when
    `coordinator_core.pyresolve` itself cannot be imported. Before this fix,
    the import and the resolution call shared ONE `try`/`except` block whose
    except tuple named `PythonPinInvalid`, a name the SAME import statement
    was supposed to bind -- when the import itself raised `ImportError`,
    Python had to evaluate that except tuple to see if it matched, and
    `PythonPinInvalid` was unbound at that moment, so the function raised
    `UnboundLocalError` instead of returning `"python3"`. This drives that
    exact path and asserts a raise-free fallback, not any implementation
    detail of how the fix is structured.
    """

    def test_import_error_falls_back_to_python3(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "coordinator_core.pyresolve", None)
        assert dc._bt_python3_invocation() == "python3"
    def test_never_denies_on_recognized_chain(self):
        cmd = 'echo "=== SESSION FACTS ==="; pwd; whoami; date; uname'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        assert out["hookSpecificOutput"]["permissionDecision"] != "deny"
        assert "updatedInput" in out["hookSpecificOutput"]

    def test_unrecognized_probe_emits_nothing(self):
        """C4 (2026-08-01): no concrete rewrite can be offered when a probe
        segment is unrecognized, so the check emits nothing -- exit (b), not
        a prose-only advisory naming no applicable alternative."""
        cmd = 'echo "=== FACTS ==="; git log -1; pwd'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        assert out is None

    @pytest.mark.pending_fix
    def test_bare_facts_equivalence(self, tmp_path):
        """state/bash-guards/known-red.json group "dispatch-checks-windows-path"
        (state/bug-backlog/2026-08-07-dispatch-checks-py-corrupts-windows-path-4bd80b653b51.yaml).
        Owner: docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-detectors.md."""
        cmd = 'echo "=== FACTS ==="; pwd; whoami'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]

        original = _run_shell(cmd)
        rewritten = _run_python_c(rewrite)
        assert original == rewritten

    @pytest.mark.pending_fix
    def test_date_and_uname_a_equivalence(self):
        """`date`'s output carries a wall-clock second, so the shell run and
        the rewritten run are compared with that second masked -- the two
        subprocesses cannot be made to straddle the same tick, and under
        parallel workers they routinely do not (observed failing at `-n 6`,
        passing in isolation, 2026-08-03).

        Negative-spec: masking is deliberately narrowed to `HH:MM:SS`, not
        applied to the whole line. The equivalence under test is that the
        rewrite reproduces the shell's output byte-for-byte -- timezone, day,
        month, year, and every `uname -a` field stay compared exactly, so a
        rewrite that dropped or reordered a field still fails."""
        cmd = "date; uname -a"
        out = dc.check_multiprobe_banner_rewrite("echo \"=== X === Y ===\"; " + cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        original = _run_shell("echo \"=== X === Y ===\"; " + cmd)
        rewritten = _run_python_c(rewrite)
        assert _mask_clock(original) == _mask_clock(rewritten)

    def test_git_facts_batched_into_one_git_invocation(self, tmp_path):
        cmd = (
            'echo "=== SESSION FACTS ==="; git rev-parse --abbrev-ref HEAD; '
            "git status --porcelain; pwd"
        )
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        # Exactly one 'git' subprocess.run call in the generated script --
        # the whole point of this row's "batch every git fact into ONE
        # invocation" instruction.
        script = rewrite[len(_payload_prefix()) : -1]
        assert script.count("subprocess.run([\"git\"") == 1

        branch_original = _run_shell("git rev-parse --abbrev-ref HEAD").strip()
        rewritten = _run_python_c(rewrite)
        # Second printed line is the branch fact (banner line, then branch).
        lines = rewritten.splitlines()
        assert lines[1] == branch_original

    def test_quoted_banner_marker_not_mis_split(self):
        """A banner echo argument containing a `;` inside quotes must not be
        mistaken for a segment boundary -- the tokenizer, not a raw-text
        split, owns segmentation."""
        cmd = 'echo "=== a ; b ==="; pwd; whoami'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        assert out["hookSpecificOutput"]["permissionDecision"] != "deny"
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        assert "a ; b" in rewrite

    def test_piped_stage_inside_banner_emits_nothing(self):
        cmd = 'echo "=== FACTS ==="; pwd | cat; whoami'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        assert out is None

    def test_no_banner_shape_returns_none(self):
        assert dc.check_multiprobe_banner_rewrite("pwd; whoami") is None
        assert dc.check_multiprobe_banner_rewrite("") is None

    def test_override_disables_check(self, monkeypatch):
        monkeypatch.setenv("COORDINATOR_ALLOW_MULTIPROBE_BANNER", "1")
        cmd = 'echo "=== FACTS ==="; pwd; whoami; date'
        assert dc.check_multiprobe_banner_rewrite(cmd) is None

    def test_unparseable_command_returns_none(self):
        assert dc.check_multiprobe_banner_rewrite("echo 'unterminated") is None


def _run_python_c_with_faked_git_status(command: str, porcelain_lines) -> str:
    """Execute a `check_multiprobe_banner_rewrite` payload's embedded script
    with `git status --porcelain=v2 --branch`'s subprocess call faked to
    return `porcelain_lines` -- lets Finding 2's detached-HEAD/unborn-branch
    sentinel-mapping fix be verified without a subagent needing to create a
    real git repo in that exact state (both are governed-repo states this
    guard's own caller-context lock forbids a dispatched review-integrator
    from constructing directly)."""
    prefix = _payload_prefix()
    assert command.startswith(prefix) and command.endswith("'")
    script = command[len(prefix) : -1]

    class _FakeResult:
        def __init__(self, stdout):
            self.stdout = stdout

    class _FakeSubprocess:
        @staticmethod
        def run(*_a, **_k):
            return _FakeResult("\n".join(porcelain_lines))

    real_subprocess = sys.modules.get("subprocess")
    sys.modules["subprocess"] = _FakeSubprocess
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            exec(compile(script, "<bx16-rewrite-faked-git>", "exec"), {})
    finally:
        if real_subprocess is not None:
            sys.modules["subprocess"] = real_subprocess
    return buf.getvalue()


class TestGitFactDetachedHeadAndUnbornBranchFidelity:
    """Finding 2 (P1): `# branch.head`/`# branch.oid`'s porcelain sentinel
    strings (`(detached)`, `(initial)`) used to be printed VERBATIM for
    whichever of `git rev-parse --abbrev-ref HEAD` / `git branch
    --show-current` / `git rev-parse HEAD` was asked -- matching NEITHER
    command's real output in that state. Fixed by tracking which of the two
    branch-probe forms a segment actually was (`_bt_git_probe_kind`'s new
    `form` return) and mapping each sentinel to that form's real behavior."""

    _cmd = (
        'echo "=== FACTS ==="; git rev-parse HEAD; '
        "git rev-parse --abbrev-ref HEAD; git branch --show-current; pwd"
    )

    def test_detached_head_sentinel_mapped_per_form(self):
        out = dc.check_multiprobe_banner_rewrite(self._cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_python_c_with_faked_git_status(
            rewrite, ["# branch.oid abc123", "# branch.head (detached)"]
        )
        lines = result.splitlines()
        # order: banner, head_sha, rev-parse --abbrev-ref, show-current, pwd
        assert lines[1] == "abc123"
        assert lines[2] == "HEAD"  # real `rev-parse --abbrev-ref HEAD` on detached HEAD
        assert lines[3] == ""  # real `branch --show-current` on detached HEAD

    def test_unborn_branch_sentinel_mapped_per_form(self):
        out = dc.check_multiprobe_banner_rewrite(self._cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_python_c_with_faked_git_status(
            rewrite, ["# branch.oid (initial)", "# branch.head master"]
        )
        lines = result.splitlines()
        assert lines[1] == ""  # real `rev-parse HEAD` fails (no stdout) on unborn branch
        assert lines[2] == "master"
        assert lines[3] == "master"


class TestGitStatusPorcelainV2RenameAndSpacePathFidelity:
    """Finding 7 (P2): porcelain=v2 kind-"2" (rename/copy) records append a
    rename-score field, then the two paths joined by a literal TAB, not
    another space -- a blind `_l.split(" ")` glued them into one string
    instead of splitting them; the same blind split also fragmented any
    tracked path containing its own literal space. Fixed by splitting each
    record kind on its own documented fixed-field count via `maxsplit`."""

    def test_rename_record_reconstructed_as_short_style_arrow_line(self):
        cmd = 'echo "=== X ==="; git status --porcelain; pwd'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_python_c_with_faked_git_status(
            rewrite,
            [
                "# branch.oid abc",
                "# branch.head main",
                "2 R100 N... 100644 100644 100644 aaa bbb R100 "
                "new name.txt\told name.txt",
            ],
        )
        assert "R100 new name.txt -> old name.txt" in result.splitlines()

    def test_modified_path_containing_space_not_fragmented(self):
        cmd = 'echo "=== X ==="; git status --porcelain; pwd'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_python_c_with_faked_git_status(
            rewrite,
            [
                "# branch.oid abc",
                "# branch.head main",
                "1 M. N... 100644 100644 100644 aaa bbb file with space.txt",
            ],
        )
        assert "M. file with space.txt" in result.splitlines()

    def test_untracked_path_containing_space_not_fragmented(self):
        cmd = 'echo "=== X ==="; git status --porcelain; pwd'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        result = _run_python_c_with_faked_git_status(
            rewrite,
            [
                "# branch.oid abc",
                "# branch.head main",
                "? untracked with space.txt",
            ],
        )
        assert "?? untracked with space.txt" in result.splitlines()


class TestDatePortableAcrossPlatforms:
    """Finding 3 (P1): `%e` (space-padded day) is a glibc/BSD `strftime`
    EXTENSION -- absent from Windows CRT, raising `ValueError` there. Fixed
    by computing the space-padded day with plain Python string formatting
    instead of asking `strftime` for it, so no platform branch is needed and
    the real `date` command's exact output is still reproduced."""

    @pytest.mark.pending_fix
    def test_date_rewrite_never_raises_and_matches_real_date(self):
        """state/bash-guards/known-red.json group "dispatch-checks-windows-path"
        (date/uname divergence). Owner:
        docs/plans/2026-08-07-spawn-storm-culprit-taxonomy-and-detectors.md."""
        cmd = 'echo "=== FACTS ==="; pwd; date'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        original = _run_shell(cmd)
        rewritten = _run_python_c(rewrite)  # would raise ValueError pre-fix on Windows
        assert original == rewritten

    def test_date_rewrite_uses_no_percent_e_directive(self):
        cmd = 'echo "=== FACTS ==="; pwd; date'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        script = rewrite[len(_payload_prefix()) : -1]
        assert "%e" not in script


# ---------------------------------------------------------------------------
# check_head_tail_plumbing_rewrite
# ---------------------------------------------------------------------------


@pytest.fixture()
def census_dir(tmp_path):
    (tmp_path / "a.txt").write_text("hello TODO world\n")
    (tmp_path / "b.txt").write_text("nothing here\n")
    (tmp_path / "c.log").write_text("another TODO line\n")
    return tmp_path


class TestHeadTailPlumbingRewriteGeneratorPathJoin:
    """AC3 regression (2026-08-07 guard-suite-back-to-a-gate, C2): the
    `find`/`grep` generator bodies `_bt_build_generator_lines` emits must
    join path components with `posixpath.join` ('/'), not `os.path.join` --
    the real `find`/`grep` binary a Bash-tool command shells out to is
    ALWAYS a POSIX (Git-Bash/coreutils) binary, even when the Bash tool
    itself runs on Windows, so its own output always joins with '/'.
    `os.path.join` on a Windows host instead glues the matched filename on
    with a native '\\', producing a rewrite whose output is byte-for-byte
    different from the real command it claims to reproduce exactly (a
    census/report consumer diffing the two would see spurious drift on
    every nested match). Pre-fix this failed: `_bt_build_generator_lines`
    called `os.path.join`, which on a POSIX CI/dev host is
    indistinguishable from `posixpath.join` (both emit '/' there) -- these
    two assertions pin the SOURCE TEXT of the emitted generator body
    directly, so the regression is caught on every host, not only
    Windows."""

    def test_find_generator_joins_with_posixpath_not_os_path(self):
        parsed = ht._bt_parse_find_census_segment(["find", "/tmp/census", "-type", "f"])
        script = "\n".join(ht._bt_build_generator_lines("find", parsed))
        assert "posixpath.join(root, fn)" in script
        assert "os.path.join(root, fn)" not in script

    def test_grep_generator_joins_with_posixpath_not_os_path(self):
        parsed = dc._bt_grep_flags_and_operands(["grep", "-rn", "TODO", "/tmp/census"])
        script = "\n".join(ht._bt_build_generator_lines("grep", parsed))
        assert "posixpath.join(root, fn)" in script
        assert "os.path.join(root, fn)" not in script


class TestHeadTailPlumbingRewrite:
    def test_never_denies(self, census_dir):
        for cmd in [
            "ls %s | head -n 2" % _posix(census_dir),
            "find %s -type f | head" % _posix(census_dir),
            "grep -rn TODO %s | tail -n 1" % _posix(census_dir),
            "cat file | head",  # unrecognized upstream -> advisory
            "a | b | c | head",  # too many segments -> advisory
        ]:
            out = ht.check_head_tail_plumbing_rewrite(cmd)
            assert out is None or out["hookSpecificOutput"]["permissionDecision"] != "deny"

    def test_ls_head_equivalence(self, census_dir):
        cmd = "ls %s | head -n 2" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        original = _run_shell(cmd)
        rewritten = _run_python_c(rewrite)
        assert original == rewritten

    def test_find_head_equivalence_sorted(self, census_dir):
        cmd = "find %s -type f -name '*.txt' | head -n 5" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        # find's own traversal order is not guaranteed -- compare against a
        # SORTED oracle (this rewrite's own documented, deliberate choice).
        original = _run_shell(
            "find %s -type f -name '*.txt' | sort | head -n 5" % _posix(census_dir)
        )
        rewritten = _run_python_c(rewrite)
        assert original == rewritten

    def test_grep_tail_equivalence_sorted(self, census_dir):
        cmd = "grep -rn TODO %s | tail -n 1" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        original = _run_shell("grep -rn TODO %s | sort | tail -n 1" % _posix(census_dir))
        rewritten = _run_python_c(rewrite)
        assert original == rewritten

    def test_tail_zero_does_not_return_everything(self, census_dir):
        """Python's own `_out[-0:]` slice quirk (== `_out[0:]`, the WHOLE
        list) must not leak into the generated rewrite for `tail -n 0`."""
        cmd = "ls %s | tail -n 0" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        assert _run_python_c(rewrite) == ""

    def test_unrecognized_upstream_advises_not_rewrites(self):
        out = ht.check_head_tail_plumbing_rewrite("sort weird.txt | head -n 3")
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] != "deny"
        assert "updatedInput" not in out["hookSpecificOutput"]

    def test_grep_dash_w_upstream_advises_not_rewrites(self, census_dir):
        """Finding 4: `check_head_tail_plumbing_rewrite`'s grep-upstream
        translation feeds the same `_bt_grep_flags_and_operands` choke
        point as `check_grep_via_bash_rewrite` -- `-w` must be refused here
        too, not just at the standalone-grep entry point, since dropping
        `-w` from `_GREP_SUBSTITUTABLE_SHORT_FLAGS` closes both callers at
        once."""
        out = ht.check_head_tail_plumbing_rewrite(
            "grep -wrn TODO %s | head -n 5" % _posix(census_dir)
        )
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] != "deny"
        assert "updatedInput" not in out["hookSpecificOutput"]

    def test_unrecognized_count_form_advises(self, census_dir):
        out = ht.check_head_tail_plumbing_rewrite("ls %s | head -c 10" % _posix(census_dir))
        assert out is not None
        assert "updatedInput" not in out["hookSpecificOutput"]

    def test_composed_three_stage_pipeline_advises(self, census_dir):
        out = ht.check_head_tail_plumbing_rewrite(
            "ls %s | sort | head -n 2" % _posix(census_dir)
        )
        assert out is not None
        assert "updatedInput" not in out["hookSpecificOutput"]

    def test_quoted_pipe_character_not_mis_split(self, census_dir):
        """A `|` inside a quoted grep pattern must not be treated as a
        segment boundary.

        Uses `-F` (fixed-string) so the rewrite is offered: a bare `grep`
        (POSIX BRE, no `-E`/`-F`) pattern containing a literal `|` is
        dialect-ambiguous (see `_bt_grep_dialect`'s own docstring -- BRE
        treats bare `|` as a literal character, Python `re` treats it as
        alternation) and is now correctly REFUSED rather than silently
        mistranslated, so it can no longer stand as "the rewrite still
        happened" evidence for the segmentation claim this test makes.
        `-F` keeps the same quoted-separator-token shape while giving this
        test a pattern the fix can faithfully translate, so segmentation
        equivalence is still checked end-to-end rather than by substring."""
        (census_dir / "pipe.txt").write_text("a|b marker\n")
        cmd = "grep -Frn 'a|b' %s | head -n 1" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] != "deny"
        rewrite = out["hookSpecificOutput"]["updatedInput"]["command"]
        original = _run_shell(cmd)
        rewritten = _run_python_c(rewrite)
        assert original == rewritten
        assert rewritten  # segmentation wasn't fooled into matching nothing

    def test_quoted_pipe_bare_bre_pattern_refuses_not_mistranslates(self, census_dir):
        """The founding-incident shape, at the `head`-pipeline call site:
        a bare (non `-E`/`-F`) grep pattern containing `|` must NOT be
        auto-rewritten (its BRE-vs-Python-re meaning is ambiguous), and must
        fall through to the advisory skeleton rather than a silently wrong
        `updatedInput`."""
        (census_dir / "pipe.txt").write_text("a|b marker\n")
        cmd = "grep -rn 'a|b' %s | head -n 1" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] != "deny"
        assert "updatedInput" not in out["hookSpecificOutput"]

    def test_bare_pipeline_without_head_tail_returns_none(self, census_dir):
        assert ht.check_head_tail_plumbing_rewrite("ls %s" % _posix(census_dir)) is None
        assert ht.check_head_tail_plumbing_rewrite("") is None

    def test_override_disables_check(self, monkeypatch, census_dir):
        monkeypatch.setenv("COORDINATOR_ALLOW_HEAD_TAIL_PLUMBING", "1")
        cmd = "ls %s | head -n 2" % _posix(census_dir)
        assert ht.check_head_tail_plumbing_rewrite(cmd) is None

    def test_bare_head_no_pipe_returns_none(self, census_dir):
        """`head file.txt` with no upstream pipe is an ordinary bounded read,
        not the plumbing shape -- the classifier itself excludes it, so this
        check must never manufacture a match on it."""
        assert ht.check_head_tail_plumbing_rewrite("head %s/a.txt" % _posix(census_dir)) is None


class TestRedirectionDeclinesRewriteNotRewritesWrong(object):
    """Task 4 (2026-07-29, live incident): a shell redirection operator
    (`2>`, `>`, `>>`, `<`) among the upstream segment's own tokens is NOT a
    path/pattern operand. Before this fix, `_bt_parse_ls_segment` took the
    LAST non-flag token as the directory to list, so
    `ls DIR 2>/dev/null | head -40` silently rewrote to
    `os.listdir("2>/dev/null")`, which raised `FileNotFoundError` at
    runtime -- an AUTO-APPLIED `updatedInput` rewrite, not an advisory, so
    the caller's command was silently replaced with one that meant
    something different before it failed. Every case here must decline to
    rewrite (advisory, `updatedInput` absent) rather than emit a wrong one --
    "prefer not rewriting over rewriting approximately".
    """

    def _declines_rewrite(self, cmd: str) -> None:
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        assert out is not None
        assert out["hookSpecificOutput"]["permissionDecision"] != "deny"
        assert "updatedInput" not in out["hookSpecificOutput"], out

    def test_exact_live_repro_command(self, census_dir):
        """The exact shape that crashed live: `ls DIR 2>/dev/null | head -N`."""
        self._declines_rewrite("ls %s 2>/dev/null | head -40" % _posix(census_dir))

    def test_ls_stderr_redirect_glued(self, census_dir):
        self._declines_rewrite("ls %s 2>/dev/null | head -n 2" % _posix(census_dir))

    def test_ls_stderr_redirect_separate_token(self, census_dir):
        self._declines_rewrite("ls %s 2> /dev/null | head -n 2" % _posix(census_dir))

    def test_ls_stdout_redirect(self, census_dir):
        self._declines_rewrite("ls %s >out.txt | head -n 2" % _posix(census_dir))

    def test_ls_append_redirect(self, census_dir):
        self._declines_rewrite("ls %s >>out.txt | head -n 2" % _posix(census_dir))

    def test_ls_input_redirect(self, census_dir):
        self._declines_rewrite("ls %s <in.txt | head -n 2" % _posix(census_dir))

    def test_find_stderr_redirect(self, census_dir):
        self._declines_rewrite(
            "find %s -type f 2>/dev/null | head -n 5" % _posix(census_dir)
        )

    def test_find_bare_redirect_as_first_token(self, tmp_path):
        """The position-sensitive shape: a redirection as the FIRST predicate
        token, which used to be assigned to `path` directly."""
        self._declines_rewrite("find %s 2>/dev/null | head -n 5" % _posix(tmp_path))

    def test_grep_stderr_redirect(self, census_dir):
        self._declines_rewrite("grep -rn TODO %s 2>/dev/null | head -n 5" % _posix(census_dir))

    def test_standalone_grep_rewrite_also_declines(self, census_dir):
        """`_bt_grep_flags_and_operands` is shared with the standalone-grep
        rewrite check -- the fix must protect both callers, not just the
        head/tail one. `check_grep_via_bash_rewrite`'s own contract falls
        through to bare `None` (not an advisory dict) for anything its
        parser doesn't recognize -- BX-6 owns deny policy for that residue,
        this check only owns the rewrite target -- so `None` here IS the
        "declined to rewrite" outcome for this function, not a gap."""
        out = dc.check_grep_via_bash_rewrite(
            "grep -rn TODO %s 2>/dev/null" % _posix(census_dir)
        )
        if out is not None:
            assert out["hookSpecificOutput"]["permissionDecision"] != "deny"
            assert "updatedInput" not in out["hookSpecificOutput"]

    def test_unredirected_control_still_rewrites(self, census_dir):
        """Sanity companion: an ordinary pipeline with no redirection must
        still auto-rewrite -- this fix must not have widened into a blanket
        decline."""
        out = ht.check_head_tail_plumbing_rewrite("ls %s | head -n 2" % _posix(census_dir))
        assert out is not None
        assert "updatedInput" in out["hookSpecificOutput"]


# ---------------------------------------------------------------------------
# Shell-quoting safety: every auto-rewrite emitted by either check must parse
# cleanly when embedded in an OUTER single-quoted shell argument (the harness
# wraps ``updatedInput.command`` verbatim) -- a raw `'` inside the generated
# python source would terminate that outer quoting early.
# ---------------------------------------------------------------------------


class TestGeneratedPayloadShellSafety:
    def test_banner_rewrite_has_no_raw_single_quote_in_payload(self):
        cmd = 'echo "=== FACTS ==="; git rev-parse --abbrev-ref HEAD; git status; pwd; whoami'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        command = out["hookSpecificOutput"]["updatedInput"]["command"]
        prefix = _payload_prefix()
        assert command.startswith(prefix) and command.endswith("'")
        inner = command[len(prefix) : -1]
        assert "'" not in inner

    def test_head_tail_rewrite_has_no_raw_single_quote_in_payload(self, census_dir):
        prefix = _payload_prefix()
        for cmd in [
            "ls %s | head -n 2" % _posix(census_dir),
            "find %s -type f | tail -n 1" % _posix(census_dir),
            "grep -rn TODO %s | head -n 1 -l" % _posix(census_dir),
        ]:
            out = ht.check_head_tail_plumbing_rewrite(cmd)
            if out is None or "updatedInput" not in out["hookSpecificOutput"]:
                continue
            command = out["hookSpecificOutput"]["updatedInput"]["command"]
            inner = command[len(prefix) : -1]
            assert "'" not in inner, command

    def test_banner_rewrite_parses_under_current_interpreter(self):
        cmd = 'echo "=== FACTS ==="; pwd; whoami; date; uname'
        out = dc.check_multiprobe_banner_rewrite(cmd)
        command = out["hookSpecificOutput"]["updatedInput"]["command"]
        inner = command[len(_payload_prefix()) : -1]
        compile(inner, "<test>", "exec")  # raises SyntaxError on failure

    def test_head_tail_rewrite_parses_under_current_interpreter(self, census_dir):
        cmd = "grep -rn TODO %s | tail -n 1" % _posix(census_dir)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        command = out["hookSpecificOutput"]["updatedInput"]["command"]
        inner = command[len(_payload_prefix()) : -1]
        compile(inner, "<test>", "exec")


# ---------------------------------------------------------------------------
# Head short-circuit / tail ring-buffer fix (2026-07-29): the emitted script
# used to accumulate the WHOLE generator output into `_out` before slicing
# head/tail off the end, so a `head -n 5` over a large tree walked the
# entire tree first -- unbounded work for a bounded result. This section
# proves (a) the short-circuited/ring-buffered rewrite is byte-identical to
# the pre-fix (full-materialization) rewrite for the same inputs, and (b)
# the head short-circuit genuinely stops early rather than merely being
# "the new script still runs".
# ---------------------------------------------------------------------------


def _exec_script_text(script: str) -> str:
    """Execute a raw (unwrapped) python script body and capture its stdout
    -- used to run the OLD-style (pre-fix, full-materialization) script
    text this section reconstructs inline, since that text never goes
    through `check_head_tail_plumbing_rewrite`'s own `<interpreter> -c
    '...'`-wrapping (`_run_python_c` expects that wrapper prefix; this does
    not carry one)."""
    buf = io.StringIO()
    with redirect_stdout(buf):
        exec(compile(script, "<old-style-bx16-script>", "exec"), {})
    return buf.getvalue()


def _old_style_head_tail_script(kind: str, parsed: dict, is_head: bool, n: int) -> str:
    """Reconstruct the PRE-FIX `check_head_tail_plumbing_rewrite` script
    body verbatim (full materialization into `_out`, then a single
    `_out[:n]`/`_out[-n:]` slice) -- the differential-equivalence oracle
    this section's tests compare the CURRENT (short-circuited / ring-
    buffered) rewrite against. Deliberately NOT calling any of the new
    helpers (`_bt_head_short_circuit_lines` / `_bt_tail_ring_buffer_lines`)
    -- this is the behavior being verified equivalent to, not a wrapper
    around it.
    """
    gen_lines = ht._bt_build_generator_lines(kind, parsed)
    if is_head:
        slice_expr = "_out[:%d]" % n
    elif n > 0:
        slice_expr = "_out[-%d:]" % n
    else:
        slice_expr = "[]"
    return "\n".join(gen_lines + ["for _l in %s:" % slice_expr, "    print(_l)"])


class TestHeadTailRewriteDifferentialEquivalence:
    """Old (full-materialization) rewrite vs. current (short-circuited /
    ring-buffered) rewrite, same inputs, same real temp tree -- output must
    be byte-identical."""

    @pytest.fixture()
    def big_tree(self, tmp_path):
        for i in range(30):
            (tmp_path / ("f%03d.txt" % i)).write_text("TODO line %d\n" % i)
        (tmp_path / "skip.log").write_text("not a match\n")
        return tmp_path

    @pytest.mark.parametrize("n", [0, 1, 3, 10, 30, 999])
    def test_find_head(self, big_tree, n):
        up_tokens = ["find", _posix(big_tree), "-type", "f", "-name", "*.txt"]
        parsed = ht._bt_parse_find_census_segment(up_tokens)
        old = _exec_script_text(_old_style_head_tail_script("find", parsed, True, n))
        cmd = "find %s -type f -name '*.txt' | head -n %d" % (_posix(big_tree), n)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        new = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])
        assert old == new

    @pytest.mark.parametrize("n", [0, 1, 3, 10, 30, 999])
    def test_find_tail(self, big_tree, n):
        up_tokens = ["find", _posix(big_tree), "-type", "f", "-name", "*.txt"]
        parsed = ht._bt_parse_find_census_segment(up_tokens)
        old = _exec_script_text(_old_style_head_tail_script("find", parsed, False, n))
        cmd = "find %s -type f -name '*.txt' | tail -n %d" % (_posix(big_tree), n)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        new = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])
        assert old == new

    @pytest.mark.parametrize("n", [0, 1, 3, 30, 999])
    def test_grep_head(self, big_tree, n):
        up_tokens = ["grep", "-rn", "TODO", _posix(big_tree)]
        parsed = dc._bt_grep_flags_and_operands(up_tokens)
        old = _exec_script_text(_old_style_head_tail_script("grep", parsed, True, n))
        cmd = "grep -rn TODO %s | head -n %d" % (_posix(big_tree), n)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        new = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])
        assert old == new

    @pytest.mark.parametrize("n", [0, 1, 3, 30, 999])
    def test_grep_tail(self, big_tree, n):
        up_tokens = ["grep", "-rn", "TODO", _posix(big_tree)]
        parsed = dc._bt_grep_flags_and_operands(up_tokens)
        old = _exec_script_text(_old_style_head_tail_script("grep", parsed, False, n))
        cmd = "grep -rn TODO %s | tail -n %d" % (_posix(big_tree), n)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        new = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])
        assert old == new

    @pytest.mark.parametrize("n", [0, 1, 3, 30, 999])
    def test_ls_head_and_tail(self, big_tree, n):
        up_tokens = ["ls", _posix(big_tree)]
        parsed = ht._bt_parse_ls_segment(up_tokens)
        for is_head in (True, False):
            old = _exec_script_text(
                _old_style_head_tail_script("ls", parsed, is_head, n)
            )
            verb = "head" if is_head else "tail"
            cmd = "ls %s | %s -n %d" % (_posix(big_tree), verb, n)
            out = ht.check_head_tail_plumbing_rewrite(cmd)
            new = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])
            assert old == new, (verb, n)


class TestHeadShortCircuitStopsEarly:
    """The short-circuit must genuinely stop the underlying `os.walk` early
    -- not merely produce a script that happens to still run and return the
    right answer (that's the equivalence class above). Every directory
    visited in this fixture yields exactly one match, so the number of
    `os.walk` tuples consumed before a `head -n K` collects its K matches is
    deterministic regardless of the OS's own (unsorted) directory-entry
    order: 1 (the top-level root, no matches) + K (one match-bearing subdir
    per match).
    """

    def test_find_head_walk_is_bounded(self, monkeypatch, tmp_path):
        total_dirs = 50
        for i in range(total_dirs):
            d = tmp_path / ("d%03d" % i)
            d.mkdir()
            (d / "match.txt").write_text("hit\n")

        real_walk = os.walk
        visited = {"count": 0}

        def counting_walk(*args, **kwargs):
            for item in real_walk(*args, **kwargs):
                visited["count"] += 1
                yield item

        monkeypatch.setattr(os, "walk", counting_walk)

        n = 3
        cmd = "find %s -type f -name 'match.txt' | head -n %d" % (_posix(tmp_path), n)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        result = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])

        assert len(result.strip().splitlines()) == n
        # 1 top-level root (no matches) + exactly `n` match-bearing subdirs
        # -- proves the walk stopped the instant the n-th match was
        # collected, never touching the other (total_dirs - n) subdirs.
        assert visited["count"] == 1 + n
        assert visited["count"] < total_dirs

    def test_grep_head_walk_is_bounded(self, monkeypatch, tmp_path):
        total_dirs = 50
        for i in range(total_dirs):
            d = tmp_path / ("d%03d" % i)
            d.mkdir()
            (d / "match.txt").write_text("TODO hit\n")

        real_walk = os.walk
        visited = {"count": 0}

        def counting_walk(*args, **kwargs):
            for item in real_walk(*args, **kwargs):
                visited["count"] += 1
                yield item

        monkeypatch.setattr(os, "walk", counting_walk)

        n = 4
        cmd = "grep -rn TODO %s | head -n %d" % (_posix(tmp_path), n)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        result = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])

        assert len(result.strip().splitlines()) == n
        assert visited["count"] == 1 + n
        assert visited["count"] < total_dirs

    def test_tail_still_observes_whole_stream(self, monkeypatch, tmp_path):
        """Sanity companion: TAIL must NOT short-circuit (it genuinely needs
        every item), so the walk count for a tail rewrite over the same
        fixture is NOT bounded the same way -- it visits every directory."""
        total_dirs = 12
        for i in range(total_dirs):
            d = tmp_path / ("d%03d" % i)
            d.mkdir()
            (d / "match.txt").write_text("hit\n")

        real_walk = os.walk
        visited = {"count": 0}

        def counting_walk(*args, **kwargs):
            for item in real_walk(*args, **kwargs):
                visited["count"] += 1
                yield item

        monkeypatch.setattr(os, "walk", counting_walk)

        cmd = "find %s -type f -name 'match.txt' | tail -n 3" % _posix(tmp_path)
        out = ht.check_head_tail_plumbing_rewrite(cmd)
        result = _run_python_c(out["hookSpecificOutput"]["updatedInput"]["command"])

        assert len(result.strip().splitlines()) == 3
        assert visited["count"] == 1 + total_dirs
