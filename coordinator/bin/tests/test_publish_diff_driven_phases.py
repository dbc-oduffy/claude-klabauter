"""Seed, report and surviving-write legs of a cold publish row work on the source delta, not the tree."""

from __future__ import annotations

import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location("publish_diff_driven_phases_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()
publish._bootstrap_engine()

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        capture_output=True, check=True, text=True, creationflags=_NO_WINDOW,
    )
    return result.stdout.strip()


def _commit(repo: Path, files: "dict[str, str | None]", message: str) -> str:
    for rel, text in files.items():
        path = repo / rel
        if text is None:
            path.unlink()
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)
    return _git(repo, "rev-parse", "HEAD")


def _repo(path: Path) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q")
    return path


def _stage(root: Path, files: "dict[str, str]") -> None:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(text.encode("utf-8"))


_FILES = {f"pkg/m{i:03d}.py": f"x = {i}\n" for i in range(200)}


def test_report_and_surviving_writes_read_only_the_delta(tmp_path, monkeypatch):
    dest = _repo(tmp_path / "dest")
    _commit(dest, _FILES, "base")
    staging = tmp_path / "staging"
    _stage(staging, _FILES)
    manifest = {}
    for rel, text in _FILES.items():
        st = (staging / rel).stat()
        manifest[rel] = [st.st_size, st.st_mtime_ns, publish._GIT_FILE_MODE, publish._blob_sha1(text.encode()), ""]
    delta = {"pkg/m001.py": "x = 'changed'\n", "pkg/m002.py": "x = 'also'\n", "pkg/new.py": "y = 1\n"}
    _stage(staging, delta)
    for rel in delta:
        manifest.pop(rel, None)

    compared: "list[Path]" = []
    real_differ = publish.bytes_differ
    monkeypatch.setattr(publish, "bytes_differ", lambda a, b: compared.append(a) or real_differ(a, b))
    buf = io.StringIO()
    changed, _removed = publish._report_staged_diff(
        staging, dest, frozenset(), publish.RunTotals(), out=buf, seed_manifest=manifest
    )
    assert changed == frozenset({"pkg/m001.py", "pkg/m002.py", "pkg/new.py"}), buf.getvalue()
    assert len(compared) <= len(delta)

    reads: "list[Path]" = []
    real_read = publish._read_staged_entry
    monkeypatch.setattr(publish, "_read_staged_entry", lambda p: reads.append(p) or real_read(p))
    entries = {rel: staging / rel for rel in _FILES if (staging / rel).is_file()}
    entries["pkg/new.py"] = staging / "pkg/new.py"
    trusted = {rel: (ent[2], ent[3]) for rel, ent in manifest.items()}
    survivors, dels = publish._surviving_writes(dest, entries, [], None, None, trusted)
    assert set(survivors) == set(delta) and dels == []
    assert {p.relative_to(staging).as_posix() for p in reads} == set(delta)


def test_a_trusted_entry_that_differs_from_head_is_still_a_write(tmp_path):
    dest = _repo(tmp_path / "dest")
    _commit(dest, {"a.py": "old\n"}, "base")
    staging = tmp_path / "staging"
    _stage(staging, {"a.py": "new\n"})
    st = (staging / "a.py").stat()
    ent = [st.st_size, st.st_mtime_ns, publish._GIT_FILE_MODE, publish._blob_sha1(b"new\n"), ""]
    survivors, _ = publish._surviving_writes(
        dest, {"a.py": staging / "a.py"}, [], None, None, {"a.py": (ent[2], ent[3])}
    )
    assert survivors["a.py"].data == b"new\n"


def test_a_destination_file_edited_or_missing_in_the_worktree_is_reported(tmp_path):
    dest = _repo(tmp_path / "dest")
    _commit(dest, {"a.py": "same\n", "b.py": "same\n", "c.py": "same\n"}, "base")
    staging = tmp_path / "staging"
    _stage(staging, {"a.py": "same\n", "b.py": "same\n", "c.py": "same\n"})
    manifest = {}
    for rel in ("a.py", "b.py", "c.py"):
        st = (staging / rel).stat()
        manifest[rel] = [st.st_size, st.st_mtime_ns, publish._GIT_FILE_MODE, publish._blob_sha1(b"same\n"), ""]
    (dest / "b.py").write_bytes(b"locally edited\n")
    (dest / "c.py").unlink()
    buf = io.StringIO()
    changed, _ = publish._report_staged_diff(
        staging, dest, frozenset(), publish.RunTotals(), out=buf, seed_manifest=manifest
    )
    assert changed == frozenset({"b.py", "c.py"}), buf.getvalue()


