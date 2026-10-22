"""dispatch.terminal_commit tolerates a non-`.py` drift only when the repo's declared verifier,
run for real, exits 0 over it; every other outcome stays real drift."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import run_token, terminal_commit
from coordinator_core.ops.dispatch_emit import commit_request as cr
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_comment_only_drift import (
    _REVIEWED,
    _reviewed,
)
from coordinator_core.ops.dispatch_emit.tests.test_terminal_commit_refuses_blob_drift import (  # noqa: F401 -- fixtures
    SID,
    _git,
    _sha,
    repo,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_SOURCE = "// Reviewed-by: kira\nconst x = 1;\n"
_CURRENT = "const x = 1;\n"


def _setup(repo, tmp_path, exit_code: int, declare: bool = True) -> Path:
    marker = tmp_path / "v-ran.marker"
    tools = repo / "tools"
    tools.mkdir()
    (tools / "v.py").write_text(
        "import sys\n"
        f"open({str(marker)!r}, 'w').write('ran')\n"
        f"sys.exit({exit_code})\n",
        encoding="utf-8",
    )
    _git(["add", "tools/v.py"], repo)
    _git(["commit", "-q", "-m", "verifier"], repo)
    if declare:
        (repo / "coordinator.local.md").write_text(
            "---\n"
            f'comment_only_verifier: ["{sys.executable}", "tools/v.py"]\n'
            'comment_only_verifier_suffixes: [".ts"]\n'
            "---\n",
            encoding="utf-8",
        )
    return marker


def _fire_ts(repo, monkeypatch, paths=("a.ts",)):
    request = cr.CommitRequest(chunks=(cr.ChunkCommit(id="C4", title="t4", paths=tuple(paths)),))
    (repo / "run.mjs").write_text("// s\n" + cr.render_marker(request) + "\n", encoding="utf-8", newline="")
    token = run_token.mint(repo, "run-1", "run.mjs")
    return terminal_commit._handler(
        {"script_path": "run.mjs", "incomplete_chunks": [], "inline_review": _REVIEWED,
         "session_id": SID, "run_id": "run-1", "run_token": token},
        repo_root=repo / ".git",
    )


def test_verifier_exit_zero_tolerates_ts_drift_with_trailer(repo, tmp_path, monkeypatch):
    marker = _setup(repo, tmp_path, 0)
    _reviewed(repo, {"a.ts": _SOURCE}, {"a.ts": _CURRENT})
    out = _fire_ts(repo, monkeypatch)
    assert out["committed"] is True, out
    assert [d["path"] for d in out["comment_only_drift"]] == ["a.ts"]
    assert f"Comment-Only-Drift: a.ts {_sha(_SOURCE)[:12]}->{_sha(_CURRENT)[:12]}" in _git(
        ["log", "-1", "--format=%B"], repo
    )
    assert marker.exists()


def test_verifier_exit_one_is_real_drift(repo, tmp_path, monkeypatch):
    marker = _setup(repo, tmp_path, 1)
    _reviewed(repo, {"a.ts": _SOURCE}, {"a.ts": _CURRENT})
    head = _git(["rev-parse", "HEAD"], repo)
    out = _fire_ts(repo, monkeypatch)
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert "a.ts" in out["error"].split("Withhold call:", 1)[1]
    assert marker.exists()
    assert _git(["rev-parse", "HEAD"], repo) == head


def test_no_declaration_refuses(repo, tmp_path, monkeypatch):
    marker = _setup(repo, tmp_path, 0, declare=False)
    _reviewed(repo, {"a.ts": _SOURCE}, {"a.ts": _CURRENT})
    out = _fire_ts(repo, monkeypatch)
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert not marker.exists()


def test_verifier_in_the_landing_write_set_refuses_without_running(repo, tmp_path, monkeypatch):
    marker = _setup(repo, tmp_path, 0)
    edited = (repo / "tools" / "v.py").read_text(encoding="utf-8") + "# edited\n"
    _reviewed(
        repo,
        {"a.ts": _SOURCE, "tools/v.py": edited},
        {"a.ts": _CURRENT, "tools/v.py": edited},
    )
    out = _fire_ts(repo, monkeypatch, paths=("a.ts", "tools/v.py"))
    assert out["committed"] is False
    assert out["refused"] == "blob-drift"
    assert not marker.exists()
