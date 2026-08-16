"""coordinator/bin/tests/test_publish_dest_engine_root_viable.py — C10
(docs/plans/2026-08-15-klabauter-release-channels.md): `publish.py` refuses
to dispatch a row when its dest is in a state the published-engine rung
(`resolve_engine_root()`'s downstream consumers) would reject: detached HEAD,
or a REGISTERED mirror missing `coordinator_core/` with no in-flight write
that would populate it this round.

This guard shipped in the same diff as C8/C9 with zero direct coverage
(state/subagent-share/93578a3d-c17d-45c0-984c-6f393c342fd4/
coordinatorcode-reviewer-e094bd79.md, finding 1) and, before the fix this
file pins, applied unconditionally to every mirror row regardless of
registration (finding 1) and judged only the dest's CURRENT state rather
than its state after this row's own write would complete (finding 2) —
refusing any unregistered dest (a docs mirror, a toplevel flat mirror, a
plain test fixture) and any virgin dest whose own row-write is what would
populate `coordinator_core/` for the first time.

Run: python -m pytest coordinator/bin/tests/test_publish_dest_engine_root_viable.py -q -p no:cacheprovider --maxfail=1
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        capture_output=True,
        check=True,
        creationflags=_NO_WINDOW,
    )


def _init_git_repo(root: Path, *, branch: str = "main") -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", branch, cwd=root)
    _git("config", "user.email", "publish-dest-engine-root-viable-test@claude-klabauter.test", cwd=root)
    _git("config", "user.name", "Publish Dest Engine Root Viable Test", cwd=root)
    _git("config", "commit.gpgsign", "false", cwd=root)
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep", cwd=root)
    _git("commit", "-m", "chore: init", cwd=root)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_dest_engine_root_viable_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_ROW_KEY = "engine-mirror"


def _write_registry(registry_dir: Path, *, dest: Path) -> None:
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "registry.toml").write_text(
        "[publish.mirrors]\n" f'"{_ROW_KEY}.path" = "{dest.as_posix()}"\n',
        encoding="utf-8",
    )


def _make_target(name: str, source_dir: Path, dest_dir: Path) -> "publish.ResolvedTarget":
    return publish.ResolvedTarget(
        name=name,
        mode="mirror",
        source_dir=source_dir,
        dest_dir=dest_dir,
    )


# ---------------------------------------------------------------------------
# 1. Unregistered dest missing coordinator_core/ — out of scope, proceeds.
# This is the coverage-gap shape a live regression was reported against: a
# generic test/docs/flat-mirror dest with no `coordinator_core/` and no
# registry entry must never be refused by this guard.
# ---------------------------------------------------------------------------


def test_unregistered_dest_missing_engine_root_proceeds(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    _init_git_repo(dest)
    (dest / "payload.txt").write_text("x\n", encoding="utf-8")
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "registry.toml").write_text(
        '[publish.mirrors]\n"unrelated-key.path" = "/nowhere"\n', encoding="utf-8"
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    target = _make_target("row-unregistered", tmp_path / "src", dest)
    totals = publish.RunTotals()

    assert publish.assert_dest_engine_root_viable(target, totals) is True


# ---------------------------------------------------------------------------
# 2. Registered mirror, genuinely degraded: coordinator_core/ absent, dest
# already has other content (not a virgin dest), this row's own source does
# not carry coordinator_core/ either — refused.
# ---------------------------------------------------------------------------


def test_registered_mirror_degraded_engine_root_refuses(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    _init_git_repo(dest)
    (dest / "some_other_payload.txt").write_text("stale\n", encoding="utf-8")
    _git("add", ".", cwd=dest)
    _git("commit", "-q", "-m", "prior round", cwd=dest)

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    src = tmp_path / "src"
    src.mkdir()

    target = _make_target("row-degraded", src, dest)
    totals = publish.RunTotals()

    result = publish.assert_dest_engine_root_viable(target, totals)
    captured = capsys.readouterr()

    assert result is False
    assert "coordinator_core" in (captured.out + captured.err)


# ---------------------------------------------------------------------------
# 3. Registered mirror, dest already carries coordinator_core/ from a prior
# round — proceeds.
# ---------------------------------------------------------------------------


def test_registered_mirror_with_existing_engine_root_proceeds(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    _init_git_repo(dest)
    (dest / "coordinator_core").mkdir()
    (dest / "coordinator_core" / "marker.py").write_text("", encoding="utf-8")
    _git("add", ".", cwd=dest)
    _git("commit", "-q", "-m", "prior round", cwd=dest)

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    target = _make_target("row-existing", tmp_path / "src", dest)
    totals = publish.RunTotals()

    assert publish.assert_dest_engine_root_viable(target, totals) is True


# ---------------------------------------------------------------------------
# 4. Registered mirror, virgin dest whose own row-write is what would
# populate coordinator_core/ this round (source already carries it) —
# proceeds, POST-write state, not current state.
# ---------------------------------------------------------------------------


def test_registered_mirror_virgin_dest_row_populates_engine_root_proceeds(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    _init_git_repo(dest)  # virgin: only the .gitkeep init commit

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    src = tmp_path / "src"
    (src / "coordinator_core").mkdir(parents=True)
    (src / "coordinator_core" / "marker.py").write_text("", encoding="utf-8")

    target = _make_target("row-virgin-populates", src, dest)
    totals = publish.RunTotals()

    assert publish.assert_dest_engine_root_viable(target, totals) is True


# ---------------------------------------------------------------------------
# 5. Registered mirror, detached HEAD — refused.
# ---------------------------------------------------------------------------


def test_registered_mirror_detached_head_refuses(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    _init_git_repo(dest)
    (dest / "coordinator_core").mkdir()
    (dest / "coordinator_core" / "marker.py").write_text("", encoding="utf-8")
    _git("add", ".", cwd=dest)
    _git("commit", "-q", "-m", "prior round", cwd=dest)
    head_sha = publish._git_head(dest)
    _git("checkout", "-q", head_sha, cwd=dest)

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    target = _make_target("row-detached", tmp_path / "src", dest)
    totals = publish.RunTotals()

    result = publish.assert_dest_engine_root_viable(target, totals)
    captured = capsys.readouterr()

    assert result is False
    assert "detached" in (captured.out + captured.err)