def test_a_same_size_local_edit_of_a_destination_file_is_reported(tmp_path):
    dest = _repo(tmp_path / "dest")
    _commit(dest, {"a.py": "same\n", "b.py": "same\n"}, "base")
    staging = tmp_path / "staging"
    _stage(staging, {"a.py": "same\n", "b.py": "same\n"})
    manifest = {}
    for rel in ("a.py", "b.py"):
        st = (staging / rel).stat()
        manifest[rel] = [st.st_size, st.st_mtime_ns, publish._GIT_FILE_MODE, publish._blob_sha1(b"same\n"), ""]
    (dest / "b.py").write_bytes(b"SAME\n")
    buf = io.StringIO()
    changed, _ = publish._report_staged_diff(
        staging, dest, frozenset(), publish.RunTotals(), out=buf, seed_manifest=manifest
    )
    assert changed == frozenset({"b.py"}), buf.getvalue()


def test_an_unmatched_stat_is_not_trusted(tmp_path):
    staging = tmp_path / "staging"
    _stage(staging, {"a.py": "abc\n"})
    st = (staging / "a.py").stat()
    assert publish._seed_stat_matches(staging / "a.py", [st.st_size, st.st_mtime_ns])
    assert not publish._seed_stat_matches(staging / "a.py", [st.st_size + 1, st.st_mtime_ns])
    assert not publish._seed_stat_matches(staging / "gone.py", [0, 0])


@pytest.fixture
def seeded_world(tmp_path, monkeypatch):
    src = _repo(tmp_path / "src")
    base_files = {f"pkg/f{i:02d}.py": f"v = {i}\n" for i in range(20)}
    sha1 = _commit(src, base_files, "one")
    dest = _repo(tmp_path / "dest")
    target = SimpleNamespace(
        name="row", dest_dir=dest / "pkg", source_dir=src / "pkg", mode="mirror", source_map="", allowlist=""
    )
    pinned = {"sha": sha1}
    monkeypatch.setattr(publish, "_seed_eligible", lambda *a, **k: "")
    monkeypatch.setattr(publish, "_dest_repo_root", lambda _d: dest)
    monkeypatch.setattr(publish, "_resolve_show_toplevel", lambda _p: str(src))
    monkeypatch.setattr(publish, "_round_pin_source_sha", lambda *a, **k: pinned["sha"])
    monkeypatch.setattr(publish, "_row_rename_pairs", lambda *a, **k: ([], []))
    return SimpleNamespace(src=src, dest=dest, target=target, pinned=pinned, sha1=sha1, base=base_files, tmp=tmp_path)


def _fake_delta(monkeypatch, base, head, changed, deleted):
    from percolate import source_delta as module

    monkeypatch.setattr(
        module,
        "source_delta",
        lambda *a, **k: SimpleNamespace(
            base_sha=base, head_sha=head, changed={p: "0" * 40 for p in changed}, deleted=frozenset(deleted),
            unchanged_blob=lambda _r: None,
        ),
    )


def _snapshot(world, staging):
    manifest, why = publish._snapshot_staging_seed(
        world.target, staging, signature="sig", round_pinned_shas={}, carry=None, renamed_dsts=frozenset()
    )
    assert why == "" and manifest is not None
    return manifest


def _seed(world, staging, sig="sig"):
    return publish._seed_staging_from_cache(
        world.target, staging, engine_ctx=None, signature=sig, round_pinned_shas={},
        publish_sync_module=object(), dest_prefix="pkg",
    )


def test_seed_links_unchanged_paths_and_hands_the_sweep_known_inputs(seeded_world, monkeypatch):
    w = seeded_world
    first = w.tmp / "staging1"
    _stage(first, {rel[len("pkg/"):]: text for rel, text in w.base.items()})
    manifest = _snapshot(w, first)
    assert manifest["f03.py"][4] == publish._blob_sha1(b"v = 3\n")

    sha2 = _commit(w.src, {"pkg/f01.py": "v = 'new'\n", "pkg/f02.py": None, "pkg/f99.py": "v = 99\n"}, "two")
    w.pinned["sha"] = sha2
    _fake_delta(monkeypatch, w.sha1, sha2, {"pkg/f01.py", "pkg/f99.py"}, {"pkg/f02.py"})
    second = w.tmp / "staging2"
    second.mkdir()
    seed, why = _seed(w, second)
    assert why == "" and seed is not None
    assert seed.only_paths == frozenset({"f01.py", "f02.py", "f99.py"})
    assert not (second / "f01.py").exists() and not (second / "f02.py").exists()
    assert (second / "f03.py").read_bytes() == b"v = 3\n"
    assert seed.known_inputs["pkg/f03.py"] == publish._blob_sha1(b"v = 3\n")
    assert not {"pkg/f01.py", "pkg/f02.py", "pkg/f99.py"} & set(seed.known_inputs)
    cached = next((w.dest.parent / f".{w.dest.name}.publish-seed").glob("row/*/tree/f03.py"))
    assert os.path.samefile(cached, second / "f03.py")


