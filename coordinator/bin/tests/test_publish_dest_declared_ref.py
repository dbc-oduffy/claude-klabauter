"""coordinator/bin/tests/test_publish_dest_declared_ref.py — C8
(docs/plans/2026-08-15-klabauter-release-channels.md): `publish.py` refuses
to dispatch a row when its dest's checked-out branch doesn't match the row's
declared `publish.mirrors.<key>.track_ref` (C9). DoE's highest-severity
finding: nothing on the copy path previously asserted the dest's checked-out
branch, so a publish intended for one branch could land silently on whatever
happened to be checked out, and under whole-box dogfooding a mis-targeted
publish poisons every session running the published engine at once.

Exercises `assert_dest_on_declared_ref` (the row-level gate) directly for the
comparison/default/normalization/out-of-scope properties, and drives
`process_target`'s real call site to pin the load-bearing PLACEMENT property:
the refusal fires before the one-shot `repo-cut` bootstrap, `_ensure_dest_ready`'s
mkdir, and `dispatch_mirror_like` — never after a copy has already run.

Uses a non-klabauter row key throughout (`some-other-mirror`) so the test
itself would fail if the check were ever special-cased to klabauter.

Run: python -m pytest coordinator/bin/tests/test_publish_dest_declared_ref.py -q -p no:cacheprovider --maxfail=1
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
    """Real `git init` fixture, checked out on `branch` — never `cd` into the
    clone from the TEST's own perspective either; all git calls below go via
    `git -C`/`cwd=`, matching the module-under-test's own outside-the-clone
    discipline."""
    root.mkdir(parents=True, exist_ok=True)
    _git("init", "-b", branch, cwd=root)
    _git("config", "user.email", "publish-dest-declared-ref-test@claude-klabauter.test", cwd=root)
    _git("config", "user.name", "Publish Dest Declared Ref Test", cwd=root)
    _git("config", "commit.gpgsign", "false", cwd=root)
    keeper = root / ".gitkeep"
    keeper.write_text("", encoding="utf-8")
    _git("add", ".gitkeep", cwd=root)
    _git("commit", "-m", "chore: init", cwd=root)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_dest_declared_ref_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

_ROW_KEY = "some-other-mirror"  # deliberately not klabauter — see module docstring


def _write_registry(registry_dir: Path, *, dest: Path, track_ref: str | None) -> None:
    registry_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        "[publish.mirrors]",
        f'"{_ROW_KEY}.path" = "{dest.as_posix()}"',
    ]
    if track_ref is not None:
        lines.append(f'"{_ROW_KEY}.track_ref" = "{track_ref}"')
    (registry_dir / "registry.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _make_target(name: str, source_dir: Path, dest_dir: Path) -> "publish.ResolvedTarget":
    return publish.ResolvedTarget(
        name=name,
        mode="mirror",
        source_dir=source_dir,
        dest_dir=dest_dir,
    )


# ---------------------------------------------------------------------------
# 1. Match — dest on the declared ref, row proceeds.
# ---------------------------------------------------------------------------


def test_dest_on_declared_ref_proceeds(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="candidate")
    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest, track_ref="candidate")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    target = _make_target("row-match", tmp_path / "src", dest)
    totals = publish.RunTotals()

    assert publish.assert_dest_on_declared_ref(target, totals) is True


# ---------------------------------------------------------------------------
# 2. Mismatch — refused, naming both the expected and actual ref.
# ---------------------------------------------------------------------------


def test_dest_mismatch_refuses_and_names_both_refs(tmp_path, monkeypatch, capsys):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="main")
    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest, track_ref="candidate")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    target = _make_target("row-mismatch", tmp_path / "src", dest)
    totals = publish.RunTotals()

    result = publish.assert_dest_on_declared_ref(target, totals)
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert result is False
    assert "candidate" in combined  # expected
    assert "main" in combined  # actual


# ---------------------------------------------------------------------------
# 3. Absent track_ref — hard-defaults to the remote default branch.
# ---------------------------------------------------------------------------


def test_absent_track_ref_defaults_to_remote_default_branch(tmp_path, monkeypatch):
    dest_ok = tmp_path / "dest-ok"
    _init_git_repo(dest_ok, branch="main")
    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest_ok, track_ref=None)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    target_ok = _make_target("row-default-ok", tmp_path / "src", dest_ok)
    totals = publish.RunTotals()
    assert publish.assert_dest_on_declared_ref(target_ok, totals) is True

    dest_bad = tmp_path / "dest-bad"
    _init_git_repo(dest_bad, branch="candidate")
    registry_dir_bad = tmp_path / "registry-bad"
    _write_registry(registry_dir_bad, dest=dest_bad, track_ref=None)
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir_bad))

    target_bad = _make_target("row-default-bad", tmp_path / "src", dest_bad)
    assert publish.assert_dest_on_declared_ref(target_bad, totals) is False


# ---------------------------------------------------------------------------
# 4. `origin/<branch>` form normalizes to the local branch name and matches.
# ---------------------------------------------------------------------------


def test_origin_prefixed_track_ref_normalizes_and_matches(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="candidate")
    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest, track_ref="origin/candidate")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    target = _make_target("row-origin-form", tmp_path / "src", dest)
    totals = publish.RunTotals()

    assert publish.assert_dest_on_declared_ref(target, totals) is True


# ---------------------------------------------------------------------------
# 5. Unregistered dest — out of scope, proceeds.
# ---------------------------------------------------------------------------


def test_unregistered_dest_proceeds(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="whatever")
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir(parents=True, exist_ok=True)
    # No `publish.mirrors.*` entry at all names this dest.
    (registry_dir / "registry.toml").write_text(
        '[publish.mirrors]\n"unrelated-key.path" = "/nowhere"\n', encoding="utf-8"
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    target = _make_target("row-unregistered", tmp_path / "src", dest)
    totals = publish.RunTotals()

    assert publish.assert_dest_on_declared_ref(target, totals) is True


# ---------------------------------------------------------------------------
# 6. The gate runs before any write — no dispatch_mirror_like, no
# _ensure_dest_ready mkdir, on a mismatched row driven through the real
# `process_target` call site.
# ---------------------------------------------------------------------------


def test_gate_placement_precedes_every_write(tmp_path, monkeypatch):
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="main")
    src = tmp_path / "src"
    src.mkdir(parents=True, exist_ok=True)

    registry_dir = tmp_path / "registry"
    _write_registry(registry_dir, dest=dest, track_ref="candidate")
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    # C8's own placement (per its module comment) is BEFORE the one-shot
    # `repo-cut` bootstrap, `_ensure_dest_ready`'s mkdir, and
    # `dispatch_mirror_like` -- but AFTER `run_pre_sync_gates`, which runs
    # unconditionally ahead of it in `process_target` and does not write to
    # `target.dest_dir` itself. Stub it to a trivial passing gate rather than
    # failing the test on it, so this test pins the load-bearing placement
    # property (no write happens) without asserting a property C8 never
    # claimed.
    def _fake_run_pre_sync_gates(target, *_args, **_kwargs):
        return publish.GateResult(proceed=True, source_dir=target.source_dir)

    monkeypatch.setattr(publish, "run_pre_sync_gates", _fake_run_pre_sync_gates)

    def _fail_ensure_dest_ready(*_args, **_kwargs):
        raise AssertionError("_ensure_dest_ready must not run after C8 refuses the row")

    monkeypatch.setattr(publish, "_ensure_dest_ready", _fail_ensure_dest_ready)

    def _fail_dispatch_mirror_like(*_args, **_kwargs):
        raise AssertionError("dispatch_mirror_like must not run after C8 refuses the row")

    monkeypatch.setattr(publish, "dispatch_mirror_like", _fail_dispatch_mirror_like)

    dest_files_before = sorted(p.relative_to(dest) for p in dest.rglob("*"))

    target = _make_target("row-placement", src, dest)
    totals = publish.RunTotals()
    engine_ctx = object()

    publish.process_target(
        target,
        tmp_path,
        totals,
        identity_file_exists=False,
        identity=None,
        dry_run=False,
        engine_ctx=engine_ctx,
    )

    dest_files_after = sorted(p.relative_to(dest) for p in dest.rglob("*"))
    assert dest_files_after == dest_files_before, (
        "dest must not have been written to after the row was refused"
    )
    assert totals.processed == 0


# ---------------------------------------------------------------------------
# 7. Generic, not klabauter-specific (see `_ROW_KEY` at module scope, used by
# every test above) — a klabauter-specific implementation would either fail
# to resolve `some-other-mirror`'s row at all (falling through to
# proceed=True everywhere, including the mismatch test) or hardcode a
# klabauter branch name that would never match `candidate`/`main` here.
# This test pins that by re-running the mismatch case with a second,
# differently-named row key.
# ---------------------------------------------------------------------------


def test_check_is_generic_across_row_keys(tmp_path, monkeypatch, capsys):
    key = "yet-another-mirror"
    dest = tmp_path / "dest"
    _init_git_repo(dest, branch="release")
    registry_dir = tmp_path / "registry"
    registry_dir.mkdir(parents=True, exist_ok=True)
    (registry_dir / "registry.toml").write_text(
        "[publish.mirrors]\n"
        f'"{key}.path" = "{dest.as_posix()}"\n'
        f'"{key}.track_ref" = "staging"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("MACHINE_LOCAL_REGISTRY_DIR", str(registry_dir))

    target = _make_target("row-generic", tmp_path / "src", dest)
    totals = publish.RunTotals()

    result = publish.assert_dest_on_declared_ref(target, totals)
    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert result is False
    assert "staging" in combined
    assert "release" in combined
    assert key in combined
