"""A percolate round publishes a named committed source ref (default HEAD), never the working tree."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_BIN = Path(__file__).resolve().parent.parent


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _BIN / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def round_mod():
    return _load("_pr_named_source_round", "percolate-round.py")


@pytest.fixture(scope="module")
def publish_mod():
    return _load("_pr_named_source_publish", "publish.py")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args],
        check=True, capture_output=True, text=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    _git(root, "init", "-q")
    (root / "mod.py").write_text("committed v1\n", encoding="utf-8")
    _git(root, "add", "mod.py")
    _git(root, "commit", "-q", "-m", "v1")
    first = _git(root, "rev-parse", "HEAD")
    (root / "mod.py").write_text("committed v2\n", encoding="utf-8")
    _git(root, "commit", "-q", "-am", "v2")
    (root / "mod.py").write_text("DIRTY half-written edit\n", encoding="utf-8")
    (root / "untracked.py").write_text("never committed\n", encoding="utf-8")
    return root, first


def test_default_head_ships_committed_content_not_the_dirty_tree(publish_mod, repo, monkeypatch):
    root, _first = repo
    monkeypatch.setattr(publish_mod, "_source_ref_override", "")
    monkeypatch.setattr(publish_mod, "_MATERIALIZED_REF_CACHE", {})
    pins: dict = {}
    sha = publish_mod._round_pin_source_sha(root, pins)
    shadow = publish_mod._git_materialize_ref(root, ref=sha)

    assert sha == _git(root, "rev-parse", "HEAD")
    assert (shadow / "mod.py").read_text(encoding="utf-8").replace("\r\n", "\n") == "committed v2\n"
    assert not (shadow / "untracked.py").exists()


def test_named_ref_ships_that_commit_and_the_stamp_names_it(publish_mod, round_mod, repo, monkeypatch):
    root, first = repo
    # --commit names claude-klabauter's own commit, so it pins only rows sourced from the engine's toplevel
    # (3c9871f946); the fixture repo stands in for that toplevel here.
    monkeypatch.setattr(
        publish_mod, "_engine_toplevel_key", lambda: str(Path(_git(root, "rev-parse", "--show-toplevel")))
    )
    monkeypatch.setattr(publish_mod, "_source_ref_override", first)
    monkeypatch.setattr(publish_mod, "_MATERIALIZED_REF_CACHE", {})
    sha = publish_mod._round_pin_source_sha(root, {})
    shadow = publish_mod._git_materialize_ref(root, ref=sha)

    assert sha == first
    assert (shadow / "mod.py").read_text(encoding="utf-8").replace("\r\n", "\n") == "committed v1\n"
    assert not (shadow / "untracked.py").exists()
    assert round_mod._source_sha_suffix(sha) == f" [source-head {first[:12]}]"


def test_round_pins_remote_tip_before_delegating(round_mod, repo, tmp_path, monkeypatch):
    origin, old = repo
    _git(origin, "branch", "release/stamp", old)
    clone = tmp_path / "clone"
    _git(tmp_path, "clone", str(origin), str(clone))
    # Make the advertised commit absent from the clone as well as absent from its tracking ref.
    (origin / "next.txt").write_text("next", encoding="utf-8")
    _git(origin, "add", "next.txt")
    _git(origin, "commit", "-qm", "advance source")
    current = _git(origin, "rev-parse", "HEAD")
    _git(origin, "branch", "-f", "release/stamp", current)
    assert _git(clone, "rev-parse", "origin/release/stamp") == old
    monkeypatch.setattr(round_mod, "_resolve_percolate_root", lambda _: str(tmp_path))
    monkeypatch.setattr(round_mod, "_branch0_gate", lambda *_: str(clone))
    monkeypatch.setattr(round_mod, "_commit_clone_module", lambda: SimpleNamespace(is_commit_clone=lambda _: False))
    delegated = []
    monkeypatch.setattr(round_mod, "_delegate_to_commit_clone", lambda argv, sha: delegated.append((argv, sha)) or 0)

    assert round_mod.main(["mirror", "--source-ref", "origin/release/stamp"]) == 0

    [(argv, sha)] = delegated
    assert sha == round_mod._build_parser().parse_args(argv).source_ref == current
    assert _git(clone, "rev-parse", f"{current}^{{commit}}") == current
    assert _git(clone, "rev-parse", "origin/release/stamp") == current

    _git(origin, "branch", "-D", "release/stamp")
    delegated.clear()
    assert round_mod.main(["mirror", "--source-ref=refs/remotes/origin/release/stamp"]) != 0
    assert not delegated


@pytest.fixture
def two_repos(tmp_path):
    """An engine stand-in repo and a non-engine target source repo; the target's first sha is no object in the engine."""
    engine = tmp_path / "engine"
    target = tmp_path / "target"
    for root, text in ((engine, "engine\n"), (target, "target v1\n")):
        root.mkdir()
        _git(root, "init", "-q")
        (root / "f.txt").write_text(text, encoding="utf-8")
        _git(root, "add", "f.txt")
        _git(root, "commit", "-q", "-m", "c1")
    target_first = _git(target, "rev-parse", "HEAD")
    (target / "f.txt").write_text("target v2\n", encoding="utf-8")
    _git(target, "commit", "-q", "-am", "c2")
    return engine, target, target_first


