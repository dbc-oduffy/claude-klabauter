"""test_percolate_round — binds C2's stop-before-push contract
(docs/plans/2026-08-13-percolate-round-one-command-and-scan-register.md
§ C2/AC6-AC10) to real assertions instead of the module's own docstring.

Every sibling CLI `percolate-round.py` sequences is stubbed at the
`subprocess.run` boundary (never via a source grep, which passes on a file
that greps its own docstring, and never via `mod._run` alone, which would
miss a hypothetical direct `subprocess.run`/`Popen` call bypassing that
wrapper) — no real publish, commit, or CI process ever spawns.

Push-detection threat model (Review: code-reviewer, hardened by
review-integrator): `test_never_invokes_git_push` patches `os.system` and
`subprocess.Popen` in addition to the `subprocess.run` boundary, so a future
edit routing a push through either would fail the test loudly instead of
slipping past a `subprocess.run`-only spy.

Run: python -m pytest coordinator/bin/tests/test_percolate_round.py -q
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
from pathlib import Path
from typing import List

import pytest

# Declares a real external-process spawn (spawn ratchet Rule 2). Tiering onto the
# cadence suite is the separate threshold ruling, not this declaration.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

# Review: code-reviewer — no `cadence` marker: subprocess.run is fully
# monkeypatched below, nothing here spawns a real process, so this suite
# belongs in the per-commit tier, not deferred to cadence gates.

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "percolate_round", _BIN_DIR / "percolate-round.py"
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class _SubprocessSpy:
    """Stands in for the module-level `subprocess.run` boundary. Dispatches
    a canned CompletedProcess by matching argv shape — never actually spawns
    anything, so a real `git push`, if the code under test ever emitted one,
    would surface as an unrecognized/unhandled call rather than silently
    doing nothing."""

    def __init__(self, *, dryrun_stdout, real_stdout, parse1_stdout, parse2_stdout,
                 scan_stdout="Content-leakage scan:\n  HIGH (credential/secret shapes -- BLOCKS publish):\n    (none)\n  MEDIUM (identity / internal paths / peer-repo names -- surfaces to gate):\n    (none)\n  LOW (informational -- commit SHAs, doctrine language):\n    (none)\n",
                 scan_returncode=0,
                 drift_stdout="anchor_mode: 30day-fallback\n",
                 commit_stdout='{"status": "ok"}',
                 ci_returncode=0,
                 ci_stdout="all green\n",
                 dest_status_stdout="",
                 dest_status_returncode=0,
                 push_returncode=0,
                 dest_ahead_stdout="",
                 dest_ahead_returncode=0,
                 reset_returncode=0,
                 clean_returncode=0,
                 rev_parse_stdout="deadbeef1234\n",
                 check_ignore_stdout="",
                 check_ignore_returncode=1,
                 ls_files_returncode=0,
                 toplevel_stdout=None,
                 toplevel_returncode=0):
        self.calls: List[List[str]] = []
        self.call_kwargs: List[dict] = []
        #: `--pathspec-from-file <path>`'s file content, captured AT CALL
        #: TIME (never after `_run_round` returns) -- `_cmd_round`'s own
        #: `tempfile.TemporaryDirectory` is torn down before control
        #: returns to the test, so the file itself is gone by the time an
        #: assertion could read it back any other way.
        self.pathspec_from_file_content: list[str] | None = None
        self._dryrun_stdout = dryrun_stdout
        self._real_stdout = real_stdout
        self._parse1_stdout = parse1_stdout
        self._parse2_stdout = parse2_stdout
        self._scan_stdout = scan_stdout
        self._scan_returncode = scan_returncode
        self._drift_stdout = drift_stdout
        self._commit_stdout = commit_stdout
        self._ci_returncode = ci_returncode
        self._ci_stdout = ci_stdout
        # Review: review-integrator — working tree diverged further than
        # the dispatch brief described: `_cmd_round` now runs a
        # `_dest_dirty_status` crash-recovery pre-flight (`git status
        # --porcelain` on `dest`) between the Step 3 gate and Step 4. Every
        # existing test needs this handled to stay clean/not-dirty by
        # default, matching today's "nothing landed yet" fixtures.
        self._dest_status_stdout = dest_status_stdout
        self._dest_status_returncode = dest_status_returncode
        # C3: a clean round now pushes by default — `git push` and the
        # no-op paths' `git status --porcelain=v2 --branch` ahead-count
        # check both need stub responses too.
        self._push_returncode = push_returncode
        self._dest_ahead_stdout = dest_ahead_stdout
        self._dest_ahead_returncode = dest_ahead_returncode
        self._reset_returncode = reset_returncode
        self._clean_returncode = clean_returncode
        self._rev_parse_stdout = rev_parse_stdout
        # `_build_commit_pathspec`'s pathspec-filter leg (§ gitignored /
        # already-absent deletion-intent drops) probes `git check-ignore`
        # and `git ls-files` at dest -- default to "nothing ignored" /
        # "tracked" so existing fixtures (no DELETE/REMOVE tags in the
        # default real-run stdout) stay unaffected.
        self._check_ignore_stdout = check_ignore_stdout
        self._check_ignore_returncode = check_ignore_returncode
        self._ls_files_returncode = ls_files_returncode
        # Review: coordinatorcode-reviewer-c58be590 (live-round follow-up) --
        # `_resolve_repo_root`'s `git rev-parse --show-toplevel` probe.
        # Defaults to `None` so the caller (`_run_round`) can bind it to the
        # fixture's own `dest`, preserving today's "dest is already the
        # worktree root" behaviour for every existing test unchanged.
        self._toplevel_stdout = toplevel_stdout
        self._toplevel_returncode = toplevel_returncode

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        # Review: coordinatorcode-reviewer-c58be590 -- kwargs were
        # discarded, so no test could assert `timeout=` actually reaches
        # the two `publish.py` legs.
        self.call_kwargs.append(dict(kwargs))
        joined = " ".join(str(c) for c in cmd)

        if "machine-local" in joined:
            return _completed(1, "", "")

        if cmd and str(cmd[0]) == "git" and any(str(t) == "push" for t in cmd):
            return _completed(self._push_returncode, "", "")

        if cmd and str(cmd[0]) == "git" and "reset" in cmd and "--hard" in cmd:
            return _completed(self._reset_returncode, "", "")

        if cmd and str(cmd[0]) == "git" and "clean" in cmd:
            return _completed(self._clean_returncode, "", "")

        if cmd and str(cmd[0]) == "git" and "rev-parse" in cmd and "--show-toplevel" in cmd:
            # Default (no explicit `toplevel_stdout`): echo back the `-C
            # <path>` argument itself, matching every existing fixture's
            # "dest is already the worktree root" shape unchanged.
            if self._toplevel_stdout is not None:
                return _completed(self._toplevel_returncode, self._toplevel_stdout, "")
            dash_c_path = ""
            for i, tok in enumerate(cmd):
                if str(tok) == "-C" and i + 1 < len(cmd):
                    dash_c_path = str(cmd[i + 1])
                    break
            return _completed(self._toplevel_returncode, f"{dash_c_path}\n", "")

        if cmd and str(cmd[0]) == "git" and "rev-parse" in cmd:
            return _completed(0, self._rev_parse_stdout, "")

        if cmd and str(cmd[0]) == "git" and "check-ignore" in cmd:
            return _completed(self._check_ignore_returncode, self._check_ignore_stdout, "")

        if cmd and str(cmd[0]) == "git" and "ls-files" in cmd:
            return _completed(self._ls_files_returncode, "", "")

        if "status" in cmd and "--porcelain=v2" in cmd:
            return _completed(self._dest_ahead_returncode, self._dest_ahead_stdout, "")

        if "status" in cmd and "--porcelain" in cmd:
            return _completed(self._dest_status_returncode, self._dest_status_stdout, "")

        if str(_mod._PUBLISH) in joined and "--dry-run" in cmd:
            return _completed(0, self._dryrun_stdout, "")
        if str(_mod._PUBLISH) in joined:
            return _completed(0, self._real_stdout, "")
        if str(_mod._PERCOLATE_GATE) in joined and "scan-secrets" in cmd:
            return _completed(self._scan_returncode, self._scan_stdout, "")
        if str(_mod._PERCOLATE_GATE) in joined and "inverse-drift" in cmd:
            return _completed(0, self._drift_stdout, "")
        if str(_mod._PARSE_DRYRUN) in joined and "--medium-leak-count" in cmd:
            return _completed(0, self._parse2_stdout, "")
        if str(_mod._PARSE_DRYRUN) in joined:
            return _completed(0, self._parse1_stdout, "")
        if str(_mod._SCOPED_GIT_COMMIT) in joined:
            if "--pathspec-from-file" in cmd:
                idx = cmd.index("--pathspec-from-file")
                self.pathspec_from_file_content = (
                    Path(str(cmd[idx + 1])).read_text(encoding="utf-8").splitlines()
                )
            return _completed(0, self._commit_stdout, "")
        if "run-all-checks.py" in joined:
            return _completed(self._ci_returncode, self._ci_stdout, "")
        raise AssertionError(f"unhandled subprocess.run call in test stub: {cmd!r}")


def _dryrun_stdout() -> str:
    # Deliberately different from the real-run change lines below — proves
    # the commit pathspec is derived from the REAL run, not the dry run.
    return "NEW: dryrun-only-file.md\n"


def _real_stdout() -> str:
    return "NEW: added-file.md\nUPDATE: changed-file.md\n"


def _parse1_stdout() -> str:
    return json.dumps({"preflight": {"step2c_scan_file_list": []}})


def _parse2_stdout(gate_fires: bool = False) -> str:
    return json.dumps(
        {
            "gates": {"step3_gate_fires": gate_fires},
            "judgment_points": [
                {
                    "id": "jp_step3_percolate_confirmation_gate",
                    "evidence": "1 medium hit(s)",
                }
            ],
        }
    )


def _run_round(tmp_path, monkeypatch, *, ci_returncode=0, ci_exists=True, gate_fires=False, yes=True,
                scan_returncode=0, commit_stdout='{"status": "ok"}', dest_status_stdout="",
                dest_status_returncode=0, no_publish=False, push_returncode=0,
                dest_ahead_stdout="", dest_ahead_returncode=0, percolate_root=None,
                reconcile_dest=None, reset_returncode=0, clean_returncode=0,
                rev_parse_stdout="deadbeef1234\n", no_delta=False,
                check_ignore_stdout="", check_ignore_returncode=1,
                ls_files_returncode=0, toplevel_stdout=None,
                toplevel_returncode=0, invocation_authorized=False,
                stdin_isatty=True, dry_run_first=True):
    dest = tmp_path / "dest"
    dest.mkdir()
    if ci_exists:
        ci_dir = dest / ".github" / "scripts"
        ci_dir.mkdir(parents=True)
        (ci_dir / "run-all-checks.py").write_text("", encoding="utf-8")

    source_dir = tmp_path / "source"
    source_dir.mkdir()

    if percolate_root is None:
        percolate_root = tmp_path / "percolate-root"
        (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=_real_stdout(),
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(gate_fires),
        ci_returncode=ci_returncode,
        scan_returncode=scan_returncode,
        commit_stdout=commit_stdout,
        dest_status_stdout=dest_status_stdout,
        dest_status_returncode=dest_status_returncode,
        push_returncode=push_returncode,
        dest_ahead_stdout=dest_ahead_stdout,
        dest_ahead_returncode=dest_ahead_returncode,
        reset_returncode=reset_returncode,
        clean_returncode=clean_returncode,
        rev_parse_stdout=rev_parse_stdout,
        check_ignore_stdout=check_ignore_stdout,
        check_ignore_returncode=check_ignore_returncode,
        ls_files_returncode=ls_files_returncode,
        toplevel_stdout=toplevel_stdout,
        toplevel_returncode=toplevel_returncode,
    )
    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    argv = ["alpha", "--percolate-root", str(percolate_root)]
    if dry_run_first:
        argv.append("--dry-run-first")
    if yes:
        argv.append("--yes")
    if no_publish:
        argv.append("--no-publish")
    if reconcile_dest is not None:
        argv += ["--reconcile-dest", reconcile_dest]
    if no_delta:
        argv.append("--no-delta")
    if invocation_authorized:
        argv.append("--invocation-authorized")
    args = parser.parse_args(argv)

    monkeypatch.setattr(_mod.sys.stdin, "isatty", lambda: stdin_isatty)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod._cmd_round(args)

    return rc, buf.getvalue(), spy, dest


# ---------------------------------------------------------------------------
# Load-bearing: absence of git push / allow-xrepo-write, asserted directly
# at the subprocess boundary and on the filesystem — not via source grep.
# ---------------------------------------------------------------------------

def test_no_publish_flag_prints_notice_and_does_not_push(tmp_path, monkeypatch):
    """`--no-publish` keeps the old print-and-stop terminus: no `git push`
    argv is ever spawned, and the printed command names the short
    `percolate-push` entry point, not a raw `git -C <abs-path> push` line
    (state/handoffs/2026-08-13-one-command-publish.md, shape 2)."""
    # Review: review-integrator — harden past the subprocess.run boundary:
    # a future edit routing a push through os.system or a direct
    # subprocess.Popen call would previously slip past this test silently
    # (see this module's own docstring). Any call to either now fails loud.
    def _forbidden_system(cmd, *a, **kw):
        raise AssertionError(f"os.system invoked: {cmd!r}")

    def _forbidden_popen(cmd, *a, **kw):
        raise AssertionError(f"subprocess.Popen invoked: {cmd!r}")

    # `percolate-round.py` has no module-level `os` import (only a local
    # `import os` inside `_build_commit_pathspec`) — patch the shared `os`
    # module object directly rather than a nonexistent `_mod.os` attribute;
    # any `import os` anywhere resolves to the same cached module.
    monkeypatch.setattr(os, "system", _forbidden_system)
    monkeypatch.setattr(_mod.subprocess, "Popen", _forbidden_popen)

    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, no_publish=True)
    assert rc == _mod._EXIT_OK
    for call in spy.calls:
        assert not any(str(token) == "push" for token in call)
    assert "percolate-push alpha" in out
    assert f"git -C {dest} push" not in out


def test_clean_round_pushes_by_default(tmp_path, monkeypatch):
    """AC2: a clean round publishes with no `--no-publish` opt-out and no
    operator step after the first command."""
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch)
    assert rc == _mod._EXIT_OK

    push_calls = [c for c in spy.calls if c and str(c[0]) == "git" and any(str(t) == "push" for t in c)]
    assert len(push_calls) == 1
    assert push_calls[0] == ["git", "-C", str(dest), "push"]
    assert f"Published: pushed to {dest}." in out

    commit_idx = next(
        i for i, c in enumerate(spy.calls) if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)
    )
    push_idx = spy.calls.index(push_calls[0])
    assert commit_idx < push_idx


def test_source_scan_no_allow_xrepo_write_marker_creation():
    """AC5: no code path creates or clears an `allow-xrepo-write` marker —
    a source scan, not just a runtime assertion, so a marker constructed
    but never exercised at runtime would still fail loud. The string still
    appears legitimately in prose (docstring/comments explaining this
    module does NOT touch it) — only a path/write construction is
    forbidden."""
    src = (_BIN_DIR / "percolate-round.py").read_text(encoding="utf-8")
    for line in src.splitlines():
        stripped = line.strip()
        if "allow-xrepo-write" not in stripped:
            continue
        assert not any(
            marker in stripped for marker in ("Path(", ".write_text(", ".touch(", "open(")
        ), f"allow-xrepo-write appears in a path/write construction: {stripped!r}"


def test_never_writes_allow_xrepo_write_marker(tmp_path, monkeypatch):
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch)
    assert rc == _mod._EXIT_OK
    hits = list(tmp_path.rglob("*allow-xrepo-write*"))
    assert hits == []
    for call in spy.calls:
        joined = " ".join(str(c) for c in call)
        assert "allow-xrepo-write" not in joined


# ---------------------------------------------------------------------------
# AC7 — commit pathspec has no directory element and is derived from the
# REAL run's change lines, not the dry-run's.
# ---------------------------------------------------------------------------

def test_commit_pathspec_derived_from_real_run_not_dry_run(tmp_path, monkeypatch):
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch)
    assert rc == _mod._EXIT_OK

    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert len(commit_calls) == 1
    commit_cmd = commit_calls[0]

    # AC (WinError 206 fix): the pathspec never rides argv — no bare `--`
    # separator is emitted by `_cmd_round` anymore, only a file reference.
    assert "--" not in commit_cmd
    assert "--pathspec-from-file" in commit_cmd
    assert spy.pathspec_from_file_content is not None
    pathspec = spy.pathspec_from_file_content

    # Review: coordinatorcode-reviewer-c58be590 (live-round follow-up) --
    # `5858489a8` (repo-relative pathspec entries) predates these
    # assertions; `_run_round`'s default `repo_root` echoes `dest` itself
    # (§ `_SubprocessSpy`'s `--show-toplevel` stub), so entries are
    # `dest`-relative, not absolute.
    assert "added-file.md" in pathspec
    assert "changed-file.md" in pathspec
    # The dry-run-only file must NOT leak into the pathspec.
    assert "dryrun-only-file.md" not in pathspec

    # Review: code-reviewer — `Path(entry).suffix` is not a valid file-ness
    # proxy (an extensionless tracked file like LICENSE would fail it); test
    # the actual AC7 claim instead: no entry is the dest root, and no entry
    # is a path-prefix of another entry (which a directory element would be).
    for entry in pathspec:
        assert entry != str(dest)
        assert Path(entry).name  # non-empty basename
        for other in pathspec:
            if other == entry:
                continue
            assert not other.startswith(entry + "/"), (
                f"{entry!r} is a directory prefix of {other!r}"
            )


# ---------------------------------------------------------------------------
# WinError 206 regression pin: a several-thousand-path pathspec must never
# ride the commit subprocess's argv. Windows CreateProcess caps a command
# line at 32767 characters, which a full-publish ~2000-path pathspec
# exceeds outright (the actual live-round failure this fix addresses). This
# cannot portably assert the Windows limit itself (this suite runs
# cross-platform) -- instead it asserts the argv this code hands
# `subprocess.run` stays small and bounded regardless of pathspec size,
# with every path routed through `--pathspec-from-file` instead.
# ---------------------------------------------------------------------------

def test_large_pathspec_commit_argv_stays_bounded_not_on_argv(tmp_path, monkeypatch):
    large_real_stdout = "".join(
        f"NEW: generated/file-{i:05d}.md\n" for i in range(4000)
    )

    dest = tmp_path / "dest"
    dest.mkdir()
    ci_dir = dest / ".github" / "scripts"
    ci_dir.mkdir(parents=True)
    (ci_dir / "run-all-checks.py").write_text("", encoding="utf-8")
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=large_real_stdout,
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(False),
    )
    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])
    monkeypatch.setattr(_mod.sys.stdin, "isatty", lambda: True)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod._cmd_round(args)
    assert rc == _mod._EXIT_OK

    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert len(commit_calls) == 1
    commit_cmd = commit_calls[0]

    # The regression this pins: 4000 individual path tokens must NOT appear
    # in the subprocess argv this code constructs — only a short file path
    # naming where they live.
    assert len(commit_cmd) < 20
    assert "--pathspec-from-file" in commit_cmd
    assert not any("generated/file-" in str(tok) for tok in commit_cmd)

    assert spy.pathspec_from_file_content is not None
    assert len(spy.pathspec_from_file_content) == 4000
    assert "generated/file-00000.md" in spy.pathspec_from_file_content
    assert "generated/file-03999.md" in spy.pathspec_from_file_content


def test_commit_ordered_after_ci_smoke_is_false_ci_runs_after_commit(tmp_path, monkeypatch):
    """AC8: CI smoke is ordered AFTER the commit."""
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch)
    assert rc == _mod._EXIT_OK

    def _first_index(marker: str) -> int:
        for i, call in enumerate(spy.calls):
            if marker in " ".join(str(c) for c in call):
                return i
        raise AssertionError(f"no call matched {marker!r}: {spy.calls!r}")

    commit_idx = _first_index(str(_mod._SCOPED_GIT_COMMIT))
    ci_idx = _first_index("run-all-checks.py")
    assert commit_idx < ci_idx


# ---------------------------------------------------------------------------
# AC8 — a red CI exit means no push command is printed and exit is non-zero.
# ---------------------------------------------------------------------------

def test_red_ci_prints_no_push_command_and_fails(tmp_path, monkeypatch):
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, ci_returncode=1)
    assert rc == _mod._EXIT_FAIL
    assert "git -C" not in out
    assert f"git -C {dest} push" not in out
    assert "percolate-push alpha" not in out
    for call in spy.calls:
        assert not any(str(token) == "push" for token in call)

    # The commit itself must still have landed (locally) before the red CI
    # was even observed -- CI-after-commit ordering holds on the FAIL path
    # too, not just the PASS path.
    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert len(commit_calls) == 1


# ---------------------------------------------------------------------------
# Review: code-reviewer — the gate_fires=True branch (evidence-print,
# --yes skipping the blocking input(), and the declined -> cancelled path)
# had no coverage; today's suite would pass identically if --yes were
# silently ignored, since the gate never fired to exercise it.
# ---------------------------------------------------------------------------

def test_gate_fires_with_yes_skips_input_and_prints_evidence(tmp_path, monkeypatch):
    """gate_fires=True, --yes: the evidence is printed, input() is never
    called (--yes's actual job), and the round proceeds to a real publish
    and commit."""
    input_calls = []
    monkeypatch.setattr("builtins.input", lambda *a, **k: input_calls.append(1) or "n")

    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, gate_fires=True, yes=True)

    assert rc == _mod._EXIT_OK
    assert input_calls == []
    assert "Step 3 gate fired: 1 medium hit(s)" in out
    assert "Proceed with real publish? [y/N] y (--yes)" in out

    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert len(commit_calls) == 1


def test_gate_fires_with_no_publish_falls_through_and_skips_only_push(tmp_path, monkeypatch):
    """gate_fires=True, --no-publish (--yes stays at `_run_round`'s default
    True): control falls through the gate to the real run, commits land,
    and `--no-publish` skips only the final `git push` -- it does not
    re-block on the gate a second time or skip the commit (DR-301)."""
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, gate_fires=True, no_publish=True)

    assert rc == _mod._EXIT_OK
    assert "Step 3 gate fired: 1 medium hit(s)" in out
    assert "Proceed with real publish? [y/N] y (--yes)" in out

    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert len(commit_calls) == 1

    for call in spy.calls:
        assert not any(str(token) == "push" for token in call)
    assert "percolate-push alpha" in out
    assert f"git -C {dest} push" not in out


def test_gate_fires_without_yes_declined_cancels_before_real_run(tmp_path, monkeypatch):
    """gate_fires=True, no --yes, operator declines: the round cancels with
    exit 0 and never reaches the real publish/commit steps."""
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, gate_fires=True, yes=False)

    assert rc == _mod._EXIT_OK
    assert "Publish cancelled." in out

    real_run_calls = [
        c for c in spy.calls
        if str(_mod._PUBLISH) in " ".join(str(x) for x in c) and "--dry-run" not in c
    ]
    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert real_run_calls == []
    assert commit_calls == []


# ---------------------------------------------------------------------------
# cross-repo/inbox/2026-08-14-doe-claude-em-percolate-round-non-tty-confirm-
# crashes.md + ...-depersonalize-corrupts-python-identifiers.md § 4: the
# Step 3 confirm must ride an explicit invocation token (not a bare
# isatty() check), else a cron/nested-agent caller could auto-proceed.
# ---------------------------------------------------------------------------

def test_gate_fires_with_invocation_authorized_skips_input_and_proceeds(tmp_path, monkeypatch):
    """--invocation-authorized (the skill-wrapper token): input() is never
    called and the round proceeds to a real publish and commit, exactly
    like --yes but via the distinct flag (--yes stays forbidden on an
    interactive PM session per the invoking skill's own rules)."""
    input_calls = []
    monkeypatch.setattr("builtins.input", lambda *a, **k: input_calls.append(1) or "n")

    rc, out, spy, dest = _run_round(
        tmp_path, monkeypatch, gate_fires=True, yes=False, invocation_authorized=True,
    )

    assert rc == _mod._EXIT_OK
    assert input_calls == []
    assert "Step 3 gate fired: 1 medium hit(s)" in out
    assert "Proceed with real publish? [y/N] y (--invocation-authorized)" in out

    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert len(commit_calls) == 1


def test_gate_fires_tty_without_token_still_prompts(tmp_path, monkeypatch):
    """A bare interactive tty invocation with no token behaves exactly as
    before: input() is called, unchanged from today's prompt."""
    input_calls = []
    monkeypatch.setattr("builtins.input", lambda *a, **k: input_calls.append(1) or "n")

    rc, out, spy, dest = _run_round(
        tmp_path, monkeypatch, gate_fires=True, yes=False, invocation_authorized=False,
        stdin_isatty=True,
    )

    assert rc == _mod._EXIT_OK
    assert input_calls == [1]
    assert "Publish cancelled." in out


def test_gate_fires_non_tty_without_token_named_refusal_no_eoferror(tmp_path, monkeypatch):
    """The regression that matters: a non-tty caller with no token must
    NOT hit input() (which would raise EOFError under redirected/closed
    stdin) -- it gets a named refusal at the distinct exit code instead,
    and never reaches the real publish/commit steps."""
    def _raise_eof(*a, **k):
        raise EOFError("EOF when reading a line")
    monkeypatch.setattr("builtins.input", _raise_eof)

    rc, out, spy, dest = _run_round(
        tmp_path, monkeypatch, gate_fires=True, yes=False, invocation_authorized=False,
        stdin_isatty=False,
    )

    assert rc == _mod._EXIT_CONFIRM_REQUIRED
    assert rc == 3
    assert "Step 3 confirm required" in out
    assert "Step 3 gate fired: 1 medium hit(s)" in out  # verdict-so-far still printed

    real_run_calls = [
        c for c in spy.calls
        if str(_mod._PUBLISH) in " ".join(str(x) for x in c) and "--dry-run" not in c
    ]
    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert real_run_calls == []
    assert commit_calls == []


# ---------------------------------------------------------------------------
# Review: code-reviewer — the HIGH-tier content-leak abort path (Step 2c)
# had no coverage; `_SubprocessSpy`'s default `scan_stdout` always yields
# rc 0, so a regression that stopped scan-secrets from returning 2 on a
# HIGH hit, or stopped round.py from checking for it, would pass unnoticed.
# ---------------------------------------------------------------------------

def test_high_tier_scan_hit_aborts_before_step3(tmp_path, monkeypatch):
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, scan_returncode=2)

    assert rc == _mod._EXIT_FAIL

    # Step 3/4/commit must never run — no publish real-run, no commit call.
    real_run_calls = [
        c for c in spy.calls
        if str(_mod._PUBLISH) in " ".join(str(x) for x in c) and "--dry-run" not in c
    ]
    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert real_run_calls == []
    assert commit_calls == []


# ---------------------------------------------------------------------------
# Review: code-reviewer — the `committed and declined_paths` partial-landed
# branch had no test; the default `commit_stdout` has no `committed`/
# `declined_paths` keys, so this whole `if` block was dead in the suite.
# ---------------------------------------------------------------------------

def test_commit_landed_with_declined_paths_reports_partial_and_fails(tmp_path, monkeypatch):
    commit_stdout = json.dumps(
        {
            "status": "partial",
            "committed": True,
            "sha": "abc123def456",
            "declined_paths": [{"path": "some/declined.md", "reason": "outside allowlist"}],
        }
    )
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, commit_stdout=commit_stdout)

    assert rc == _mod._EXIT_FAIL

    # The commit itself must have run exactly once — it DID land locally.
    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert len(commit_calls) == 1

    # CI smoke must never run past a partial-landed commit report.
    ci_calls = [c for c in spy.calls if "run-all-checks.py" in " ".join(str(x) for x in c)]
    assert ci_calls == []


