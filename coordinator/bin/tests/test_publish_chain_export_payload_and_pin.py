"""A percolate round run from a claude-klabauter-publish-chain export grades the payload and the provenance
pin against the export's SOURCE repo, not the export directory.

The export is a directory whose `.git` FILE reads `gitdir: <repo>/.git`
(`publish.py :: _chain_export_source_repo`); it carries no `pyproject.toml` and its toplevel is
the export itself. Real git under tmp_path; registry facts are monkeypatched at publish.py's seams.

Run: python -m pytest coordinator/bin/tests/test_publish_chain_export_payload_and_pin.py -q
"""

from __future__ import annotations

import importlib.util
import io
import os
import shutil
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
_PYPROJECT = '[project]\nname = "engine"\nversion = "0"\ndependencies = ["PyYAML>=6"]\n'
_PLAN_OK = "import json\nprint(json.dumps({'schema': 'x'}))\n"
_HOOK = (
    "import sys\nfrom pathlib import Path\n"
    "sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'lib'))\nimport yaml\n"
)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_chain_export_payload_and_pin_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()
publish._bootstrap_engine()

from coordinator_core.git.git_state import format_source_sha_suffix  # noqa: E402


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


def _make_export(tmp_path: Path, source: Path) -> Path:
    """A chain export of `source`: `.git` file naming `<source>/.git`, an engine tree, no pyproject."""
    export = tmp_path / "claude-klabauter-publish-chain" / "78eda8a5"
    (export / "coordinator").mkdir(parents=True)
    (export / ".git").write_text(f"gitdir: {source / '.git'}\n", encoding="utf-8")
    return export


def test_export_layout_maps_back_to_its_source_repo(tmp_path):
    source = tmp_path / "source"
    _init(source)
    export = _make_export(tmp_path, source)
    assert publish._chain_export_source_repo(export) == source


# ---------------------------------------------------------------------------
# Case A: the payload gate's declared-dependency set
# ---------------------------------------------------------------------------


def _grade_payload(tmp_path: Path, monkeypatch):
    """Run the end-of-run payload gate with the publisher living in a chain export of a source repo
    whose pyproject declares PyYAML. Returns (ok, out, err)."""
    source = tmp_path / "source"
    _init(source)
    _write(source, "pyproject.toml", _PYPROJECT)
    _commit(source, "engine")
    export = _make_export(tmp_path, source)

    dest = tmp_path / "dest"
    _init(dest)
    _write(dest, "README.md", "x\n")
    _commit(dest, "init")
    assembled = tmp_path / "assembled"
    shutil.copytree(dest, assembled, ignore=shutil.ignore_patterns(".git"))
    _write(assembled, "bin/x.py", _HOOK)
    _write(assembled, "lib/install/coordinator_install.py", _PLAN_OK)

    from percolate import doc_path_refs

    # `default_pyproject()` is file-relative; the publisher running from the export resolves it there.
    monkeypatch.setattr(doc_path_refs, "default_pyproject", lambda: export / "pyproject.toml")
    monkeypatch.setattr(publish, "_REPO_ROOT", export)
    row = publish.ResolvedTarget(
        name="coordinator-claude",
        mode="mirror",
        source_dir=tmp_path / "src",
        dest_dir=dest,
        allowlist="x",
    )
    out, err = io.StringIO(), io.StringIO()
    ok = publish.dispatch_end_of_run_plugin_payload_gate(
        [assembled], rows_by_repo_root={assembled: [row]}, err=err, out=out
    )
    return ok, out.getvalue(), err.getvalue()


def test_payload_gate_from_chain_export_reads_source_repo_declared_deps(tmp_path, monkeypatch):
    ok, out, err = _grade_payload(tmp_path, monkeypatch)
    assert ok and "0 import(s)" in out, (
        "payload gate graded with an empty declared-dependency set: the pyproject came from the "
        f"chain export, not its source repo, so declared PyYAML did not resolve `import yaml`.\n{out}{err}"
    )


# ---------------------------------------------------------------------------
# Case B: the published-engine pin's toplevel
# ---------------------------------------------------------------------------


class _World:
    """Source repo S (A then B on an upstream-less branch, dirty tracked file), the chain export of S,
    mirror M stamped with A, and a content repo."""

    def __init__(self, tmp_path: Path) -> None:
        self.engine = tmp_path / "engine"
        _init(self.engine)
        _write(self.engine, "coordinator/bin/tool.py", "print('A')\n")
        _write(self.engine, "coordinator/bin/other.py", "other\n")
        _write(self.engine, "coordinator/lib/x.py", "x = 'A'\n")
        self.sha_a = _commit(self.engine, "A")
        _git(self.engine, "checkout", "-q", "-b", "feature-no-upstream")
        _write(self.engine, "coordinator/bin/tool.py", "print('B')\n")
        _commit(self.engine, "B")
        _write(self.engine, "coordinator/bin/other.py", "dirty\n")
        self.export = _make_export(tmp_path, self.engine)

        self.mirror = tmp_path / "mirror"
        _init(self.mirror)
        _write(self.mirror, "f0.txt", "0\n")
        _commit(self.mirror, "older round")
        _write(self.mirror, "f1.txt", "1\n")
        tip = _commit(self.mirror, "round" + format_source_sha_suffix(self.sha_a))
        _git(self.mirror, "update-ref", f"refs/remotes/{TRACK_REF}", tip)

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


