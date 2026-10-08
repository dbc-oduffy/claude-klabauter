"""The diff-scaled publish round, end to end against real source and destination repos.

`process_target` is the one fake: it stages what C8's staging does (the shadow's changed files on
a warm row, the whole source dir on a cold one). Planning, union, gates' wiring, landing, trailers
and bookkeeping are the real code.

Run: python -m pytest coordinator/bin/tests/test_publish_diff_scaled_round.py -q
"""

from __future__ import annotations

import importlib.util
import os
import shutil
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
    spec = importlib.util.spec_from_file_location("publish_diff_scaled_round_under_test", _BIN_DIR / "publish.py")
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
    def __init__(self, tmp_path: Path, monkeypatch, capsys, *, files: int = 4):
        self.tmp = tmp_path
        self.capsys = capsys
        self.src_repo = tmp_path / "src-repo"
        self.dest = tmp_path / "dest-repo"
        _init_repo(self.src_repo, self_origin=False)
        _init_repo(self.dest, self_origin=True)
        with (self.dest / ".git" / "info" / "exclude").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(".percolate/\n")
        self.plugin = self.src_repo / "plugin"
        for i in range(files):
            _write(self.plugin / f"f{i}.txt", f"v0 {i}\n")
        _write(self.src_repo / "other" / "unpublished.txt", "v0\n")
        self.src_head = _commit_all(self.src_repo, "src: seed")
        self.calls: list = []
        self.signature = _SIG
        self._wire(monkeypatch)

    def _wire(self, monkeypatch) -> None:
        tmp = self.tmp
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp))
        row = f"row-a|mirror|{self.plugin}|{self.dest / 'out'}"
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
        monkeypatch.setattr(publish, "compute_delta_invalidation_signature", lambda store_path, ctx: self.signature)
        for gate in _GATES:
            monkeypatch.setattr(publish, gate, lambda *a, **k: True)

        def fake_process_target(target, setup_dir, totals, **kwargs):
            only_paths = kwargs.get("only_paths")
            shadow = kwargs.get("source_shadow") or {}
            self.calls.append(("process_target", None if only_paths is None else frozenset(only_paths)))
            src = Path(shadow.get(target.source_dir, target.source_dir))
            staging = Path(tempfile.mkdtemp(prefix=".publish-staging-", dir=str(target.dest_dir.parent)))
            if only_paths is None:
                rels = {p.relative_to(src).as_posix() for p in src.rglob("*") if p.is_file()}
            else:
                rels = {r for r in only_paths if (src / r).is_file()}
            for rel in rels:
                _write(staging / rel, (src / rel).read_bytes().decode("utf-8"))
            removed = set() if only_paths is None else {r for r in only_paths if not (src / r).exists()}
            totals.processed += 1
            return publish.StagedRowResult(
                staging_dir=staging,
                row_visited={Path(r) for r in rels},
                row_changed_files=set(rels),
                row_removed_files=removed,
                row_published_files={Path(r) for r in rels},
                report_text="",
                synced=len(rels),
                deleted=len(removed),
            )

        monkeypatch.setattr(publish, "process_target", fake_process_target)

        def forbidden(*args, **kwargs):
            self.calls.append(("throwaway-or-copytree", args))
            raise AssertionError("the diff-scaled round must not build a throwaway or copy a tree")

        monkeypatch.setattr(publish, "build_throwaway_tree", forbidden, raising=False)
        monkeypatch.setattr(shutil, "copytree", forbidden)

    def src_change(self, files: "dict[str, str]", *, under: str = "plugin") -> str:
        for name, text in files.items():
            _write(self.src_repo / under / name, text)
        self.src_head = _commit_all(self.src_repo, "src: change")
        return self.src_head

    def run(self, *argv: str) -> "tuple[int, str]":
        rc = publish.main(["row-a", *argv])
        out, err = self.capsys.readouterr()
        return rc, out + err

    def dest_head_message(self) -> str:
        return _git(self.dest, "log", "-1", "--format=%B")

    def dest_commits(self) -> int:
        return int(_git(self.dest, "rev-list", "--count", "HEAD").strip())

    def dest_file(self, rel: str) -> str:
        return _git(self.dest, "show", f"HEAD:{rel}")

    def published_calls(self) -> list:
        return [c for c in self.calls if c[0] == "process_target"]


