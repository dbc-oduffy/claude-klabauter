"""A percolate round over a source range holding a run-open checkpoint and a later closed fix.

Real git source repo (bare origin, work/* branch) and a real mirror repo, all under tmp_path.
`_cmd_round` runs at default arguments with the real source preflight, the real
`_cmd_round_default` and the real `commit_paths`; the publish.py child is stood in for by a
fake that parses the round's argv with publish.py's own parser and materializes the shipped
tree through publish.py's real `_git_materialize_ref` with the handed `--withheld-paths-json`.
Stubbed, as the other round tests stub them: the sibling scan/drift/parse steps, the engine
import gate, the lock, and the push.

Run: python -m pytest coordinator/bin/tests/test_percolate_round_withholds_open_checkpoint.py -q
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

REAL_SOURCE_PREFLIGHT = True

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_BRANCH = "work/withheld-e2e"
_PLAN = "docs/plans/p.md"
_RUN_BASE = "a" * 40
_Y_BASE = "".join(f"y{i} = 'base'\n" for i in range(8))


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _BIN_DIR / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_round = _load("percolate_round_withholds_e2e", "percolate-round.py")
_publish = _load("publish_withholds_e2e", "publish.py")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, check=True, text=True, creationflags=_NO_WINDOW
    ).stdout.strip()


def _init_repo(root: Path) -> None:
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@claude-klabauter.test")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitattributes").write_bytes(b"* -text\n")


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _checkpoint(root: Path) -> str:
    """Checkpoint A: adds tests/x.py, modifies y.py (line 0), and touches its plan."""
    _write(root, "tests/x.py", "X = 'checkpoint'\n")
    _write(root, "y.py", _Y_BASE.replace("y0 = 'base'", "y0 = 'checkpoint'"))
    _write(root, _PLAN, "plan\n")
    return _commit(
        root,
        f"checkpoint(wave 0): 1 rows — C0\n\nCheckpoint-Plan: {_PLAN}\nCheckpoint-Base: {_RUN_BASE}\n"
        "Checkpoint-Not-Landed: run-open\n",
    )


class _World:
    def __init__(self, tmp_path: Path):
        self.root = tmp_path
        self.origin = tmp_path / "origin.git"
        self.src = tmp_path / "source"
        self.mirror = tmp_path / "mirror"
        self.percolate_root = tmp_path / "percolate-root"
        (self.percolate_root / "setup").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True, creationflags=_NO_WINDOW)
        _init_repo(self.src)
        _write(self.src, "y.py", _Y_BASE)
        _write(self.src, "z.py", "Z = 'base'\n")
        self.base = _commit(self.src, "seed")
        _git(self.src, "branch", "-m", _BRANCH)
        _git(self.src, "remote", "add", "origin", str(self.origin))
        _git(self.src, "push", "-q", "-u", "origin", _BRANCH)
        _init_repo(self.mirror)
        _write(self.mirror, "y.py", _Y_BASE)
        _write(self.mirror, "z.py", "Z = 'base'\n")
        _commit(self.mirror, f"percolate publish: seed [source-head {self.base[:12]}]")

    def mirror_file(self, rel: str) -> "bytes | None":
        path = self.mirror / rel
        return path.read_bytes() if path.is_file() else None

    def mirror_head_message(self) -> str:
        return _git(self.mirror, "log", "-1", "--format=%B")


@pytest.fixture
def world(tmp_path):
    return _World(tmp_path)


def _manifest_module():
    from coordinator_core.percolate import manifest as manifest_mod
    from coordinator_core.percolate import round as round_mod

    return manifest_mod, round_mod


def _install_round_stubs(monkeypatch, world: _World) -> None:
    """Everything the other round tests stub, plus the publish.py child (real parser, real
    `_git_materialize_ref`, real manifest write)."""
    monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(world.root / "settings-home"))
    monkeypatch.setattr(_round, "_fold_bar_refusal", lambda *a, **k: None)
    monkeypatch.setattr(_round, "_resolve_percolate_root", lambda override: str(world.percolate_root))
    monkeypatch.setattr(_round, "_branch0_gate", lambda target, root: str(world.src))
    monkeypatch.setattr(_round, "_resolve_dest", lambda target, root: str(world.mirror))
    monkeypatch.setattr(_round, "_field7_preflight", lambda *a, **k: None)
    monkeypatch.setattr(_round, "_resolve_central_state", lambda: None)
    monkeypatch.setattr(_round, "_wiki_seed_mismatch", lambda *a, **k: None)
    monkeypatch.setattr(_round, "_round_changelog_lag_line", lambda *a, **k: "")
    monkeypatch.setattr(_round, "_engine_import_failures", lambda *a, **k: None)
    monkeypatch.setattr(_round, "_round_held_lock", lambda *a, **k: contextlib.nullcontext(), raising=False)
    pushes: list = []
    monkeypatch.setattr(
        _round, "_push_dest", lambda dest: pushes.append(dest) or subprocess.CompletedProcess([], 0, "", "")
    )
    world.pushes = pushes

    def _fake_step(script, argv):
        if str(script) == str(_round._PARSE_DRYRUN):
            out = json.dumps({"gates": {"step3_gate_fires": False}, "judgment_points": []})
        elif "scan-secrets" in argv:
            out = json.dumps({
                "schema": "scan-secrets.v1",
                "counts": {"high": 0, "medium_informational": 0, "medium_gating": 0, "low": 0},
                "render": "Content-leakage scan:\n  (none)\n",
            })
        else:
            out = "anchor_mode: 30day-fallback\n"
        return subprocess.CompletedProcess([], 0, out + "\n", "")

    monkeypatch.setattr(_round, "_run_step", _fake_step)

    real_run = _round._run
    parser = _publish.build_arg_parser()

    def _fake_run(cmd, *, timeout, **kwargs):
        if str(_round._PUBLISH) not in cmd:
            return real_run(cmd, timeout=timeout, **kwargs)
        world.publish_argv = list(cmd)
        args = parser.parse_args(cmd[cmd.index(str(_round._PUBLISH)) + 1:])
        withheld = None
        if args.withheld_paths_json:
            withheld = dict(json.loads(Path(args.withheld_paths_json).read_text(encoding="utf-8"))["paths"])
        head = _git(world.src, "rev-parse", "HEAD")
        _publish._MATERIALIZED_REF_CACHE.clear()
        shadow = _publish._git_materialize_ref(world.src, ref=head, withheld=withheld or None)
        written = []
        for path in sorted(shadow.rglob("*")):
            rel = path.relative_to(shadow).as_posix()
            if not path.is_file() or rel == ".gitattributes" or rel.startswith(".git/"):
                continue
            if world.mirror_file(rel) != path.read_bytes():
                _write(world.mirror, rel, path.read_text(encoding="utf-8"))
                written.append(rel)
        _publish._cleanup_shadow_roots((shadow,))
        if written:
            manifest_mod, round_mod = _manifest_module()
            manifest_mod.write_manifest(
                manifest_mod.RoundManifest(
                    round_id="e2e",
                    added_or_updated=frozenset(written),
                    declared_payload=frozenset(written),
                    source_sha=head,
                ),
                round_mod.default_manifest_path(world.mirror, "e2e"),
            )
        return subprocess.CompletedProcess(cmd, 0, "publish: done\n", "")

    monkeypatch.setattr(_round, "_run", _fake_run)


def _run_round(monkeypatch, world: _World) -> int:
    _install_round_stubs(monkeypatch, world)
    args = _round._build_parser().parse_args(["klabauter", "--percolate-root", str(world.percolate_root)])
    assert args.source_ref is None and not args.allow_source_rollback
    return _round._cmd_round(args)


def _fix(world: _World, *rels: str) -> str:
    for rel in rels:
        _write(world.src, rel, f"{rel} = 'fix'\n" if rel != "y.py" else _Y_BASE.replace("y7 = 'base'", "y7 = 'fix'"))
    return _commit(world.src, "fix: closed commit")


def test_open_checkpoint_then_closed_fix_ships_the_fix_without_the_checkpoint_paths(monkeypatch, world, capsys):
    _checkpoint(world.src)
    _fix(world, "z.py")

    rc = _run_round(monkeypatch, world)

    out = capsys.readouterr()
    assert rc == 0, out.err
    assert world.mirror_file("z.py") == b"z.py = 'fix'\n"
    assert world.mirror_file("tests/x.py") is None
    assert world.mirror_file("y.py") == _Y_BASE.encode("utf-8")
    assert "--withheld-paths-json" in world.publish_argv
    assert f"Source-Withheld-Base: {world.base[:12]}" in world.mirror_head_message()
    assert "withholding" in out.out and "tests/x.py" in out.out
    assert world.pushes == [str(world.mirror)]


def test_closed_commit_touching_a_withheld_path_refuses_naming_it(monkeypatch, world, capsys):
    _checkpoint(world.src)
    _fix(world, "z.py", "y.py")
    before = _git(world.mirror, "rev-parse", "HEAD")

    rc = _run_round(monkeypatch, world)

    err = capsys.readouterr().err
    assert rc == _round._EXIT_FAIL
    assert "percolate-round: refused:" in err and "y.py" in err
    assert _git(world.mirror, "rev-parse", "HEAD") == before
    assert world.mirror_file("z.py") == b"Z = 'base'\n"
    assert world.pushes == []


def test_closing_the_run_ships_the_withheld_paths_on_the_next_round(monkeypatch, world, capsys):
    _checkpoint(world.src)
    _fix(world, "z.py")
    assert _run_round(monkeypatch, world) == 0
    assert world.mirror_file("tests/x.py") is None

    _write(world.src, "closed.txt", "closed\n")
    _commit(world.src, f"land\n\nCheckpoint-Run-Closed: {_RUN_BASE}\n")
    capsys.readouterr()
    rc = _run_round(monkeypatch, world)

    out = capsys.readouterr()
    assert rc == 0, out.err
    assert world.mirror_file("tests/x.py") == b"X = 'checkpoint'\n"
    assert world.mirror_file("y.py") == _Y_BASE.replace("y0 = 'base'", "y0 = 'checkpoint'").encode("utf-8")
    assert "--withheld-paths-json" not in world.publish_argv
    assert "Source-Withheld-Base" not in world.mirror_head_message()


def test_second_round_with_the_checkpoint_still_open_withholds_the_same_paths(monkeypatch, world):
    _checkpoint(world.src)
    _fix(world, "z.py")
    assert _run_round(monkeypatch, world) == 0

    _write(world.src, "extra.py", "E = 1\n")
    _commit(world.src, "fix: another closed commit")
    assert _run_round(monkeypatch, world) == 0

    assert world.mirror_file("extra.py") == b"E = 1\n"
    assert world.mirror_file("tests/x.py") is None
    assert world.mirror_file("y.py") == _Y_BASE.encode("utf-8")
    assert f"Source-Withheld-Base: {world.base[:12]}" in world.mirror_head_message()


def test_sibling_branch_commit_merged_after_the_checkpoint_refuses(monkeypatch, world, capsys):
    _git(world.src, "checkout", "-q", "-b", "side")
    _write(world.src, "y.py", _Y_BASE.replace("y7 = 'base'", "y7 = 'side'"))
    _commit(world.src, "side: touches y.py")
    _git(world.src, "checkout", "-q", _BRANCH)
    _checkpoint(world.src)
    _git(world.src, "merge", "-q", "--no-ff", "-m", "merge side", "side")
    before = _git(world.mirror, "rev-parse", "HEAD")

    rc = _run_round(monkeypatch, world)

    err = capsys.readouterr().err
    assert rc == _round._EXIT_FAIL
    assert "percolate-round: refused:" in err and "y.py" in err
    assert _git(world.mirror, "rev-parse", "HEAD") == before
