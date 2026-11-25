"""A DoE plugin round pins the engine toplevel to the sha stamped on the last published
klabauter commit, never to the engine's working HEAD.

Falsifier for docs/plans/2026-10-06-plugin-round-pins-published-source-head.md. Real git under
tmp_path; the registry facts the real round reads are monkeypatched at publish.py's seams.

Run: python -m pytest coordinator/bin/tests/test_plugin_round_pins_published_engine.py -q
"""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

TRIPWIRE = "A-PUBLISH-IS-REPRODUCIBLE-ONLY-FROM-PUSHED-SOURCE"
MIRROR_KEY = "claude_klabauter"
TRACK_REF = "origin/candidate"


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_plugin_round_pin_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()
publish._bootstrap_engine()

from coordinator_core.git.git_state import format_source_sha_suffix  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_engine_toplevel_key():
    """`_engine_toplevel_key` is process-cached; each test points `_REPO_ROOT` at its own tree."""
    publish._engine_toplevel_key.cache_clear()
    yield
    publish._engine_toplevel_key.cache_clear()


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return done.stdout.strip()


def _init(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")


def _write(root: Path, rel: str, body: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body.encode("utf-8"))


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _mirror(root: Path, subjects: "list[str]") -> str:
    """Mirror repo whose `origin/candidate` remote-tracking ref points at its tip."""
    _init(root)
    tip = ""
    for i, subject in enumerate(subjects):
        _write(root, f"f{i}.txt", f"{i}\n")
        tip = _commit(root, subject)
    _git(root, "update-ref", f"refs/remotes/{TRACK_REF}", tip)
    return tip


class _World:
    """Engine E (A then B on an upstream-less branch, dirty tracked file), mirror M, content repo."""

    def __init__(self, tmp_path: Path, mirror_subjects: "list[str] | None" = None) -> None:
        self.engine = tmp_path / "engine"
        _init(self.engine)
        _write(self.engine, "coordinator/bin/tool.py", "print('A')\n")
        _write(self.engine, "coordinator/bin/other.py", "other\n")
        _write(self.engine, "coordinator/lib/x.py", "x = 'A'\n")
        self.sha_a = _commit(self.engine, "A")
        _git(self.engine, "checkout", "-q", "-b", "feature-no-upstream")
        _write(self.engine, "coordinator/bin/tool.py", "print('B')\n")
        self.sha_b = _commit(self.engine, "B")
        _write(self.engine, "coordinator/bin/other.py", "dirty\n")

        self.mirror = tmp_path / "mirror"
        subjects = mirror_subjects
        if subjects is None:
            subjects = ["older round", "round" + format_source_sha_suffix(self.sha_a)]
        self.mirror_tip = _mirror(self.mirror, subjects)

        self.content = tmp_path / "content"
        _init(self.content)
        _write(self.content, "hooks/h.py", "h\n")
        _commit(self.content, "content")
        self.plugin_dest = tmp_path / "plugin-dest"
        self.plugin_dest.mkdir()
        self.engine_coord = self.engine / "coordinator"

    def plugin_row(self, allowlist: str = "bin,lib"):
        return publish.ResolvedTarget(
            name="coordinator-claude",
            mode="mirror",
            source_dir=self.content,
            dest_dir=self.plugin_dest,
            allowlist=allowlist,
            source_map=f"{self.engine_coord}=bin,lib",
        )

    def klabauter_row(self):
        return publish.ResolvedTarget(
            name="claude-klabauter",
            mode="mirror",
            source_dir=self.engine_coord,
            dest_dir=self.mirror,
            allowlist="bin,lib",
        )


def _patch_registry(monkeypatch, world: _World, *, keys=frozenset({MIRROR_KEY}), registered=True):
    from coordinator_core import machine_resolver

    facts = {
        f"publish.mirrors.{MIRROR_KEY}.path": str(world.mirror) if registered else None,
        f"publish.mirrors.{MIRROR_KEY}.track_ref": TRACK_REF if registered else None,
    }

    def fake_registry_get(key, *args, **kwargs):
        return facts.get(key)

    monkeypatch.setattr(publish, "_REPO_ROOT", world.engine)
    monkeypatch.setattr(publish, "_engine_declaring_mirror_keys", lambda *a, **k: frozenset(keys))
    monkeypatch.setattr(machine_resolver, "registry_get", fake_registry_get)
    mirror_real = os.path.realpath(str(world.mirror))
    monkeypatch.setattr(
        publish,
        "_publish_mirror_key_for_repo_root",
        lambda root: MIRROR_KEY if os.path.realpath(str(root)) == mirror_real else None,
    )


def _seed(world: _World, rows):
    pins: "dict[str, str]" = {}
    out = io.StringIO()
    pin = publish._seed_published_engine_pin(rows, pins, out=out)
    return pin, pins, out.getvalue()


def _gate(world: _World, rows, pin):
    root = world.plugin_dest
    err, out = io.StringIO(), io.StringIO()
    ok = publish.dispatch_end_of_run_plugin_provenance_gate(
        [root],
        rows_by_repo_root={root: rows},
        err=err,
        out=out,
        **({"published_engine_pin": pin} if pin is not None else {}),
    )
    return ok, out.getvalue(), err.getvalue()


def test_baseline_shape_refuses_no_upstream_but_not_dirt(tmp_path, monkeypatch):
    """Without the pin the gate still refuses an upstream-less engine branch (the 2026-10-03
    shape); the dirty tracked file is an advisory, never a refusal reason."""
    world = _World(tmp_path)
    ok, out, err = _gate(world, [world.plugin_row()], None)
    assert not ok
    assert TRIPWIRE in err and "no upstream" in err and "dirty tracked" not in err
    assert "coordinator/bin/other.py" in out and "Advisory" in out


def _pushed_content(tmp_path: Path) -> "tuple[Path, str]":
    """Content repo whose HEAD is pushed to an upstream; returns (work tree, pushed sha)."""
    bare = tmp_path / "content-remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(bare)],
        check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    work = tmp_path / "pushed-content"
    _init(work)
    _write(work, "hooks/h.py", "pushed\n")
    _write(work, "hooks/k.py", "k\n")
    sha = _commit(work, "pushed")
    _git(work, "remote", "add", "origin", str(bare))
    _git(work, "push", "-q", "-u", "origin", "main")
    return work, sha


def test_dirty_tree_with_pushed_head_passes_with_one_advisory_and_ships_pushed_bytes(tmp_path, monkeypatch):
    work, sha = _pushed_content(tmp_path)
    _write(work, "hooks/h.py", "dirty edit\n")
    _write(work, "hooks/k.py", "dirty k\n")
    row = publish.ResolvedTarget(
        name="coordinator-claude", mode="mirror", source_dir=work,
        dest_dir=tmp_path / "plugin-dest", allowlist="hooks",
    )
    root = tmp_path / "gate-root"
    err, out = io.StringIO(), io.StringIO()
    ok = publish.dispatch_end_of_run_plugin_provenance_gate(
        [root], rows_by_repo_root={root: [row]}, err=err, out=out,
    )
    assert ok, err.getvalue()
    assert err.getvalue() == ""
    advisories = [ln for ln in out.getvalue().splitlines() if "Advisory" in ln]
    assert len(advisories) == 1
    assert "hooks/h.py" in advisories[0] and "hooks/k.py" in advisories[0]

    monkeypatch.setattr(publish, "_required_pathspec_for_toplevel", lambda *_a, **_k: ("hooks",))
    shadow = publish._git_materialize_ref(work, ref=sha)
    assert (shadow / "hooks" / "h.py").read_bytes() == b"pushed\n"
    assert (work / "hooks" / "h.py").read_bytes() == b"dirty edit\n"


def test_round_read_path_ships_pushed_bytes_from_a_dirty_root(tmp_path, monkeypatch):
    """The round's own shadow materialisation (run_pre_sync_gates) hands back the pushed bytes."""
    work, _sha = _pushed_content(tmp_path)
    _write(work, "hooks/h.py", "dirty edit\n")
    dest = tmp_path / "plugin-dest"
    dest.mkdir()
    row = publish.ResolvedTarget(
        name="coordinator-claude", mode="mirror", source_dir=work, dest_dir=dest, allowlist="hooks",
    )
    monkeypatch.setattr(publish, "check_live_install_clobber", lambda *a, **k: True)
    monkeypatch.setattr(publish, "check_marketplace_version_regression", lambda *a, **k: True)
    monkeypatch.setattr(publish, "check_version_consistency", lambda *a, **k: True)
    monkeypatch.setattr(publish, "warn_machine_slug_net", lambda *a, **k: None)
    monkeypatch.setattr(publish, "_required_pathspec_for_toplevel", lambda *_a, **_k: ("hooks",))
    out, err = io.StringIO(), io.StringIO()
    result = publish.run_pre_sync_gates(
        row, setup_dir=tmp_path, identity_file_exists=False, identity=None,
        totals=publish.RunTotals(), dry_run=True, out=out, err=err,
    )
    assert result.proceed, err.getvalue()
    assert result.source_dir != work
    shadow_h = next(result.source_dir.rglob("h.py"))
    assert shadow_h.read_bytes() == b"pushed\n"
    assert (work / "hooks" / "h.py").read_bytes() == b"dirty edit\n"


def test_whole_round_on_a_dirty_tree_gates_with_advisory_then_ships_pushed_bytes(tmp_path, monkeypatch):
    """One dirty root through the round's gate and its pre-sync read: advisory, no refusal, pushed bytes."""
    work, _sha = _pushed_content(tmp_path)
    _write(work, "hooks/h.py", "dirty edit\n")
    dest = tmp_path / "plugin-dest"
    dest.mkdir()
    row = publish.ResolvedTarget(
        name="coordinator-claude", mode="mirror", source_dir=work, dest_dir=dest, allowlist="hooks",
    )
    root = tmp_path / "gate-root"
    gate_err, gate_out = io.StringIO(), io.StringIO()
    assert publish.dispatch_end_of_run_plugin_provenance_gate(
        [root], rows_by_repo_root={root: [row]}, err=gate_err, out=gate_out,
    ), gate_err.getvalue()
    assert gate_err.getvalue() == ""
    assert len([ln for ln in gate_out.getvalue().splitlines() if "Advisory" in ln]) == 1

    monkeypatch.setattr(publish, "check_live_install_clobber", lambda *a, **k: True)
    monkeypatch.setattr(publish, "check_marketplace_version_regression", lambda *a, **k: True)
    monkeypatch.setattr(publish, "check_version_consistency", lambda *a, **k: True)
    monkeypatch.setattr(publish, "warn_machine_slug_net", lambda *a, **k: None)
    monkeypatch.setattr(publish, "_required_pathspec_for_toplevel", lambda *_a, **_k: ("hooks",))
    out, err = io.StringIO(), io.StringIO()
    result = publish.run_pre_sync_gates(
        row, setup_dir=tmp_path, identity_file_exists=False, identity=None,
        totals=publish.RunTotals(), dry_run=True, out=out, err=err,
    )
    assert result.proceed, err.getvalue()
    assert next(result.source_dir.rglob("h.py")).read_bytes() == b"pushed\n"


def test_advisory_line_is_capped_for_a_large_dirty_tree(tmp_path):
    work, _sha = _pushed_content(tmp_path)
    for i in range(publish._DIRTY_ADVISORY_MAX_PATHS + 5):
        _write(work, f"hooks/many{i}.py", "x\n")
    _commit(work, "many")
    _git(work, "push", "-q")
    for i in range(publish._DIRTY_ADVISORY_MAX_PATHS + 5):
        _write(work, f"hooks/many{i}.py", "dirty\n")
    row = publish.ResolvedTarget(
        name="coordinator-claude", mode="mirror", source_dir=work,
        dest_dir=tmp_path / "plugin-dest", allowlist="hooks",
    )
    root = tmp_path / "gate-root"
    err, out = io.StringIO(), io.StringIO()
    ok = publish.dispatch_end_of_run_plugin_provenance_gate(
        [root], rows_by_repo_root={root: [row]}, err=err, out=out,
    )
    assert ok, err.getvalue()
    advisories = [ln for ln in out.getvalue().splitlines() if "Advisory" in ln]
    assert len(advisories) == 1
    assert f"{publish._DIRTY_ADVISORY_MAX_PATHS + 5} dirty" in advisories[0]
    assert "and 5 more" in advisories[0]
    assert advisories[0].count("many") == publish._DIRTY_ADVISORY_MAX_PATHS


def test_unpushed_head_still_refuses_even_when_clean(tmp_path):
    work, _sha = _pushed_content(tmp_path)
    _write(work, "hooks/h.py", "second\n")
    _commit(work, "unpushed")
    row = publish.ResolvedTarget(
        name="coordinator-claude", mode="mirror", source_dir=work,
        dest_dir=tmp_path / "plugin-dest", allowlist="hooks",
    )
    root = tmp_path / "gate-root"
    err = io.StringIO()
    ok = publish.dispatch_end_of_run_plugin_provenance_gate(
        [root], rows_by_repo_root={root: [row]}, err=err, out=io.StringIO(),
    )
    assert not ok and TRIPWIRE in err.getvalue() and "1 commit(s) ahead" in err.getvalue()


def test_seed_pins_engine_toplevel_to_stamped_sha_not_head(tmp_path, monkeypatch):
    world = _World(tmp_path)
    _patch_registry(monkeypatch, world)
    pin, pins, _printed = _seed(world, [world.plugin_row()])
    assert pin is not None and pin.source_sha == world.sha_a
    out = io.StringIO()
    resolved = publish._round_pin_source_sha(world.engine_coord, pins, out=out)
    assert resolved == world.sha_a != world.sha_b
    assert "Round source pinned:" not in out.getvalue(), "HEAD was resolved instead of the seeded pin"


def test_materialized_bytes_are_the_stamped_commits(tmp_path, monkeypatch):
    world = _World(tmp_path)
    _patch_registry(monkeypatch, world)
    _pin, pins, _printed = _seed(world, [world.plugin_row()])
    monkeypatch.setattr(
        publish,
        "_required_pathspec_for_toplevel",
        lambda *_a, **_k: ("coordinator/bin", "coordinator/lib"),
    )
    sha = publish._round_pin_source_sha(world.engine_coord, pins, out=io.StringIO())
    shadow = publish._git_materialize_ref(world.engine_coord, ref=sha)
    expected = subprocess.run(
        ["git", "-C", str(world.engine), "show", f"{world.sha_a}:coordinator/bin/tool.py"],
        capture_output=True,
        check=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout
    assert (shadow / "bin" / "tool.py").read_bytes() == expected
    assert b"'B'" not in expected


def test_provenance_gate_passes_and_names_both_shas(tmp_path, monkeypatch):
    world = _World(tmp_path)
    _patch_registry(monkeypatch, world)
    pin, _pins, _printed = _seed(world, [world.plugin_row()])
    ok, out, err = _gate(world, [world.plugin_row()], pin)
    assert ok, err
    assert world.sha_a[:12] in out and world.mirror_tip[:12] in out
    assert "no upstream" not in out + err and "dirty" not in err


def test_commit_subject_stamp_names_the_pinned_sha(tmp_path, monkeypatch):
    world = _World(tmp_path)
    _patch_registry(monkeypatch, world)
    _pin, pins, _printed = _seed(world, [world.plugin_row()])
    assert publish._source_sha_suffix(pins) == format_source_sha_suffix(world.sha_a)


def test_klabauter_round_still_pins_working_head(tmp_path, monkeypatch):
    world = _World(tmp_path)
    _patch_registry(monkeypatch, world)
    pin, pins, _printed = _seed(world, [world.plugin_row(), world.klabauter_row()])
    assert pin is None
    resolved = publish._round_pin_source_sha(world.engine_coord, pins, out=io.StringIO())
    assert resolved == world.sha_b


def test_no_engine_mirror_registered_keeps_head_pin_and_gate(tmp_path, monkeypatch):
    world = _World(tmp_path)
    _patch_registry(monkeypatch, world, keys=frozenset(), registered=False)
    pin, pins, _printed = _seed(world, [world.plugin_row()])
    assert pin is None
    resolved = publish._round_pin_source_sha(world.engine_coord, pins, out=io.StringIO())
    assert resolved == world.sha_b
    ok, _out, err = _gate(world, [world.plugin_row()], None)
    assert not ok and TRIPWIRE in err


def test_unstamped_mirror_refuses_and_leaves_no_engine_pin(tmp_path, monkeypatch):
    world = _World(tmp_path, mirror_subjects=["unstamped one", "unstamped two"])
    _patch_registry(monkeypatch, world)
    from percolate.published_engine_pin import PublishedEnginePinError

    pins: "dict[str, str]" = {}
    with pytest.raises(PublishedEnginePinError):
        publish._seed_published_engine_pin([world.plugin_row()], pins, out=io.StringIO())
    engine_key = str(Path(_git(world.engine, "rev-parse", "--show-toplevel")))
    assert engine_key not in pins


def _engine_pin(world: _World, monkeypatch):
    _patch_registry(monkeypatch, world)
    pin, _pins, _printed = _seed(world, [world.plugin_row()])
    assert pin is not None
    return pin


def _push_engine_upstream(world: _World, tmp_path: Path) -> None:
    bare = tmp_path / "engine-remote.git"
    subprocess.run(
        ["git", "init", "-q", "--bare", "-b", "main", str(bare)],
        check=True,
        capture_output=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    _git(world.engine, "remote", "add", "origin", str(bare))
    _git(world.engine, "push", "-q", "-u", "origin", "feature-no-upstream")


def test_gate_passes_engine_root_that_is_ahead(tmp_path, monkeypatch):
    world = _World(tmp_path)
    _git(world.engine, "checkout", "-q", "--", "coordinator/bin/other.py")
    _push_engine_upstream(world, tmp_path)
    _write(world.engine, "coordinator/lib/x.py", "x = 'C'\n")
    _commit(world.engine, "C")
    pin = _engine_pin(world, monkeypatch)
    assert "1 commit(s) ahead" in _gate(world, [world.plugin_row()], None)[2]
    ok, _out, err = _gate(world, [world.plugin_row()], pin)
    assert ok, err


def test_gate_passes_engine_root_with_no_upstream(tmp_path, monkeypatch):
    world = _World(tmp_path)
    _git(world.engine, "checkout", "-q", "--", "coordinator/bin/other.py")
    pin = _engine_pin(world, monkeypatch)
    assert "no upstream" in _gate(world, [world.plugin_row()], None)[2]
    ok, _out, err = _gate(world, [world.plugin_row()], pin)
    assert ok, err


def test_gate_passes_engine_root_dirty_under_allowlist(tmp_path, monkeypatch):
    world = _World(tmp_path)
    _push_engine_upstream(world, tmp_path)
    pin = _engine_pin(world, monkeypatch)
    before = _gate(world, [world.plugin_row()], None)
    assert before[0], before[2]
    assert "dirty tracked" not in before[2]
    assert "Advisory" in before[1] and "other.py" in before[1]
    ok, _out, err = _gate(world, [world.plugin_row()], pin)
    assert ok, err


def test_second_root_outside_the_pin_is_still_refused(tmp_path, monkeypatch):
    world = _World(tmp_path)
    pin = _engine_pin(world, monkeypatch)
    row = world.plugin_row(allowlist="bin,lib,hooks")
    ok, _out, err = _gate(world, [row], pin)
    assert not ok
    assert TRIPWIRE in err and str(world.content) in err and "no upstream" in err
    assert str(world.engine_coord) not in err


def _as_commit_clone(monkeypatch, world: _World, tmp_path: Path, *, keys):
    """`_REPO_ROOT` is a commit clone with no setup/ (4.6.25 round 3); the live source repo has it."""
    clone = tmp_path / "commit-clone"
    (clone / "coordinator").mkdir(parents=True)
    (world.engine / "setup").mkdir(exist_ok=True)
    seen: "list[Path]" = []

    def fake_keys(setup_dir, *a, **k):
        seen.append(Path(setup_dir))
        return frozenset(keys) if Path(setup_dir) == world.engine / "setup" else frozenset()

    monkeypatch.setattr(publish, "_REPO_ROOT", clone)
    monkeypatch.setattr(publish, "_source_repo", lambda: world.engine)
    monkeypatch.setattr(publish, "_engine_declaring_mirror_keys", fake_keys)
    return seen


def test_commit_clone_without_setup_reads_topology_from_source_and_pins(tmp_path, monkeypatch):
    world = _World(tmp_path)
    _patch_registry(monkeypatch, world)
    seen = _as_commit_clone(monkeypatch, world, tmp_path, keys={MIRROR_KEY})
    pin, _pins, printed = _seed(world, [world.plugin_row()])
    assert seen == [world.engine / "setup"]
    assert pin is not None and pin.source_sha == world.sha_a, printed
    ok, _out, err = _gate(world, [world.plugin_row()], pin)
    assert ok, err


def test_commit_clone_with_no_declared_engine_mirror_refuses_rather_than_pin_head(tmp_path, monkeypatch):
    world = _World(tmp_path)
    _patch_registry(monkeypatch, world)
    _as_commit_clone(monkeypatch, world, tmp_path, keys=set())
    from percolate.published_engine_pin import PublishedEnginePinError

    with pytest.raises(PublishedEnginePinError, match="commit clone"):
        _seed(world, [world.plugin_row()])