def test_a_crashed_snapshot_tmp_dir_is_reaped_by_the_next_snapshot(seeded_world):
    w = seeded_world
    first = w.tmp / "staging1"
    _stage(first, {rel[len("pkg/"):]: text for rel, text in w.base.items()})
    _snapshot(w, first)
    row_dir = next((w.dest.parent / f".{w.dest.name}.publish-seed").glob("row"))
    orphan = row_dir / f"{'a' * 40}.tmp-999999"
    (orphan / "tree").mkdir(parents=True)
    _snapshot(w, first)
    assert not orphan.exists()


def test_seed_is_refused_when_the_transform_identity_or_a_rule_path_changed(seeded_world, monkeypatch):
    w = seeded_world
    first = w.tmp / "staging1"
    _stage(first, {rel[len("pkg/"):]: text for rel, text in w.base.items()})
    _snapshot(w, first)
    sha2 = _commit(w.src, {"pkg/f01.py": "v = 'new'\n"}, "two")
    w.pinned["sha"] = sha2
    _fake_delta(monkeypatch, w.sha1, sha2, {"pkg/f01.py"}, set())
    changed_sig = w.tmp / "staging2"
    changed_sig.mkdir()
    seed, why = _seed(w, changed_sig, sig="other")
    assert seed is None and "identity" in why and not any(changed_sig.iterdir())
    _fake_delta(monkeypatch, w.sha1, sha2, {"pkg/.percolate-ignore"}, set())
    rule = w.tmp / "staging3"
    rule.mkdir()
    seed, why = _seed(w, rule)
    assert seed is None and "rule-path" in why and not any(rule.iterdir())
    _fake_delta(monkeypatch, "f" * 40, sha2, {"pkg/f01.py"}, set())
    nocache = w.tmp / "staging4"
    nocache.mkdir()
    seed, why = _seed(w, nocache)
    assert seed is None and "no seed cached" in why


def test_a_cache_file_rewritten_in_place_is_resynced_from_source_not_trusted(seeded_world, monkeypatch):
    w = seeded_world
    first = w.tmp / "staging1"
    _stage(first, {rel[len("pkg/"):]: text for rel, text in w.base.items()})
    _snapshot(w, first)
    with open(first / "f05.py", "wb") as handle:  # a later phase writing through the shared inode
        handle.write(b"injected\n")
    sha2 = _commit(w.src, {"pkg/f01.py": "v = 'new'\n"}, "two")
    w.pinned["sha"] = sha2
    _fake_delta(monkeypatch, w.sha1, sha2, {"pkg/f01.py"}, set())
    second = w.tmp / "staging2"
    second.mkdir()
    seed, why = _seed(w, second)
    assert why == "" and seed is not None
    assert "f05.py" in seed.only_paths and not (second / "f05.py").exists()
    assert "pkg/f05.py" not in seed.known_inputs


def test_post_rsync_forwards_known_inputs_to_the_engine(monkeypatch, tmp_path):
    seen: "dict[str, object]" = {}

    def run_percolate(*args, **kwargs):
        seen.update(kwargs)
        raise RuntimeError("stop after capture")

    engine = SimpleNamespace(resolve_target=lambda _s, _n: {}, run_percolate=run_percolate)
    ctx = publish.PercolateEngineContext(engine_claude_klabauter=engine, store={}) if hasattr(
        publish, "PercolateEngineContext"
    ) else None
    monkeypatch.setattr(publish, "_compute_effective_source_count", lambda *a, **k: 1)
    target = SimpleNamespace(name="row", dest_dir=tmp_path / "staging")
    with pytest.raises(publish.EngineUnavailableError):
        publish.dispatch_percolate_post_rsync(
            ctx, tmp_path / "store.yaml", target, tmp_path, known_inputs={"pkg/f03.py": "a" * 40}
        )
    assert seen["known_inputs"] == {"pkg/f03.py": "a" * 40}
