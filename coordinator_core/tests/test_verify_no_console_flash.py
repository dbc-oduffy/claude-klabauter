"""Characterization tests for coordinator_core.ops.verify_no_console_flash.

The bash oracle was the canonical guard scanning a coordinator-claude tree
for unsuppressed console-window-flash spawn shapes (python/node/powershell
without CREATE_NO_WINDOW/windowsHide/spawn-hidden routing). These tests
exercise the ported detection logic against seeded fixture trees mirroring
the coordinator-claude-side bats/sh oracle tests.

Port of: verify-no-console-flash.sh (coordinator-claude 894d4bc6, 2026-07-22)
Test oracle: test-verify-no-console-flash.sh (coordinator-claude 894d4bc6, 2026-07-22)
"""
from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core.ops.verify_no_console_flash import (
    _in_inline_comment,
    _is_suppressed,
    main,
)


def _make_root(tmp_path: Path) -> Path:
    """Build a `<root>/coordinator-claude/{bin,hooks}` fixture tree."""
    coord = tmp_path / "coordinator-claude"
    (coord / "bin").mkdir(parents=True)
    (coord / "hooks").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Clean-fixture: exit 0 (mirrors oracle Test 1)
# ---------------------------------------------------------------------------


def test_clean_fixture_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _make_root(tmp_path)
    coord = root / "coordinator-claude"
    (coord / "bin" / "clean.sh").write_text(
        "#!/bin/bash\n# A script with no console-spawning invocations at all.\necho \"hello world\"\n"
    )
    (coord / "bin" / "suppressed.sh").write_text(
        "#!/bin/bash\n# Suppressed via spawn-hidden.sh routing\nlib/spawn-hidden.sh python3 my-script.py \"$@\"\n"
    )
    (coord / "bin" / "allowlisted.sh").write_text(
        '#!/bin/bash\npython3 bin/verify-no-console-flash.sh "$@" # verify-no-console-flash: allow\n'
    )
    (coord / "hooks" / "hooks.json").write_text(
        '{"hooks": [{"name": "some-hook", "event": "PostToolUse", "command": "bash bin/some-helper.sh"}]}\n'
    )

    rc = main([str(root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK:" in out


# ---------------------------------------------------------------------------
# Seeded-violation fixture: exit 1, per-shape assertions (mirrors oracle Test 2)
# ---------------------------------------------------------------------------


def test_violation_fixture_exits_one_all_shapes_caught(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _make_root(tmp_path)
    coord = root / "coordinator-claude"

    # Shape (1)+(2): variable-interpreter WITH heredoc
    (coord / "bin" / "drift-like.sh").write_text(
        "#!/bin/bash\n"
        "_lookup_source_subpath() {\n"
        '    local pname="$1" reg_file="$2"\n'
        "    \"${PYTHON:-python3}\" - \"$pname\" \"$reg_file\" <<'PYEOF' 2>/dev/null | tr -d '\\r'\n"
        "import sys\n"
        "print(sys.argv[1])\n"
        "PYEOF\n"
        "}\n"
    )
    # Shape (3): bare literal python3
    (coord / "bin" / "bare-literal.sh").write_text("#!/bin/bash\npython3 my_script.py\n")
    # Shape (4): node -e
    (coord / "bin" / "node-e.sh").write_text('#!/bin/bash\nnode -e "console.log(\'hi\')"\n')
    # Shape (5): hooks.json with bare node command — architecturally exempt
    (coord / "hooks" / "hooks.json").write_text(
        '{"hooks": [{"name": "frontmatter-validator", "event": "PreToolUse", '
        '"command": "node hooks/scripts/validate-frontmatter-schema.js"}]}\n'
    )

    rc = main([str(root)])
    out = capsys.readouterr().out
    assert rc == 1

    assert "UNSUPPRESSED" in out and ("PYTHON" in out or "python" in out)
    assert "UNSUPPRESSED" in out and "python3" in out
    assert "UNSUPPRESSED" in out and "node" in out
    # hooks.json command-field spawns are architecturally exempt: not flagged.
    assert "hooks.json" not in out


def test_heredoc_shape_reported_in_output(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _make_root(tmp_path)
    coord = root / "coordinator-claude"
    (coord / "bin" / "heredoc-only.sh").write_text(
        "#!/bin/bash\n"
        "\"${PYTHON:-python3}\" - \"$pname\" \"$reg_file\" <<'PYEOF' 2>/dev/null | tr -d '\\r'\n"
        "import sys; print(sys.argv[1])\n"
        "PYEOF\n"
    )
    rc = main([str(root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "python" in out.lower()


def test_bare_pwsh_without_windowstyle_hidden_flagged(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _make_root(tmp_path)
    coord = root / "coordinator-claude"
    (coord / "bin" / "ps-bare.sh").write_text('#!/bin/bash\npwsh -Command "Get-Process"\n')
    rc = main([str(root)])
    assert rc == 1


# ---------------------------------------------------------------------------
# File-level allow marker (mirrors verify-no-console-flash-file-allow.bats)
# ---------------------------------------------------------------------------


def test_bare_python_c_spawn_flagged(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    root = _make_root(tmp_path)
    coord = root / "coordinator-claude"
    (coord / "bin" / "probe.sh").write_text(
        '#!/usr/bin/env bash\npython -c "import sys; print(sys.version)"\n'
    )
    rc = main([str(root)])
    out = capsys.readouterr().out
    assert rc == 1
    assert "probe.sh" in out


def test_file_allow_header_marker_suppresses_whole_file(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _make_root(tmp_path)
    coord = root / "coordinator-claude"
    (coord / "bin" / "runpod_train.sh").write_text(
        "#!/usr/bin/env bash\n"
        "# verify-no-console-flash: file-allow — Linux-only training pipeline\n"
        'python -c "import sys; print(sys.version)"\n'
        'python -c "import torch"\n'
    )
    rc = main([str(root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK:" in out


def test_pure_comment_mentioning_pwsh_not_flagged(
    tmp_path: Path, capsys: pytest.CaptureFixture
) -> None:
    root = _make_root(tmp_path)
    coord = root / "coordinator-claude"
    (coord / "bin" / "notes.sh").write_text(
        "#!/usr/bin/env bash\n# we used to call pwsh here but stopped\necho hi\n"
    )
    rc = main([str(root)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK:" in out


# ---------------------------------------------------------------------------
# Edge cases not in the coordinator-claude oracle suite but load-bearing for the port
# ---------------------------------------------------------------------------


def test_nonexistent_root_exits_clean(tmp_path: Path, capsys: pytest.CaptureFixture) -> None:
    rc = main([str(tmp_path / "does-not-exist")])
    out = capsys.readouterr().out
    assert rc == 0
    assert "OK:" in out


def test_default_root_used_when_argv_empty(capsys: pytest.CaptureFixture) -> None:
    # No assertion on content (real $HOME/.claude/plugins tree may or may not
    # exist in CI) — only that main() does not raise on an empty argv, i.e.
    # the ROOT-defaulting branch (mirrors bash `${1:-$HOME/.claude/plugins}`)
    # is reachable and returns a valid rc.
    rc = main([])
    capsys.readouterr()
    assert rc in (0, 1)


class TestIsSuppressed:
    def test_allow_marker_suppresses(self) -> None:
        line = 'path/to/file.sh:10:python3 foo.py # verify-no-console-flash: allow'
        assert _is_suppressed(line) is True

    def test_spawn_hidden_routing_suppresses(self) -> None:
        line = "path/to/file.sh:10:lib/spawn-hidden.sh python3 foo.py"
        assert _is_suppressed(line) is True

    def test_create_no_window_flag_suppresses(self) -> None:
        line = "path/to/file.sh:10:subprocess.run(..., creationflags=CREATE_NO_WINDOW)"
        assert _is_suppressed(line) is True

    def test_unsuppressed_line_not_suppressed(self) -> None:
        line = "path/to/file.sh:10:python3 foo.py"
        assert _is_suppressed(line) is False

    def test_drive_letter_path_extraction_does_not_crash(self) -> None:
        # Regression pin (code-reviewer F7 in the bash oracle): the
        # drive-letter-safe strip (`:[0-9]+:.*$`) must not raise on a
        # synthetic Windows-shaped path, even though it can't resolve on
        # POSIX (os.path.isfile is False, so the file-allow branch just
        # falls through — no crash is the assertion).
        line = 'C:/path/file.sh:42:    python -c "code"'
        assert _is_suppressed(line) is False


class TestInInlineComment:
    def test_spawn_before_hash_is_not_comment(self) -> None:
        line = "path/to/file.sh:10:python3 foo.py  # trailing note"
        assert _in_inline_comment(line) is False

    def test_spawn_only_after_hash_is_comment(self) -> None:
        line = "path/to/file.sh:10:# we used to call pwsh here but stopped"
        assert _in_inline_comment(line) is True

    def test_no_spawn_token_at_all_is_comment_shaped(self) -> None:
        line = "path/to/file.sh:10:echo hi"
        assert _in_inline_comment(line) is True