# ---------------------------------------------------------------------------
# Review: code-reviewer — early-return failure branches in `_cmd_round` had
# no coverage; every one of these calls `_print_step_failure` and returns
# _EXIT_FAIL/_EXIT_USAGE, none previously asserted.
# ---------------------------------------------------------------------------

def test_resolve_percolate_root_failure_returns_usage_error(tmp_path, monkeypatch):
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(tmp_path / "unused"))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(tmp_path / "unused"))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    def _fail_run(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "resolve-root" in joined:
            return _completed(1, "", "no root")
        raise AssertionError(f"unexpected call before percolate-root resolution: {cmd!r}")

    monkeypatch.setattr(_mod.subprocess, "run", _fail_run)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--yes"])
    rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_USAGE


def test_branch0_gate_failure_returns_usage_error(tmp_path, monkeypatch):
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: None)
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(tmp_path / "unused"))

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])
    rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_USAGE


def test_resolve_dest_failure_returns_usage_error(tmp_path, monkeypatch):
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])
    rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_USAGE


def test_dryrun_step_failure_returns_fail(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    def _dryrun_fail(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if str(_mod._PUBLISH) in joined and "--dry-run" in cmd:
            return _completed(1, "", "dry-run exploded")
        raise AssertionError(f"unexpected call past dry-run failure: {cmd!r}")

    monkeypatch.setattr(_mod.subprocess, "run", _dryrun_fail)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes", "--dry-run-first"])
    rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_FAIL


def test_parse_dryrun_pass1_failure_returns_fail(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    def _parse1_fail(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if str(_mod._PUBLISH) in joined and "--dry-run" in cmd:
            return _completed(0, _dryrun_stdout(), "")
        if str(_mod._PARSE_DRYRUN) in joined:
            return _completed(1, "", "parse1 exploded")
        raise AssertionError(f"unexpected call past parse1 failure: {cmd!r}")

    monkeypatch.setattr(_mod.subprocess, "run", _parse1_fail)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes", "--dry-run-first"])
    rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_FAIL


def test_scan_secrets_non2_nonzero_failure_returns_fail(tmp_path, monkeypatch):
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, scan_returncode=1)
    assert rc == _mod._EXIT_FAIL


def test_inverse_drift_failure_returns_fail(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=_real_stdout(),
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
    )

    def _drift_fail(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if str(_mod._PERCOLATE_GATE) in joined and "inverse-drift" in cmd:
            return _completed(1, "", "drift exploded")
        return spy(cmd, **kwargs)

    monkeypatch.setattr(_mod.subprocess, "run", _drift_fail)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])
    rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_FAIL


def test_real_run_failure_returns_fail(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=_real_stdout(),
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
    )

    def _real_fail(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if str(_mod._PUBLISH) in joined and "--dry-run" not in cmd:
            return _completed(1, "", "real run exploded")
        return spy(cmd, **kwargs)

    monkeypatch.setattr(_mod.subprocess, "run", _real_fail)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])
    rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_FAIL


def test_real_run_partial_row_failure_exits_nonzero_with_verdict(tmp_path, monkeypatch):
    """Regression for the doe-claude-em-reported defect: a Step 4 real run
    that reports some rows failed (`Rows succeeded: 3/5`, `Rows FAILED: ...`
    on stderr, `STATUS: PARTIAL` on stderr) must never let the round exit 0
    or end silently — assert BOTH the exit code AND the printed verdict,
    since either alone would have missed the original regression (a caller
    that only checks exit code, or only greps output, each missed half of
    it)."""
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=_real_stdout(),
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
    )

    def _real_partial(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if str(_mod._PUBLISH) in joined and "--dry-run" not in cmd:
            return _completed(
                1,
                _real_stdout() + "Rows succeeded: 3/5 (a, b, c)\n",
                "Rows FAILED:    2 (coordinator-claude, coordinator-claude-publish-repo-toplevel)\n"
                "STATUS: PARTIAL — this publish is now PARTIALLY synced (3 row(s) landed, 2 did not).\n",
            )
        return spy(cmd, **kwargs)

    monkeypatch.setattr(_mod.subprocess, "run", _real_partial)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod._cmd_round(args)
    out = buf.getvalue()

    assert rc == _mod._EXIT_FAIL
    assert "percolate-round alpha — FAIL" in out
    assert "Rows succeeded: 3/5" in out


def test_real_run_partial_row_failure_via_stderr_only_still_fails(tmp_path, monkeypatch):
    """Defense-in-depth case: `publish.py` reports failed rows on stderr
    (`Rows FAILED:` / `STATUS: PARTIAL`) but its own exit code somehow comes
    back 0 — the round must not trust the exit code alone and must still
    fail loud with a verdict, since a future `publish.py` exit-code
    regression is exactly the shape of the original incident."""
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=_real_stdout(),
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
    )

    def _real_mismatch(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if str(_mod._PUBLISH) in joined and "--dry-run" not in cmd:
            return _completed(
                0,
                _real_stdout() + "Rows succeeded: 3/5 (a, b, c)\n",
                "Rows FAILED:    2 (coordinator-claude, coordinator-claude-publish-repo-toplevel)\n"
                "STATUS: PARTIAL — this publish is now PARTIALLY synced (3 row(s) landed, 2 did not).\n",
            )
        return spy(cmd, **kwargs)

    monkeypatch.setattr(_mod.subprocess, "run", _real_mismatch)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod._cmd_round(args)
    out = buf.getvalue()

    assert rc == _mod._EXIT_FAIL
    assert "percolate-round alpha — FAIL" in out
    assert "Rows succeeded: 3/5" in out


def test_all_rows_succeeded_still_exits_ok_with_verdict(tmp_path, monkeypatch):
    """A clean all-rows-succeeded run keeps exiting 0 and still prints its
    (PASS) verdict — the partial-failure fix must not regress the happy
    path into a false failure."""
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, ci_returncode=0, gate_fires=False)

    assert rc == _mod._EXIT_OK
    assert "percolate-round alpha — PASS" in out


