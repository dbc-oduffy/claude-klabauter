"""Selection rules of publish._ignore_reaped_dest_rels on small tmp_path git repos.

Run: python -m pytest coordinator/bin/tests/test_publish_ignore_reap_selection.py -q
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_IGNORE = ".percolate-ignore"


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_ignore_reap_selection_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()
publish._bootstrap_engine()  # binds the lazily imported mirror_like_wire_names/descriptor_for

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
        creationflags=_NO_WINDOW,
    )
    return done.stdout.strip()


def _repo(root: Path, files: "dict[str, str]") -> str:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8", newline="\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "seed")
    return _git(root, "rev-parse", "HEAD")


class World:
    """A source repo with the row at `src/` and a dest mirror repo whose root is the row's dest."""

    def __init__(self, tmp_path: Path, source_files: "dict[str, str]", dest_files: "dict[str, str]", mode: str = "mirror"):
        self.source = tmp_path / "source"
        self.dest = tmp_path / "dest"
        self.head = _repo(self.source, source_files)
        _repo(self.dest, dest_files)
        self.target = SimpleNamespace(
            name="row",
            mode=mode,
            source_dir=self.source / "src",
            dest_dir=self.dest,
        )

    def warm_plan(self, changed: "list[str]", deleted: "list[str] | None" = None) -> SimpleNamespace:
        return SimpleNamespace(
            cold=None,
            warm=SimpleNamespace(changed={"row": changed}, deleted={"row": deleted or []}),
            toplevel=self.source,
            head=self.head,
            prefixes={"row": "src"},
        )

    def cold_plan(self) -> SimpleNamespace:
        return SimpleNamespace(cold=object(), warm=None, toplevel=None, head=self.head, prefixes={"row": "src"})

    def reap(self, plan: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, head_paths=None) -> frozenset:
        monkeypatch.setattr(publish, "_REPO_ROOT", self.source)
        return publish._ignore_reaped_dest_rels(self.target, self.dest, plan, head_paths)


_STRAY = "tests/stray_case.dat"
_SRC_IGNORE = f"{_STRAY}\n*.log\n"


def _basic(tmp_path: Path) -> World:
    return World(
        tmp_path,
        {
            "src/kept.py": "k\n",
            f"src/{_STRAY}": "s\n",
            f"src/{_IGNORE}": _SRC_IGNORE,
        },
        {
            "kept.py": "k\n",
            _STRAY: "s\n",
            "owned.log": "o\n",
        },
    )


def test_warm_round_reaps_only_when_the_ignore_file_is_in_the_changed_set(tmp_path, monkeypatch):
    world = _basic(tmp_path)

    assert world.reap(world.warm_plan(["src/kept.py"]), monkeypatch) == frozenset()
    assert world.reap(world.warm_plan([f"src/{_IGNORE}"]), monkeypatch) == frozenset({_STRAY})
    assert world.reap(world.warm_plan([], [f"src/{_IGNORE}"]), monkeypatch) == frozenset({_STRAY})


def test_warm_round_refuses_when_the_worktree_ignore_differs_from_the_source_commit(tmp_path, monkeypatch):
    world = _basic(tmp_path)
    (world.source / "src" / _IGNORE).write_text("*.log\n", encoding="utf-8", newline="\n")

    assert world.reap(world.warm_plan([f"src/{_IGNORE}"]), monkeypatch) == frozenset()


def test_cold_round_reaps_from_the_dest_head_paths(tmp_path, monkeypatch):
    world = _basic(tmp_path)

    reaped = world.reap(world.cold_plan(), monkeypatch, head_paths={"kept.py": "x", _STRAY: "y", "owned.log": "z"})

    assert reaped == frozenset({_STRAY})


def test_an_ignore_matched_dest_file_the_source_does_not_track_is_kept(tmp_path, monkeypatch):
    world = _basic(tmp_path)

    reaped = world.reap(world.warm_plan([f"src/{_IGNORE}"]), monkeypatch)

    assert "owned.log" not in reaped
    assert reaped == frozenset({_STRAY})


def test_top_level_dotfiles_and_any_percolate_ignore_are_never_reaped(tmp_path, monkeypatch):
    ignore = f"{_STRAY}\n.hidden\n{_IGNORE}\n"
    world = World(
        tmp_path,
        {
            "src/kept.py": "k\n",
            f"src/{_STRAY}": "s\n",
            "src/.hidden": "h\n",
            f"src/tests/{_IGNORE}": "nested\n",
            f"src/{_IGNORE}": ignore,
        },
        {
            "kept.py": "k\n",
            _STRAY: "s\n",
            ".hidden": "h\n",
            f"tests/{_IGNORE}": "nested\n",
            _IGNORE: "dest root\n",
        },
    )

    reaped = world.reap(world.warm_plan([f"src/{_IGNORE}"]), monkeypatch)

    assert reaped == frozenset({_STRAY})


def test_a_source_tracked_ignored_file_the_dest_head_does_not_track_is_not_returned(tmp_path, monkeypatch):
    world = World(
        tmp_path,
        {"src/kept.py": "k\n", f"src/{_STRAY}": "s\n", f"src/{_IGNORE}": _SRC_IGNORE},
        {"kept.py": "k\n"},
    )

    assert world.reap(world.warm_plan([f"src/{_IGNORE}"]), monkeypatch) == frozenset()
    assert world.reap(world.cold_plan(), monkeypatch, head_paths={"kept.py": "x"}) == frozenset()


def test_a_source_dir_outside_the_source_toplevel_returns_empty(tmp_path, monkeypatch):
    world = _basic(tmp_path)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / _IGNORE).write_text(_SRC_IGNORE, encoding="utf-8", newline="\n")
    world.target.source_dir = outside

    assert world.reap(world.warm_plan([f"src/{_IGNORE}"]), monkeypatch) == frozenset()
    assert world.reap(world.cold_plan(), monkeypatch, head_paths={_STRAY: "y"}) == frozenset()


def test_a_non_mirror_like_row_returns_empty(tmp_path, monkeypatch):
    world = _basic(tmp_path)
    world.target.mode = "manifest"

    assert world.reap(world.warm_plan([f"src/{_IGNORE}"]), monkeypatch) == frozenset()


def test_the_derivation_spawns_no_subprocess(tmp_path, monkeypatch):
    world = _basic(tmp_path)
    plan = world.warm_plan([f"src/{_IGNORE}"])

    def _no_spawn(*args, **kwargs):
        raise AssertionError("_ignore_reaped_dest_rels spawned a subprocess")

    monkeypatch.setattr(subprocess, "Popen", _no_spawn)
    monkeypatch.setattr(subprocess, "run", _no_spawn)

    assert world.reap(plan, monkeypatch) == frozenset({_STRAY})
