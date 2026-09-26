from __future__ import annotations

import pytest

from coordinator_core.argv_fidelity import (
    ArgvFidelityError,
    refuse_newline_argv,
    resolve_body,
    resolve_optional_prose,
)


#: The three cases below load a `coordinator/bin` CLI IN-PROCESS via
@pytest.fixture
def bin_cli_loader():
    import importlib.machinery
    import importlib.util
    import sys
    from pathlib import Path

    bin_dir = Path(__file__).resolve().parents[2] / "coordinator" / "bin"
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


def test_refuse_newline_argv_default_message_names_the_file_sibling():
    with pytest.raises(ArgvFidelityError) as exc:
        refuse_newline_argv("a\nb", flag_name="--body")
    assert "pass --body-file instead." in str(exc.value)


def test_refuse_newline_argv_remedy_replaces_the_file_sibling_suggestion():
    with pytest.raises(ArgvFidelityError) as exc:
        refuse_newline_argv(
            "a\nb", flag_name="--title", remedy="pass a single-line --title."
        )
    msg = str(exc.value)
    assert msg == "--title contains a newline; pass a single-line --title."
    assert "--title-file" not in msg


def test_refuse_newline_argv_remedy_is_inert_on_a_clean_value():
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
    # without naming a root. `COORDINATOR_ENGINE_ROOT` is the documented rung-1
    from pathlib import Path

    monkeypatch.setenv(
        "COORDINATOR_ENGINE_ROOT", str(Path(__file__).resolve().parents[2])
    )
    import importlib.machinery
    import importlib.util
    import unittest.mock
    from pathlib import Path

    cli_mod = bin_cli_loader("coordinator-lesson-promote.py", "coordinator_lesson_promote_argv_fidelity_test")

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
