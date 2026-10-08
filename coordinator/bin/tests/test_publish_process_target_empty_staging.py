"""`process_target` staged-delta mode: staging into an empty directory over a
given path set (sparse source shadow, `only_paths` sync, staged engine scope).

Drives the real `process_target` and the real `publish_sync`, with the engine
phase dispatchers faked. `copytree`, `_git_materialize_ref` and
`_extract_git_archive` raise if reached.

Run: python -m pytest coordinator/bin/tests/test_publish_process_target_empty_staging.py -q
"""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent
_FILES = 3000
_NAMED = ("plug0/f0.txt", "plug1/f1.txt", "plug2/f2.txt")


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_empty_staging_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()


def _write(root: Path, rel: str, text: str = "x") -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _files(root: Path) -> "set[str]":
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


def _git_init(path: Path) -> None:
    (path / ".git").mkdir(parents=True, exist_ok=True)


class _Run:
    def __init__(self) -> None:
        self.scopes: "list[tuple[str, str]]" = []
        self.staging_at_post_rsync: "set[str]" = set()
        self.pre_rsync_calls = 0
        self.result = None


def _run_row(tmp_path, monkeypatch, *, only_paths, shadow_files, rename_pairs=(), dest_extra=()):
    source = tmp_path / "source"
    dest = tmp_path / "dest"
    shadow = tmp_path / "shadow"
    _git_init(dest)
    source.mkdir()
    shadow.mkdir()
    for i in range(_FILES):
        _write(source, f"plug{i % 30}/f{i}.txt", str(i))
        _write(dest, f"plug{i % 30}/f{i}.txt", str(i))
    for rel in dest_extra:
        _write(dest, rel, "dest-held")
    for rel in shadow_files:
        _write(shadow, rel, "fresh")

    target = publish.ResolvedTarget(name="empty-staging-row", mode="mirror", source_dir=source, dest_dir=dest)
    run = _Run()

    def forbidden(*_a, **_k):
        raise AssertionError("whole-tree materialization or copy reached")

    monkeypatch.setattr(publish, "_git_materialize_ref", forbidden)
    monkeypatch.setattr(publish, "_extract_git_archive", forbidden)
    monkeypatch.setattr(publish.shutil, "copytree", forbidden)

    monkeypatch.setattr(publish, "assert_dest_on_declared_ref", lambda *a, **k: True)
    monkeypatch.setattr(publish, "assert_dest_engine_root_viable", lambda *a, **k: True)
    monkeypatch.setattr(publish, "dispatch_standalone_guards", lambda *a, **k: None)
    monkeypatch.setattr(publish, "assert_authored_parity", lambda *a, **k: None)
    monkeypatch.setattr(publish, "_door_emission_is_reportable", lambda *a, **k: False)
    monkeypatch.setattr(publish, "emit_door_name_map_for_publish_row", lambda *a, **k: None)
    monkeypatch.setattr(
        publish,
        "_import_percolate_store_resolve_target",
        lambda: (lambda _store, _name: {"basename_rename": list(rename_pairs)}),
    )
    rewrite_basename = publish._import_percolate_rewrite_basename_module()
    monkeypatch.setattr(rewrite_basename, "rename_ledger_path", lambda _name: tmp_path / "ledger.json")

    def pre_rsync(*_a, **_k):
        run.pre_rsync_calls += 1

    def post_rsync(_ctx, _store, sync_target, _src, *, visited_sink=None, sync_changed_paths=None, scope="full"):
        run.scopes.append(("post_rsync", scope))
        run.staging_at_post_rsync = _files(sync_target.dest_dir)
        if visited_sink is not None:
            visited_sink.update(sync_target.dest_dir / rel for rel in run.staging_at_post_rsync)
        return None, None

    def pre_ci(*_a, scope="full", **_k):
        run.scopes.append(("pre_ci", scope))

    monkeypatch.setattr(publish, "dispatch_percolate_pre_rsync", pre_rsync)
    monkeypatch.setattr(publish, "dispatch_percolate_post_rsync", post_rsync)
    monkeypatch.setattr(publish, "dispatch_percolate_inject", lambda *a, **k: ())
    monkeypatch.setattr(publish, "dispatch_percolate_pre_ci", pre_ci)

    def gate_that_must_not_run(*_a, **_k):
        raise AssertionError("whole-tree pre-swap gate ran in the staged-delta mode")

    monkeypatch.setattr(publish, "dispatch_preswap_function_gate", gate_that_must_not_run)
    monkeypatch.setattr(publish, "dispatch_preswap_payload_parity_gate", gate_that_must_not_run)

    run.result = publish.process_target(
        target,
        tmp_path,
        publish.RunTotals(),
        identity_file_exists=True,
        identity=None,
        dry_run=False,
        engine_ctx=publish.PercolateEngineContext(engine_claude_klabauter=object(), store={}),
        percolate_store_path=tmp_path / "store.yaml",
        publish_sync_module=publish._import_publish_sync(tmp_path),
        published_files_sink=set(),
        source_shadow=shadow,
        only_paths=only_paths,
        out=io.StringIO(),
    )
    return run, dest