def _row(source_dir: Path):
    return SimpleNamespace(source_dir=source_dir)


def _engine_pinned_to_its_head(publish_mod, engine, monkeypatch):
    top = str(Path(_git(engine, "rev-parse", "--show-toplevel")))
    monkeypatch.setattr(publish_mod, "_engine_toplevel_key", lambda: top)
    monkeypatch.setattr(publish_mod, "_source_ref_override", _git(engine, "rev-parse", "HEAD"))
    return top


def test_source_ref_pins_the_non_engine_targets_repo_and_the_engine_stays_at_its_head(
    publish_mod, two_repos, monkeypatch
):
    engine, target, target_first = two_repos
    _engine_pinned_to_its_head(publish_mod, engine, monkeypatch)
    pins = publish_mod._resolve_target_source_ref([_row(target)], target_first)
    monkeypatch.setattr(publish_mod, "_SOURCE_REF_PINS", pins)
    round_pins: dict = {}

    assert publish_mod._round_pin_source_sha(target, round_pins) == target_first
    assert publish_mod._round_pin_source_sha(engine, round_pins) == _git(engine, "rev-parse", "HEAD")


def test_without_a_target_pin_a_foreign_root_resolves_head_not_the_ref(publish_mod, two_repos, monkeypatch):
    engine, target, target_first = two_repos
    _engine_pinned_to_its_head(publish_mod, engine, monkeypatch)
    monkeypatch.setattr(publish_mod, "_SOURCE_REF_PINS", {})

    assert publish_mod._round_pin_source_sha(target, {}) == _git(target, "rev-parse", "HEAD")


def test_a_source_ref_that_is_no_object_in_the_target_repo_refuses(publish_mod, two_repos):
    _engine, target, _first = two_repos
    with pytest.raises(RuntimeError, match="not a commit in the target source repo"):
        publish_mod._resolve_target_source_ref([_row(target)], "f" * 40)


def test_a_sha_of_another_repo_refuses_for_the_target(publish_mod, two_repos):
    engine, target, _first = two_repos
    with pytest.raises(RuntimeError):
        publish_mod._resolve_target_source_ref([_row(target)], _git(engine, "rev-parse", "HEAD"))


class _RecordingPublish:
    def __init__(self):
        self.override_seen = None

    def _round_pin_source_sha(self, root, pinned):
        self.override_seen = self._source_ref_override
        return "abc123"

    def _field7_declaration_check(self, pinned_sha=None):
        return True, "", None


