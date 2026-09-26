
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.win_portability import no_console_creationflags  # noqa: E402

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_SCRIPT = _BIN_DIR / "statusline.py"
_SETTINGS_PATH = _REPO_ROOT / "coordinator" / "settings.json"

_SAMPLE_STDIN = json.dumps(
    {
        "session_id": "test-session-abc",
        "model": {"display_name": "Opus", "id": "claude-opus-5"},
        "context_window": {
            "used_percentage": 25,
            "remaining_percentage": 75,
            "context_window_size": 200000,
            "current_usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        },
    }
).encode("utf-8")


def _run(stdin_bytes: bytes, tmp_path: Path, extra_env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["TMPDIR"] = str(tmp_path)
    # The sidecar lives under the SETTINGS HOME, not a tempdir --
    env["COORDINATOR_SETTINGS_HOME"] = str(tmp_path)
    env.pop("COORDINATOR_STATUSLINE_DEBUG", None)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(_REPO_ROOT),
        timeout=30,
        **no_console_creationflags(),
    )


def _sidecar_path(tmp_path: Path, session_id: str) -> Path:
    """Mirrors `context_usage_sidecar.sidecar_path` with `tmp_path` as the
    settings home (§ `_run`'s COORDINATOR_SETTINGS_HOME)."""
    return tmp_path / "state" / "context-window" / f"{session_id}.json"


@pytest.fixture(autouse=True)
def isolated_settings():
    """Snapshot any real `coordinator/settings.json` before the test and
    restore it exactly afterward — this file lives in the tracked repo
    tree, not a tmp_path, and this repo's 50-70 concurrent-session load norm
    means a real operator config could exist or be written mid-run. Tests
    that need their own `statusLineCommand` write through `_SETTINGS_PATH`
    directly; this fixture only guarantees the original state comes back.
    """
    original = _SETTINGS_PATH.read_bytes() if _SETTINGS_PATH.exists() else None
    try:
        yield
    finally:
        if original is None:
            _SETTINGS_PATH.unlink(missing_ok=True)
        else:
            _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            _SETTINGS_PATH.write_bytes(original)


def test_sidecar_written_from_representative_stdin(tmp_path):
    _SETTINGS_PATH.unlink(missing_ok=True)

    result = _run(_SAMPLE_STDIN, tmp_path)

    assert result.returncode == 0
    sidecar = _sidecar_path(tmp_path, "test-session-abc")
    assert sidecar.exists()
    payload = json.loads(sidecar.read_bytes())
    assert payload["context_window"]["used_percentage"] == 25
    assert isinstance(payload["captured_at"], (int, float))


def test_inner_command_invoked_with_identical_stdin_and_stdout_reproduced(tmp_path):
    inner_script = tmp_path / "inner_statusline.py"
    inner_script.write_text(
        textwrap.dedent(
            """\
            #!/usr/bin/env python3
            import sys
            raw = sys.stdin.buffer.read()
            sys.stdout.buffer.write(b"ECHO:" + raw)
            """
        )
    )
    inner_script.chmod(0o755)

    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(
        json.dumps({"statusLineCommand": [sys.executable, str(inner_script)]})
    )

    result = _run(_SAMPLE_STDIN, tmp_path)

    assert result.returncode == 0
    assert result.stdout == b"ECHO:" + _SAMPLE_STDIN
    assert _sidecar_path(tmp_path, "test-session-abc").exists()


def test_inner_command_failure_does_not_crash_the_wrapper(tmp_path):
    _SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _SETTINGS_PATH.write_text(
        json.dumps({"statusLineCommand": [str(tmp_path / "does-not-exist-binary")]})
    )

    result = _run(_SAMPLE_STDIN, tmp_path)

    assert result.returncode == 0
    assert result.stdout


def test_malformed_stdin_json_does_not_crash(tmp_path):
    _SETTINGS_PATH.unlink(missing_ok=True)

    result = _run(b"{not valid json at all", tmp_path)

    assert result.returncode == 0
    assert result.stdout
    assert list(tmp_path.glob("context-usage-*")) == []


def test_sidecar_write_failure_still_yields_a_status_line(tmp_path):
    _SETTINGS_PATH.unlink(missing_ok=True)

    not_a_dir = tmp_path / "not-a-directory"
    not_a_dir.write_text("occupied")

    result = _run(_SAMPLE_STDIN, not_a_dir)

    assert result.returncode == 0
    assert result.stdout


def test_selftest_prints_resolved_sidecar_path(tmp_path):
    _SETTINGS_PATH.unlink(missing_ok=True)
    env = dict(os.environ)
    env["TMPDIR"] = str(tmp_path)
    env["COORDINATOR_SETTINGS_HOME"] = str(tmp_path)
    result = subprocess.run(
        [sys.executable, str(_SCRIPT), "--selftest"],
        input=_SAMPLE_STDIN,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        cwd=str(_REPO_ROOT),
        timeout=30,
        **no_console_creationflags(),
    )

    assert result.returncode == 0
    printed_path = result.stdout.decode("utf-8").strip()
    assert printed_path == str(_sidecar_path(tmp_path, "test-session-abc"))
    assert Path(printed_path).exists()
