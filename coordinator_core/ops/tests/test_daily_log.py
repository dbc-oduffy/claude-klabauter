"""changelog.daily_log: registered, ranked, zero-model, within the brightline on a fixture repo."""
from __future__ import annotations

import asyncio
import datetime
import os
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ipc import get_op_handler
from coordinator_core.ops import daily_log

pytestmark = pytest.mark.complete

UTC = datetime.timezone.utc
PROCESS_BUDGET_SECS = 0.5


def _write(repo: Path, rel: str, text: str) -> None:
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def _plan(title: str, status: str, size: str, completed: str) -> str:
    return (
        f"---\ntitle: \"{title}\"\nstatus: {status}\ncompleted: {completed}\n"
        f"sizing_object: \"state/sizings/{size}.yaml\"\n---\n\n"
        f"# {title}\n\n## Problem\n\nThe {title} summary sentence.\n\n## Tasks\n"
    )


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    now = datetime.datetime.now(UTC)
    today = now.strftime("%Y-%m-%d")
    old = (now - datetime.timedelta(days=3)).strftime("%Y-%m-%d")
    r = tmp_path / "repo"
    for size, tshirt in (("small", "S"), ("big", "L")):
        _write(r, f"state/sizings/{size}.yaml", f"estimate:\n  tshirt: {tshirt}\n")
    month = f"{now:%Y-%m}"
    _write(r, f"docs/plans/{month}/a.md", _plan("a", "complete", "small", today))
    _write(r, f"docs/plans/{month}/b.md", _plan("b", "complete", "big", today))
    _write(r, f"docs/plans/{month}/wip.md", _plan("wip", "executing", "big", today))
    _write(r, f"docs/plans/{month}/old.md", _plan("old", "complete", "big", old))
    return r


def _invoke(repo: Path, params: dict) -> dict:
    handler = get_op_handler("changelog.daily_log")
    assert handler is not None
    return asyncio.run(handler(params, repo_root=repo))


def test_op_default_window_ranks_by_sizing_and_marks_scan_degraded(repo, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-home"))
    monkeypatch.setattr(daily_log, "main_worktree_root", lambda p: p)
    out = _invoke(repo, {})
    month = f"{datetime.datetime.now(UTC):%Y-%m}"
    assert [e["plan"] for e in out["entries"]] == [f"docs/plans/{month}/b.md", f"docs/plans/{month}/a.md"]
    assert out["until"] - out["since"] == 24 * 3600
    assert all(e["sha"] is None for e in out["entries"])
    assert out["degraded"]["mode"] == "view-absent"
    assert "Degraded (view-absent)" in out["text"] and f"`docs/plans/{month}/a.md` (-)" in out["text"]


def test_explicit_window_overrides_default_and_bad_input_is_refused(repo, monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-home"))
    monkeypatch.setattr(daily_log, "main_worktree_root", lambda p: p)
    now = datetime.datetime.now(UTC)
    wide = _invoke(repo, {"since": (now - datetime.timedelta(days=5)).isoformat(), "until": now.isoformat()})
    assert any(e["plan"].endswith("old.md") for e in wide["entries"])
    narrow = _invoke(repo, {"since": int(now.timestamp()) + 3 * 86400, "until": int(now.timestamp()) + 4 * 86400})
    assert narrow["count"] == 0 and "None." in narrow["text"]
    assert "error" in _invoke(repo, {"since": "not-a-date"})


def test_default_args_on_this_repo_are_under_the_brightline_with_zero_spawns(monkeypatch, tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    spawns: "list[object]" = []
    real_popen = subprocess.Popen
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: spawns.append(a) or real_popen(*a, **k))
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp_path / "no-home"))
    before = os.times()
    out = daily_log.daily_log(root, settings_home=tmp_path / "no-home")
    after = os.times()
    cpu = sum(getattr(after, f) - getattr(before, f) for f in ("user", "system", "children_user", "children_system"))
    assert out["degraded"]["mode"] == "view-absent"
    assert cpu < PROCESS_BUDGET_SECS
    assert spawns == []


def test_no_agent_or_model_module_is_imported_by_the_op() -> None:
    probe = (
        "import sys\n"
        "import coordinator_core.ops.daily_log as m\n"
        "banned = ('anthropic', 'openai', 'claude_agent_sdk', 'coordinator_core.dispatch')\n"
        "hit = sorted(n for n in sys.modules if n.split('.')[0] in banned[:3] or n.startswith(banned[3]))\n"
        "print(hit)\n"
    )
    root = str(Path(__file__).resolve().parents[3])
    cp = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True,
        cwd=root, env={**os.environ, "PYTHONPATH": root},
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert cp.returncode == 0, cp.stderr
    assert cp.stdout.strip() == "[]"