def test_field7_preflight_pins_a_resolved_sha_of_the_checked_repo_never_the_raw_ref(
    round_mod, publish_mod, two_repos, monkeypatch
):
    engine, target, target_first = two_repos
    publish = _RecordingPublish()
    publish._resolve_round_commit = publish_mod._resolve_round_commit
    monkeypatch.setattr(round_mod, "_sibling_cli", lambda script: publish)
    monkeypatch.setattr(round_mod, "_live_repo_root", lambda: engine)

    round_mod._field7_preflight("row", target_first)
    assert publish.override_seen == ""

    engine_head = _git(engine, "rev-parse", "HEAD")
    round_mod._field7_preflight("row", engine_head[:10])
    assert publish.override_seen == engine_head


def test_a_claude_klabauter_only_ref_refuses_for_a_non_claude_klabauter_target(publish_mod, two_repos):
    engine, target, _first = two_repos
    engine_only = _git(engine, "rev-parse", "HEAD")
    with pytest.raises(RuntimeError, match="not a commit in the target source repo"):
        publish_mod._resolve_target_source_ref([_row(target)], engine_only, engine_holds=True)


def test_a_bad_ref_refuses_when_no_target_has_a_toplevel_unless_the_engine_holds_it(publish_mod, tmp_path):
    bare = tmp_path / "not-a-repo"
    bare.mkdir()
    with pytest.raises(RuntimeError, match="not a commit in the target source repo"):
        publish_mod._resolve_target_source_ref([_row(bare)], "f" * 40)
    assert publish_mod._resolve_target_source_ref([_row(bare)], "f" * 40, engine_holds=True) == {}


@pytest.fixture
def main_world(publish_mod, two_repos, monkeypatch):
    """`_main` driven in-process on one row sourced from the non-engine target repo."""
    engine, target, target_first = two_repos
    publish_mod._bootstrap_engine()
    monkeypatch.setattr(publish_mod, "_REPO_ROOT", engine)
    monkeypatch.setattr(publish_mod, "_resolve_percolate_root_and_rung", lambda override=None: (engine, "test"))
    monkeypatch.setattr(publish_mod, "_committed_portable_targets_file", lambda setup_dir: None)
    monkeypatch.setattr(publish_mod, "_load_rows", lambda *a, **k: ["row"])
    monkeypatch.setattr(publish_mod, "parse_target_row", lambda row: _row(target))
    monkeypatch.setattr(publish_mod, "_assert_klabauter_parity_group_ordering", lambda rows: False)
    for name, value in (
        ("_SOURCE_REF_PINS", {}),
        ("_source_ref_override", ""),
        ("_source_ref_explicit", False),
        ("_PERCOLATE_ROOT_OVERRIDE", None),
    ):
        monkeypatch.setattr(publish_mod, name, value)
    return engine, target, target_first


def test_main_refuses_a_ref_no_target_source_repo_holds_before_any_publish_work(publish_mod, main_world, capsys):
    assert publish_mod._main(["--dry-run", "--source-ref", "f" * 40]) == 1
    assert "not a commit in the target source repo" in capsys.readouterr().err


def test_main_refuses_a_claude_klabauter_only_ref_for_a_non_claude_klabauter_target(publish_mod, main_world, capsys):
    engine, _target, _first = main_world
    assert publish_mod._main(["--dry-run", "--source-ref", _git(engine, "rev-parse", "HEAD")]) == 1
    assert "not a commit in the target source repo" in capsys.readouterr().err


def test_main_admits_the_targets_own_ref_past_the_refusal(publish_mod, main_world, capsys):
    _engine, _target, first = main_world
    publish_mod._main(["--dry-run", "--source-ref", first])
    assert "not a commit in the target source repo" not in capsys.readouterr().err
    assert publish_mod._SOURCE_REF_PINS


def test_round_cli_accepts_source_ref_and_defaults_to_none(round_mod):
    parser = round_mod._build_parser()
    assert parser.parse_args(["row"]).source_ref is None
    assert parser.parse_args(["row", "--source-ref", "abc123"]).source_ref == "abc123"

