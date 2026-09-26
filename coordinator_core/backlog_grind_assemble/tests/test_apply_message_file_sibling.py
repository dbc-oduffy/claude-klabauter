from __future__ import annotations

from pathlib import Path
from unittest import mock

from coordinator_core.backlog_grind_assemble import apply as bga_apply


def test_message_file_round_trips_multiline_body_byte_exact(tmp_path: Path, capsys) -> None:
    body = "subject\n\nmulti-line body\nsecond body line\n"
    message_file = tmp_path / "msg.txt"
    message_file.write_text(body, encoding="utf-8")

    captured: dict[str, object] = {}

    def fake_apply(cadence, **kwargs):
        captured["extra_directives"] = kwargs.get("extra_directives")
        return bga_apply.APPLY_EXIT_OK, {"ok": True}

    with mock.patch.object(bga_apply, "apply", side_effect=fake_apply):
        rc = bga_apply.main_apply(
            [
                "bug-blitz",
                "--session-id",
                "test-session",
                "--wave-path",
                "some/file.py",
                "--granularity",
                "per-item",
                "--message-file",
                str(message_file),
            ]
        )

    assert rc == bga_apply.APPLY_EXIT_OK
    directives = captured["extra_directives"]
    assert directives is not None
    assert directives[0]["message"] == body


def test_message_and_message_file_are_mutually_exclusive(tmp_path: Path, capsys) -> None:
    message_file = tmp_path / "msg.txt"
    message_file.write_text("a body\n", encoding="utf-8")

    rc = bga_apply.main_apply(
        [
            "bug-blitz",
            "--session-id",
            "test-session",
            "--wave-path",
            "some/file.py",
            "--granularity",
            "per-item",
            "--message",
            "inline",
            "--message-file",
            str(message_file),
        ]
    )
    assert rc == bga_apply.APPLY_EXIT_TRANSPORT_FAIL
    err = capsys.readouterr().err
    assert "mutually exclusive" in err