def test_fully_failed_real_run_keeps_existing_exit_code(tmp_path, monkeypatch):
    """A real run where every requested row failed (`Rows succeeded: 0/2`,
    nonzero exit) keeps the existing `_EXIT_FAIL` contract — this fix only
    adds the verdict line and the stderr-only fail-safe; it does not move
    the fully-failed case's exit code."""
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=_real_stdout(),
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
    )

    def _real_all_failed(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if str(_mod._PUBLISH) in joined and "--dry-run" not in cmd:
            return _completed(
                1,
                "Rows succeeded: 0/2\n",
                "Rows FAILED:    2 (a, b)\n",
            )
        return spy(cmd, **kwargs)

    monkeypatch.setattr(_mod.subprocess, "run", _real_all_failed)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod._cmd_round(args)
    out = buf.getvalue()

    assert rc == _mod._EXIT_FAIL
    assert "percolate-round alpha — FAIL" in out
    assert "Rows succeeded: 0/2" in out


def test_generic_commit_failure_returns_fail(tmp_path, monkeypatch):
    commit_stdout = "not-json-and-nonzero-exit"
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=_real_stdout(),
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
        commit_stdout=commit_stdout,
    )

    def _commit_fail(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if str(_mod._SCOPED_GIT_COMMIT) in joined:
            return _completed(1, commit_stdout, "commit exploded")
        return spy(cmd, **kwargs)

    monkeypatch.setattr(_mod.subprocess, "run", _commit_fail)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])
    rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_FAIL


