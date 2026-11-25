"""A publish round refuses before syncing any row when the version surfaces disagree, naming the lagging surface.

`process_target` is the one fake (it stages the source bytes); the version gate child, the preflight,
the lock, refresh and landing are the real code driven through `publish.main`.

Run: python -m pytest coordinator/bin/tests/test_publish_version_surface_preflight.py -q
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
    spec = importlib.util.spec_from_file_location("publish_version_preflight_under_test", _BIN_DIR / "publish.py")
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


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))


def _plugin_json(version: str) -> str:
    return f'{{\n  "name": "coordinator-claude",\n  "version": "{version}"\n}}\n'


def _marketplace_json(version: str) -> str:
    return (
        '{\n  "name": "coordinator-claude",\n  "metadata": {\n'
        f'    "version": "{version}"\n  }},\n  "plugins": []\n}}\n'
    )


def _changelog(version: str) -> str:
    return f"# Changelog\n\n## [Unreleased]\n\n## [{version}] - 2026-11-25\n\n- entry\n"


class World:
    def __init__(self, tmp_path: Path, monkeypatch, capsys, *, changelog_version: str):
        self.tmp = tmp_path
        self.capsys = capsys
        self.src_repo = tmp_path / "src-repo"
        self.engine_repo = tmp_path / "claude-klabauter"
        self.dest = tmp_path / "dest-repo"
        _init_repo(self.src_repo, self_origin=False)
        _init_repo(self.engine_repo, self_origin=False)
        _init_repo(self.dest, self_origin=True)
        with (self.dest / ".git" / "info" / "exclude").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(".percolate/\n")
        self.src = self.src_repo / "plugin"
        _write(self.src / ".claude-plugin" / "plugin.json", _plugin_json("4.6.30"))
        _write(self.src / ".claude-plugin" / "marketplace.json", _marketplace_json("4.6.30"))
        _write(self.src / "CHANGELOG.md", _changelog(changelog_version))
        _git(self.src_repo, "add", "-A")
        _git(self.src_repo, "commit", "-q", "-m", "src: seed")
        _write(self.dest / "README.md", "mirror\n")
        _git(self.dest, "add", "-A")
        _git(self.dest, "commit", "-q", "-m", "mirror: seed")
        self.dest_head = _git(self.dest, "rev-parse", "HEAD").strip()
        self.process_calls = 0
        self._wire(monkeypatch)

    def _wire(self, monkeypatch) -> None:
        tmp = self.tmp
        monkeypatch.setenv("COORDINATOR_SETTINGS_HOME", str(tmp))
        monkeypatch.setenv("COORDINATOR_BIN", str(_BIN_DIR))
        monkeypatch.delenv("COORDINATOR_OVERRIDE_VERSION_CONSISTENCY", raising=False)
        publish._bootstrap_engine()
        from percolate import publish_sync as real_publish_sync

        monkeypatch.setattr(publish, "_REPO_ROOT", self.engine_repo)
        row = f"row-a|mirror|{self.src}|{self.dest}|||"
        monkeypatch.setattr(publish, "_resolve_percolate_root_and_rung", lambda **kw: (tmp, "test-rung"))
        monkeypatch.setattr(publish, "load_targets", lambda setup_dir, target_filter=None, **_: [row])

        class _FakeClaudeKlabauter:
            def resolve_target(self, store, name):
                raise KeyError(name)

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
        self.process_calls += 1
        payload = {p.relative_to(self.src).as_posix(): p.read_bytes() for p in self.src.rglob("*") if p.is_file()}
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

    def run(self) -> "tuple[int, str, str]":
        rc = publish.main(["row-a"])
        out, err = self.capsys.readouterr()
        return rc, out, err


def test_lagging_changelog_refuses_the_round_before_any_row_syncs(tmp_path, monkeypatch, capsys):
    world = World(tmp_path, monkeypatch, capsys, changelog_version="4.6.29")

    rc, out, err = world.run()

    assert rc != 0, out + err
    assert world.process_calls == 0
    assert _git(world.dest, "rev-parse", "HEAD").strip() == world.dest_head
    assert _git(world.dest, "status", "--porcelain").strip() == ""
    assert "CHANGELOG (latest) 4.6.29 lags plugin.json 4.6.30" in err
    assert "Round refused before any row synced" in err
    assert "not bumped" not in out + err
    assert "version-stamp" not in out + err


def test_agreeing_surfaces_round_runs_as_before(tmp_path, monkeypatch, capsys):
    world = World(tmp_path, monkeypatch, capsys, changelog_version="4.6.30")

    rc, out, err = world.run()

    assert rc == 0, out + err
    assert world.process_calls == 1
    assert "Round refused" not in err


def test_override_env_still_lets_a_disagreeing_round_through(tmp_path, monkeypatch, capsys):
    world = World(tmp_path, monkeypatch, capsys, changelog_version="4.6.29")
    monkeypatch.setenv("COORDINATOR_OVERRIDE_VERSION_CONSISTENCY", "1")

    rc, out, err = world.run()

    assert world.process_calls == 1, out + err
    assert "Round refused" not in err


_GATE_REPORT = (
    "check-version-consistency: VERSION SURFACES DISAGREE\n"
    "  plugin.json      : 4.6.30   (a)\n"
    "  marketplace.json : {market}   (b)\n"
    "  CHANGELOG (latest): {changelog}   (c)\n"
)


def test_description_names_each_lagging_and_leading_surface():
    text = _GATE_REPORT.format(market="4.6.31", changelog="4.6.29")

    described = publish.describe_version_disagreement(text)

    assert "marketplace.json 4.6.31 is ahead of plugin.json 4.6.30" in described
    assert "CHANGELOG (latest) 4.6.29 lags plugin.json 4.6.30" in described


def test_description_skips_a_surface_that_matches_the_ssot():
    text = _GATE_REPORT.format(market="4.6.30", changelog="4.6.29")

    described = publish.describe_version_disagreement(text)

    assert "marketplace.json" not in described
    assert "CHANGELOG (latest) 4.6.29 lags" in described
