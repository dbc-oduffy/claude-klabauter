"""test_workday_complete_reconcile.py — regression suite for
workday-complete-reconcile.py's two ported bash fences (/workday-complete
Step 1.5 cruft-sweep dispatch, Step 2.6 completion-entry reconcile sweep
with cross-machine liveness gating).

Covers: cruft-sweep non-blocking WARN-on-nonzero behavior; month-scoped
pending-release-only gating; malformed authored_by (missing/null) silent
skip; own-session vs cross-machine dispatch routing (the F1 fix — a
cross-machine dead entry is reconciled with ITS OWN authored_by as
--session-id, never the wrapping session's); the live-cross-machine
stand-down; appended-count accumulation/summary line; and the
reconcile-helper-nonzero-exit fallthrough. All completion-reconcile cases
exercise `run_completion_reconcile` directly against a tmp_path fixture
tree with a stubbed reconcile-completion-commits.py — no real git repo or
real claude-klabauter liveness resolution is needed (`live_session_ids=` is injected
directly, bypassing CLAUDE_KLABAUTER_ROOT resolution).

Spec backlink: coordinator/bin/workday-complete-reconcile.py
Spec backlink: example-doctrine-repo coordinator/commands/workday-complete.md § Step 1.5, § Step 2.6
"""
from __future__ import annotations

