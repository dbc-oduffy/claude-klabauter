"""A cold publish round removes mirror files the routed source root deleted, and only those.

The mirror row mirrors a default root and routes `bin` to a second root through a `source_map`
(the coordinator-claude shape: DoE default root, claude-klabauter routed root). `process_target` is the one
fake: it stages the payload and reports the destination's tracked orphans as removals, as the
orphan sweep does. Union, provenance filter, live-source refusal and landing are the real code.

Run: python -m pytest coordinator/bin/tests/test_publish_round_removes_source_absent.py -q
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
    spec = importlib.util.spec_from_file_location("publish_removes_source_absent_under_test", _BIN_DIR / "publish.py")
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
    """A mirror at `dest`, a default root, and a claude-klabauter-like repo whose `coordinator/` is the routed root."""

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
        _commit_all(self.default_repo, "default: seed")

        _write(self.routed / "bin" / "kept.py", "kept = 1\n")
        _write(self.routed / "bin" / "gone.py", "gone = 1\n")
        _write(self.routed / "bin" / "live.py", "live = 1\n")
        _commit_all(self.claude_klabauter, "claude-klabauter: seed bin")
        (self.routed / "bin" / "gone.py").unlink()
        (self.routed / "bin" / "live.py").unlink()
        _commit_all(self.claude_klabauter, "claude-klabauter: delete gone.py and live.py from bin")
        _write(self.routed / "bin" / "live.py", "live = 2\n")
        self.routed_head = _commit_all(self.claude_klabauter, "claude-klabauter: restore live.py")

        for rel, text in (
            ("README.md", "readme\n"),
            (".gitkeep", ""),
            ("bin/kept.py", "kept = 1\n"),
            ("bin/gone.py", "gone = 1\n"),
            ("bin/foreign.txt", "another publisher's file\n"),
        ):
            _write(self.dest / rel, text)
        self.seed_head = _commit_all(self.dest, "mirror: seed")

        self.omit: "set[str]" = set()
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

    def _payload(self) -> "dict[str, bytes]":
        payload: "dict[str, bytes]" = {}
        for base, prefix in ((self.default_root, ""), (self.routed / "bin", "bin/")):
            for p in base.rglob("*"):
                if p.is_file():
                    payload[prefix + p.relative_to(base).as_posix()] = p.read_bytes()
        return {rel: data for rel, data in payload.items() if rel not in self.omit}

    def _process_target(self, target, setup_dir, totals, **kwargs):
        payload = self._payload()
        staging = Path(tempfile.mkdtemp(prefix=".publish-staging-", dir=str(target.dest_dir.parent)))
        for rel, data in payload.items():
            _write(staging / rel, data.decode("utf-8"))
        head_files = set(_git(self.dest, "ls-tree", "-r", "--name-only", "HEAD").split())
        removed = {rel for rel in head_files if rel not in payload}
        totals.processed += 1
        return publish.StagedRowResult(
            staging_dir=staging,
            row_visited={Path(r) for r in payload},
            row_changed_files=set(payload),
            row_removed_files=removed,
            row_published_files={Path(r) for r in payload},
            report_text="",
            synced=len(payload),
            deleted=len(removed),
        )

    def run(self, *argv: str) -> "tuple[int, str]":
        rc = publish.main(["row-a", *argv])
        out, err = self.capsys.readouterr()
        return rc, out + err

    def head_files(self) -> "set[str]":
        return set(_git(self.dest, "ls-tree", "-r", "--name-only", "HEAD").split())


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    return World(tmp_path, monkeypatch, capsys)


def test_a_file_the_routed_root_deleted_leaves_the_mirror_and_a_never_shipped_one_stays(world):
    rc, out = world.run()

    assert rc == 0, out
    assert "bin/gone.py" not in world.head_files()
    assert not (world.dest / "bin" / "gone.py").exists()
    assert "bin/foreign.txt" in world.head_files()
    assert (world.dest / "bin" / "foreign.txt").exists()
    assert "another publisher's" in out and "bin/foreign.txt" in out
    assert "bin/gone.py" not in out.split("another publisher's", 1)[1]


def test_a_removal_still_live_in_the_routed_root_is_refused_before_any_write(world):
    from percolate.diff_commit import RemovalSourceLiveError

    world.omit.add("bin/live.py")
    _write(world.dest / "bin" / "live.py", "live = 1\n")
    head = _commit_all(world.dest, "mirror: ship live.py")

    with pytest.raises(RemovalSourceLiveError):
        world.run()

    assert _git(world.dest, "rev-parse", "HEAD").strip() == head
    assert "bin/gone.py" in world.head_files()
    assert (world.dest / "bin" / "gone.py").exists()
