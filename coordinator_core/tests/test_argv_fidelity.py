"""
coordinator_core.tests.test_argv_fidelity

Behavioural tests for coordinator_core.argv_fidelity: the shared
--body/--body-file resolution seam and the newline-argv refusal that
closes the cmd.exe-truncation class documented in that module's docstring.

Spec backlink: docs/plans/2026-08-20-newline-bearing-argv-fails-loud.md, C1.

The "CLI-integration section" below is a placeholder each wiring chunk
(C2 coordinator-lesson-add.py, C3 coordinator-lesson-promote.py, C4
queue-triage.py) extends with one case asserting its own CLI's non-zero
exit and --body-file substring on a newline-bearing --body. Nothing above
that marker is theirs to touch.
"""
from __future__ import annotations

import pytest

from coordinator_core.argv_fidelity import (
    ArgvFidelityError,
    refuse_newline_argv,
    resolve_body,
    resolve_optional_prose,
)


# ---------------------------------------------------------------------------
# resolve_body
# ---------------------------------------------------------------------------


#: The three cases below load a `coordinator/bin` CLI IN-PROCESS via
#: `SourceFileLoader`. Those CLIs bootstrap their siblings with a bare
#: `import lib`, which `coordinator/bin/lib/__init__.py` documents as resolving
#: "because a script's own directory is `sys.path[0]`" -- true when the CLI is
#: executed, false when a test loads it by path. Until 2026-09-06 these three
#: therefore passed only when some EARLIER test in the same worker had already
#: put those directories on `sys.path`: order-dependent, green or red purely on
#: how xdist happened to distribute the run. Reproducing the interpreter state a
#: real invocation provides is the test's job, not a neighbour's side effect.
#:
#: A FIXTURE, not a helper that restores on the way out: these CLIs bootstrap
#: LAZILY (`_bootstrap_imports()` moved off module scope precisely so importing
#: one would stop mutating the warm server's `sys.path`), so the imports fire
#: when the test CALLS the CLI, not when it loads it. Restoring at the end of
#: the load put the path back before the only line that needed it.
@pytest.fixture
def bin_cli_loader():
    import importlib.machinery
    import importlib.util
    import sys
    from pathlib import Path

    bin_dir = Path(__file__).resolve().parents[2] / "coordinator" / "bin"
    # BOTH directories, and `bin/lib` is the load-bearing one. The CLIs
    # bootstrap via a bare `import lib`, but TWO packages in this repo are
    # importable under that bare name -- `coordinator/lib` and
    # `coordinator/bin/lib` -- and whichever a process imports first wins in
    # `sys.modules` for its whole life. Under pytest `coordinator/lib` can get
    # there first (it is in `testpaths`), making the CLI's `import lib` a cache
    # hit on the WRONG package that never runs the line adding
    # `coordinator/bin/lib`. Adding it directly makes `cc_invoke` and
    # `coordinator_registry` resolve regardless of who won that race.
    lib_dir = bin_dir / "lib"
    added = [str(d) for d in (bin_dir, lib_dir) if str(d) not in sys.path]
    for entry in added:
        sys.path.insert(0, entry)

    def _load(filename: str, module_name: str):
        loader = importlib.machinery.SourceFileLoader(
            module_name, str(bin_dir / filename)
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        cli_mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
        loader.exec_module(cli_mod)
        return cli_mod

    try:
        yield _load
    finally:
        # Leave `sys.path` as found -- this suite runs in a warm interpreter
        # ~50 sessions share, and the neighbour-pollution above is exactly what
        # this fixture exists to stop; it must not become a source of it.
        for entry in added:
            if entry in sys.path:
                sys.path.remove(entry)


def test_resolve_body_mutually_exclusive():
    with pytest.raises(ArgvFidelityError, match="mutually exclusive"):
        resolve_body("inline body", "some/path.txt")


def test_resolve_body_neither_given_names_both_flags():
    with pytest.raises(ArgvFidelityError) as exc_info:
        resolve_body(None, None)
    message = str(exc_info.value)
    assert "--body" in message
    assert "--body-file" in message


def test_resolve_body_stdin_sentinel(monkeypatch):
    monkeypatch.setattr(
        "sys.stdin", __import__("io").StringIO("body from stdin\nsecond line\n")
    )
    result = resolve_body(None, "-")
    assert result == "body from stdin\nsecond line\n"


def test_resolve_body_file_path(tmp_path):
    body_path = tmp_path / "body.txt"
    body_path.write_text("line one\nline two\nline three\n", encoding="utf-8")
    result = resolve_body(None, str(body_path))
    assert result == "line one\nline two\nline three\n"


def test_resolve_body_unreadable_path_fails_loud(tmp_path):
    missing_path = tmp_path / "does-not-exist.txt"
    with pytest.raises(ArgvFidelityError, match="unreadable"):
        resolve_body(None, str(missing_path))


def test_resolve_body_empty_file_fails_loud(tmp_path):
    body_path = tmp_path / "empty.txt"
    body_path.write_text("   \n", encoding="utf-8")
    with pytest.raises(ArgvFidelityError, match="empty"):
        resolve_body(None, str(body_path))


def test_resolve_body_empty_argv_body_fails_loud():
    with pytest.raises(ArgvFidelityError, match="empty"):
        resolve_body("   ", None)


def test_resolve_body_clean_argv_pass_through():
    assert resolve_body("a plain one-line body", None) == "a plain one-line body"


def test_resolve_body_clean_file_pass_through(tmp_path):
    body_path = tmp_path / "body.txt"
    body_path.write_text("content", encoding="utf-8")
    assert resolve_body(None, str(body_path)) == "content"


def test_resolve_body_custom_flag_name_in_messages():
    with pytest.raises(ArgvFidelityError) as exc_info:
        resolve_body(None, None, flag_name="--summary")
    message = str(exc_info.value)
    assert "--summary" in message
    assert "--summary-file" in message


# ---------------------------------------------------------------------------
# resolve_optional_prose
# ---------------------------------------------------------------------------


def test_refuse_newline_argv_default_message_names_the_file_sibling():
    """The default assumes a -file sibling exists, which is right for most callers."""
    with pytest.raises(ArgvFidelityError) as exc:
        refuse_newline_argv("a\nb", flag_name="--body")
    assert "pass --body-file instead." in str(exc.value)


def test_refuse_newline_argv_remedy_replaces_the_file_sibling_suggestion():
    """A flag denied a file leg must not be sent to one that does not exist.

    `coordinator-doc-new --title` is the live case: it earns the refusal but has
    no `--title-file`, and before `remedy` existed it hand-rolled its own
    `parser.error` purely to avoid this message -- which also cost it coverage,
    since the transport probe credits only refusals routed through the seam.
    """
    with pytest.raises(ArgvFidelityError) as exc:
        refuse_newline_argv(
            "a\nb", flag_name="--title", remedy="pass a single-line --title."
        )
    msg = str(exc.value)
    assert msg == "--title contains a newline; pass a single-line --title."
    assert "--title-file" not in msg


def test_refuse_newline_argv_remedy_is_inert_on_a_clean_value():
    """`remedy` must not change WHEN the refusal fires, only what it says."""
    assert refuse_newline_argv(
        "one line", flag_name="--title", remedy="pass a single-line --title."
    ) is None
    assert refuse_newline_argv(
        None, flag_name="--title", remedy="pass a single-line --title."
    ) is None


def test_resolve_optional_prose_both_absent_returns_none():
    assert resolve_optional_prose(None, None, flag_name="--summary") is None


def test_resolve_optional_prose_inline_only():
    result = resolve_optional_prose("a plain value", None, flag_name="--summary")
    assert result == "a plain value"


def test_resolve_optional_prose_file_only(tmp_path):
    body_path = tmp_path / "summary.txt"
    body_path.write_text("line one\nline two\n", encoding="utf-8")
    result = resolve_optional_prose(
        None, str(body_path), flag_name="--summary"
    )
    assert result == "line one\nline two\n"


def test_resolve_optional_prose_both_supplied_is_usage_error():
    with pytest.raises(ArgvFidelityError, match="mutually exclusive"):
        resolve_optional_prose(
            "inline value", "some/path.txt", flag_name="--summary"
        )


def test_resolve_optional_prose_newline_inline_refused_names_file_flag():
    with pytest.raises(ArgvFidelityError, match="--summary-file"):
        resolve_optional_prose(
            "line one\nline two", None, flag_name="--summary"
        )


def test_resolve_optional_prose_unreadable_file_refuses(tmp_path):
    missing_path = tmp_path / "does-not-exist.txt"
    with pytest.raises(ArgvFidelityError, match="unreadable"):
        resolve_optional_prose(
            None, str(missing_path), flag_name="--summary"
        )


def test_resolve_optional_prose_stdin_sentinel_already_eof_raises(monkeypatch):
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
    with pytest.raises(ArgvFidelityError, match="empty"):
        resolve_optional_prose(None, "-", flag_name="--summary")


# ---------------------------------------------------------------------------
# refuse_newline_argv
# ---------------------------------------------------------------------------


def test_refuse_newline_argv_refuses_embedded_newline():
    with pytest.raises(ArgvFidelityError, match="--body-file"):
        refuse_newline_argv("line one\nline two", flag_name="--body")


def test_refuse_newline_argv_passes_clean_value():
    refuse_newline_argv("a plain one-line value", flag_name="--body")


def test_refuse_newline_argv_passes_none():
    refuse_newline_argv(None, flag_name="--body")


def test_refuse_newline_argv_names_the_flag():
    with pytest.raises(ArgvFidelityError) as exc_info:
        refuse_newline_argv("a\nb", flag_name="--summary")
    message = str(exc_info.value)
    assert "--summary" in message
    assert "--summary-file" in message


# ---------------------------------------------------------------------------
# CLI-integration section
#
# Each wiring chunk (C2, C3, C4) adds ONE case here asserting its own CLI's
# refusal + --body-file acceptance end to end (subprocess or in-process
# main() invocation, per that CLI's existing test conventions) -- non-zero
# exit and the substring "--body-file" in stderr for a newline-bearing
# --body, and a successful write when --body-file is passed alone. Do not
# add cases here for CLIs outside this plan's scope (cross-repo-memo.py,
# coordinator-queue-append.py already ship their own).
# ---------------------------------------------------------------------------


def test_coordinator_lesson_add_refuses_newline_body(capsys, bin_cli_loader):
    import importlib.machinery
    import importlib.util
    import unittest.mock
    from pathlib import Path

    cli_mod = bin_cli_loader("coordinator-lesson-add.py", "coordinator_lesson_add_argv_fidelity_test")

    argv = [
        "coordinator-lesson-add",
        "--title", "Test lesson about argv fidelity refusal",
        "--body", "line one\nline two",
        "--scope", "project",
    ]
    with unittest.mock.patch("sys.argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli_mod.main()
    assert exc_info.value.code not in (0, None)
    assert "--body-file" in capsys.readouterr().err


def test_coordinator_lesson_promote_refuses_newline_body(
    capsys, bin_cli_loader, monkeypatch
):
    # This CLI's lazy bootstrap resolves the claude-klabauter root before argparse ever
    # runs, and the suite-root home quarantine leaves the machine-local
    # registry empty by design -- so the refusal under test is unreachable
    # without naming a root. `COORDINATOR_ENGINE_ROOT` is the documented rung-1
    # override (`coordinator/lib/resolve-claude-klabauter/_resolve_claude_klabauter.py`), pointed at
    # THIS checkout: an explicit, machine-independent answer rather than
    # `@pytest.mark.real_home`, which is scoped to live-parity oracles and this
    # is not one -- it asserts a pure argv refusal.
    from pathlib import Path

    monkeypatch.setenv(
        "COORDINATOR_ENGINE_ROOT", str(Path(__file__).resolve().parents[2])
    )
    import importlib.machinery
    import importlib.util
    import unittest.mock
    from pathlib import Path

    cli_mod = bin_cli_loader("coordinator-lesson-promote.py", "coordinator_lesson_promote_argv_fidelity_test")

    # Refusal fires from post-parse validation, before any schema-derived
    # write path is reached -- stub the schema.describe lookup so this case
    # does not depend on engine-root/registry resolution under the suite's
    # quarantined home.
    cli_mod._describe_schema_node = lambda _schema: {
        "enums": {"change_kind": ["doctrine-edit"]}
    }

    argv = [
        "coordinator-lesson-promote",
        "--title", "Test lesson about argv fidelity refusal",
        "--body", "line one\nline two",
        "--change-kind", "doctrine-edit",
        "--target-wiki", "unknown",
    ]
    with unittest.mock.patch("sys.argv", argv):
        with pytest.raises(SystemExit) as exc_info:
            cli_mod.main(argv[1:])
    assert exc_info.value.code not in (0, None)
    assert "--body-file" in capsys.readouterr().err


def test_queue_triage_scaffold_baton_body_flag(monkeypatch, tmp_path, capsys, bin_cli_loader):
    import importlib.machinery
    import importlib.util
    from pathlib import Path

    cli_mod = bin_cli_loader("queue-triage.py", "queue_triage_argv_fidelity_test")

    with pytest.raises(SystemExit) as exc_info:
        cli_mod.main(
            [
                "--repo-root", "/tmp/repo",
                "scaffold-baton", "debt-backlog",
                "--entry-path", "a.yaml",
                "--body", "line one\nline two",
            ]
        )
    assert exc_info.value.code not in (0, None)
    assert "--body-file" in capsys.readouterr().err

    seen = {}

    def _fake_route_mutation(op, params, repo_root, legacy_fn):
        seen["params"] = params
        return {"status": "ok"}

    monkeypatch.setattr(cli_mod, "route_mutation", _fake_route_mutation)

    body_path = tmp_path / "body.txt"
    body_path.write_text("first line\nsecond line\n", encoding="utf-8")

    rc = cli_mod.main(
        [
            "--repo-root", "/tmp/repo",
            "scaffold-baton", "debt-backlog",
            "--entry-path", "a.yaml",
            "--body-file", str(body_path),
        ]
    )

    assert rc == 0
    assert seen["params"]["body"] == "first line\nsecond line\n"
