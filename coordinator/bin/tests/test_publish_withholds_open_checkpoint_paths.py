"""publish.py overlays a withheld path's pre-checkpoint blob on the shipped tree.

Real git repo: a base commit, then a checkpoint that adds one path and modifies another,
then a closed fix. The materialized shadow carries the fix's bytes everywhere except the
withheld paths, which carry the base blob (modified path) or are absent (added path).

Run: python -m pytest coordinator/bin/tests/test_publish_withholds_open_checkpoint_paths.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_withholds_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=True, text=True, creationflags=_NO_WINDOW
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@claude-klabauter.test")
    _git(root, "config", "user.name", "T")
    _git(root, "config", "commit.gpgsign", "false")
    (root / ".gitattributes").write_bytes(b"* -text\n")
    (root / "y.py").write_bytes(b"Y = 'base'\n")
    (root / "z.py").write_bytes(b"Z = 'base'\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")
    (root / "tests").mkdir()
    (root / "tests" / "x.py").write_bytes(b"X = 'checkpoint'\n")
    (root / "y.py").write_bytes(b"Y = 'checkpoint'\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "checkpoint")
    (root / "z.py").write_bytes(b"Z = 'fix'\n")
    _git(root, "commit", "-q", "-am", "fix")
    head = _git(root, "rev-parse", "HEAD")
    publish._MATERIALIZED_REF_CACHE.clear()
    return root, base, head


def test_withheld_paths_carry_base_blob_or_are_absent_and_the_rest_ships(repo):
    root, base, head = repo
    withheld = {"tests/x.py": None, "y.py": base}

    shadow = publish._git_materialize_ref(root, ref=head, withheld=withheld)

    assert not (shadow / "tests" / "x.py").exists()
    assert (shadow / "y.py").read_bytes() == b"Y = 'base'\n"
    assert (shadow / "z.py").read_bytes() == b"Z = 'fix'\n"
    publish._cleanup_shadow_roots((shadow,))


def test_withheld_digest_widens_the_cache_key_so_a_plain_materialization_is_unaffected(repo):
    root, base, head = repo
    plain = publish._git_materialize_ref(root, ref=head)
    held = publish._git_materialize_ref(root, ref=head, withheld={"y.py": base})

    assert plain != held
    assert (plain / "y.py").read_bytes() == b"Y = 'checkpoint'\n"
    assert (held / "y.py").read_bytes() == b"Y = 'base'\n"
    assert publish._git_materialize_ref(root, ref=head, withheld={"y.py": base}) == held
    publish._cleanup_shadow_roots((plain, held))
    assert not publish._MATERIALIZED_REF_CACHE


def test_overlay_fails_closed_on_an_unresolvable_base(repo):
    root, _base, head = repo
    with pytest.raises(publish.GitMaterializeError):
        publish._git_materialize_ref(root, ref=head, withheld={"y.py": "0" * 40})
    assert not publish._MATERIALIZED_REF_CACHE


def test_sync_commit_message_carries_withheld_base_only_when_withholding_and_stamped(monkeypatch):
    suffix = " [source-head abcdef123456]"
    monkeypatch.setattr(publish, "_WITHHELD_PATHS", {"y.py": None})
    monkeypatch.setattr(publish, "_WITHHELD_WALK_BASE", "b" * 40)
    msg = publish._sync_commit_message("m", ["r"], ["a"], [], suffix)
    assert msg.endswith(f"Source-Withheld-Base: {'b' * 40}")
    assert msg.splitlines()[0].endswith(suffix)
    removed = publish._sync_commit_message("m", ["r"], ["a"], ["gone"], suffix)
    assert "Removed: gone" in removed and removed.endswith(f"Source-Withheld-Base: {'b' * 40}")
    assert "Source-Withheld-Base" not in publish._sync_commit_message("m", ["r"], ["a"], [], "")

    monkeypatch.setattr(publish, "_WITHHELD_PATHS", {})
    assert "Source-Withheld-Base" not in publish._sync_commit_message("m", ["r"], ["a"], [], suffix)


def test_parser_accepts_withheld_paths_json():
    args = publish.build_arg_parser().parse_args(["--withheld-paths-json", "w.json"])
    assert args.withheld_paths_json == "w.json"


def test_withholding_applies_to_the_withheld_toplevel_not_only_the_engine_repo(repo, monkeypatch):
    root, base, _head = repo
    monkeypatch.setattr(publish, "_WITHHELD_PATHS", {"y.py": base})
    monkeypatch.setattr(publish, "_WITHHELD_TOPLEVEL", str(root))
    monkeypatch.setattr(publish, "_engine_toplevel_key", lambda: "/elsewhere/engine")

    assert publish._withholds_root(root) is True


def test_withholding_does_not_apply_to_a_different_toplevel(repo, monkeypatch, tmp_path):
    root, base, _head = repo
    monkeypatch.setattr(publish, "_WITHHELD_PATHS", {"y.py": base})
    monkeypatch.setattr(publish, "_WITHHELD_TOPLEVEL", str(tmp_path / "other"))

    assert publish._withholds_root(root) is False
    monkeypatch.setattr(publish, "_WITHHELD_PATHS", {})
    monkeypatch.setattr(publish, "_WITHHELD_TOPLEVEL", str(root))
    assert publish._withholds_root(root) is False
