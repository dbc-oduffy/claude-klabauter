"""The handoff brief spawns no Session-Id commit walk; /handoff's apply path writes the commit list."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from coordinator_core import baton_assemble
from coordinator_core.baton_assemble import apply as baton_apply

_SID = "11111111-2222-3333-4444-555555555555"


def _completed(stdout: str = "") -> MagicMock:
    done = MagicMock()
    done.returncode = 0
    done.stdout = stdout
    done.stderr = ""
    return done


def test_brief_spawns_no_session_id_log(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", _SID)
    handoff = tmp_path / "state" / "handoffs" / "h1.md"
    handoff.parent.mkdir(parents=True)
    handoff.write_text("---\ntitle: h1\n---\n\n# h1\n", encoding="utf-8")
    argvs: list[list[str]] = []

    def _record_run(argv, *args, **kwargs):
        argvs.append(list(argv))
        return _completed("")

    real_init = subprocess.Popen.__init__

    def _record_popen(self, argv, *args, **kwargs):
        argvs.append(list(argv) if not isinstance(argv, str) else [argv])
        return real_init(self, argv, *args, **kwargs)

    with patch.object(subprocess, "run", side_effect=_record_run), patch.object(
        subprocess.Popen, "__init__", _record_popen
    ), patch.object(baton_assemble, "git_common_dir", return_value=tmp_path / ".git"):
        try:
            baton_assemble.brief("handoff", "state/handoffs/h1.md", repo_root=tmp_path)
        except Exception:  # noqa: BLE001 — only the spawned argv is under test
            pass

    assert not [a for a in argvs if any("--grep=^Session-Id" in t for t in a)]
    assert not hasattr(baton_assemble, "_print_commits_into_baton")


def test_apply_write_path_merges_session_commits(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(baton_apply, "_resolve_current_session_id", lambda: _SID)
    with patch(
        "coordinator_core.ops.session_commits.resolve_session_commits",
        return_value=[{"sha": "a" * 40}, {"sha": "b" * 40}],
    ), patch("coordinator_core.session_baton.store.merge_baton") as merge:
        baton_apply._record_commits_into_baton(tmp_path)

    merge.assert_called_once_with(_SID, cwd=str(tmp_path), commits=["a" * 40, "b" * 40])


def test_apply_write_path_is_fail_open(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(baton_apply, "_resolve_current_session_id", lambda: _SID)
    with patch(
        "coordinator_core.ops.session_commits.resolve_session_commits",
        side_effect=RuntimeError("git gone"),
    ), patch("coordinator_core.session_baton.store.merge_baton") as merge:
        baton_apply._record_commits_into_baton(tmp_path)

    merge.assert_not_called()
