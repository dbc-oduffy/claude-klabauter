"""`_cmd_round` refuses before staging anything when the source HEAD is not
pushed. Real gate (REAL_SOURCE_PREFLIGHT opts out of conftest's
stub), real git source, local bare origin; tmp_path only."""
from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest

from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

REAL_SOURCE_PREFLIGHT = True

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_module():
    spec = importlib.util.spec_from_file_location("percolate_round_source_preflight", _BIN_DIR / "percolate-round.py")
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_mod = _load_module()


def _git(args, cwd) -> None:
    subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True, **no_console_creationflags()
    )


def _source_with_origin(tmp_path: Path) -> Path:
    bare = tmp_path / "origin.git"
    _git(["init", "-q", "--bare", str(bare)], tmp_path)
    repo = tmp_path / "source"
    repo.mkdir()
    _git(["init", "-q"], repo)
    _git(["config", "user.email", "t@t.example"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
    _git(["add", "--", "a.py"], repo)
    _git(["commit", "-q", "-m", "seed"], repo)
    _git(["branch", "-m", "work/wiring"], repo)
    _git(["remote", "add", "origin", str(bare)], repo)
    _git(["push", "-q", "-u", "origin", "work/wiring"], repo)
    return repo


def _run(monkeypatch, tmp_path, source: Path):
    reached = []
    monkeypatch.setattr(_mod, "_resolve_percolate_root", lambda override: str(tmp_path / "root"))
    monkeypatch.setattr(_mod, "_branch0_gate", lambda target, root: str(source))
    monkeypatch.setattr(_mod, "_resolve_dest", lambda target, root: str(tmp_path / "dest"))
    monkeypatch.setattr(_mod, "_cmd_round_default", lambda *a, **k: reached.append(a) or 0)
    args = _mod._build_parser().parse_args(["alpha", "--percolate-root", str(tmp_path / "root")])
    return _mod._cmd_round(args), reached


def test_unpushable_source_refuses_before_the_round(monkeypatch, tmp_path, capsys):
    source = _source_with_origin(tmp_path)
    _git(["checkout", "-q", "-b", "work/no-upstream"], source)

    rc, reached = _run(monkeypatch, tmp_path, source)

    assert rc == _mod._EXIT_FAIL
    assert reached == []
    assert "has no upstream" in capsys.readouterr().err


def test_pushed_source_reaches_the_round(monkeypatch, tmp_path):
    source = _source_with_origin(tmp_path)

    rc, reached = _run(monkeypatch, tmp_path, source)

    assert rc == 0
    assert len(reached) == 1