# ---------------------------------------------------------------------------
# Review: code-reviewer — the no-op path, PASS-WITH-WARNINGS verdict, and
# ci_exists=False branch were untested.
# ---------------------------------------------------------------------------

def test_noop_dryrun_reports_pass_noop(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout="",
        real_stdout="",
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
        # Review: coordinatorcode-reviewer-c58be590 -- `_dest_ahead_count`
        # now returns `None` (undetermined), not `0`, when the
        # `branch.ab` line is absent; this fixture needs an explicit
        # genuinely-in-sync line to exercise the "already in sync" leg.
        dest_ahead_stdout="# branch.ab +0 -0\n",
    )
    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes", "--dry-run-first"])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_OK
    assert "PASS (no-op): nothing to publish" in buf.getvalue()

    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert commit_calls == []


def test_review_warnings_yield_pass_with_warnings_verdict(tmp_path, monkeypatch):
    real_stdout_with_warning = _real_stdout() + "REVIEW WARNING: check this file\n"

    percolate_root = tmp_path / "percolate-root2"
    (percolate_root / "setup").mkdir(parents=True)
    dest2 = tmp_path / "dest2"
    dest2.mkdir()
    source_dir2 = tmp_path / "source2"
    source_dir2.mkdir()

    spy2 = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=real_stdout_with_warning,
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
    )
    monkeypatch.setattr(_mod.subprocess, "run", spy2)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir2))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest2))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc2 = _mod._cmd_round(args)

    assert rc2 == _mod._EXIT_OK
    out2 = buf.getvalue()
    assert "PASS-WITH-WARNINGS" in out2
    assert "Phase 4 audit found REVIEW items" in out2


def test_no_ci_script_at_dest_skips_ci_smoke(tmp_path, monkeypatch):
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, ci_exists=False)

    assert rc == _mod._EXIT_OK
    assert "(no .github/scripts/run-all-checks.py at dest" in out
    assert "ci-smoke:  n/a (no run-all-checks.py)" in out

    ci_calls = [c for c in spy.calls if "run-all-checks.py" in " ".join(str(x) for x in c)]
    assert ci_calls == []


def test_gate_fires_without_yes_accepted_proceeds(tmp_path, monkeypatch):
    """gate_fires=True, no --yes, operator accepts: input() is consulted
    and the round proceeds through commit."""
    input_calls = []

    def _fake_input(prompt=""):
        input_calls.append(prompt)
        return "y"

    monkeypatch.setattr("builtins.input", _fake_input)

    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, gate_fires=True, yes=False)

    assert rc == _mod._EXIT_OK
    assert len(input_calls) == 1
    assert "Proceed with real publish?" in input_calls[0]

    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert len(commit_calls) == 1


# ---------------------------------------------------------------------------
# Review: review-integrator — `_dest_dirty_status` crash-recovery pre-flight
# had no coverage: a dirty dest must abort before Step 4/commit, a clean
# dest must proceed unaffected, and a `git status` failure ("undetermined")
# must proceed while still printing the loud stderr line that exists
# specifically so an undetermined answer can't become a silent pass.
# ---------------------------------------------------------------------------

def test_dest_dirty_status_clean_tree_returns_none(tmp_path, monkeypatch):
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        return _completed(0, "", "")

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)
    assert _mod._dest_dirty_status(str(tmp_path)) is None
    assert any("status" in c and "--porcelain" in c for c in calls)


def test_dest_dirty_status_dirty_tree_returns_porcelain_output(tmp_path, monkeypatch):
    porcelain = " M some/file.md\n"

    def _fake_run(cmd, **kwargs):
        return _completed(0, porcelain, "")

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)
    assert _mod._dest_dirty_status(str(tmp_path)) == porcelain


def test_dest_dirty_status_git_failure_is_undetermined_and_warns(tmp_path, monkeypatch, capsys):
    def _fake_run(cmd, **kwargs):
        return _completed(128, "", "fatal: not a git repository")

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)
    assert _mod._dest_dirty_status(str(tmp_path)) is None
    err = capsys.readouterr().err
    assert "dest dirty-check: undetermined" in err
    assert "proceeding unchecked" in err


def test_dirty_dest_aborts_round_before_real_run_and_commit(tmp_path, monkeypatch):
    rc, out, spy, dest = _run_round(
        tmp_path, monkeypatch, dest_status_stdout=" M pre-existing.md\n"
    )

    assert rc == _mod._EXIT_FAIL

    real_run_calls = [
        c for c in spy.calls
        if str(_mod._PUBLISH) in " ".join(str(x) for x in c) and "--dry-run" not in c
    ]
    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert real_run_calls == []
    assert commit_calls == []


def test_clean_dest_proceeds_through_commit(tmp_path, monkeypatch):
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, dest_status_stdout="")

    assert rc == _mod._EXIT_OK
    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert len(commit_calls) == 1


def test_undetermined_dest_status_proceeds_with_stderr_warning(tmp_path, monkeypatch, capsys):
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, dest_status_returncode=128)

    assert rc == _mod._EXIT_OK
    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert len(commit_calls) == 1
    err = capsys.readouterr().err
    assert "dest dirty-check: undetermined" in err
    assert "proceeding unchecked" in err


# ---------------------------------------------------------------------------
# `--reconcile-dest` (refuse [default] | discard) — the in-tool remedy the
# refusal message names instead of directing the operator to hand-run git
# inside a publish mirror.
# ---------------------------------------------------------------------------

def test_default_reconcile_dest_is_refuse_dirty_dest_still_refuses(tmp_path, monkeypatch):
    """No `--reconcile-dest` flag at all -- default stays `refuse`, same
    exit code as before this change."""
    rc, out, spy, dest = _run_round(
        tmp_path, monkeypatch, dest_status_stdout=" M pre-existing.md\n"
    )
    assert rc == _mod._EXIT_FAIL
    reset_calls = [c for c in spy.calls if str(c[0]) == "git" and "reset" in c]
    assert reset_calls == []


def test_refusal_message_names_the_reconcile_dest_flag(tmp_path, monkeypatch, capsys):
    rc, out, spy, dest = _run_round(
        tmp_path, monkeypatch, dest_status_stdout=" M pre-existing.md\n"
    )
    assert rc == _mod._EXIT_FAIL
    err = capsys.readouterr().err
    assert "--reconcile-dest=discard" in err


def test_reconcile_dest_discard_clears_residue_and_proceeds(tmp_path, monkeypatch):
    rc, out, spy, dest = _run_round(
        tmp_path,
        monkeypatch,
        dest_status_stdout=" M pre-existing.md\n?? untracked.txt\n",
        reconcile_dest="discard",
        dest_ahead_stdout="# branch.ab +0 -0\n",
    )
    assert rc == _mod._EXIT_OK
    reset_calls = [c for c in spy.calls if str(c[0]) == "git" and "reset" in c and "--hard" in c]
    clean_calls = [c for c in spy.calls if str(c[0]) == "git" and "clean" in c]
    assert len(reset_calls) == 1
    assert len(clean_calls) == 1
    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert len(commit_calls) == 1
    assert "discarded 2 path(s)" in out
    assert "deadbeef1234" in out


def test_reconcile_dest_discard_ahead_of_remote_resets_and_preserves_commit(tmp_path, monkeypatch):
    """The old ahead-count refusal was specified in error: `git reset --hard
    HEAD` never destroys commits, only uncommitted working-tree/index state
    -- an unpushed commit survives a reset to HEAD intact. Discard now
    resets and proceeds, reporting the ahead count rather than refusing."""
    rc, out, spy, dest = _run_round(
        tmp_path,
        monkeypatch,
        dest_status_stdout=" M pre-existing.md\n",
        reconcile_dest="discard",
        dest_ahead_stdout="# branch.ab +2 -0\n",
    )
    assert rc == _mod._EXIT_OK
    reset_calls = [c for c in spy.calls if str(c[0]) == "git" and "reset" in c and "--hard" in c]
    assert len(reset_calls) == 1
    assert "ahead of remote by 2 commit(s)" in out
    assert "reset to HEAD preserves them" in out


def test_reconcile_dest_discard_ahead_probe_failure_still_refuses(tmp_path, monkeypatch, capsys):
    """The one remaining refusal condition: an undeterminable ahead count
    (the probe itself failed) is refused under an unknown state, never
    treated as zero."""
    rc, out, spy, dest = _run_round(
        tmp_path,
        monkeypatch,
        dest_status_stdout=" M pre-existing.md\n",
        reconcile_dest="discard",
        dest_ahead_returncode=128,
    )
    assert rc == _mod._EXIT_FAIL
    reset_calls = [c for c in spy.calls if str(c[0]) == "git" and "reset" in c and "--hard" in c]
    assert reset_calls == []
    err = capsys.readouterr().err
    assert "could not determine" in err
    assert "refusing to discard" in err


def test_yes_alone_never_triggers_discard(tmp_path, monkeypatch):
    """`--yes` answers the Step 3 publish confirmation only -- a dirty dest
    with `--yes` but no `--reconcile-dest=discard` still refuses, never
    resets."""
    rc, out, spy, dest = _run_round(
        tmp_path, monkeypatch, dest_status_stdout=" M pre-existing.md\n", yes=True
    )
    assert rc == _mod._EXIT_FAIL
    reset_calls = [c for c in spy.calls if str(c[0]) == "git" and "reset" in c]
    clean_calls = [c for c in spy.calls if str(c[0]) == "git" and "clean" in c]
    assert reset_calls == []
    assert clean_calls == []


def test_reconcile_dest_discard_function_direct(tmp_path, monkeypatch):
    """Direct unit coverage of `_reconcile_dest_discard`, mirroring the
    `_dest_dirty_status` direct-call tests above."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "rev-parse" in cmd:
            return _completed(0, "abc123\n", "")
        if "status" in cmd and "--porcelain=v2" in cmd:
            return _completed(0, "# branch.ab +0 -0\n", "")
        if "reset" in cmd or "clean" in cmd:
            return _completed(0, "", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)
    ok, message = _mod._reconcile_dest_discard(str(tmp_path), " M a.md\n?? b.txt\n")
    assert ok is True
    assert "abc123" in message
    assert "discarded 2 path(s)" in message
    assert any("reset" in c and "--hard" in c for c in calls)
    assert any("clean" in c and "-fd" in c for c in calls)


def test_reconcile_dest_discard_function_ahead_of_remote_succeeds_and_preserves_commit(tmp_path, monkeypatch):
    """A reset to HEAD never destroys commits -- only uncommitted
    working-tree/index state. `dest` carrying 3 unpushed commits must not
    refuse the discard; the reset preserves them, and the report names the
    count."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "rev-parse" in cmd:
            return _completed(0, "abc123\n", "")
        if "status" in cmd and "--porcelain=v2" in cmd:
            return _completed(0, "# branch.ab +3 -0\n", "")
        if "reset" in cmd or "clean" in cmd:
            return _completed(0, "", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)
    ok, message = _mod._reconcile_dest_discard(str(tmp_path), " M a.md\n")
    assert ok is True
    assert "ahead of remote by 3 commit(s)" in message
    assert "reset to HEAD preserves them" in message
    assert any("reset" in c and "--hard" in c for c in calls)


def test_reconcile_dest_discard_function_refuses_on_ahead_probe_failure(tmp_path, monkeypatch):
    def _fake_run(cmd, **kwargs):
        if "status" in cmd and "--porcelain=v2" in cmd:
            return _completed(128, "", "fatal")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)
    ok, message = _mod._reconcile_dest_discard(str(tmp_path), " M a.md\n")
    assert ok is False
    assert "could not determine" in message


