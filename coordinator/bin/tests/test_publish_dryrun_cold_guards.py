"""A cold `publish.py --dry-run` stages each row as a live round does, so the engine-phase guards pass.

`publish.main` and the real `process_target` run on a temp mirror with an engine row
(`coordinator_core`) and a `coordinator/bin` row. The pre-swap function gate is real; the post_rsync
registration-liveness guard is the engine's own `check_registration_liveness`. Both read the staging
tree the sync fills, which an inert dry-run sync left empty.

Run: python -m pytest coordinator/bin/tests/test_publish_dryrun_cold_guards.py -q
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
_SIG = "sig-0000000000000000000000000000000000000002"
_ENGINE_STRAY = "stray_engine.dat"
_BIN_STRAY = "lib/stray_bin.dat"

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
    spec = importlib.util.spec_from_file_location("publish_dryrun_cold_guards_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

from coordinator_core.percolate import engine as pct_engine  # noqa: E402
from coordinator_core.percolate.guards import check_registration_liveness  # noqa: E402


class _Engine:
    run_function_gate = staticmethod(pct_engine.run_function_gate)
    run_parse_sweep = staticmethod(pct_engine.run_parse_sweep)
    oss_shaped_subprocess_env = staticmethod(pct_engine.oss_shaped_subprocess_env)
    hermetic_gate_env = staticmethod(pct_engine.hermetic_gate_env)
    mktcache_gate_env = staticmethod(pct_engine.mktcache_gate_env)

    @staticmethod
    def resolve_target(store, name):
        raise KeyError(name)


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


_ENGINE_FILES = {
    "__init__.py": "",
    "data_root.py": "def data_root(dir_name):\n    return None\n",
    "ops/__init__.py": '_EAGER_OP_MODULES = [("coordinator_core.data_root", "x")]\n',
}
_BIN_FILES = {
    "lib/coordinator_registry.py": "KNOWN_TYPES = ()\n",
    "lib/coordinator_data_root.py": "x = 1\n",
}


class World:
    """A source repo holding an engine tree and a bin tree; the mirror tracks one stray under each."""

    def __init__(self, tmp_path: Path, monkeypatch, capsys):
        self.tmp = tmp_path
        self.capsys = capsys
        self.source = tmp_path / "source-repo"
        self.mirror = tmp_path / "mirror-repo"
        _init_repo(self.source, self_origin=False)
        _init_repo(self.mirror, self_origin=True)
        with (self.mirror / ".git" / "info" / "exclude").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(".percolate/\n")

        self.engine_src = self.source / "coordinator_core"
        self.bin_src = self.source / "coordinator" / "bin"
        for src, files, stray in (
            (self.engine_src, _ENGINE_FILES, _ENGINE_STRAY),
            (self.bin_src, _BIN_FILES, _BIN_STRAY),
        ):
            for rel, text in files.items():
                _write(src / rel, text)
            _write(src / stray, "stray\n")
            _write(src / ".percolate-ignore", f"{stray}\n")
        _commit_all(self.source, "source: seed")

        self.engine_dest = self.mirror / "coordinator_core"
        self.bin_dest = self.mirror / "coordinator" / "bin"
        for dest, files, stray in (
            (self.engine_dest, _ENGINE_FILES, _ENGINE_STRAY),
            (self.bin_dest, _BIN_FILES, _BIN_STRAY),
        ):
            for rel, text in files.items():
                _write(dest / rel, text)
            _write(dest / stray, "stray\n")
        self.seed_head = _commit_all(self.mirror, "mirror: seed")

        self._wire(monkeypatch)

    def _wire(self, monkeypatch) -> None:
        tmp = self.tmp
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp))
        publish._bootstrap_engine()
        monkeypatch.setattr(publish, "_REPO_ROOT", self.source)
        rows = [
            f"engine-row|mirror|{self.engine_src}|{self.engine_dest}",
            f"bin-row|mirror|{self.bin_src}|{self.bin_dest}",
        ]
        monkeypatch.setattr(publish, "_resolve_percolate_root_and_rung", lambda **kw: (tmp, "test-rung"))
        monkeypatch.setattr(publish, "load_targets", lambda setup_dir, target_filter=None, **_: rows)

        from percolate import publish_sync as real_publish_sync

        monkeypatch.setattr(publish, "_import_claude_klabauter_percolate", lambda: _Engine())
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

        monkeypatch.setattr(
            publish,
            "run_pre_sync_gates",
            lambda target, *a, **k: publish.GateResult(proceed=True, source_dir=target.source_dir),
        )
        monkeypatch.setattr(publish, "assert_dest_on_declared_ref", lambda *a, **k: True)
        monkeypatch.setattr(publish, "assert_dest_engine_root_viable", lambda *a, **k: True)
        monkeypatch.setattr(publish, "dispatch_standalone_guards", lambda *a, **k: None)
        monkeypatch.setattr(publish, "assert_authored_parity", lambda target: None)
        monkeypatch.setattr(publish, "dispatch_percolate_post_rsync", self._post_rsync)
        monkeypatch.setattr(publish, "dispatch_percolate_inject", lambda *a, **k: ())
        monkeypatch.setattr(publish, "dispatch_percolate_pre_ci", lambda *a, **k: None)
        monkeypatch.setattr(publish, "emit_door_name_map_for_publish_row", lambda *a, **k: None)
        monkeypatch.setattr(publish, "_door_emission_is_reportable", lambda target: False)

    def _post_rsync(self, engine_ctx, store_path, sync_target, effective_source_dir, *, visited_sink=None, **kwargs):
        if sync_target.name == "engine-row":
            result = check_registration_liveness(sync_target.dest_dir, {})
            if not result.ok:
                raise publish.EngineUnavailableError(f"{sync_target.name}: post_rsync guard failed: {result.message}")
        return None, None

    def run(self, *argv: str) -> "tuple[int, str]":
        rc = publish.main([*argv])
        out, err = self.capsys.readouterr()
        return rc, out + err

    def head_files(self) -> "set[str]":
        return set(_git(self.mirror, "ls-tree", "-r", "--name-only", "HEAD").split())


def _would_remove(out: str, row: str, verb: str) -> "list[str]":
    lines = [ln for ln in out.splitlines() if ln.startswith(f"  {row}: {verb} ") and "ignore-covered" in ln]
    assert len(lines) == 1, out
    return lines[0].split("tracks: ", 1)[1].split(", ")


def test_cold_dry_run_passes_both_engine_phase_guards_and_lists_every_rows_reap(tmp_path, monkeypatch, capsys):
    world = World(tmp_path, monkeypatch, capsys)

    rc, out = world.run("--dry-run")

    assert rc == 0, out
    assert "pre-swap function gate FAILED" not in out
    assert "ops/__init__.py not found" not in out
    assert _would_remove(out, "engine-row", "would remove") == [f"coordinator_core/{_ENGINE_STRAY}"]
    assert _would_remove(out, "bin-row", "would remove") == [f"coordinator/bin/{_BIN_STRAY}"]
    assert _git(world.mirror, "rev-parse", "HEAD").strip() == world.seed_head
    assert (world.engine_dest / _ENGINE_STRAY).exists()
    assert (world.bin_dest / _BIN_STRAY).exists()


def test_live_round_removes_exactly_the_paths_the_cold_dry_run_listed(tmp_path, monkeypatch, capsys):
    world = World(tmp_path, monkeypatch, capsys)
    _rc, dry_out = world.run("--dry-run")
    listed = _would_remove(dry_out, "engine-row", "would remove") + _would_remove(dry_out, "bin-row", "would remove")

    rc, out = world.run()

    assert rc == 0, out
    assert _would_remove(out, "engine-row", "removing") + _would_remove(out, "bin-row", "removing") == listed
    assert world.head_files().isdisjoint(listed)
    assert not any((world.mirror / rel).exists() for rel in listed)


def test_real_round_still_refuses_an_engine_row_whose_staging_lacks_the_ops_registry(tmp_path, monkeypatch, capsys):
    monkeypatch.delitem(_ENGINE_FILES, "ops/__init__.py")
    world = World(tmp_path, monkeypatch, capsys)

    rc, out = world.run()

    assert rc != 0
    assert "ops/__init__.py not found" in out