import importlib.util
import io
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = subprocess.run(
    ["git", "rev-parse", "--show-toplevel"],
    cwd=os.path.dirname(os.path.abspath(__file__)),
    capture_output=True,
    text=True,
    check=True,
).stdout.strip()
_TARGET = os.path.join(_REPO_ROOT, "coordinator", "bin", "workday-complete-reconcile.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("workday_complete_reconcile", _TARGET)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mod():
    return _load_module()


def _write_entry(path: Path, *, status: str, authored_by: str | None) -> None:
    lines = ["---", f"status: {status}"]
    if authored_by is not None:
        lines.append(f"authored_by: {authored_by}")
    lines.append("---")
    lines.append("body")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _fixture_reconcile_script(tmp_path: Path, *, rc: int = 0, appended: int = 0, name: str = "fake-reconcile.py") -> Path:
    """A tiny stand-in for reconcile-completion-commits.py --append: prints
    `appended=<N>` to stdout and exits with the given rc, ignoring its args.
    """
    script = tmp_path / name
    script.write_text(
        "import sys\n"
        f"print('appended={appended}')\n"
        f"sys.exit({rc})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


def _run_reconcile(mod, archive_root: Path, reconcile_script: Path, *, year_month="2026-07", session_id="sess-own", live_session_ids=frozenset()):
    out = io.StringIO()
    err = io.StringIO()
    rc = mod.run_completion_reconcile(
        archive_root=str(archive_root),
        year_month=year_month,
        reconcile_script=str(reconcile_script),
        session_id=session_id,
        live_session_ids=live_session_ids,
        git_add=False,
        out=out,
        err=err,
    )
    return rc, out.getvalue(), err.getvalue()


# ---------------------------------------------------------------------------
# completion-reconcile: gating
# ---------------------------------------------------------------------------


def test_skips_entries_outside_target_month(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    (archive_root / "2026-06").mkdir(parents=True)
    entry = archive_root / "2026-06" / "old-entry.md"
    _write_entry(entry, status="pending-release", authored_by="sess-own")

    reconcile_script = _fixture_reconcile_script(tmp_path, appended=5)
    rc, out, err = _run_reconcile(mod, archive_root, reconcile_script)

    assert rc == 0
    assert "Completion reconcile: clean" in out


def test_skips_entries_not_pending_release(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    (archive_root / "2026-07").mkdir(parents=True)
    entry = archive_root / "2026-07" / "shipped-entry.md"
    _write_entry(entry, status="shipped", authored_by="sess-own")

    reconcile_script = _fixture_reconcile_script(tmp_path, appended=5)
    rc, out, err = _run_reconcile(mod, archive_root, reconcile_script)

    assert rc == 0
    assert "Completion reconcile: clean" in out


def test_skips_missing_authored_by(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    (archive_root / "2026-07").mkdir(parents=True)
    entry = archive_root / "2026-07" / "no-author.md"
    _write_entry(entry, status="pending-release", authored_by=None)

    reconcile_script = _fixture_reconcile_script(tmp_path, appended=5)
    rc, out, err = _run_reconcile(mod, archive_root, reconcile_script)

    assert rc == 0
    assert "Completion reconcile: clean" in out


def test_skips_literal_null_authored_by(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    (archive_root / "2026-07").mkdir(parents=True)
    entry = archive_root / "2026-07" / "null-author.md"
    _write_entry(entry, status="pending-release", authored_by="null")

    reconcile_script = _fixture_reconcile_script(tmp_path, appended=5)
    rc, out, err = _run_reconcile(mod, archive_root, reconcile_script)

    assert rc == 0
    assert "Completion reconcile: clean" in out


# ---------------------------------------------------------------------------
# completion-reconcile: own-session vs cross-machine dispatch + liveness gate
# ---------------------------------------------------------------------------


def test_own_session_entry_always_reconciles(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    (archive_root / "2026-07").mkdir(parents=True)
    entry = archive_root / "2026-07" / "own-entry.md"
    _write_entry(entry, status="pending-release", authored_by="sess-own")

    reconcile_script = _fixture_reconcile_script(tmp_path, appended=2)
    rc, out, err = _run_reconcile(
        mod, archive_root, reconcile_script, session_id="sess-own", live_session_ids=frozenset()
    )

    assert rc == 0
    assert "1 entr(ies) folded 2 commit(s)" in out


def test_cross_machine_dead_entry_reconciles_with_its_own_authored_by(mod, tmp_path, monkeypatch):
    """F1 fix: a cross-machine, dead-session entry must be dispatched with
    ITS OWN authored_by as --session-id, not the wrapping session's id.
    """
    archive_root = tmp_path / "archive" / "completed"
    (archive_root / "2026-07").mkdir(parents=True)
    entry = archive_root / "2026-07" / "peer-entry.md"
    _write_entry(entry, status="pending-release", authored_by="sess-peer-dead")

    captured_argv = {}
    real_run = subprocess.run

    def _spy_run(argv, **kwargs):
        if len(argv) >= 2 and "reconcile-completion-commits" in argv[1]:
            captured_argv["argv"] = argv
        return real_run(argv, **kwargs)

    monkeypatch.setattr(mod.subprocess, "run", _spy_run)

    reconcile_script = _fixture_reconcile_script(tmp_path, appended=1, name="reconcile-completion-commits-fake.py")
    rc, out, err = _run_reconcile(
        mod,
        archive_root,
        reconcile_script,
        session_id="sess-own",
        live_session_ids=frozenset(),  # sess-peer-dead is NOT live
    )

    assert rc == 0
    assert "1 entr(ies) folded 1 commit(s)" in out
    argv = captured_argv["argv"]
    sid_idx = argv.index("--session-id") + 1
    assert argv[sid_idx] == "sess-peer-dead"


def test_cross_machine_live_entry_stands_down(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    (archive_root / "2026-07").mkdir(parents=True)
    entry = archive_root / "2026-07" / "peer-live-entry.md"
    _write_entry(entry, status="pending-release", authored_by="sess-peer-live")

    reconcile_script = _fixture_reconcile_script(tmp_path, appended=9)
    rc, out, err = _run_reconcile(
        mod,
        archive_root,
        reconcile_script,
        session_id="sess-own",
        live_session_ids=frozenset({"sess-peer-live"}),
    )

    assert rc == 0
    assert "Completion reconcile: clean" in out


# ---------------------------------------------------------------------------
# completion-reconcile: appended-count accumulation + helper failure
# ---------------------------------------------------------------------------


def test_multiple_entries_accumulate_summary(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    (archive_root / "2026-07").mkdir(parents=True)
    _write_entry(archive_root / "2026-07" / "a.md", status="pending-release", authored_by="sess-own")
    _write_entry(archive_root / "2026-07" / "b.md", status="pending-release", authored_by="sess-own")

    reconcile_script = _fixture_reconcile_script(tmp_path, appended=3)
    rc, out, err = _run_reconcile(mod, archive_root, reconcile_script, session_id="sess-own")

    assert rc == 0
    assert "2 entr(ies) folded 6 commit(s)" in out


def test_zero_appended_entry_not_counted(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    (archive_root / "2026-07").mkdir(parents=True)
    _write_entry(archive_root / "2026-07" / "clean.md", status="pending-release", authored_by="sess-own")

    reconcile_script = _fixture_reconcile_script(tmp_path, appended=0)
    rc, out, err = _run_reconcile(mod, archive_root, reconcile_script, session_id="sess-own")

    assert rc == 0
    assert "Completion reconcile: clean" in out


def test_reconcile_helper_nonzero_exit_warns_and_continues(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    (archive_root / "2026-07").mkdir(parents=True)
    entry = archive_root / "2026-07" / "helper-fail.md"
    _write_entry(entry, status="pending-release", authored_by="sess-own")

    reconcile_script = _fixture_reconcile_script(tmp_path, rc=1, appended=0)
    rc, out, err = _run_reconcile(mod, archive_root, reconcile_script, session_id="sess-own")

    assert rc == 0
    assert "reconcile-completion-commits failed" in err
    assert "Completion reconcile: clean" in out


def test_session_id_unresolvable_skips_whole_sweep(mod, tmp_path):
    archive_root = tmp_path / "archive" / "completed"
    (archive_root / "2026-07").mkdir(parents=True)
    entry = archive_root / "2026-07" / "any.md"
    _write_entry(entry, status="pending-release", authored_by="sess-own")

    reconcile_script = _fixture_reconcile_script(tmp_path, appended=5)
    out = io.StringIO()
    err = io.StringIO()
    rc = mod.run_completion_reconcile(
        archive_root=str(archive_root),
        year_month="2026-07",
        reconcile_script=str(reconcile_script),
        session_id="",
        live_session_ids=frozenset(),
        git_add=False,
        out=out,
        err=err,
    )

    assert rc == 0
    assert out.getvalue() == ""
    assert "session-id unresolvable" in err.getvalue()


# ---------------------------------------------------------------------------
# completion-reconcile: helpers
# ---------------------------------------------------------------------------


def test_authored_by_strips_inline_comment_and_quotes(mod):
    content = '---\nauthored_by: "sess-xyz"  # forensic tracing only\n---\n'
    assert mod._authored_by_field(content) == "sess-xyz"


def test_default_reconcile_script_resolves_sibling_path(mod):
    resolved = mod._default_reconcile_script()
    assert os.path.basename(resolved) == "reconcile-completion-commits.py"
    # os.path.normpath, not a bare ==: _TARGET is built by os.path.join()-ing
    # onto `git rev-parse --show-toplevel`'s forward-slash-only output
    # (unnormalized even on Windows), while `resolved` comes from
    # os.path.abspath(__file__) (fully backslash-normalized on Windows) —
    # same directory, cosmetically different separators.
    assert os.path.normpath(os.path.dirname(resolved)) == os.path.normpath(os.path.dirname(_TARGET))


# ---------------------------------------------------------------------------
# cruft-sweep dispatch
# ---------------------------------------------------------------------------


def test_cruft_sweep_success_is_silent(mod, tmp_path):
    fake_bin = tmp_path / "cruft-sweep-ok"
    fake_bin.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(0)\n", encoding="utf-8")
    fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IEXEC)

    out = io.StringIO()
    err = io.StringIO()
    rc = mod.run_cruft_sweep(
        cruft_sweep_bin=str(fake_bin),
        state_root_script=str(tmp_path / "nonexistent-state-root.py"),
        out=out,
        err=err,
    )

    assert rc == 0
    assert err.getvalue() == ""


def test_cruft_sweep_nonzero_exit_warns_but_returns_zero(mod, tmp_path):
    fake_bin = tmp_path / "cruft-sweep-fail"
    fake_bin.write_text("#!/usr/bin/env python3\nimport sys\nsys.exit(1)\n", encoding="utf-8")
    fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IEXEC)

    out = io.StringIO()
    err = io.StringIO()
    rc = mod.run_cruft_sweep(
        cruft_sweep_bin=str(fake_bin),
        state_root_script=str(tmp_path / "nonexistent-state-root.py"),
        out=out,
        err=err,
    )

    assert rc == 0
    assert "WARN: cruft-sweep Step 1.5 exited non-zero" in err.getvalue()


def test_cruft_sweep_missing_binary_warns_but_returns_zero(mod, tmp_path):
    out = io.StringIO()
    err = io.StringIO()
    rc = mod.run_cruft_sweep(
        cruft_sweep_bin=str(tmp_path / "does-not-exist"),
        state_root_script=str(tmp_path / "nonexistent-state-root.py"),
        out=out,
        err=err,
    )

    assert rc == 0
    assert "WARN" in err.getvalue()


# ---------------------------------------------------------------------------
# CLI wiring smoke test
# ---------------------------------------------------------------------------


def test_main_completion_reconcile_defaults_to_cwd_relative_archive_root(mod, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-smoke")
    # `main(argv)` is args-only (no leading program-name token) — the
    # convention every sibling consumes-manifest CLI's `main(argv)` follows
    # and the one `workday_complete.apply._invoke_cli_main` relies on
    # (2026-07-26 arg-mismatch audit; this call used to carry a fake
    # leading `"workday-complete-reconcile.py"` token, matching the
    # pre-fix `argv[1:]` off-by-one this test would otherwise have masked).
    rc = mod.main(["completion-reconcile"])
    assert rc == 0


def test_main_requires_a_subcommand(mod, capsys):
    with pytest.raises(SystemExit):
        mod.main([])
