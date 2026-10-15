"""
coordinator_core.baton_assemble.tests.test_d2_session_ledger_body_check --
d2 (`_dispatch_lint_frontmatter`) no longer checks the body for a `## Session
Ledger` heading: `handoff.append_session_ledger` adds the heading with the
first row, so a heading-less minted baton is the norm and warning on it
would prescribe a remedy the appender owns.

No process spawn (subprocess.run is monkeypatched), no git -- fast tier.
"""

from __future__ import annotations

import pytest

from coordinator_core.baton_assemble import apply as ba_apply


class _FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _stub_lint_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        ba_apply.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(returncode=0)
    )
    import coordinator_core.resolution.facade as facade

    monkeypatch.setattr(facade, "resolve_operator_config", lambda: {"claude_klabauter_bin": "unused"})


def _write(tmp_path, rel: str, body: str) -> str:
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(f"---\nkind: session-handoff\n---\n{body}\n", encoding="utf-8")
    return rel


def test_heading_absent_body_is_silent_and_reports_only_cli_and_args(tmp_path, capsys) -> None:
    rel = _write(tmp_path, "state/handoffs/2026-08-20-x.md", "# Some Handoff\n\nno ledger here.")
    detail = ba_apply._dispatch_lint_frontmatter(["--file", rel], tmp_path)
    assert detail == {"cli": "lint-frontmatter", "args": ["--file", rel]}
    assert capsys.readouterr().err == ""


def test_heading_present_body_is_silent_and_reports_only_cli_and_args(tmp_path, capsys) -> None:
    rel = _write(tmp_path, "state/handoffs/2026-08-20-ok.md", "## Session Ledger\n")
    detail = ba_apply._dispatch_lint_frontmatter(["--file", rel], tmp_path)
    assert detail == {"cli": "lint-frontmatter", "args": ["--file", rel]}
    assert capsys.readouterr().err == ""


def test_lint_failure_still_raises(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(
        ba_apply.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(returncode=1, stderr="bad")
    )
    with pytest.raises(RuntimeError, match="lint-frontmatter"):
        ba_apply._dispatch_lint_frontmatter(["--file", "x.md"], tmp_path)


def test_check_helper_is_gone() -> None:
    assert not hasattr(ba_apply, "_check_session_ledger_body")
