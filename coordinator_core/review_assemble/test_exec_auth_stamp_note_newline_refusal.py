from __future__ import annotations

from coordinator_core.review_assemble import exec_auth_stamp


def test_note_with_embedded_newline_is_refused(tmp_path, capsys) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("---\nstatus: draft\n---\nbody\n", encoding="utf-8")

    rc = exec_auth_stamp.main(
        ["stamp", str(plan), "--by", "PM", "--note", "line one\nline two"]
    )

    assert rc == exec_auth_stamp.EXIT_USAGE
    err = capsys.readouterr().err
    assert "--note" in err
    assert "newline" in err


def test_append_note_with_embedded_newline_is_refused(tmp_path, capsys) -> None:
    plan = tmp_path / "plan.md"
    plan.write_text("---\nstatus: draft\n---\nbody\n", encoding="utf-8")

    rc = exec_auth_stamp.main(
        ["stamp", str(plan), "--by", "PM", "--append-note", "line one\nline two"]
    )

    assert rc == exec_auth_stamp.EXIT_USAGE
    err = capsys.readouterr().err
    assert "--append-note" in err
    assert "newline" in err
