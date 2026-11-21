"""changelog.daily_log: registered, ranked, zero-model, within the brightline on a fixture repo."""
from __future__ import annotations

import datetime
import os
import subprocess
import sys
from pathlib import Path

import pytest


pytestmark = [pytest.mark.complete, pytest.mark.spawns_process, pytest.mark.cadence]

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