def test_only_paths_stages_exactly_the_named_outputs_into_an_empty_directory(tmp_path, monkeypatch):
    run, dest = _run_row(tmp_path, monkeypatch, only_paths=frozenset(_NAMED), shadow_files=_NAMED)

    result = run.result
    assert result is not None
    assert _files(result.staging_dir) == set(_NAMED)
    assert run.staging_at_post_rsync == set(_NAMED)
    assert result.staging_dir.parent == dest.parent
    assert run.pre_rsync_calls == 0
    assert run.scopes == [("post_rsync", "staged"), ("pre_ci", "staged")]
    assert result.row_changed_files == frozenset(_NAMED)
    publish._discard_publish_staging_dir(result.staging_dir)


def test_deleted_source_path_is_removed_under_its_renamed_name(tmp_path, monkeypatch):
    pairs = [{"src": "old.txt", "dst": "new.txt"}, {"src": "olddir/", "dst": "newdir/"}]
    deleted = ("plug0/old.txt", "olddir/inner/old.txt")
    renamed = {"plug0/new.txt", "newdir/inner/new.txt"}
    run, _dest = _run_row(
        tmp_path,
        monkeypatch,
        only_paths=frozenset((*_NAMED, *deleted)),
        shadow_files=_NAMED,
        rename_pairs=pairs,
        dest_extra=renamed,
    )

    result = run.result
    assert result is not None
    assert result.removed_dest_rels == renamed
    assert result.row_removed_files == renamed
    assert not (set(deleted) & result.removed_dest_rels)
    assert _files(result.staging_dir) == set(_NAMED)
    publish._discard_publish_staging_dir(result.staging_dir)


def test_deleted_source_path_not_held_by_the_destination_is_not_reported_removed(tmp_path, monkeypatch):
    run, _dest = _run_row(
        tmp_path,
        monkeypatch,
        only_paths=frozenset((*_NAMED, "plug0/gone.txt")),
        shadow_files=_NAMED,
    )

    assert run.result is not None
    assert run.result.removed_dest_rels == {"plug0/gone.txt"}
    assert run.result.row_removed_files == set()
    publish._discard_publish_staging_dir(run.result.staging_dir)


def test_sync_module_lacking_only_paths_is_reported_before_any_row_work(tmp_path):
    class _Lagging:
        @staticmethod
        def sync_mirror(src_dir, dst_dir, ignore, dry_run):
            raise AssertionError("must not be called")

        sync_flat_mirror = sync_mirror

    source = tmp_path / "source"
    source.mkdir()
    dest = tmp_path / "dest"
    _git_init(dest)
    target = publish.ResolvedTarget(name="r", mode="mirror", source_dir=source, dest_dir=dest)

    assert not publish._module_accepts_only_paths(_Lagging)
    with pytest.raises(publish.SyncModuleLacksOnlyPathsError) as raised:
        publish.process_target(
            target,
            tmp_path,
            publish.RunTotals(),
            identity_file_exists=True,
            identity=None,
            dry_run=False,
            engine_ctx=publish.PercolateEngineContext(engine_claude_klabauter=object(), store={}),
            publish_sync_module=_Lagging,
            source_shadow=tmp_path / "shadow",
            only_paths=frozenset({"a.txt"}),
            out=io.StringIO(),
        )
    assert raised.value.reason == publish.SYNC_MODULE_LACKS_ONLY_PATHS == "sync-module-lacks-only-paths"
    assert list(tmp_path.glob(".dest.publish-staging-*")) == []


def test_real_sync_module_accepts_only_paths(tmp_path):
    assert publish._module_accepts_only_paths(publish._import_publish_sync(tmp_path))


def test_only_paths_without_a_source_shadow_is_refused(tmp_path):
    target = publish.ResolvedTarget(
        name="r", mode="mirror", source_dir=tmp_path / "s", dest_dir=tmp_path / "d"
    )
    with pytest.raises(ValueError, match="source_shadow"):
        publish.process_target(
            target,
            tmp_path,
            publish.RunTotals(),
            identity_file_exists=True,
            identity=None,
            dry_run=False,
            engine_ctx=publish.PercolateEngineContext(engine_claude_klabauter=object(), store={}),
            only_paths=frozenset({"a.txt"}),
        )


def test_empty_staging_without_only_paths_does_not_seed_from_the_destination(tmp_path):
    dest = tmp_path / "dest"
    _write(dest, "dest_native.txt", "native")

    staging = publish._create_publish_staging_dir(dest)
    try:
        assert staging.parent == dest.parent
        assert _files(staging) == set()
        assert staging.resolve() in publish._MINTED_STAGING_DIRS
    finally:
        publish._discard_publish_staging_dir(staging)