def test_dest_ahead_count_no_upstream_line_is_undetermined_not_zero(tmp_path, monkeypatch):
    """Review: coordinatorcode-reviewer-c58be590 -- git omits the
    `# branch.ab` line entirely when the checked-out branch has no
    upstream tracking ref (or dest is detached HEAD). That must return
    `None` (undetermined), the same as a probe failure -- never fall
    through to `ahead = 0`, which is indistinguishable from a real
    zero-ahead dest to every consumer."""
    def _fake_run(cmd, **kwargs):
        if "status" in cmd and "--porcelain=v2" in cmd:
            # Exit 0, but no `# branch.ab` line at all -- the real shape
            # git emits for a branch with no upstream configured.
            return _completed(0, "# branch.oid deadbeef\n? untracked.txt\n", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)
    assert _mod._dest_ahead_count(str(tmp_path)) is None


def test_dest_ahead_count_genuine_zero_is_distinguished(tmp_path, monkeypatch):
    """The real +0 case (upstream configured, genuinely in sync) must
    still return `0`, not `None` -- this and the no-upstream case above
    must be distinguishable."""
    def _fake_run(cmd, **kwargs):
        if "status" in cmd and "--porcelain=v2" in cmd:
            return _completed(0, "# branch.ab +0 -0\n", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)
    assert _mod._dest_ahead_count(str(tmp_path)) == 0


def test_dest_ahead_probe_distinguishes_no_upstream_from_probe_failure():
    """`_dest_ahead_probe` is the three-way source of truth
    `_reconcile_dest_discard` acts on (PM ruling, 2026-08-14): no-upstream
    is `(None, False, True)` -- definite, probe succeeded -- never
    conflated with a genuine probe failure `(None, False, False)`."""
    def _fake_run_no_upstream(cmd, **kwargs):
        return _completed(0, "# branch.oid deadbeef\n? untracked.txt\n", "")

    def _fake_run_failure(cmd, **kwargs):
        return _completed(128, "", "fatal: not a git repository")

    import unittest.mock as mock

    with mock.patch.object(_mod.subprocess, "run", _fake_run_no_upstream):
        ahead, has_upstream, probe_ok = _mod._dest_ahead_probe("/fake/dest")
    assert ahead is None
    assert has_upstream is False
    assert probe_ok is True

    with mock.patch.object(_mod.subprocess, "run", _fake_run_failure):
        ahead, has_upstream, probe_ok = _mod._dest_ahead_probe("/fake/dest")
    assert ahead is None
    assert has_upstream is False
    assert probe_ok is False


def test_reconcile_dest_discard_function_no_upstream_proceeds_permissively(tmp_path, monkeypatch):
    """PM ruling, 2026-08-14: no upstream tracking ref is a definite,
    non-error state (a fresh mirror never pushed) -- discard must PROCEED,
    not refuse, and must report the no-upstream state distinguishably from
    both the ahead-by-N and probe-failure reports."""
    calls = []

    def _fake_run(cmd, **kwargs):
        calls.append(list(cmd))
        if "rev-parse" in cmd:
            return _completed(0, "cafef00d\n", "")
        if "status" in cmd and "--porcelain=v2" in cmd:
            return _completed(0, "# branch.oid cafef00d\n? untracked.txt\n", "")
        if "reset" in cmd or "clean" in cmd:
            return _completed(0, "", "")
        raise AssertionError(f"unhandled: {cmd!r}")

    monkeypatch.setattr(_mod.subprocess, "run", _fake_run)
    ok, message = _mod._reconcile_dest_discard(str(tmp_path), " M a.md\n")
    assert ok is True
    assert "no upstream tracking ref" in message
    assert "could not determine" not in message
    assert any("reset" in c and "--hard" in c for c in calls)
    assert any("clean" in c and "-fd" in c for c in calls)


def test_reconcile_dest_discard_real_repo_no_upstream_proceeds_and_resets(tmp_path):
    """Real-git-repo coverage (no mocking of the git boundary itself): a
    freshly `git init`-ed dest with a commit but no `git push`/upstream
    configured at all is the literal 'fresh mirror, never pushed' case the
    PM ruling names -- discard must run `git reset --hard` + `git clean
    -fd` against it and leave the repo's committed history intact."""
    dest = tmp_path / "fresh-mirror"
    dest.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731 -- local test helper
        ["git", "-C", str(dest), *args], check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "test")
    (dest / "tracked.txt").write_text("v1\n")
    run("add", "tracked.txt")
    run("commit", "-q", "-m", "initial")

    (dest / "tracked.txt").write_text("dirty uncommitted edit\n")
    (dest / "untracked.txt").write_text("stray\n")

    ok, message = _mod._reconcile_dest_discard(str(dest), " M tracked.txt\n?? untracked.txt\n")

    assert ok is True
    assert "no upstream tracking ref" in message
    assert (dest / "tracked.txt").read_text() == "v1\n"
    assert not (dest / "untracked.txt").exists()
    log = subprocess.run(
        ["git", "-C", str(dest), "log", "--oneline"], check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert "initial" in log.stdout


# ---------------------------------------------------------------------------
# state/audits/2026-08-13-percolate-round-race-repro.md — Step 4's real-run
# subprocess and the scoped-git-commit subprocess must be held under one
# `held_lock(dest)` acquired by `_cmd_round` itself, spanning the whole
# sequence (closing the gap `publish.py`'s own internal held_lock leaves
# open between its own release and the commit subprocess starting).
# ---------------------------------------------------------------------------

class _RecordingLockCtx:
    """Stands in for `_round_held_lock`'s real context manager: records
    'acquire'/'release' markers into the same call-order list the subprocess
    spy appends to, so a test can assert the lock's acquire precedes the
    Step 4 real-run call and its release follows the commit call — proving
    the lock SPANS both, not merely that it is acquired somewhere."""

    def __init__(self, order: List[str], target, **kwargs):
        self._order = order

    def __enter__(self):
        self._order.append("lock-acquired")
        return self

    def __exit__(self, exc_type, exc, tb):
        self._order.append("lock-released")
        return False


def test_lock_spans_real_run_and_commit(tmp_path, monkeypatch):
    """The held lock's acquire happens before Step 4's real-run subprocess
    and its release happens after the commit subprocess — not narrower."""
    order: List[str] = []

    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=_real_stdout(),
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
    )

    def _recording_run(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if str(_mod._PUBLISH) in joined and "--dry-run" not in cmd:
            order.append("real-run")
        elif str(_mod._SCOPED_GIT_COMMIT) in joined:
            order.append("commit")
        return spy(cmd, **kwargs)

    monkeypatch.setattr(_mod.subprocess, "run", _recording_run)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)
    monkeypatch.setattr(
        _mod, "_round_held_lock", lambda target, **kw: _RecordingLockCtx(order, target, **kw)
    )

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])
    rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_OK
    assert order == ["lock-acquired", "real-run", "commit", "lock-released"], order


def test_lock_timeout_fails_loud_before_real_run(tmp_path, monkeypatch):
    """A contended dest fails FAST via `_print_step_failure`'s `_EXIT_FAIL`
    path, naming that another round is running against the dest, and never
    reaches the real-run or commit subprocesses."""
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=_real_stdout(),
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
    )

    class _TimeoutLockCtx:
        def __init__(self, target, **kwargs):
            pass

        def __enter__(self):
            raise _mod._RoundLockTimeout("another holder has target locked")

        def __exit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)
    monkeypatch.setattr(_mod, "_round_held_lock", lambda target, **kw: _TimeoutLockCtx(target, **kw))

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])

    buf = io.StringIO()
    with contextlib.redirect_stderr(buf):
        rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_FAIL
    err = buf.getvalue()
    assert "another round is running against this dest" in err

    real_run_calls = [
        c for c in spy.calls
        if str(_mod._PUBLISH) in " ".join(str(x) for x in c) and "--dry-run" not in c
    ]
    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert real_run_calls == []
    assert commit_calls == []


# ---------------------------------------------------------------------------
# C2/AC3 — `_round_refusal_reason` predicate coverage: one test per real
# early-return path in `_cmd_round` (failed row, non-empty `declined_paths`,
# CI-red), each asserting refusal, the named reason, and that no push occurs.
# Per the C2 calibration, the first two conditions never reach the
# predicate's own call site (post-CI-smoke) at all — `_cmd_round` already
# returns FAIL earlier, with its own existing reason text; forcing those
# conditions at the gate site itself would mean monkeypatching states
# unreachable in production. Only the CI-red case reaches the predicate's
# call site and surfaces its own `refusal_reason` string.
# ---------------------------------------------------------------------------

def test_failed_row_refuses_with_reason_and_no_push(tmp_path, monkeypatch):
    """A failed real-run row (`publish.py` exits non-zero) is the first
    refusing condition `_round_refusal_reason` names
    ("the real publish run did not succeed") — `_cmd_round` returns FAIL for
    it at Step 4, before CI smoke or the predicate's own call site are ever
    reached."""
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=_real_stdout(),
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
    )

    def _real_fail(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if str(_mod._PUBLISH) in joined and "--dry-run" not in cmd:
            return _completed(1, "", "real run exploded")
        return spy(cmd, **kwargs)

    monkeypatch.setattr(_mod.subprocess, "run", _real_fail)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes", "--dry-run-first"])

    out_buf = io.StringIO()
    err_buf = io.StringIO()
    with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
        rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_FAIL
    assert "Step 4 (real run) failed" in err_buf.getvalue()

    assert _mod._round_refusal_reason(
        real_returncode=1,
        declined_paths=[],
        has_review_warnings=False,
        ci_exit=None,
    ) == "the real publish run did not succeed"

    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    ci_calls = [c for c in spy.calls if "run-all-checks.py" in " ".join(str(x) for x in c)]
    assert commit_calls == []
    assert ci_calls == []
    for call in spy.calls:
        assert not any(str(token) == "push" for token in call)


def test_declined_paths_refuses_with_reason_and_no_push(tmp_path, monkeypatch):
    """Non-empty `declined_paths` is the second refusing condition
    `_round_refusal_reason` names — `_cmd_round` returns FAIL for it at the
    commit branch, before CI smoke or the predicate's own call site are ever
    reached."""
    commit_stdout = json.dumps(
        {
            "status": "partial",
            "committed": True,
            "sha": "abc123def456",
            "declined_paths": [{"path": "some/declined.md", "reason": "outside allowlist"}],
        }
    )
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, commit_stdout=commit_stdout)

    assert rc == _mod._EXIT_FAIL

    assert _mod._round_refusal_reason(
        real_returncode=0,
        declined_paths=[{"path": "some/declined.md", "reason": "outside allowlist"}],
        has_review_warnings=False,
        ci_exit=None,
    ) == "1 path(s) were declined during commit"

    ci_calls = [c for c in spy.calls if "run-all-checks.py" in " ".join(str(x) for x in c)]
    assert ci_calls == []
    for call in spy.calls:
        assert not any(str(token) == "push" for token in call)


def test_ci_red_refuses_with_named_reason_and_no_push(tmp_path, monkeypatch):
    """CI-red is the one refusing condition that actually reaches
    `_round_refusal_reason`'s own call site in `_cmd_round` (the other two
    already returned early) — the terminal message names the CI failure
    instead of a generic "not clean"."""
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, ci_returncode=1)

    assert rc == _mod._EXIT_FAIL
    assert "percolate-round: publish refused — CI smoke came back red (exit 1)" in out

    assert _mod._round_refusal_reason(
        real_returncode=0,
        declined_paths=[],
        has_review_warnings=False,
        ci_exit=1,
    ) == "CI smoke came back red (exit 1)"

    for call in spy.calls:
        assert not any(str(token) == "push" for token in call)


# ---------------------------------------------------------------------------
# PM ruling 1 (2026-08-14) — the round-failure marker. C4's own reader
# (`percolate-push.py::_round_failure_marker_path`) MUST agree byte-for-byte
# with this module's writer on path and JSON shape.
# ---------------------------------------------------------------------------