def _patch_registry(monkeypatch, world: _World) -> None:
    from coordinator_core import machine_resolver

    facts = {
        f"publish.mirrors.{MIRROR_KEY}.path": str(world.mirror),
        f"publish.mirrors.{MIRROR_KEY}.track_ref": TRACK_REF,
    }
    monkeypatch.setattr(publish, "_REPO_ROOT", world.export)
    monkeypatch.setattr(publish, "_engine_declaring_mirror_keys", lambda *a, **k: frozenset({MIRROR_KEY}))
    monkeypatch.setattr(machine_resolver, "registry_get", lambda key, *a, **k: facts.get(key))
    mirror_real = os.path.realpath(str(world.mirror))
    monkeypatch.setattr(
        publish,
        "_publish_mirror_key_for_repo_root",
        lambda root: MIRROR_KEY if os.path.realpath(str(root)) == mirror_real else None,
    )


def _seed(rows):
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


def test_pin_from_chain_export_resolves_toplevel_to_source_repo(tmp_path, monkeypatch):
    world = _World(tmp_path)
    _patch_registry(monkeypatch, world)
    pin, pins, printed = _seed([world.plugin_row()])
    assert pin is not None, (
        "published-engine pin not seeded: its toplevel resolved to the chain export, so no admitted "
        f"row's root sat under it (`no admitted row reads the engine toplevel`).\n{printed}"
    )
    assert Path(pin.engine_toplevel).resolve() == world.engine.resolve()
    assert pin.source_sha == world.sha_a
    assert pins == {str(pin.engine_toplevel): world.sha_a}


def test_provenance_gate_accepts_dirty_engine_tree_under_the_chain_export_pin(tmp_path, monkeypatch):
    world = _World(tmp_path)
    _patch_registry(monkeypatch, world)
    pin, _pins, printed = _seed([world.plugin_row()])
    assert pin is not None, f"published-engine pin not seeded from the chain export:\n{printed}"
    ok, out, err = _gate(world, [world.plugin_row()], pin)
    assert ok, err
    assert "dirty tracked" not in err and "no upstream" not in err


def test_root_outside_the_pin_still_refuses_unpushed_head_and_reports_dirt_as_advisory(tmp_path, monkeypatch):
    """The pin exempts only roots under the engine toplevel; an upstream-less content root still
    refuses, and its dirty file is reported as an advisory, not a refusal reason."""
    world = _World(tmp_path)
    _patch_registry(monkeypatch, world)
    row = world.plugin_row(allowlist="bin,lib,hooks")
    pin, _pins, printed = _seed([row])
    assert pin is not None, f"published-engine pin not seeded from the chain export:\n{printed}"
    _write(world.content, "hooks/h.py", "dirty content\n")
    ok, out, err = _gate(world, [row], pin)
    assert not ok and TRIPWIRE in err
    assert str(world.content) in err and "no upstream" in err and "dirty tracked" not in err
    assert "Advisory" in out and "hooks/h.py" in out
    assert str(world.engine_coord) not in err


# ---------------------------------------------------------------------------
# Case C: the coordinator-claude source_map payload carries bin/seed-cross-session-inbound.py
# ---------------------------------------------------------------------------

_SEED_INBOUND = "seed-cross-session-inbound.py"
_PORTABLE = _REPO_ROOT / "setup" / "publish-targets.portable"


def _portable_row_allowlist(name: str) -> "list[str]":
    for line in _PORTABLE.read_text(encoding="utf-8").splitlines():
        if line.startswith(name + "|"):
            return line.split("|")[6].split(",")
    raise AssertionError(f"row {name!r} absent from {_PORTABLE}")


def _engine_ignore(root: Path):
    sys.path.insert(0, str(_REPO_ROOT / "coordinator" / "lib"))
    try:
        from percolate import ignore
    finally:
        sys.path.pop(0)
    return ignore.load_percolate_ignore(root / ".percolate-ignore"), ignore.is_ignored


def test_seed_cross_session_inbound_clears_every_filter_on_the_coordinator_claude_route():
    # coordinator-claude's `plugin-source:claude-klabauter/coordinator=bin,lib` reads the published
    # klabauter engine tree: the klabauter allowlist row admits it, and neither contributing
    # root's .percolate-ignore drops it.
    assert _SEED_INBOUND in _portable_row_allowlist("claude-klabauter-coordinator-bin")
    assert (_REPO_ROOT / "coordinator" / "bin" / _SEED_INBOUND).is_file()
    for root, rel in (
        (_REPO_ROOT / "coordinator", f"bin/{_SEED_INBOUND}"),
        (_REPO_ROOT / "coordinator" / "bin", _SEED_INBOUND),
    ):
        patterns, is_ignored = _engine_ignore(root)
        assert not is_ignored(rel, patterns), f"{root}/.percolate-ignore drops {rel}"


def test_a_real_sibling_bin_script_off_the_klabauter_allowlist_is_still_refused():
    allowlist = _portable_row_allowlist("claude-klabauter-coordinator-bin")
    sibling = "publish.py"
    assert (_REPO_ROOT / "coordinator" / "bin" / sibling).is_file()
    assert _SEED_INBOUND in allowlist
    assert sibling not in allowlist
