"""A publish round leaves every retained mirror file at its source-declared mode and the tree clean.

The mirror row mirrors a default root and routes `bin` to a second root through a `source_map`.
`process_target` is the one fake: it stages the payload bytes, which carry no mode (the same as a
filesystem without an exec bit). Union, landing, commit and trailers are the real code. Disk-bit
assertions are skipped on Windows; the tree-mode ones run everywhere.

Run: python -m pytest coordinator/bin/tests/test_publish_round_lands_source_modes.py -q
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SIG = "sig-0000000000000000000000000000000000000001"

_GATES = (
    "dispatch_end_of_run_identity_check",
    "dispatch_end_of_run_install_doc_payload_check",
    "dispatch_end_of_run_unscanned_published_check",
    "dispatch_end_of_run_function_gate",
    "dispatch_end_of_run_entrypoint_gate",
    "dispatch_end_of_run_argv_parity_gate",
    "dispatch_end_of_run_plugin_payload_gate",
    "dispatch_end_of_run_plugin_provenance_gate",
    "dispatch_end_of_run_plugin_version_stamp_gate",
)

_EXEC, _PLAIN = "100755", "100644"


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_lands_source_modes_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, creationflags=_NO_WINDOW
    ).stdout


def _init_repo(root: Path, *, self_origin: bool) -> None:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-b", "main")
    for key, value in (("user.email", "t@t.test"), ("user.name", "t"), ("commit.gpgsign", "false"),
                       ("core.autocrlf", "false"), ("core.fileMode", "true")):
        _git(root, "config", key, value)
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "chore: init")
    if self_origin:
        _git(root, "remote", "add", "origin", str(root))
        _git(root, "fetch", "--no-tags", "origin")
        _git(root, "branch", "--set-upstream-to=origin/main", "main")


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _commit_with_modes(root: Path, message: str, exec_rels: "tuple[str, ...]") -> str:
    """Commit everything, declaring each path in `exec_rels` 100755 and the rest as the index has them."""
    _git(root, "add", "-A")
    if exec_rels:
        _git(root, "add", "--chmod=+x", "--", *exec_rels)
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def _tree_mode(root: Path, rev: str, rel: str) -> str:
    return _git(root, "ls-tree", rev, "--", rel).split()[0]


class World:
    """kept-tool: shebanged, 100755 at source and mirror HEAD, 644 on the mirror's disk.
    demoted.py: no shebang, 100755 at mirror HEAD, 100644 at source."""

    def __init__(self, tmp_path: Path, monkeypatch, capsys):
        self.tmp = tmp_path
        self.capsys = capsys
        self.default_repo = tmp_path / "default-repo"
        self.claude_klabauter = tmp_path / "claude-klabauter"
        self.routed = self.claude_klabauter / "coordinator"
        self.dest = tmp_path / "dest-repo"
        _init_repo(self.default_repo, self_origin=False)
        _init_repo(self.claude_klabauter, self_origin=False)
        _init_repo(self.dest, self_origin=True)
        with (self.dest / ".git" / "info" / "exclude").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(".percolate/\n")
        self.default_root = self.default_repo / "plugin"
        _write(self.default_root / "README.md", "readme\n")
        _write(self.default_root / ".gitkeep", "")
        _commit_with_modes(self.default_repo, "default: seed", ())

        _write(self.routed / "bin" / "kept-tool", "#!/usr/bin/env python3\nprint('kept')\n")
        _write(self.routed / "bin" / "demoted.py", "demoted = 1\n")
        self.source_head = _commit_with_modes(self.claude_klabauter, "claude-klabauter: seed bin", ("coordinator/bin/kept-tool",))
        if os.name != "nt":
            os.chmod(self.routed / "bin" / "kept-tool", 0o755)

        _write(self.dest / "README.md", "readme\n")
        _write(self.dest / ".gitkeep", "")
        _write(self.dest / "bin" / "kept-tool", "#!/usr/bin/env python3\nprint('kept')\n")
        _write(self.dest / "bin" / "demoted.py", "demoted = 1\n")
        self.seed_head = _commit_with_modes(self.dest, "mirror: seed", ("bin/kept-tool", "bin/demoted.py"))
        if os.name != "nt":
            os.chmod(self.dest / "bin" / "kept-tool", 0o644)
            os.chmod(self.dest / "bin" / "demoted.py", 0o755)
        self._wire(monkeypatch)

    def _wire(self, monkeypatch) -> None:
        tmp = self.tmp
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp))
        monkeypatch.setattr(publish, "_REPO_ROOT", self.claude_klabauter)
        row = f"row-a|mirror|{self.default_root}|{self.dest}|||{self.routed}=bin"
        monkeypatch.setattr(publish, "_resolve_percolate_root_and_rung", lambda **kw: (tmp, "test-rung"))
        monkeypatch.setattr(publish, "load_targets", lambda setup_dir, target_filter=None, **_: [row])

        class _FakeClaudeKlabauter:
            def resolve_target(self, store, name):
                raise KeyError(name)

        publish._bootstrap_engine()
        from percolate import publish_sync as real_publish_sync

        monkeypatch.setattr(publish, "_import_claude_klabauter_percolate", lambda: _FakeClaudeKlabauter())
        monkeypatch.setattr(publish, "assert_percolate_store_ready", lambda engine_claude_klabauter, path: {})
        monkeypatch.setattr(publish, "locate_percolate_store", lambda setup_dir: tmp / "store.yaml")
        monkeypatch.setattr(publish, "resolve_percolate_identity_path", lambda setup_dir: tmp / "id")
        monkeypatch.setattr(publish, "check_identity_file_present", lambda path, setup_dir: tmp / "id")
        monkeypatch.setattr(publish, "check_identity_file_safe", lambda path: None)
        monkeypatch.setattr(
            publish, "parse_percolate_identity", lambda path: publish.PercolateIdentity(review=["dummy"])
        )
        monkeypatch.setattr(publish, "_resolve_publish_sync_module_path", lambda setup_dir: tmp / "publish_sync.py")
        monkeypatch.setattr(publish, "_import_publish_sync", lambda setup_dir: real_publish_sync)
        monkeypatch.setattr(publish, "check_publish_sync_contract", lambda *a, **k: None)
        monkeypatch.setattr(publish, "write_publish_provenance_record", lambda **kwargs: None)
        monkeypatch.setattr(publish, "compute_delta_invalidation_signature", lambda store_path, ctx: _SIG)
        for gate in _GATES:
            monkeypatch.setattr(publish, gate, lambda *a, **k: True)
        monkeypatch.setattr(publish, "process_target", self._process_target)

    def _process_target(self, target, setup_dir, totals, **kwargs):
        payload: "dict[str, bytes]" = {}
        for base, prefix in ((self.default_root, ""), (self.routed / "bin", "bin/")):
            for p in base.rglob("*"):
                if p.is_file():
                    payload[prefix + p.relative_to(base).as_posix()] = p.read_bytes()
        staging = Path(tempfile.mkdtemp(prefix=".publish-staging-", dir=str(target.dest_dir.parent)))
        for rel, data in payload.items():
            _write(staging / rel, data.decode("utf-8"))
        totals.processed += 1
        return publish.StagedRowResult(
            staging_dir=staging,
            row_visited={Path(r) for r in payload},
            row_changed_files=set(payload),
            row_removed_files=set(),
            row_published_files={Path(r) for r in payload},
            report_text="",
            synced=len(payload),
            deleted=0,
        )

    def run(self) -> "tuple[int, str]":
        rc = publish.main(["row-a"])
        out, err = self.capsys.readouterr()
        return rc, out + err


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    return World(tmp_path, monkeypatch, capsys)


def test_every_retained_file_lands_at_its_source_tree_mode(world):
    rc, out = world.run()

    assert rc == 0, out
    for rel in ("bin/kept-tool", "bin/demoted.py"):
        source_mode = _tree_mode(world.claude_klabauter, "HEAD", f"coordinator/{rel}")
        assert _tree_mode(world.dest, "HEAD", rel) == source_mode, rel
    assert _tree_mode(world.dest, "HEAD", "bin/demoted.py") == _PLAIN
    assert _tree_mode(world.dest, "HEAD", "bin/kept-tool") == _EXEC


@pytest.mark.skipif(os.name == "nt", reason="the disk exec bit is not modelled on Windows")
def test_every_retained_file_has_a_disk_exec_bit_equal_to_its_head_mode(world):
    rc, out = world.run()

    assert rc == 0, out
    for rel in ("bin/kept-tool", "bin/demoted.py"):
        head_exec = _tree_mode(world.dest, "HEAD", rel) == _EXEC
        assert bool((world.dest / rel).stat().st_mode & 0o111) == head_exec, rel


@pytest.mark.skipif(os.name == "nt", reason="the disk exec bit is not modelled on Windows")
def test_a_head_exec_file_drifted_to_644_on_disk_is_converged_back(world):
    assert not (world.dest / "bin" / "kept-tool").stat().st_mode & 0o111

    rc, out = world.run()

    assert rc == 0, out
    assert (world.dest / "bin" / "kept-tool").stat().st_mode & 0o111


def test_the_mirror_tree_is_clean_after_the_round(world):
    rc, out = world.run()

    assert rc == 0, out
    assert _git(world.dest, "status", "--porcelain").strip() == ""