def _marker_path(percolate_root: Path, target: str = "alpha") -> Path:
    return percolate_root / "setup" / "percolate-state" / f"{target}.round-failed.json"


def test_declined_paths_failure_writes_round_failure_marker(tmp_path, monkeypatch):
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)
    commit_stdout = json.dumps(
        {
            "status": "partial",
            "committed": True,
            "sha": "abc123def456",
            "declined_paths": [{"path": "some/declined.md", "reason": "outside allowlist"}],
        }
    )
    rc, out, spy, dest = _run_round(
        tmp_path, monkeypatch, commit_stdout=commit_stdout, percolate_root=percolate_root
    )
    assert rc == _mod._EXIT_FAIL

    marker = _marker_path(percolate_root)
    assert marker.is_file()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["reason"] == "declined_paths"
    assert data["sha"] == "abc123def456"
    assert "timestamp" in data


def test_ci_red_failure_writes_round_failure_marker(tmp_path, monkeypatch):
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)
    commit_stdout = json.dumps({"status": "ok", "committed": True, "sha": "deadbeef0001"})
    rc, out, spy, dest = _run_round(
        tmp_path, monkeypatch, ci_returncode=1, commit_stdout=commit_stdout, percolate_root=percolate_root
    )
    assert rc == _mod._EXIT_FAIL

    marker = _marker_path(percolate_root)
    assert marker.is_file()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["reason"] == "ci_red"
    assert data["sha"] == "deadbeef0001"
    assert "timestamp" in data


def test_subsequent_clean_round_clears_marker_before_publishing(tmp_path, monkeypatch):
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup" / "percolate-state").mkdir(parents=True)
    marker = _marker_path(percolate_root)
    marker.write_text(
        json.dumps({"reason": "ci_red", "sha": "stale0001", "timestamp": "2026-08-01T00:00:00Z"}),
        encoding="utf-8",
    )

    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, percolate_root=percolate_root)

    assert rc == _mod._EXIT_OK
    assert not marker.exists()
    push_calls = [c for c in spy.calls if c and str(c[0]) == "git" and any(str(t) == "push" for t in c)]
    assert len(push_calls) == 1


# ---------------------------------------------------------------------------
# Review: review-integrator (P2 polarity inversion) — the round-failure
# marker is now written IMMEDIATELY once a commit lands (before CI smoke and
# the gate), not only on a failure path, so a crash anywhere after the
# commit lands leaves the marker standing (fail-safe) instead of leaving a
# landed-but-uncertified commit unmarked. It is cleared only on a genuinely
# clean verdict (`refusal_reason is None`), whether or not this round
# itself pushes.
# ---------------------------------------------------------------------------

def test_crash_after_commit_before_ci_smoke_leaves_marker_standing(tmp_path, monkeypatch):
    """A process death between the commit landing and CI smoke completing
    must leave the marker in place — simulated here by making the CI-smoke
    subprocess call raise, so the round never reaches the clear-marker
    branch at all."""
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)
    commit_stdout = json.dumps({"status": "ok", "committed": True, "sha": "cafebabe0001"})

    dest = tmp_path / "dest"
    dest.mkdir()
    ci_dir = dest / ".github" / "scripts"
    ci_dir.mkdir(parents=True)
    (ci_dir / "run-all-checks.py").write_text("", encoding="utf-8")
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=_real_stdout(),
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
        commit_stdout=commit_stdout,
    )

    class _SimulatedCrash(Exception):
        pass

    def _crash_on_ci(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if "run-all-checks.py" in joined:
            raise _SimulatedCrash("process died mid CI-smoke")
        return spy(cmd, **kwargs)

    monkeypatch.setattr(_mod.subprocess, "run", _crash_on_ci)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])

    with pytest.raises(_SimulatedCrash):
        _mod._cmd_round(args)

    marker = _marker_path(percolate_root)
    assert marker.is_file()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["reason"] == "uncommitted-verdict"
    assert data["sha"] == "cafebabe0001"

    push_calls = [c for c in spy.calls if c and str(c[0]) == "git" and any(str(t) == "push" for t in c)]
    assert push_calls == []


def test_clean_round_clears_marker_before_pushing_itself(tmp_path, monkeypatch):
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, percolate_root=percolate_root)

    assert rc == _mod._EXIT_OK
    marker = _marker_path(percolate_root)
    assert not marker.exists()
    push_calls = [c for c in spy.calls if c and str(c[0]) == "git" and any(str(t) == "push" for t in c)]
    assert len(push_calls) == 1


def test_clean_round_with_no_publish_still_clears_marker(tmp_path, monkeypatch):
    """`--no-publish` only defers the push, not certification — a genuinely
    clean round must clear the marker even though it doesn't push itself,
    so a subsequent manual `percolate-push` is not falsely refused."""
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    rc, out, spy, dest = _run_round(
        tmp_path, monkeypatch, no_publish=True, percolate_root=percolate_root
    )

    assert rc == _mod._EXIT_OK
    marker = _marker_path(percolate_root)
    assert not marker.exists()
    push_calls = [c for c in spy.calls if c and str(c[0]) == "git" and any(str(t) == "push" for t in c)]
    assert push_calls == []


def test_review_warnings_refusal_leaves_marker_standing_even_with_no_publish(tmp_path, monkeypatch):
    """A `refusal_reason`-refused round (unacknowledged review warnings)
    leaves the marker standing regardless of `--no-publish` — that commit
    is genuinely uncertified, not merely deferred."""
    real_stdout_with_warning = _real_stdout() + "REVIEW WARNING: check this file\n"
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)
    commit_stdout = json.dumps({"status": "ok", "committed": True, "sha": "deadbeefcafe"})

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=real_stdout_with_warning,
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
        commit_stdout=commit_stdout,
    )
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()

    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(
        ["alpha", "--percolate-root", str(percolate_root), "--yes", "--no-publish"]
    )

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_OK
    marker = _marker_path(percolate_root)
    assert marker.is_file()
    data = json.loads(marker.read_text(encoding="utf-8"))
    assert data["reason"] == "uncommitted-verdict"
    assert data["sha"] == "deadbeefcafe"
    for call in spy.calls:
        assert not any(str(token) == "push" for token in call)


def test_review_warnings_refuse_publish_and_print_notice_naming_reason(tmp_path, monkeypatch):
    """PASS-WITH-WARNINGS is a refusing condition (C2) — `_print_push_notice`
    names it rather than pushing or failing silently."""
    real_stdout_with_warning = _real_stdout() + "REVIEW WARNING: check this file\n"
    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=real_stdout_with_warning,
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
    )
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod._cmd_round(args)

    out = buf.getvalue()
    assert rc == _mod._EXIT_OK
    assert "PASS-WITH-WARNINGS" in out
    assert "Publish refused — Phase 4 audit found unacknowledged REVIEW warnings" in out
    assert "percolate-push alpha" in out
    for call in spy.calls:
        assert not any(str(token) == "push" for token in call)


# ---------------------------------------------------------------------------
# AC2b — a no-op round still evaluates the gate and publishes unpushed dest
# commits from an earlier round that stopped at the old print-and-stop
# terminus; a no-op round with a dest already in sync does nothing and says
# so. Covered for both no-op branches: the dry-run-detects-nothing branch
# (before Step 4 ever runs) and the real-run-reports-nothing-to-commit
# branch (after Step 4, still under the held lock).
# ---------------------------------------------------------------------------

def test_dryrun_noop_with_unpushed_dest_commits_still_publishes(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout="",
        real_stdout="",
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
        dest_ahead_stdout="# branch.ab +2 -0\n",
    )
    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes", "--dry-run-first"])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod._cmd_round(args)

    out = buf.getvalue()
    assert rc == _mod._EXIT_OK
    assert "PASS (no-op): nothing to publish" in out
    push_calls = [c for c in spy.calls if c and str(c[0]) == "git" and any(str(t) == "push" for t in c)]
    assert len(push_calls) == 1
    assert "pushed 2 unpushed commit(s)" in out


def test_dryrun_noop_with_dest_already_in_sync_does_nothing(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout="",
        real_stdout="",
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
        dest_ahead_stdout="# branch.ab +0 -0\n",
    )
    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod._cmd_round(args)

    out = buf.getvalue()
    assert rc == _mod._EXIT_OK
    assert "already in sync with its upstream" in out
    push_calls = [c for c in spy.calls if c and str(c[0]) == "git" and any(str(t) == "push" for t in c)]
    assert push_calls == []


def test_realrun_noop_with_unpushed_dest_commits_still_publishes(tmp_path, monkeypatch):
    """The real-run-reports-nothing-to-commit branch, exercised while the
    round's own lock is already held (must not re-acquire it)."""
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout="",
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
        dest_ahead_stdout="# branch.ab +1 -0\n",
    )
    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod._cmd_round(args)

    out = buf.getvalue()
    assert rc == _mod._EXIT_OK
    assert "real run reported no changed files; nothing to commit" in out
    push_calls = [c for c in spy.calls if c and str(c[0]) == "git" and any(str(t) == "push" for t in c)]
    assert len(push_calls) == 1


# ---------------------------------------------------------------------------
# The push runs INSIDE the held lock, not after it releases (Anti-scope /
# state/audits/2026-08-13-percolate-round-race-repro.md, extended by C3).
# ---------------------------------------------------------------------------

def test_push_happens_inside_held_lock(tmp_path, monkeypatch):
    order: List[str] = []

    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=_real_stdout(),
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
    )

    def _recording_run(cmd, **kwargs):
        joined = " ".join(str(c) for c in cmd)
        if str(_mod._PUBLISH) in joined and "--dry-run" not in cmd:
            order.append("real-run")
        elif str(_mod._SCOPED_GIT_COMMIT) in joined:
            order.append("commit")
        elif cmd and str(cmd[0]) == "git" and any(str(t) == "push" for t in cmd):
            order.append("push")
        return spy(cmd, **kwargs)

    monkeypatch.setattr(_mod.subprocess, "run", _recording_run)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)
    monkeypatch.setattr(
        _mod, "_round_held_lock", lambda target, **kw: _RecordingLockCtx(order, target, **kw)
    )

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])
    rc = _mod._cmd_round(args)

    assert rc == _mod._EXIT_OK
    assert order == ["lock-acquired", "real-run", "commit", "push", "lock-released"], order


# ---------------------------------------------------------------------------
# --delta-by-default wiring (PM ruling: --delta is the round's default mode).
# ---------------------------------------------------------------------------

def _publish_calls(spy):
    return [c for c in spy.calls if str(_mod._PUBLISH) in " ".join(str(x) for x in c)]


def _publish_call_kwargs(spy):
    return [
        kw
        for c, kw in zip(spy.calls, spy.call_kwargs)
        if str(_mod._PUBLISH) in " ".join(str(x) for x in c)
    ]


def _commit_call_kwargs(spy):
    return [
        kw
        for c, kw in zip(spy.calls, spy.call_kwargs)
        if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)
    ]


def _non_publish_call_kwargs(spy):
    return [
        kw
        for c, kw in zip(spy.calls, spy.call_kwargs)
        if str(_mod._PUBLISH) not in " ".join(str(x) for x in c)
        and str(_mod._SCOPED_GIT_COMMIT) not in " ".join(str(x) for x in c)
    ]


def test_delta_is_passed_to_publish_by_default(tmp_path, monkeypatch):
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch)

    assert rc == _mod._EXIT_OK
    publish_calls = _publish_calls(spy)
    assert publish_calls, "expected at least one publish.py invocation"
    for call in publish_calls:
        assert "--delta" in call, call


def test_no_delta_flag_suppresses_delta_on_every_publish_call(tmp_path, monkeypatch):
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, no_delta=True)

    assert rc == _mod._EXIT_OK
    publish_calls = _publish_calls(spy)
    assert publish_calls, "expected at least one publish.py invocation"
    for call in publish_calls:
        assert "--delta" not in call, call


