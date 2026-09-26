
from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Optional

import pytest

from coordinator_core.win_portability import no_console_creationflags

import coordinator_core.ops.handoff_archive_transition  # noqa: F401
import coordinator_core.ops.handoff_stamp  # noqa: F401
import coordinator_core.ops.handoff_transition  # noqa: F401
import coordinator_core.ops.session.record_pickup  # noqa: F401

import coordinator_core.archive_stamp as arstamp
from coordinator_core.session import claims

pytestmark = [
    pytest.mark.spawns_process,
    pytest.mark.cadence,
]

_GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "t@t",
}

_DEFAULT_TEST_SESSION_ID = "22222222-2222-2222-2222-222222222222"


def _git(
    repo: Path, *args: str, session_id: Optional[str] = _DEFAULT_TEST_SESSION_ID
) -> subprocess.CompletedProcess:
    args_list = list(args)
    if (
        len(args_list) >= 3
        and args_list[0] == "commit"
        and args_list[1] == "-m"
        and session_id is not None
        and "Session-Id:" not in args_list[2]
    ):
        args_list[2] = f"{args_list[2]}\n\nSession-Id: {session_id}"
    return subprocess.run(
        ["git", "-C", str(repo), *args_list],
        capture_output=True,
        text=True,
        env=_GIT_ENV,
        timeout=15,
        stdin=subprocess.DEVNULL,
        **no_console_creationflags(),
    )


@pytest.fixture(autouse=True)
def _default_caller_session_id(monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", _DEFAULT_TEST_SESSION_ID)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


def _init_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    _git(repo, "init")
    _git(repo, "config", "commit.gpgsign", "false")
    (repo / "README.md").write_text("init\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")


def _seed_handoff(repo: Path, name: str, status: str, deployment_state: str, extra: str = "") -> Path:
    path = repo / "state" / "handoffs" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    fm = (
        f'title: "Test Handoff {name}"\n'
        "created: 2026-01-01\n"
        "branch: work/test/2026-01-01\n"
        f"status: {status}\n"
        'predecessor: "none"\n'
        f"deployment_state: {deployment_state}\n"
    )
    if extra:
        fm += extra
    path.write_text(f"---\n{fm}---\n\n# Handoff\n\nBody.\n", encoding="utf-8")
    _git(repo, "add", str(path.relative_to(repo)))
    _git(repo, "commit", "-m", f"add {name}")
    return path


def _claim_dir(repo: Path, class_: str, basename: str) -> Path:
    return repo / ".git" / "coordinator-sessions" / f"{class_}-claims" / basename


class TestShipReleasesClaim:
    def test_a_genuine_ship_releases_the_claim_it_held(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship-release1.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/ship-release1.md\n",
        )
        assert claims.claim_artifact("handoff", "ship-release1.md", cwd=str(repo)) is True
        assert _claim_dir(repo, "handoff", "ship-release1.md").is_dir()

        rc = arstamp.cs_ship_handoff(str(hp))

        assert rc == 0
        assert not _claim_dir(repo, "handoff", "ship-release1.md").exists(), (
            "a genuine ship must release the claim it was holding"
        )

    def test_a_ship_with_no_prior_claim_is_still_a_clean_success(self, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship-release2.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/ship-release2.md\n",
        )
        assert not _claim_dir(repo, "handoff", "ship-release2.md").exists()

        rc = arstamp.cs_ship_handoff(str(hp))

        assert rc == 0
        assert not _claim_dir(repo, "handoff", "ship-release2.md").exists()

    def test_a_refused_ship_never_reaches_the_release_call(self, tmp_path, monkeypatch):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship-refused.md", "open", "ready_to_fire",
            extra=(
                "kind: goal-seed\n"
                "handoff_phase: execution\n"
                "scope:\n  - state/handoffs/ship-refused.md\n"
            ),
        )
        assert claims.claim_artifact("handoff", "ship-refused.md", cwd=str(repo)) is True

        released = []
        real_release = claims.release_artifact

        def _spy(*args, **kwargs):
            released.append((args, kwargs))
            return real_release(*args, **kwargs)

        monkeypatch.setattr(claims, "release_artifact", _spy)

        rc = arstamp.cs_ship_handoff(str(hp))

        assert rc != 0
        assert released == []
        assert _claim_dir(repo, "handoff", "ship-refused.md").is_dir()

    def test_a_retained_outcome_is_never_released(self, monkeypatch, tmp_path):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship-retained.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/ship-retained.md\n",
        )
        assert claims.claim_artifact("handoff", "ship-retained.md", cwd=str(repo)) is True

        def _fake_call(handoff_path, params):
            return {
                "exit_code": 0,
                "mode": params.get("mode"),
                "retained": True,
                "retain_reason": "forced-for-test",
                "message": None,
                "warnings": None,
            }

        monkeypatch.setattr(arstamp, "_call_handoff_archive_transition", _fake_call)

        released = []
        real_release = claims.release_artifact

        def _spy(*args, **kwargs):
            released.append((args, kwargs))
            return real_release(*args, **kwargs)

        monkeypatch.setattr(claims, "release_artifact", _spy)

        rc = arstamp.cs_ship_handoff(str(hp))

        assert rc == 0
        assert released == []
        assert _claim_dir(repo, "handoff", "ship-retained.md").is_dir()

    def test_release_failure_never_converts_a_successful_ship_into_an_error(
        self, tmp_path, monkeypatch, capsys
    ):
        repo = tmp_path / "repo"
        _init_repo(repo)
        hp = _seed_handoff(
            repo, "ship-release-fails.md", "claimed", "in_flight",
            extra="scope:\n  - state/handoffs/ship-release-fails.md\n",
        )

        def _boom(*args, **kwargs):
            raise RuntimeError("boom")

        monkeypatch.setattr(claims, "release_artifact", _boom)

        rc = arstamp.cs_ship_handoff(str(hp))

        assert rc == 0
        err = capsys.readouterr().err
        assert "claim release" in err
        assert "boom" in err
