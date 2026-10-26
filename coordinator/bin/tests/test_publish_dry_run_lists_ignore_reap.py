"""A dry-run publish lists every ignore-covered path the live round reaps, on one untruncated line.

`process_target` is the one stand-in; the plan, reap derivation, print helper, union and landing
are the real code.

Run: python -m pytest coordinator/bin/tests/test_publish_dry_run_lists_ignore_reap.py -q
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SIG = "sig-0000000000000000000000000000000000000001"
_STRAYS = [f"tests/stray_{n}.dat" for n in range(7)]

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


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_dry_run_ignore_reap_under_test", _BIN_DIR / "publish.py")
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
                       ("core.autocrlf", "false")):
        _git(root, "config", key, value)
    (root / ".gitkeep").write_text("", encoding="utf-8")
    _git(root, "add", ".gitkeep")
    _git(root, "commit", "-m", "chore: init")
    if self_origin:
        _git(root, "remote", "add", "origin", str(root))
        _git(root, "fetch", "--no-tags", "origin")
        _git(root, "branch", "--set-upstream-to=origin/main", "main")


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


class World:
    """A source repo whose ignore file covers seven tracked files the mirror also tracks."""

    def __init__(self, tmp_path: Path, monkeypatch, capsys):
        self.tmp = tmp_path
        self.capsys = capsys
        self.source = tmp_path / "source-repo"
        self.src = self.source / "src"
        self.dest = tmp_path / "dest-repo"
        _init_repo(self.source, self_origin=False)
        _init_repo(self.dest, self_origin=True)
        with (self.dest / ".git" / "info" / "exclude").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(".percolate/\n")

        _write(self.src / "kept.py", "kept = 1\n")
        for stray in _STRAYS:
            _write(self.src / stray, "stray\n")
        _write(self.src / ".percolate-ignore", "\n".join(["*.log", *_STRAYS]) + "\n")
        _commit_all(self.source, "source: seed")

        _write(self.dest / "kept.py", "kept = 1\n")
        for stray in _STRAYS:
            _write(self.dest / stray, "stray\n")
        _write(self.dest / "owned.log", "dest-owned\n")
        self.seed_head = _commit_all(self.dest, "mirror: seed")

        self._wire(monkeypatch)

    def _wire(self, monkeypatch) -> None:
        tmp = self.tmp
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp))
        publish._bootstrap_engine()
        monkeypatch.setattr(publish, "_REPO_ROOT", self.source)
        row = f"row-a|mirror|{self.src}|{self.dest}"
        monkeypatch.setattr(publish, "_resolve_percolate_root_and_rung", lambda **kw: (tmp, "test-rung"))
        monkeypatch.setattr(publish, "load_targets", lambda setup_dir, target_filter=None, **_: [row])

        class _FakeClaudeKlabauter:
            def resolve_target(self, store, name):
                raise KeyError(name)

        from percolate import publish_sync as real_publish_sync

        self.sync = real_publish_sync
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
        matcher = self.sync.load_ignore(self.src / ".percolate-ignore")
        staging = Path(tempfile.mkdtemp(prefix=".publish-staging-", dir=str(target.dest_dir.parent)))
        self.sync.sync_mirror(self.src, staging, matcher, dry_run=False)
        removed: "set[str]" = set()
        self.sync.sync_mirror(self.src, self.dest, matcher, dry_run=True, removed_sink=removed)
        staged = {p.relative_to(staging).as_posix() for p in staging.rglob("*") if p.is_file()}
        totals.processed += 1
        return publish.StagedRowResult(
            staging_dir=staging,
            row_visited={Path(r) for r in staged},
            row_changed_files=set(staged),
            row_removed_files=removed,
            row_published_files={Path(r) for r in staged},
            report_text="",
            synced=len(staged),
            deleted=len(removed),
        )

    def run(self, *argv: str) -> "tuple[int, str]":
        rc = publish.main(["row-a", *argv])
        out, err = self.capsys.readouterr()
        return rc, out + err

    def head_files(self) -> "set[str]":
        return set(_git(self.dest, "ls-tree", "-r", "--name-only", "HEAD").split())


def _reap_line(out: str, verb: str) -> str:
    lines = [ln for ln in out.splitlines() if "ignore-covered" in ln]
    assert len(lines) == 1, out
    assert f": {verb} {len(_STRAYS)} ignore-covered path(s) the source still tracks: " in lines[0]
    return lines[0]


def test_dry_run_names_every_reapable_path_on_one_line_and_lands_nothing(tmp_path, monkeypatch, capsys):
    world = World(tmp_path, monkeypatch, capsys)

    rc, out = world.run("--dry-run")

    assert rc == 0, out
    line = _reap_line(out, "would remove")
    assert line.split("tracks: ", 1)[1] == ", ".join(sorted(_STRAYS))
    assert "..." not in line
    assert _git(world.dest, "rev-parse", "HEAD").strip() == world.seed_head
    assert set(_STRAYS) <= world.head_files()
    assert all((world.dest / stray).exists() for stray in _STRAYS)
    assert (world.dest / "owned.log").exists()


def test_live_round_removes_exactly_the_paths_the_dry_run_listed(tmp_path, monkeypatch, capsys):
    world = World(tmp_path, monkeypatch, capsys)
    _rc, dry_out = world.run("--dry-run")
    dry_paths = _reap_line(dry_out, "would remove").split("tracks: ", 1)[1]

    rc, out = world.run()

    assert rc == 0, out
    assert _reap_line(out, "removing").split("tracks: ", 1)[1] == dry_paths
    assert world.head_files().isdisjoint(_STRAYS)
    assert not any((world.dest / stray).exists() for stray in _STRAYS)
    assert {"owned.log", "kept.py"} <= world.head_files()