def test_publish_legs_use_heavier_timeout_other_legs_keep_default(tmp_path, monkeypatch):
    """Review: coordinatorcode-reviewer-c58be590 -- `_SubprocessSpy` previously
    discarded `**kwargs`, so no test asserted `timeout=_PUBLISH_LEG_TIMEOUT_SECS`
    actually reached the two `publish.py` legs (`519cc8baf7`'s whole point).
    Pins both the heavier bound on the publish legs and the shared default
    on every other leg. The `scoped-git-commit` leg is excluded from "other"
    here -- it carries the same heavier bound, covered by
    test_commit_leg_uses_publish_leg_timeout_not_shared_default below."""
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch)

    assert rc == _mod._EXIT_OK
    publish_kwargs = _publish_call_kwargs(spy)
    assert len(publish_kwargs) == 2, publish_kwargs
    for kw in publish_kwargs:
        assert kw.get("timeout") == _mod._PUBLISH_LEG_TIMEOUT_SECS, kw

    other_kwargs = _non_publish_call_kwargs(spy)
    assert other_kwargs, "expected at least one non-publish.py invocation"
    for kw in other_kwargs:
        assert kw.get("timeout") == _mod._SUBPROCESS_TIMEOUT_SECS, kw


def test_commit_leg_uses_publish_leg_timeout_not_shared_default(tmp_path, monkeypatch):
    """The `scoped-git-commit` leg commits a full publish round's files --
    the same weight class as the two `publish.py` legs, not the light
    single-`git`-command legs the shared 600s bound is sized for (see the
    `_PUBLISH_LEG_TIMEOUT_SECS` docstring). Covers BOTH call sites: the
    `--dry-run-first` path (`_cmd_round`'s inline branch) and the default
    no-dry-run path (`_cmd_round_default`)."""
    for dry_run_first in (True, False):
        case_dir = tmp_path / str(dry_run_first)
        case_dir.mkdir()
        rc, out, spy, dest = _run_round(case_dir, monkeypatch, dry_run_first=dry_run_first)
        assert rc == _mod._EXIT_OK

        commit_kwargs = _commit_call_kwargs(spy)
        assert len(commit_kwargs) == 1, (dry_run_first, commit_kwargs)
        assert commit_kwargs[0].get("timeout") == _mod._PUBLISH_LEG_TIMEOUT_SECS, (
            dry_run_first, commit_kwargs[0],
        )


# ---------------------------------------------------------------------------
# Review: coordinatorcode-reviewer-c58be590 (live-round follow-up) --
# `dest` can be a subdirectory of the mirror's actual git worktree root;
# `--repo` and the pathspec's `repo_root` must resolve to, and share, that
# worktree root rather than `dest` itself.
# ---------------------------------------------------------------------------

def test_commit_uses_resolved_worktree_root_not_dest(tmp_path, monkeypatch):
    """`dest` is a `dest_subdir` beneath the worktree root -- `--repo` on
    the `scoped-git-commit` invocation must be the RESOLVED root, not
    `dest`, and it must be the identical value used to build the
    pathspec."""
    # `_run_round`'s own fixture `dest` is `tmp_path / "dest"` -- bind the
    # fake worktree root to its parent so `dest` genuinely resolves beneath
    # it, matching the real `dest_subdir` shape this finding describes.
    worktree_root = tmp_path
    rc, out, spy, _dest = _run_round(
        tmp_path, monkeypatch,
        toplevel_stdout=f"{worktree_root}\n",
    )
    # `_run_round` hard-codes its own fixture `dest`; re-derive the commit
    # call directly rather than relying on its returned `dest`.
    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert commit_calls, "expected a scoped-git-commit invocation"
    commit_cmd = commit_calls[0]
    repo_idx = commit_cmd.index("--repo")
    assert commit_cmd[repo_idx + 1] == str(worktree_root)
    assert rc == _mod._EXIT_OK


def test_repo_root_resolution_failure_returns_fail(tmp_path, monkeypatch):
    """`git rev-parse --show-toplevel` failing (e.g. dest not inside a git
    worktree) must fail the round loudly rather than fall back to `dest`
    silently."""
    rc, out, spy, dest = _run_round(
        tmp_path, monkeypatch,
        toplevel_stdout="",
        toplevel_returncode=1,
    )
    assert rc == _mod._EXIT_FAIL


# ---------------------------------------------------------------------------
# D1 regression -- inherited-holder handoff (§ docs/plans/2026-08-14-
# percolate-round-deadlock-and-gate-attribution.md, chunk C1). `_cmd_round`
# spans `_round_held_lock(Path(dest))` across the Step 4 real-run subprocess;
# `publish.py::main`'s own lock loop re-acquires the SAME key
# (`held_lock`'s `sha1(realpath(target))`) whenever a row's dest resolves to
# that same repo root. `held_lock` is a real flock, non-reentrant even
# WITHIN one process (its own module docstring: "Do NOT call held_lock
# re-entrantly on the same target in the same process") -- these tests
# reproduce the collision directly, in-process, with no subprocess spawn
# needed, exactly the hazard that docstring warns about.
#
# Pre-fix failure evidence (this docstring, not a stashed file): before this
# chunk's fix, `publish.py::main`'s lock loop had no knowledge of
# `PERCOLATE_ROUND_INHERITED_LOCK_ROOTS` at all -- it unconditionally
# attempted `_publish_held_lock` for every resolved root, so
# `test_inherited_root_does_not_deadlock` below (which holds the parent lock
# over root A, sets the inheritance env var for A only, and expects `main`
# to complete both rows) reproduces the exact D1 timeout: with the fix
# reverted (env var absent from the check, or the `continue` skip removed),
# `main` blocks on root A until `LOCK_TIMEOUT_SECS`, returns 1, and
# `rows_reached` never gets past the lock loop -- confirmed by temporarily
# reverting the `publish.py` lock-loop edit and re-running this test locally
# (`could not acquire the per-destination publish lock for <A>` on stderr,
# `rc == 1`, `rows_reached == []`).
# ---------------------------------------------------------------------------

def _init_git_repo_for_lock(root: Path) -> None:
    """Minimal git init so `_dest_repo_root` resolves `root` as a repo root
    (a bare `.git` directory is sufficient -- `held_lock`'s key derivation
    is pure path arithmetic over `os.path.realpath`, it never inspects git
    history)."""
    (root / ".git").mkdir(parents=True, exist_ok=True)


