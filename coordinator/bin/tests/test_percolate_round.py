"""test_percolate_round — binds C2's stop-before-push contract
(docs/plans/2026-08-13-percolate-round-one-command-and-scan-register.md
§ C2/AC6-AC10) to real assertions instead of the module's own docstring.

Every sibling CLI `percolate-round.py` sequences is stubbed at the
`subprocess.run` boundary (never via a source grep, which passes on a file
that greps its own docstring, and never via `mod._run` alone, which would
miss a hypothetical direct `subprocess.run`/`Popen` call bypassing that
wrapper) — no real publish, commit, or CI process ever spawns.

Push-detection threat model (Review: code-reviewer): the never-push
assertions cover the `subprocess.run` boundary only — a hypothetical
`os.system("git ... push")` or direct `subprocess.Popen` call bypassing
`subprocess.run` would not be caught here. Neither is used anywhere in this
diff today.

Run: python -m pytest coordinator/bin/tests/test_percolate_round.py -q
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
from pathlib import Path
from typing import List

import pytest

pytestmark = [pytest.mark.cadence]

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
                 drift_stdout="anchor_mode: 30day-fallback\n",
                 commit_stdout='{"status": "ok"}',
                 ci_returncode=0,
                 ci_stdout="all green\n"):
        self.calls: List[List[str]] = []
        self._dryrun_stdout = dryrun_stdout
        self._real_stdout = real_stdout
        self._parse1_stdout = parse1_stdout
        self._parse2_stdout = parse2_stdout
        self._scan_stdout = scan_stdout
        self._drift_stdout = drift_stdout
        self._commit_stdout = commit_stdout
        self._ci_returncode = ci_returncode
        self._ci_stdout = ci_stdout

    def __call__(self, cmd, **kwargs):
        self.calls.append(list(cmd))
        joined = " ".join(str(c) for c in cmd)

        # Token-exact, not substring: a pytest tmp_path for this very test
        # module legitimately contains the substring "push" in its own
        # directory name (e.g. "test_red_ci_prints_no_push_command"), which
        # a naive substring check on the full joined argv would misfire on.
        if any(str(token) == "push" for token in cmd):
            raise AssertionError(f"subprocess.run invoked with a 'push' argv token: {cmd!r}")

        if "machine-local" in joined:
            return _completed(1, "", "")

        if str(_mod._PUBLISH) in joined and "--dry-run" in cmd:
            return _completed(0, self._dryrun_stdout, "")
        if str(_mod._PUBLISH) in joined:
            return _completed(0, self._real_stdout, "")
        if str(_mod._PERCOLATE_GATE) in joined and "scan-secrets" in cmd:
            return _completed(0, self._scan_stdout, "")
        if str(_mod._PERCOLATE_GATE) in joined and "inverse-drift" in cmd:
            return _completed(0, self._drift_stdout, "")
        if str(_mod._PARSE_DRYRUN) in joined and "--medium-leak-count" in cmd:
            return _completed(0, self._parse2_stdout, "")
        if str(_mod._PARSE_DRYRUN) in joined:
            return _completed(0, self._parse1_stdout, "")
        if str(_mod._SCOPED_GIT_COMMIT) in joined:
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


def _run_round(tmp_path, monkeypatch, *, ci_returncode=0, ci_exists=True, gate_fires=False, yes=True):
    dest = tmp_path / "dest"
    dest.mkdir()
    if ci_exists:
        ci_dir = dest / ".github" / "scripts"
        ci_dir.mkdir(parents=True)
        (ci_dir / "run-all-checks.py").write_text("", encoding="utf-8")

    source_dir = tmp_path / "source"
    source_dir.mkdir()

    percolate_root = tmp_path / "percolate-root"
    (percolate_root / "setup").mkdir(parents=True)

    spy = _SubprocessSpy(
        dryrun_stdout=_dryrun_stdout(),
        real_stdout=_real_stdout(),
        parse1_stdout=_parse1_stdout(),
        parse2_stdout=_parse2_stdout(gate_fires),
        ci_returncode=ci_returncode,
    )
    monkeypatch.setattr(_mod.subprocess, "run", spy)
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source_dir))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(dest))
    monkeypatch.setattr(_mod, "_resolve_central_state", lambda: None)

    parser = _mod._build_parser()
    argv = ["alpha", "--percolate-root", str(percolate_root)]
    if yes:
        argv.append("--yes")
    args = parser.parse_args(argv)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = _mod._cmd_round(args)

    return rc, buf.getvalue(), spy, dest


# ---------------------------------------------------------------------------
# Load-bearing: absence of git push / allow-xrepo-write, asserted directly
# at the subprocess boundary and on the filesystem — not via source grep.
# ---------------------------------------------------------------------------

def test_never_invokes_git_push(tmp_path, monkeypatch):
    rc, out, spy, dest = _run_round(tmp_path, monkeypatch)
    assert rc == _mod._EXIT_OK
    for call in spy.calls:
        assert not any(str(token) == "push" for token in call)
    # The push command is only ever PRINTED, never argv-shaped.
    assert f"git -C {dest} push" in out


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

    dash_idx = commit_cmd.index("--")
    pathspec = commit_cmd[dash_idx + 1 :]

    assert str(dest / "added-file.md") in pathspec
    assert str(dest / "changed-file.md") in pathspec
    # The dry-run-only file must NOT leak into the pathspec.
    assert str(dest / "dryrun-only-file.md") not in pathspec

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