@pytest.fixture
def world(tmp_path, monkeypatch, capsys):
    return World(tmp_path, monkeypatch, capsys)


def _trailer(message: str, key: str) -> "str | None":
    for line in message.splitlines():
        if line.startswith(f"{key}: "):
            return line.split(": ", 1)[1].strip()
    return None


def test_first_round_is_cold_and_stamps_the_destination(world):
    rc, out = world.run()

    assert rc == 0, out
    assert "cold: foreign-commit" in out
    assert world.published_calls() == [("process_target", None)]
    assert world.dest_file("out/f0.txt") == "v0 0\n"
    message = world.dest_head_message()
    assert _trailer(message, "Percolate-Source-Head") == world.src_head
    assert _trailer(message, "Percolate-Signature") == _SIG
    assert _trailer(message, "Percolate-Round")
    assert not [c for c in world.calls if c[0] == "throwaway-or-copytree"]


def test_next_round_is_warm_and_lands_only_the_changed_path(world):
    world.run()
    new_head = world.src_change({"f1.txt": "v1 1\n"})
    before = world.dest_commits()

    rc, out = world.run()

    assert rc == 0, out
    assert "warm: base" in out and "1 source paths" in out
    assert world.published_calls()[-1] == ("process_target", frozenset({"f1.txt"}))
    assert world.dest_commits() == before + 1
    assert world.dest_file("out/f1.txt") == "v1 1\n"
    assert world.dest_file("out/f0.txt") == "v0 0\n"
    assert _trailer(world.dest_head_message(), "Percolate-Source-Head") == new_head
    assert _trailer(world.dest_head_message(), "Percolate-Signature") == _SIG
    assert _git(world.dest, "status", "--porcelain").strip() == ""
    assert not [c for c in world.calls if c[0] == "throwaway-or-copytree"]


def test_warm_deletion_removes_the_destination_file(world):
    world.run()
    (world.plugin / "f2.txt").unlink()
    world.src_head = _commit_all(world.src_repo, "src: delete f2")

    rc, out = world.run()

    assert rc == 0, out
    assert "warm: base" in out
    assert "out/f2.txt" not in _git(world.dest, "ls-tree", "-r", "--name-only", "HEAD").split()
    assert not (world.dest / "out" / "f2.txt").exists()


def test_second_warm_round_with_no_change_creates_no_commit(world):
    world.run()
    world.src_change({"f1.txt": "v1 1\n"})
    world.run()
    before = world.dest_commits()
    head = _git(world.dest, "rev-parse", "HEAD")
    calls_before = len(world.published_calls())

    rc, out = world.run()

    assert rc == 0, out
    assert "warm: base" in out and "0 source paths" in out
    assert world.dest_commits() == before
    assert _git(world.dest, "rev-parse", "HEAD") == head
    assert len(world.published_calls()) == calls_before, "an empty-delta row stages nothing"
    assert _git(world.dest, "status", "--porcelain").strip() == ""


def test_head_advance_on_unpublished_paths_creates_no_commit(world):
    world.run()
    before = world.dest_commits()
    world.src_change({"unpublished.txt": "v1\n"}, under="other")

    rc, out = world.run()

    assert rc == 0, out
    assert "warm: base" in out and "0 source paths" in out
    assert world.dest_commits() == before


def test_foreign_commit_in_the_destination_goes_cold_and_says_why(world):
    world.run()
    _write(world.dest / "hand.txt", "by hand\n")
    _commit_all(world.dest, "hand edit")
    world.src_change({"f1.txt": "v1 1\n"})

    rc, out = world.run()

    assert rc == 0, out
    assert "cold: foreign-commit" in out
    assert world.published_calls()[-1] == ("process_target", None)


def test_transform_rule_path_change_goes_cold(world):
    world.run()
    world.src_change({"x.py": "# hook\n"}, under="setup/percolate-hooks")

    rc, out = world.run()

    assert rc == 0, out
    assert "cold: rule-path-changed setup/percolate-hooks/x.py" in out
    assert world.published_calls()[-1] == ("process_target", None)


def test_signature_change_goes_cold(world):
    world.run()
    world.signature = "sig-0000000000000000000000000000000000000002"
    world.src_change({"f1.txt": "v1 1\n"})

    rc, out = world.run()

    assert rc == 0, out
    assert "cold: signature-changed" in out