def _load_publish_module_for_lock_test():
    spec = importlib.util.spec_from_file_location(
        "publish_percolate_round_lock_handoff_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    import sys as _sys

    _sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _wire_lock_test_fakes(publish_mod, monkeypatch, tmp_path, row_dests: "dict[str, Path]", *, rows_reached: list):
    """Same shape as `test_publish_row_isolation.py`'s `_wire_common_fakes`
    -- stubs every precondition `main()` runs before its OWN lock loop (Part
    B) and row loop, so this test drives the REAL lock-acquisition code
    (the code under test for D1) rather than a hand-rolled stand-in for it."""

    def fake_row(name: str, dest: Path) -> str:
        src = tmp_path / f"src-{name}"
        src.mkdir(parents=True, exist_ok=True)
        return f"{name}|mirror|{src}|{dest}"

    monkeypatch.setattr(
        publish_mod, "_resolve_percolate_root_and_rung", lambda **kw: (tmp_path, "test-rung")
    )
    monkeypatch.setattr(
        publish_mod, "load_targets", lambda setup_dir, target_filter=None: [
            fake_row(name, dest) for name, dest in row_dests.items()
        ]
    )

    class _FakeClaudeKlabauter:
        def resolve_target(self, store, name):
            raise KeyError(name)

        def run_parse_sweep(self, repo_root):
            return type("ParseResult", (), {"ok": True, "failures": [], "scanned": 0})()

        def enumerate_gate_entrypoints(self, repo_root):
            return ()

    monkeypatch.setattr(publish_mod, "_import_claude_klabauter_percolate", lambda: _FakeClaudeKlabauter())
    monkeypatch.setattr(publish_mod, "assert_percolate_store_ready", lambda engine_claude_klabauter, path: {})
    monkeypatch.setattr(publish_mod, "locate_percolate_store", lambda setup_dir: tmp_path / "store.yaml")
    monkeypatch.setattr(publish_mod, "resolve_percolate_identity_path", lambda setup_dir: tmp_path / "id")
    monkeypatch.setattr(publish_mod, "check_identity_file_present", lambda path, setup_dir: tmp_path / "id")
    monkeypatch.setattr(publish_mod, "check_identity_file_safe", lambda path: None)
    monkeypatch.setattr(
        publish_mod,
        "parse_percolate_identity",
        lambda path: publish_mod.PercolateIdentity(review=["dummy-pattern"]),
    )
    monkeypatch.setattr(publish_mod, "_resolve_publish_sync_module_path", lambda setup_dir: tmp_path / "publish_sync.py")
    monkeypatch.setattr(publish_mod, "_import_publish_sync", lambda setup_dir: object())
    monkeypatch.setattr(publish_mod, "check_publish_sync_contract", lambda *a, **k: None)
    monkeypatch.setattr(publish_mod, "dispatch_end_of_run_identity_check", lambda *a, **k: True)
    monkeypatch.setattr(publish_mod, "dispatch_end_of_run_install_doc_payload_check", lambda *a, **k: True)
    monkeypatch.setattr(publish_mod, "dispatch_end_of_run_unscanned_published_check", lambda *a, **k: True)

    def fake_process_target(target, setup_dir, totals, **kwargs):
        rows_reached.append(target.name)
        totals.processed += 1

    monkeypatch.setattr(publish_mod, "process_target", fake_process_target)


@pytest.mark.spawns_process
def test_inherited_root_does_not_deadlock(tmp_path, monkeypatch):
    """AC2, half 1 -- the parent (`percolate-round.py`) already holds
    `_round_held_lock` over root A when it spawns Step 4; `publish.py` must
    NOT re-attempt to acquire A when A's realpath is named in
    `PERCOLATE_ROUND_INHERITED_LOCK_ROOTS`. Root B (a second row's dest,
    NOT held by anyone) must still get acquired and the row loop reached for
    both rows -- proves the skip is per-root, not a blanket disable."""
    import coordinator_core.locked_write as locked_write

    publish_mod = _load_publish_module_for_lock_test()

    root_a = tmp_path / "dest-a"
    root_b = tmp_path / "dest-b"
    root_a.mkdir()
    root_b.mkdir()
    _init_git_repo_for_lock(root_a)
    _init_git_repo_for_lock(root_b)

    # Speed up the timeout for this test only -- `timeout` is a keyword-only
    # parameter on the real generator function `@contextmanager` wraps
    # (`held_lock.__wrapped__`), so its default lives in that function's own
    # `__kwdefaults__` and is safely patchable without touching any call
    # site.
    monkeypatch.setitem(locked_write.held_lock.__wrapped__.__kwdefaults__, "timeout", 1.0)

    # `publish_mod.main` runs IN-PROCESS below (no subprocess spawn despite
    # this test's `spawns_process` marker), so `os.getppid()` observed
    # inside `main`'s PID-verification is THIS test process's own
    # `os.getppid()` -- not `os.getpid()` -- matching the real wire format
    # (`"<true parent pid>=<realpath>"`, § code-reviewer P2 fail-closed fix).
    monkeypatch.setenv(
        "PERCOLATE_ROUND_INHERITED_LOCK_ROOTS",
        f"{os.getppid()}={os.path.realpath(str(root_a))}",
    )

    rows_reached: list = []
    _wire_lock_test_fakes(
        publish_mod, monkeypatch, tmp_path,
        {"row-a": root_a, "row-b": root_b},
        rows_reached=rows_reached,
    )

    with _mod._round_held_lock(Path(root_a), holder_label="percolate-round:test-target"):
        rc = publish_mod.main(["row-a,row-b"])

    assert rc == 0
    assert rows_reached == ["row-a", "row-b"]
    assert "PERCOLATE_ROUND_INHERITED_LOCK_ROOTS" not in os.environ


@pytest.mark.spawns_process
def test_non_inherited_root_still_locked(tmp_path, monkeypatch):
    """AC2, half 2 -- a root NOT named in the inheritance env var must still
    go through real lock acquisition. Root A is inherited (parent holds it,
    skipped); root B is independently held by a THIRD party (not the
    parent, not this run) and is NOT inherited -- `publish.py` must still
    try to acquire B, time out, and fail the run, proving the skip did not
    silently widen into "skip all locking when the token is present."""
    import coordinator_core.locked_write as locked_write

    publish_mod = _load_publish_module_for_lock_test()

    root_a = tmp_path / "dest-a"
    root_b = tmp_path / "dest-b"
    root_a.mkdir()
    root_b.mkdir()
    _init_git_repo_for_lock(root_a)
    _init_git_repo_for_lock(root_b)

    monkeypatch.setitem(locked_write.held_lock.__wrapped__.__kwdefaults__, "timeout", 1.0)

    monkeypatch.setenv(
        "PERCOLATE_ROUND_INHERITED_LOCK_ROOTS",
        f"{os.getppid()}={os.path.realpath(str(root_a))}",
    )

    rows_reached: list = []
    _wire_lock_test_fakes(
        publish_mod, monkeypatch, tmp_path,
        {"row-a": root_a, "row-b": root_b},
        rows_reached=rows_reached,
    )

    with _mod._round_held_lock(Path(root_a), holder_label="percolate-round:test-target"):
        with locked_write.held_lock(Path(root_b), holder_label="third-party-holder"):
            rc = publish_mod.main(["row-a,row-b"])

    assert rc == 1
    assert rows_reached == []


@pytest.mark.spawns_process
def test_inherited_root_pid_mismatch_still_locked(tmp_path, monkeypatch):
    """§ code-reviewer P2 fail-closed fix -- a token whose PID does NOT
    match `os.getppid()` (a stray exported value, or a nested/second-order
    invocation) must be treated as absent: the named root still goes
    through real lock acquisition and the run fails exactly as if no token
    were present at all."""
    import coordinator_core.locked_write as locked_write

    publish_mod = _load_publish_module_for_lock_test()

    root_a = tmp_path / "dest-a"
    root_a.mkdir()
    _init_git_repo_for_lock(root_a)

    monkeypatch.setitem(locked_write.held_lock.__wrapped__.__kwdefaults__, "timeout", 1.0)

    # A syntactically well-formed token, but with a PID that cannot be the
    # true parent (`os.getppid()` inside `main` below is this test
    # process's own getppid() -- guaranteed not equal to a PID one past the
    # max representable signed 32-bit value).
    monkeypatch.setenv(
        "PERCOLATE_ROUND_INHERITED_LOCK_ROOTS",
        f"2147483647={os.path.realpath(str(root_a))}",
    )

    rows_reached: list = []
    _wire_lock_test_fakes(
        publish_mod, monkeypatch, tmp_path,
        {"row-a": root_a},
        rows_reached=rows_reached,
    )

    with locked_write.held_lock(Path(root_a), holder_label="third-party-holder"):
        rc = publish_mod.main(["row-a"])

    assert rc == 1
    assert rows_reached == []


@pytest.mark.spawns_process
def test_inherited_root_malformed_token_still_locked(tmp_path, monkeypatch):
    """§ code-reviewer P2 fail-closed fix -- a malformed entry (no `=`
    delimiter, or a non-integer PID) must not be treated as an inherited
    root: fail closed, still lock."""
    import coordinator_core.locked_write as locked_write

    publish_mod = _load_publish_module_for_lock_test()

    root_a = tmp_path / "dest-a"
    root_a.mkdir()
    _init_git_repo_for_lock(root_a)

    monkeypatch.setitem(locked_write.held_lock.__wrapped__.__kwdefaults__, "timeout", 1.0)

    # No `=` delimiter at all -- the pre-fix bare-realpath format.
    monkeypatch.setenv(
        "PERCOLATE_ROUND_INHERITED_LOCK_ROOTS", os.path.realpath(str(root_a))
    )

    rows_reached: list = []
    _wire_lock_test_fakes(
        publish_mod, monkeypatch, tmp_path,
        {"row-a": root_a},
        rows_reached=rows_reached,
    )

    with locked_write.held_lock(Path(root_a), holder_label="third-party-holder"):
        rc = publish_mod.main(["row-a"])

    assert rc == 1
    assert rows_reached == []


def test_step4_inherited_lock_token_uses_producer_own_pid(tmp_path, monkeypatch):
    """Producer-side pin for the § `_INHERITED_LOCK_ROOTS_ENV` wire format.

    The consumer-side tests above (`test_inherited_root_*`) hand-build their
    own `f"{os.getppid()}=..."` fixture tokens and document, in comments,
    that this is an intentional inversion -- `publish.main` runs IN-PROCESS
    in those tests, so `os.getppid()` observed there is the test process's
    own value, standing in for "the real parent's pid" as seen from a
    simulated child. Nothing in that file exercises what `_cmd_round`
    itself actually writes.

    This test closes that gap: it runs `_cmd_round` for real (via
    `_run_round`, the module's own subprocess-boundary stub) and inspects
    the `env=` kwarg on the Step 4 real-run `publish.py` call -- proving
    the producer writes `f"{os.getpid()}=<realpath>"` using ITS OWN pid
    (`_cmd_round`'s pid, which is this test process's `os.getpid()`, since
    the test directly invokes `_cmd_round`), not `os.getppid()` or a bare
    realpath. A silent swap to `getppid()` here would never match a real
    child's `getppid()` -- publish.py's skip would never fire, and the D1
    deadlock this token exists to prevent would return with the whole
    suite still green."""
    orig_environ = dict(os.environ)

    rc, _out, spy, dest = _run_round(tmp_path, monkeypatch)
    assert rc == _mod._EXIT_OK

    real_run_kwargs = [
        kwargs
        for cmd, kwargs in zip(spy.calls, spy.call_kwargs)
        if str(_mod._PUBLISH) in " ".join(str(c) for c in cmd) and "--dry-run" not in cmd
    ]
    assert len(real_run_kwargs) == 1, "expected exactly one Step 4 real-run publish.py call"
    real_env = real_run_kwargs[0].get("env")
    assert real_env is not None, "Step 4 real-run call must pass an explicit env="

    token = real_env.get(_mod._INHERITED_LOCK_ROOTS_ENV)
    assert token is not None, f"{_mod._INHERITED_LOCK_ROOTS_ENV} missing from Step 4 env"

    pid_part, _, realpath_part = token.partition("=")
    assert pid_part == str(os.getpid()), (
        "token pid component must be the producer's OWN pid (os.getpid()), "
        f"not os.getppid() or anything else -- got {pid_part!r}, expected {os.getpid()!r}"
    )
    assert realpath_part == os.path.realpath(str(dest))

    # The token must exist only in the child's env copy -- never mutate the
    # parent (`_cmd_round`'s own, i.e. this test process's) os.environ.
    assert _mod._INHERITED_LOCK_ROOTS_ENV not in os.environ
    assert os.environ == orig_environ


# ---------------------------------------------------------------------------
# PM ruling, 2026-08-15: the dry run is optional, not default. `_run_round`'s
# own default (`dry_run_first=True`) keeps every test above pinned to the old
# `--dry-run-first` code path unchanged; the tests below cover the NEW
# default (no dry run at all) and the flag that restores the old order.
# ---------------------------------------------------------------------------

def test_default_never_invokes_publish_with_dry_run_flag(tmp_path, monkeypatch):
    """Without `--dry-run-first`, `publish.py` is invoked exactly once
    (the real run) -- `--dry-run` never appears in any `_PUBLISH` call."""
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, dry_run_first=False)

    assert rc == _mod._EXIT_OK
    publish_calls = [c for c in spy.calls if str(_mod._PUBLISH) in " ".join(str(x) for x in c)]
    assert len(publish_calls) == 1
    assert "--dry-run" not in publish_calls[0]
    assert "Step 1: real run (sync)" in out
    assert "Step 2: content-leakage scan" in out
    assert "Step 2b: inverse-drift detection" in out


def test_dry_run_first_flag_restores_old_step_order(tmp_path, monkeypatch):
    """`--dry-run-first` still runs a `--dry-run` publish.py leg before the
    real one -- two `_PUBLISH` calls total, dry run first."""
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch, dry_run_first=True)

    assert rc == _mod._EXIT_OK
    publish_calls = [c for c in spy.calls if str(_mod._PUBLISH) in " ".join(str(x) for x in c)]
    assert len(publish_calls) == 2
    assert "--dry-run" in publish_calls[0]
    assert "--dry-run" not in publish_calls[1]
    assert "Step 2: dry run" in out


def test_default_gate_fires_evidence_sourced_from_real_run(tmp_path, monkeypatch):
    """Default mode: the Step 3 gate still fires on the same evidence, and
    the printed change summary/first-10-paths come from the (single) real
    run's own change lines -- never a second materialization."""
    input_calls = []
    monkeypatch.setattr("builtins.input", lambda *a, **k: input_calls.append(1) or "n")

    rc, out, spy, dest = _run_round(
        tmp_path, monkeypatch, dry_run_first=False, gate_fires=True, yes=True,
    )

    assert rc == _mod._EXIT_OK
    assert input_calls == []
    assert "Step 3 gate fired: 1 medium hit(s)" in out
    assert "added-file.md" in out  # from _real_stdout(), not _dryrun_stdout()
    assert "dryrun-only-file.md" not in out

    publish_calls = [c for c in spy.calls if str(_mod._PUBLISH) in " ".join(str(x) for x in c)]
    assert len(publish_calls) == 1

    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert len(commit_calls) == 1


def test_default_gate_fires_declined_leaves_synced_but_uncommitted(tmp_path, monkeypatch):
    """Default mode, gate fires, operator declines: the round already
    materialized the sync (revertible), so it must NOT re-run publish.py a
    second time and must NOT commit -- but the real run itself already
    happened before the decline (unlike the old --dry-run-first order)."""
    monkeypatch.setattr("builtins.input", lambda *a, **k: "n")

    rc, out, spy, dest = _run_round(
        tmp_path, monkeypatch, dry_run_first=False, gate_fires=True, yes=False,
    )

    assert rc == _mod._EXIT_OK
    assert "Publish cancelled." in out

    publish_calls = [c for c in spy.calls if str(_mod._PUBLISH) in " ".join(str(x) for x in c)]
    assert len(publish_calls) == 1  # the sync already happened
    commit_calls = [c for c in spy.calls if str(_mod._SCOPED_GIT_COMMIT) in " ".join(str(x) for x in c)]
    assert commit_calls == []


def test_default_no_op_reports_pass_noop_from_single_real_run(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    dest.mkdir()
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout="",
        real_stdout="",
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(),
        dest_ahead_stdout="# branch.ab +0 -0\n",
    )
    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    args = parser.parse_args(["alpha", "--percolate-root", str(percolate_root), "--yes"])

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod._cmd_round(args)

    out = buf.getvalue()
    assert rc == _mod._EXIT_OK
    assert "nothing to commit" in out
    assert "already in sync with its upstream" in out

    publish_calls = [c for c in spy.calls if str(_mod._PUBLISH) in " ".join(str(x) for x in c)]
    assert len(publish_calls) == 1
    assert "--dry-run" not in publish_calls[0]


def test_default_flag_help_text_cites_pm_ruling(tmp_path, monkeypatch):
    parser = _mod._build_parser()
    help_text = parser.format_help()
    assert "--dry-run-first" in help_text
    assert "2026-08-15" in help_text
