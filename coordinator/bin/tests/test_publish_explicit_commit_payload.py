"""A publish round ships and stamps one commit, even when the source HEAD moves mid-run.

`process_target` is the one fake: it stages the payload from each source toplevel's round pin
(`_round_pin_source_sha`), never the working tree, and commits a second source commit before the
first row copy. Union, landing, commit and trailers are the real code.

Run: python -m pytest coordinator/bin/tests/test_publish_explicit_commit_payload.py -q
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
    spec = importlib.util.spec_from_file_location("publish_explicit_commit_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()
publish._bootstrap_engine()
from coordinator_core.git.git_state import format_source_sha_suffix  # noqa: E402


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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


def _blob(root: Path, rev: str, rel: str) -> str:
    return _git(root, "rev-parse", f"{rev}:{rel}").strip()


class World:
    """claude-klabauter (the engine toplevel) routes `coordinator/bin` to `bin/`; default_repo is a foreign toplevel."""

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
        _write(self.default_repo / "ext" / "ext.txt", "ext A\n")
        self.default_a = _commit_all(self.default_repo, "foreign: A")
        self.default_root = self.claude_klabauter / "plugin"
        _write(self.default_root / "README.md", "readme A\n")

        _write(self.routed / "bin" / "tool.py", "tool = 'A'\n")
        _write(self.routed / "bin" / "other.py", "other = 'A'\n")
        self.sha_a = _commit_all(self.claude_klabauter, "claude-klabauter: A")

        _write(self.dest / "README.md", "old\n")
        _write(self.dest / "bin" / "tool.py", "old\n")
        _commit_all(self.dest, "mirror: seed")
        self.dest_seed = _git(self.dest, "rev-parse", "HEAD").strip()
        self.sha_b = ""
        self.default_b = ""
        self.moved = False
        self._wire(monkeypatch)

    def _wire(self, monkeypatch) -> None:
        tmp = self.tmp
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp))
        monkeypatch.setattr(publish, "_REPO_ROOT", self.claude_klabauter)
        row = f"row-a|mirror|{self.default_root}|{self.dest}|||{self.routed}=bin;{self.default_repo}=ext"
        monkeypatch.setattr(publish, "_resolve_percolate_root_and_rung", lambda **kw: (tmp, "test-rung"))
        monkeypatch.setattr(publish, "load_targets", lambda setup_dir, target_filter=None, **_: [row])

        class _FakeClaudeKlabauter:
            def resolve_target(self, store, name):
                raise KeyError(name)

        publish._bootstrap_engine()
        from percolate import publish_sync as real_publish_sync

        engine_root = publish._ENGINE_ROOT
        monkeypatch.setattr(publish, "_seed_published_engine_pin", lambda *a, **k: None)
        monkeypatch.setattr(publish, "_export_engine_at_ref", lambda repo_root, ref: engine_root)
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

    def _move_heads(self) -> None:
        _write(self.routed / "bin" / "tool.py", "tool = 'B'\n")
        _write(self.routed / "bin" / "late.py", "late = 'B'\n")
        _write(self.default_root / "README.md", "readme B\n")
        self.sha_b = _commit_all(self.claude_klabauter, "claude-klabauter: B")
        _write(self.default_repo / "ext" / "ext.txt", "ext B\n")
        self.default_b = _commit_all(self.default_repo, "foreign: B")
        self.moved = True

    def _pinned_files(self, toplevel: Path, pins: "dict[str, str]", pathspec: str, prefix: str) -> "dict[str, bytes]":
        sha = publish._round_pin_source_sha(toplevel, pins, out=sys.stderr)
        names = _git(toplevel, "ls-tree", "-r", "--name-only", sha, "--", pathspec).split("\n")
        files = {}
        for name in filter(None, names):
            blob = subprocess.run(
                ["git", "show", f"{sha}:{name}"], cwd=str(toplevel), capture_output=True, check=True,
                creationflags=_NO_WINDOW,
            ).stdout
            files[prefix + Path(name).relative_to(pathspec).as_posix()] = blob
        return files

    def _process_target(self, target, setup_dir, totals, **kwargs):
        if not self.moved:
            self._move_heads()
        pins = kwargs["round_pinned_shas"]
        payload = self._pinned_files(self.claude_klabauter, pins, "plugin", "")
        payload.update(self._pinned_files(self.claude_klabauter, pins, "coordinator/bin", "bin/"))
        payload.update(self._pinned_files(self.default_repo, pins, "ext", "ext/"))
        staging = Path(tempfile.mkdtemp(prefix=".publish-staging-", dir=str(target.dest_dir.parent)))
        for rel, data in payload.items():
            dest = staging / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
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

    def run(self, *argv: str) -> "tuple[int, str]":
        rc = publish.main([*argv, "row-a"])
        out, err = self.capsys.readouterr()
        return rc, out + err

    def subject(self) -> str:
        return _git(self.dest, "log", "-1", "--format=%s").strip()


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    return World(tmp_path, monkeypatch, capsys)


def _assert_ships(world: World, engine_sha: str, default_sha: str) -> None:
    for rel, repo, sha, src in (
        ("bin/tool.py", world.claude_klabauter, engine_sha, "coordinator/bin/tool.py"),
        ("bin/other.py", world.claude_klabauter, engine_sha, "coordinator/bin/other.py"),
        ("README.md", world.claude_klabauter, engine_sha, "plugin/README.md"),
        ("ext/ext.txt", world.default_repo, default_sha, "ext/ext.txt"),
    ):
        assert _blob(world.dest, "HEAD", rel) == _blob(repo, sha, src), rel
    assert subprocess.run(
        ["git", "cat-file", "-e", "HEAD:bin/late.py"], cwd=str(world.dest), creationflags=_NO_WINDOW,
        capture_output=True,
    ).returncode != 0


def test_explicit_commit_ships_and_stamps_that_commit_after_head_moves(world):
    rc, out = world.run("--commit", world.sha_a)

    assert rc == 0, out
    assert world.moved and world.sha_b != world.sha_a
    _assert_ships(world, world.sha_a, world.default_a)
    assert format_source_sha_suffix(world.sha_a) in world.subject()
    assert format_source_sha_suffix(world.sha_b) not in world.subject()


def test_default_invocation_ships_and_stamps_head_at_start_after_head_moves(world):
    rc, out = world.run()

    assert rc == 0, out
    assert world.moved and world.sha_b != world.sha_a
    _assert_ships(world, world.sha_a, world.default_a)
    assert format_source_sha_suffix(world.sha_a) in world.subject()
    assert format_source_sha_suffix(world.sha_b) not in world.subject()


def test_unresolvable_commit_exits_1_and_leaves_the_dest_untouched(world):
    rc, out = world.run("--commit", "0" * 40)

    assert rc == 1, out
    assert not world.moved
    assert _git(world.dest, "rev-parse", "HEAD").strip() == world.dest_seed
    assert _git(world.dest, "status", "--porcelain").strip() == ""


def test_differing_commit_and_source_ref_exit_2_before_any_export(world):
    rc, out = world.run("--commit", world.sha_a, "--source-ref", "0" * 40)

    assert rc == 2, out
    assert "differ" in out
    assert _git(world.dest, "rev-parse", "HEAD").strip() == world.dest_seed


def test_foreign_toplevel_row_pins_its_own_head_at_round_start_not_the_engine_sha(world):
    rc, out = world.run("--commit", world.sha_a)

    assert rc == 0, out
    assert world.default_b != world.default_a
    assert _blob(world.dest, "HEAD", "ext/ext.txt") == _blob(world.default_repo, world.default_a, "ext/ext.txt")
    assert _blob(world.dest, "HEAD", "ext/ext.txt") != _blob(world.default_repo, world.default_b, "ext/ext.txt")