def test_full_sweep_forces_cold(world):
    world.run()
    world.src_change({"f1.txt": "v1 1\n"})

    rc, out = world.run("--full-sweep")

    assert rc == 0, out
    assert "cold: requested" in out
    assert world.published_calls()[-1] == ("process_target", None)


def test_lost_landing_leaves_the_destination_clean_at_its_prior_head(world, monkeypatch):
    world.run()
    world.src_change({"f1.txt": "v1 1\n"})
    head = _git(world.dest, "rev-parse", "HEAD")
    publish._bootstrap_engine()
    from percolate import diff_commit

    def refuse(*args, **kwargs):
        raise RuntimeError("simulated lost compare-and-swap")

    monkeypatch.setattr(diff_commit._gcommit, "commit_paths", refuse)

    rc, out = world.run()

    assert rc != 0
    assert "landing row-a failed" in out
    assert _git(world.dest, "rev-parse", "HEAD") == head
    assert _git(world.dest, "status", "--porcelain").strip() == ""
    assert world.dest_file("out/f1.txt") == "v0 1\n"


def test_failing_gate_lands_nothing(world, monkeypatch):
    world.run()
    world.src_change({"f1.txt": "v1 1\n"})
    head = _git(world.dest, "rev-parse", "HEAD")
    monkeypatch.setattr(publish, "dispatch_end_of_run_argv_parity_gate", lambda *a, **k: False)

    rc, out = world.run("--full-sweep")

    assert rc != 0
    assert _git(world.dest, "rev-parse", "HEAD") == head
    assert _git(world.dest, "status", "--porcelain").strip() == ""


def test_no_commit_flag_writes_the_worktree_and_stamps_nothing(world):
    world.run()
    world.src_change({"f1.txt": "v1 1\n"})
    head = _git(world.dest, "rev-parse", "HEAD")

    rc, out = world.run("--no-commit")

    assert rc == 0, out
    assert _git(world.dest, "rev-parse", "HEAD") == head
    assert (world.dest / "out" / "f1.txt").read_text(encoding="utf-8") == "v1 1\n"


class _Spawns:
    """Counts child processes and the `os.scandir` calls made while a round runs."""

    def __init__(self, monkeypatch):
        self.argvs: list = []
        self.scandirs = 0
        self._patch = monkeypatch.context()

    def __enter__(self):
        original_init = subprocess.Popen.__init__
        original_scandir = os.scandir
        spawns = self

        def counting_init(popen, args, *a, **k):
            spawns.argvs.append(args)
            return original_init(popen, args, *a, **k)

        def counting_scandir(*a, **k):
            spawns.scandirs += 1
            return original_scandir(*a, **k)

        patch = self._patch.__enter__()
        patch.setattr(subprocess.Popen, "__init__", counting_init)
        patch.setattr(os, "scandir", counting_scandir)
        return self

    def __exit__(self, *exc):
        return self._patch.__exit__(*exc)

    @property
    def count(self) -> int:
        return len(self.argvs)


def test_warm_round_cost_does_not_scale_with_the_destination_or_the_delta_size(tmp_path, monkeypatch, capsys):
    w = World(tmp_path, monkeypatch, capsys, files=3000)
    rc, out = w.run()
    assert rc == 0, out

    def warm_round(n_files: int, tag: str):
        w.src_change({f"f{i}.txt": f"{tag} {i}\n" for i in range(n_files)})
        with _Spawns(monkeypatch) as spawns:
            rc, out = w.run()
        assert rc == 0, out
        assert "warm: base" in out and f"{n_files} source paths" in out
        return spawns

    small = warm_round(5, "a")
    large = warm_round(50, "b")

    assert small.count == large.count, (small.argvs, large.argvs)
    for spawns in (small, large):
        for argv in spawns.argvs:
            words = [str(w) for w in (argv if isinstance(argv, (list, tuple)) else [argv])]
            assert "pytest" not in words and not any(Path(w).name.startswith("pytest") for w in words), argv
        assert spawns.scandirs < 400, spawns.scandirs
    assert _trailer(w.dest_head_message(), "Percolate-Source-Head") == w.src_head
    assert len(_git(w.dest, "ls-tree", "-r", "--name-only", "HEAD").split()) >= 3000
