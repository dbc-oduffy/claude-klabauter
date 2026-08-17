"""coordinator/bin/tests/test_publish_dest_engine_root_viable.py — C10
(docs/plans/2026-08-15-klabauter-release-channels.md): `publish.py` refuses
to dispatch a row when its dest is in a state the published-engine rung
(`resolve_engine_root()`'s downstream consumers) would reject: detached HEAD,
or an ENGINE-DECLARING mirror missing `coordinator_core/` with no in-flight
write that would populate it this round.

This guard shipped in the same diff as C8/C9 with zero direct coverage
(state/subagent-share/93578a3d-c17d-45c0-984c-6f393c342fd4/
coordinatorcode-reviewer-e094bd79.md, finding 1) and, before the fix this
file pins, applied unconditionally to every mirror row regardless of
registration (finding 1) and judged only the dest's CURRENT state rather
than its state after this row's own write would complete (finding 2) —
refusing any unregistered dest (a docs mirror, a toplevel flat mirror, a
plain test fixture) and any virgin dest whose own row-write is what would
populate `coordinator_core/` for the first time.

SECOND narrowing (cross-repo memo 2026-08-16-doe-claude-em-engine-guards-
block-coordinator-claude-publish.md): registration alone was still too wide
— a REGISTERED mirror that is deliberately engine-free by design (the
`coordinator_claude` shape: doctrine-only OSS mirror, never carries
`coordinator_core/`) was refused for every row, 0/5 succeeded. The guard now
additionally requires the dest's mirror key to be one the PRIMARY portable
topology (`setup/publish-targets.portable`) actually writes
`coordinator_core` into (`_engine_declaring_mirror_keys`) — every test below
that expects a REFUSAL therefore writes an isolated portable file declaring
`_ROW_KEY`'s mirror as engine-carrying via `PORTABLE_TARGETS_FILE`; tests 6
and 7 pin the new discrimination itself.

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


def _write_portable_file(
    portable_path: Path, *, key: str = _ROW_KEY, declares_engine: bool
) -> None:
    """Isolated `publish-targets.portable`-shaped file, pointed to via the
    `PORTABLE_TARGETS_FILE` env override (`_resolve_portable_file`'s
    highest-precedence rung) so no test ever falls through to reading this
    repo's REAL `setup/publish-targets.portable` — that would couple every
    test's expectation to this repo's live row set. `declares_engine=True`
    writes the `coordinator_core`-dest_subdir row shape
    `_engine_declaring_mirror_keys` looks for (the `claude-klabauter` row's
    own shape); `declares_engine=False` writes an unrelated, non-declaring
    row for the same key (the `coordinator_claude` shape — registered, but
    no row of its own ever names `coordinator_core` as a dest_subdir)."""
    portable_path.parent.mkdir(parents=True, exist_ok=True)
    if declares_engine:
        row = f"declares-engine-row|mirror|publish-mirror:{key}|coordinator_core|coordinator_core||\n"
    else:
        row = f"docs-only-row|mirror|publish-mirror:{key}|docs|docs||\n"
    portable_path.write_text(row, encoding="utf-8")


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
# 2. Registered, engine-declaring mirror, genuinely degraded: coordinator_
# core/ absent, dest already has other content (not a virgin dest), this
# row's own source does not carry coordinator_core/ either — refused. This
# is the ORIGINAL defect the guard exists for; it must not regress under the
# engine-declaring narrowing (see test 7 below for the same shape restated
# explicitly as the regression pin).
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
    portable_file = tmp_path / "publish-targets.portable"
    _write_portable_file(portable_file, declares_engine=True)
    monkeypatch.setenv("PORTABLE_TARGETS_FILE", str(portable_file))

    src = tmp_path / "src"
    src.mkdir()

    target = _make_target("row-degraded", src, dest)
    totals = publish.RunTotals()

    result = publish.assert_dest_engine_root_viable(target, totals)
    captured = capsys.readouterr()

    assert result is False
    assert "coordinator_core" in (captured.out + captured.err)


# ---------------------------------------------------------------------------
# 3. Registered, engine-declaring mirror, dest already carries coordinator_
# core/ from a prior round — proceeds.
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
    portable_file = tmp_path / "publish-targets.portable"
    _write_portable_file(portable_file, declares_engine=True)
    monkeypatch.setenv("PORTABLE_TARGETS_FILE", str(portable_file))

    target = _make_target("row-existing", tmp_path / "src", dest)
    totals = publish.RunTotals()

    assert publish.assert_dest_engine_root_viable(target, totals) is True


# ---------------------------------------------------------------------------
# 4. Registered, engine-declaring mirror, virgin dest whose own row-write is
# what would populate coordinator_core/ this round (source already carries
# it) — proceeds, POST-write state, not current state.
# ---------------------------------------------------------------------------


def test_registered_mirror_virgin_dest_row_populates_engine_root_proceeds(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    _init_git_repo(dest)  # virgin: only the .gitkeep init commit

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))
    portable_file = tmp_path / "publish-targets.portable"
    _write_portable_file(portable_file, declares_engine=True)
    monkeypatch.setenv("PORTABLE_TARGETS_FILE", str(portable_file))

    src = tmp_path / "src"
    (src / "coordinator_core").mkdir(parents=True)
    (src / "coordinator_core" / "marker.py").write_text("", encoding="utf-8")

    target = _make_target("row-virgin-populates", src, dest)
    totals = publish.RunTotals()

    assert publish.assert_dest_engine_root_viable(target, totals) is True


# ---------------------------------------------------------------------------
# 5. Registered, engine-declaring mirror, detached HEAD — refused.
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
    portable_file = tmp_path / "publish-targets.portable"
    _write_portable_file(portable_file, declares_engine=True)
    monkeypatch.setenv("PORTABLE_TARGETS_FILE", str(portable_file))

    target = _make_target("row-detached", tmp_path / "src", dest)
    totals = publish.RunTotals()

    result = publish.assert_dest_engine_root_viable(target, totals)
    captured = capsys.readouterr()

    assert result is False
    assert "detached" in (captured.out + captured.err)


# ---------------------------------------------------------------------------
# 6. Registered, ENGINE-FREE mirror (coordinator_claude shape): registry
# entry exists, but no row in the portable topology ever writes
# coordinator_core into this dest — missing coordinator_core/ is expected,
# not degraded. Guard PASSES. This pins the live defect this dispatch fixes:
# before the fix, registration alone put this dest in scope and it refused
# unconditionally (0/5 rows on the real coordinator_claude mirror).
# ---------------------------------------------------------------------------


def test_registered_engine_free_mirror_missing_engine_root_proceeds(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    _init_git_repo(dest)
    (dest / "doctrine.md").write_text("x\n", encoding="utf-8")
    _git("add", ".", cwd=dest)
    _git("commit", "-q", "-m", "prior round", cwd=dest)

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))
    portable_file = tmp_path / "publish-targets.portable"
    _write_portable_file(portable_file, declares_engine=False)
    monkeypatch.setenv("PORTABLE_TARGETS_FILE", str(portable_file))

    target = _make_target("row-engine-free", tmp_path / "src", dest)
    totals = publish.RunTotals()

    assert publish.assert_dest_engine_root_viable(target, totals) is True


# ---------------------------------------------------------------------------
# 7. Registered, ENGINE-DECLARING mirror (klabauter shape) missing
# coordinator_core/ — still REFUSED. Restates test 2 explicitly as the
# regression pin for the second narrowing: an engine-declaring mirror going
# degraded must not be waved through just because SOME registered mirrors
# are legitimately engine-free.
# ---------------------------------------------------------------------------


def test_registered_engine_declaring_mirror_missing_engine_root_still_refuses(
    tmp_path, monkeypatch, capsys
):
    dest = tmp_path / "dest"
    _init_git_repo(dest)
    (dest / "some_other_payload.txt").write_text("stale\n", encoding="utf-8")
    _git("add", ".", cwd=dest)
    _git("commit", "-q", "-m", "prior round", cwd=dest)

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))
    portable_file = tmp_path / "publish-targets.portable"
    _write_portable_file(portable_file, declares_engine=True)
    monkeypatch.setenv("PORTABLE_TARGETS_FILE", str(portable_file))

    src = tmp_path / "src"
    src.mkdir()

    target = _make_target("row-still-degraded", src, dest)
    totals = publish.RunTotals()

    result = publish.assert_dest_engine_root_viable(target, totals)
    captured = capsys.readouterr()

    assert result is False
    assert "coordinator_core" in (captured.out + captured.err)
