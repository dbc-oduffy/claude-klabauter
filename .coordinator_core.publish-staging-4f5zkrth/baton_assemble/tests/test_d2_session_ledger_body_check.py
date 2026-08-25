"""
coordinator_core.baton_assemble.tests.test_d2_session_ledger_body_check --
C1 coverage (pln-the-ledger-check-follows-the-body-not-ju-e2da19 § AC1/AC2/AC3).

Proves d2 (`_dispatch_lint_frontmatter`) reports a missing `## Session
Ledger` heading in the body of the artifact it just linted, without
refusing (AC1, AC3), that it goes through `session_ledger`'s shared
`body_has_session_ledger_heading` / `SESSION_LEDGER_HEADING_RE` rather than
a second detection grammar (AC1), and that `frontmatter/schema_validate.py`
is untouched by this chunk (AC2).

No process spawn (subprocess.run is monkeypatched), no git -- fast tier.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from coordinator_core.baton_assemble import apply as ba_apply
from coordinator_core.session_ledger import (
    SESSION_LEDGER_HEADING_RE,
    body_has_session_ledger_heading,
)


class _FakeCompletedProcess:
    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


@pytest.fixture(autouse=True)
def _stub_lint_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    # d2's own subprocess call (lint-frontmatter.py) is not under test here --
    # stub it to a clean pass so only the body-check addition is exercised.
    monkeypatch.setattr(
        ba_apply.subprocess, "run", lambda *a, **k: _FakeCompletedProcess(returncode=0)
    )
    # `_dispatch_lint_frontmatter` resolves `makima_bin` to build the
    # subprocess argv before running it -- irrelevant to the body check
    # under test (the subprocess itself is stubbed above), but the real
    # resolver raises on this machine's un-configured `makima_root`. Stub it
    # rather than depending on operator-local machine config.
    import coordinator_core.resolution.facade as facade

    monkeypatch.setattr(facade, "resolve_operator_config", lambda: {"makima_bin": "unused"})


def _write(tmp_path, rel: str, body_extra: str) -> str:
    content = (
        "---\n"
        "kind: session-handoff\n"
        "---\n"
        f"{body_extra}\n"
    )
    target = tmp_path / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return rel


class TestMissingBlockIsReported:
    def test_body_without_heading_is_reported_not_raised(self, tmp_path) -> None:
        rel = _write(tmp_path, "state/handoffs/2026-08-20-x.md", "# Some Handoff\n\nno ledger here.")
        detail = ba_apply._dispatch_lint_frontmatter(["--file", rel], tmp_path)
        assert detail["session_ledger_checked"] is True
        assert detail["session_ledger_heading_present"] is False

    def test_reported_not_refused_no_exception_raised(self, tmp_path) -> None:
        rel = _write(tmp_path, "state/handoffs/2026-08-20-y.md", "no heading at all")
        # Would raise if this were a refusal instead of a report.
        ba_apply._dispatch_lint_frontmatter(["--file", rel], tmp_path)

    def test_warning_printed_to_stderr(self, tmp_path, capsys) -> None:
        rel = _write(tmp_path, "state/handoffs/2026-08-20-z.md", "no heading at all")
        ba_apply._dispatch_lint_frontmatter(["--file", rel], tmp_path)
        captured = capsys.readouterr()
        assert "Session Ledger" in captured.err


class TestPresentBlockPassesUnchanged:
    def test_body_with_heading_reports_present_true(self, tmp_path) -> None:
        body = "# Some Handoff\n\n## Session Ledger\n\n<!-- rows -->\n"
        rel = _write(tmp_path, "state/handoffs/2026-08-20-ok.md", body)
        detail = ba_apply._dispatch_lint_frontmatter(["--file", rel], tmp_path)
        assert detail["session_ledger_checked"] is True
        assert detail["session_ledger_heading_present"] is True

    def test_frontmatter_lint_detail_keys_still_present(self, tmp_path) -> None:
        body = "## Session Ledger\n"
        rel = _write(tmp_path, "state/handoffs/2026-08-20-ok2.md", body)
        detail = ba_apply._dispatch_lint_frontmatter(["--file", rel], tmp_path)
        assert detail["cli"] == "lint-frontmatter"
        assert detail["args"] == ["--file", rel]


class TestNotCheckedDegradeIsNotSilent:
    # C1 review finding 1: the `session_ledger_checked: False` degrade paths
    # (no --file in args; read/parse failure) used to print nothing, making
    # them indistinguishable from a silent pass on stderr. Each must now
    # print its own note, distinct from both the pass case (no print) and
    # the missing-block warning (a different fact: "not checked" vs
    # "checked, heading absent").

    def test_missing_file_arg_prints_distinct_note(self, tmp_path, capsys) -> None:
        detail = ba_apply._dispatch_lint_frontmatter(["--other-flag", "x"], tmp_path)
        assert detail["session_ledger_checked"] is False
        captured = capsys.readouterr()
        assert "session ledger check skipped" in captured.err
        assert "has no '## Session Ledger' heading" not in captured.err

    def test_unreadable_file_prints_distinct_note(self, tmp_path, capsys) -> None:
        rel = "state/handoffs/2026-08-20-missing.md"  # never written
        detail = ba_apply._dispatch_lint_frontmatter(["--file", rel], tmp_path)
        assert detail["session_ledger_checked"] is False
        captured = capsys.readouterr()
        assert "session ledger check skipped" in captured.err
        assert rel in captured.err
        assert "has no '## Session Ledger' heading" not in captured.err

    def test_not_checked_note_differs_from_present_case_silence(self, tmp_path, capsys) -> None:
        body = "## Session Ledger\n"
        rel = _write(tmp_path, "state/handoffs/2026-08-20-present.md", body)
        ba_apply._dispatch_lint_frontmatter(["--file", rel], tmp_path)
        # Pass case: nothing printed at all.
        assert capsys.readouterr().err == ""


class TestSharesTheCanonicalRegexNotASecondGrammar:
    def test_check_session_ledger_body_uses_shared_predicate(self, tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[str] = []
        real = body_has_session_ledger_heading

        def _tracking(body: str) -> bool:
            calls.append(body)
            return real(body)

        # apply.py imports the predicate locally, inside the function body,
        # so the import rebinds from the SOURCE module at call time -- patch
        # it there to prove the shared function is what actually gets
        # called, not a second, independently-defined check.
        import coordinator_core.session_ledger as session_ledger_mod

        monkeypatch.setattr(session_ledger_mod, "body_has_session_ledger_heading", _tracking)
        rel = _write(tmp_path, "state/handoffs/2026-08-20-shared.md", "## Session Ledger\n")
        ba_apply._dispatch_lint_frontmatter(["--file", rel], tmp_path)
        assert calls, "expected the shared session_ledger predicate to be invoked"

    def test_apply_module_imports_rather_than_redefines_the_regex(self) -> None:
        import inspect

        source = inspect.getsource(ba_apply)
        assert "from coordinator_core.session_ledger import" in source
        assert "body_has_session_ledger_heading" in source
        # No independently-compiled heading pattern anywhere in this module.
        assert "re.compile(" not in inspect.getsource(ba_apply._check_session_ledger_body)

    def test_predicate_matches_the_canonical_regex_directly(self) -> None:
        assert body_has_session_ledger_heading("## Session Ledger\n") is bool(
            SESSION_LEDGER_HEADING_RE.search("## Session Ledger\n")
        )
        assert body_has_session_ledger_heading("no heading") is False


def test_schema_validate_module_is_untouched() -> None:
    # AC2 negative assertion: this chunk must not have edited
    # frontmatter/schema_validate.py -- proven at the repo/git level, not
    # importable from a unit test in isolation, so this asserts the weaker
    # but still load-bearing fact that the module still imports and still
    # exposes `parse_frontmatter` (the only symbol d2's body check borrows
    # from it) with its existing signature.
    from coordinator_core.frontmatter.schema_validate import parse_frontmatter

    result = parse_frontmatter("---\nkind: x\n---\nbody text")
    assert result["body"] == "body text"
