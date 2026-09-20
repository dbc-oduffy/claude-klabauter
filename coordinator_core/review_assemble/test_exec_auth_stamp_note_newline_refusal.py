"""Regression test for the bug-backlog record
2026-09-11-two-prose-flags-outside-the-file-sibling--4d564a81a0b0.yaml:
`review-exec-auth-stamp stamp --note`/`--append-note` had no `-file`
sibling and no newline refusal, so a multi-line PM-verbatim authorization
note reaching this CLI through a `.cmd` forwarder on Windows was silently
truncated to its first line before argparse ever saw it -- a misquoted PM
utterance on an authorization record. `--note` is intentionally
multi-line-shaped (the module's own block-scalar handling), so the fix here
is refuse-loudly rather than a full file transport (the record's own
`proposed_action` names this as sufficient for `--note`).

Negative-spec: does NOT exercise the frontmatter write path -- the defect,
and this pin, live entirely in `main`'s own argv-validation layer, before
any file is touched.
"""
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
