"""Behavioral tests for coordinator/bin/coordinator-postsync-marker-resync-check
— the post-merge/post-checkout self-healing gate added 2026-07-28.

Runs the script as a real subprocess (not an in-process import) since it is
a standalone bin/ entrypoint that resolves its own sys.path — the same
"prove it actually executes, not just that the text looks right" posture
as coordinator_core/ops/test_install_meta_repo_precommit_hook.py.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPT = (
    Path(__file__).resolve().parents[2] / "coordinator" / "bin" / "coordinator-postsync-marker-resync-check"
)


def _git_init(path: Path) -> None:
    subprocess.run(["git", "init", "-q", str(path)], check=True)


def _make_meta_repo(tmp_path: Path) -> Path:
    fake_home = tmp_path / "fakehome"
    meta = fake_home / ".claude"
    meta.mkdir(parents=True)
    _git_init(meta)
    return meta, fake_home


def _run(meta_repo: Path, home: Path, extra_env: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["HOME"] = str(home)
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        [sys.executable, str(_SCRIPT)],
        cwd=str(meta_repo),
        capture_output=True,
        text=True,
        env=env,
    )


def test_script_is_present_on_disk():
    # No shebang / no exec-bit assertion here on purpose: this gate is
    # invoked ONLY via an explicit interpreter (`"$_py" "$_gate_script"`,
    # see the script's own "Invocation" docstring note) — POSIX exec-bit
    # presence is itself a tracked-file portability defect class
    # (`coordinator_core.ops.check_posix_exec_assumptions`'s `mode_100755`),
    # not a property this test should require or encourage.
    assert _SCRIPT.is_file()


def test_not_meta_repo_is_a_silent_noop(tmp_path):
    meta, home = _make_meta_repo(tmp_path)
    other_home = tmp_path / "unrelated"
    result = _run(meta, other_home)
    assert result.returncode == 0


def test_no_settings_json_is_a_noop(tmp_path):
    meta, home = _make_meta_repo(tmp_path)
    result = _run(meta, home)
    assert result.returncode == 0
    # No settings.json present -> nothing to migrate or validate.
    assert not (meta / ".coordinator-hooks-enabled").exists()


def test_migration_creates_positive_marker_from_local_evidence(tmp_path):
    meta, home = _make_meta_repo(tmp_path)
    (meta / "settings.json").write_text(
        json.dumps({"env": {"COORDINATOR_CONTENT_ROOT": "/some/prior/coordinator/root"}, "hooks": {}}),
        encoding="utf-8",
    )
    assert not (meta / ".coordinator-hooks-enabled").exists()

    result = _run(meta, home)

    assert result.returncode == 0
    assert (meta / ".coordinator-hooks-enabled").is_file()
    assert "recreated positive marker" in result.stderr


def test_healthy_settings_json_is_silent_no_rearm(tmp_path):
    meta, home = _make_meta_repo(tmp_path)
    (meta / "settings.json").write_text(
        json.dumps({"hooks": {"SessionStart": [{"matcher": "", "hooks": [
            {"type": "command", "command": "python3 $COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py"}
        ]}]}}),
        encoding="utf-8",
    )

    result = _run(meta, home)

    assert result.returncode == 0
    assert not (meta / ".coordinator-hooks-disabled").exists()


def test_https_url_in_settings_json_is_not_a_false_positive(tmp_path):
    """The false-positive twin of `test_foreign_platform_shape_rearms_kill_
    switch` below: an unguarded drive-letter regex (`[A-Za-z]:[\\/]` with no
    left-boundary) matches the `s` in `https://` as a one-letter Windows
    drive and would falsely arm the kill switch on every ordinary URL. This
    gate reuses `guard_foreign_platform_paths.detect_foreign_platform_paths`
    (the shared, boundary-guarded `(?<![A-Za-z0-9])` regex) rather than
    inventing its own, so it must NOT fire here. A guard tested only on the
    blocking side is exactly how that defect shipped live once already —
    see the dispatch brief that added this test."""
    meta, home = _make_meta_repo(tmp_path)
    (meta / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 $COORDINATOR_CONTENT_ROOT/hooks/scripts/x.py",
                                }
                            ],
                        }
                    ]
                },
                "extraKnownMarketplaces": {
                    "coordinator-claude": {
                        "source": {"path": "https://github.com/dbc-example-operator/coordinator-claude"}
                    }
                },
                "someOtherUrl": "http://localhost:8080/foo",
            }
        ),
        encoding="utf-8",
    )

    result = _run(meta, home)

    assert result.returncode == 0
    assert not (meta / ".coordinator-hooks-disabled").exists()


def test_foreign_platform_shape_rearms_kill_switch(tmp_path):
    """The core scenario this gate exists for: a foreign-platform-shaped
    settings.json just arrived via git sync (simulated here by simply
    having it present in the worktree post-checkout/post-merge) — the
    NEGATIVE kill switch must be armed so generation stops."""
    meta, home = _make_meta_repo(tmp_path)
    (meta / "settings.json").write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python3 X:/example-doctrine-repo/coordinator/hooks/scripts/x.py",
                                }
                            ],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = _run(meta, home)

    assert result.returncode == 0
    marker = meta / ".coordinator-hooks-disabled"
    # detect_foreign_platform_paths defaults to os.name == "nt" -- on this
    # (POSIX) test runner a Windows-drive-letter path in settings.json IS
    # foreign-shaped, so the gate should have armed the kill switch.
    if os.name != "nt":
        assert marker.is_file()
        assert "RE-ARMED" in result.stderr
    else:
        pytest.skip("host-platform-dependent shape check; this runner is Windows")


def test_already_armed_kill_switch_is_not_duplicated(tmp_path):
    meta, home = _make_meta_repo(tmp_path)
    (meta / ".coordinator-hooks-disabled").write_text("already armed by someone else\n", encoding="utf-8")
    (meta / "settings.json").write_text(
        json.dumps(
            {"hooks": {"SessionStart": [{"matcher": "", "hooks": [
                {"type": "command", "command": "python3 X:/example-doctrine-repo/coordinator/hooks/scripts/x.py"}
            ]}]}}
        ),
        encoding="utf-8",
    )

    result = _run(meta, home)
    assert result.returncode == 0
    assert (meta / ".coordinator-hooks-disabled").read_text(encoding="utf-8") == "already armed by someone else\n"


def test_malformed_settings_json_is_a_silent_noop(tmp_path):
    meta, home = _make_meta_repo(tmp_path)
    (meta / "settings.json").write_text("{not valid json", encoding="utf-8")

    result = _run(meta, home)
    assert result.returncode == 0
