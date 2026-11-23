"""A seeded publish round lands the same union tree, byte for byte and mode for mode, as a cold-cache full pass.

Real source and destination repos, the real `process_target`, `publish_sync`, content-transform sweep
(memo and `known_inputs`), seed cache, staged-diff report, surviving-write and union assembly. Faked:
the percolate store load, the guard and inject phases, and the end-of-run gates. Two sides take the
same commits in turn; before each round the reference side has its seed cache and transform memo
wiped, so it runs the full pass the seeded side avoids. (`--full-sweep` only forces the cold row path
on both sides; it is not what makes the reference cold.)

Run: python -m pytest coordinator/bin/tests/test_publish_diff_driven_parity.py -q
"""

from __future__ import annotations

import dataclasses
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_BIN_DIR = Path(__file__).resolve().parent.parent
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_SIG_A = "sig-0000000000000000000000000000000000000001"
_SIG_B = "sig-0000000000000000000000000000000000000002"
_LEAK = "widget-project"
_EPOCH = "2026-01-01T00:00:00+00:00"

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
    spec = importlib.util.spec_from_file_location("publish_diff_driven_parity_under_test", _BIN_DIR / "publish.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()
publish._bootstrap_engine()


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(root), capture_output=True, text=True, check=True, creationflags=_NO_WINDOW,
        env={**os.environ, "GIT_AUTHOR_DATE": _EPOCH, "GIT_COMMITTER_DATE": _EPOCH},
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


def _section(value: str) -> dict:
    return {
        "hooks": ["substitute"],
        "substitute": [{"key": _LEAK, "value": value}],
        "file_surface": {},
    }


_BASE = {
    "keep.py": f"# {_LEAK}\nKEEP = 1\n",
    "denied.txt": "withheld by the row denylist\n",
    "mod.py": f"# {_LEAK}\nMOD = 0\n",
    "gone.txt": "to be deleted\n",
    "old_name.txt": "rename me\n",
    "tool.sh": "#!/bin/sh\necho hi\n",
    "ignored/base.txt": "never published\n",
    "reapme.txt": "later covered by an ignore rule\n",
    **{f"bulk/f{i:02d}.txt": f"bulk {i}\n" for i in range(20)},
}


_REAL_REMOVAL_RULES = {
    name: getattr(publish, name)
    for name in ("_source_deleted_paths", "_removals_this_source_shipped", "_ignore_reaped_dest_rels")
}


def _rooted_at(source_repo: Path, fn):
    """`fn` with `publish._source_repo` answering the side's own source repo, so a cold round's
    shipped-then-deleted removal rule reads this side's history rather than the live repo's."""

    def call(*args, **kwargs):
        saved = publish._source_repo
        publish._source_repo = lambda: source_repo
        try:
            return fn(*args, **kwargs)
        finally:
            publish._source_repo = saved

    return call


class Side:
    """One source repo, one destination repo, one set of caches under `root`."""

    def __init__(self, root: Path, monkeypatch, capsys) -> None:
        self.root = root
        self.mp = monkeypatch
        self.capsys = capsys
        self.src = root / "src-repo"
        self.dest = root / "dest-repo"
        self.signature = _SIG_A
        self.section = _section("public-widget")
        _init_repo(self.src, self_origin=False)
        _init_repo(self.dest, self_origin=True)
        with (self.dest / ".git" / "info" / "exclude").open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(".percolate/\n")
        self.row_src = self.src / "coordinator_core"
        for rel, text in _BASE.items():
            _write(self.row_src / "pkg" / rel, text)
        _write(self.row_src / ".percolate-ignore", "pkg/ignored/\n")
        _write(self.src / "other" / "unpublished.txt", "v0\n")
        self.head = _commit_all(self.src, "src: base")
        _git(self.src, "update-index", "--chmod=+x", "--", "coordinator_core/pkg/tool.sh")
        _git(self.src, "commit", "-q", "-m", "src: tool exec")
        self.head = _git(self.src, "rev-parse", "HEAD").strip()
        self.real_claude_klabauter = publish._import_claude_klabauter_percolate()
        self.real_process_target = publish.process_target
        self.row_out = io.StringIO()

    @property
    def seed_cache(self) -> Path:
        return self.dest.parent / f".{self.dest.name}.publish-seed"

    @property
    def memo_dir(self) -> Path:
        return self.root / "memo"

    def wipe_caches(self) -> None:
        shutil.rmtree(self.seed_cache, ignore_errors=True)
        shutil.rmtree(self.memo_dir, ignore_errors=True)

    def activate(self) -> None:
        mp, tmp = self.mp, self.root
        from coordinator_core.ops import percolate_run
        from coordinator_core.percolate import engine

        side = self
        mp.setenv("COORDINATOR_SETTINGS_HOME", str(tmp))
        row = f"row-a|mirror|{self.row_src}|{self.dest / 'out'}||pkg,.percolate-ignore,!pkg/denied.txt"
        for name in _REAL_REMOVAL_RULES:
            mp.setattr(publish, name, _rooted_at(self.src, _REAL_REMOVAL_RULES[name]))
        mp.setattr(publish, "_field7_declaration_check", lambda *a, **k: (True, "", None))
        mp.setattr(publish, "_resolve_percolate_root_and_rung", lambda **kw: (tmp, "test-rung"))
        mp.setattr(publish, "load_targets", lambda setup_dir, target_filter=None, **_: [row])
        mp.setattr(engine, "_transform_memo_dir", lambda: side.memo_dir)
        mp.setattr(percolate_run, "load_store", lambda path: {})
        mp.setattr(percolate_run, "resolve_target", lambda store, name: side.section)
        engine_claude_klabauter = dataclasses.replace(self.real_claude_klabauter, resolve_target=lambda store, name: side.section)
        mp.setattr(publish, "_import_claude_klabauter_percolate", lambda: engine_claude_klabauter)
        mp.setattr(publish, "_import_percolate_store_resolve_target", lambda: (lambda store, name: side.section))
        rewrite_basename = publish._import_percolate_rewrite_basename_module()
        mp.setattr(rewrite_basename, "rename_ledger_path", lambda *_a, **_k: tmp / "ledger.json")
        real_emit = rewrite_basename.emit_published_at
        mp.setattr(rewrite_basename, "emit_published_at", lambda root, **kw: real_emit(root, when=_EPOCH))
        mp.setattr(publish, "assert_percolate_store_ready", lambda engine_claude_klabauter, path: {})
        mp.setattr(publish, "locate_percolate_store", lambda setup_dir: tmp / "store.yaml")
        mp.setattr(publish, "resolve_percolate_identity_path", lambda setup_dir: tmp / "id")
        mp.setattr(publish, "check_identity_file_present", lambda path, setup_dir: tmp / "id")
        mp.setattr(publish, "check_identity_file_safe", lambda path: None)
        mp.setattr(publish, "parse_percolate_identity", lambda path: publish.PercolateIdentity(review=["dummy"]))
        mp.setattr(publish, "_resolve_publish_sync_module_path", lambda setup_dir: tmp / "publish_sync.py")
        from percolate import publish_sync as real_publish_sync

        mp.setattr(publish, "_import_publish_sync", lambda setup_dir: real_publish_sync)
        mp.setattr(publish, "check_publish_sync_contract", lambda *a, **k: None)
        mp.setattr(publish, "write_publish_provenance_record", lambda **kwargs: None)
        mp.setattr(publish, "compute_delta_invalidation_signature", lambda store_path, ctx: side.signature)
        for gate in _GATES:
            mp.setattr(publish, gate, lambda *a, **k: True)
        mp.setattr(publish, "assert_dest_on_declared_ref", lambda *a, **k: True)
        mp.setattr(publish, "assert_dest_engine_root_viable", lambda *a, **k: True)
        mp.setattr(publish, "dispatch_standalone_guards", lambda *a, **k: None)
        mp.setattr(publish, "assert_authored_parity", lambda *a, **k: None)
        mp.setattr(publish, "_door_emission_is_reportable", lambda *a, **k: False)
        mp.setattr(publish, "emit_door_name_map_for_publish_row", lambda *a, **k: None)
        mp.setattr(publish, "dispatch_percolate_inject", lambda *a, **k: ())
        mp.setattr(publish, "dispatch_percolate_pre_ci", lambda *a, **k: None)
        mp.setattr(publish, "dispatch_preswap_function_gate", lambda *a, **k: True)
        mp.setattr(publish, "dispatch_preswap_payload_parity_gate", lambda *a, **k: True)
        real_process_target = self.real_process_target

        def teeing_process_target(*args, **kwargs):
            kwargs["out"] = side.row_out
            return real_process_target(*args, **kwargs)

        mp.setattr(publish, "process_target", teeing_process_target)

    def commit(self, edit) -> str:
        edit(self.row_src / "pkg")
        self.head = _commit_all(self.src, "src: delta")
        return self.head

    def set_mode(self, rel: str, exec_bit: bool) -> None:
        _git(self.src, "update-index", "--add", f"--chmod={'+x' if exec_bit else '-x'}", "--", f"coordinator_core/pkg/{rel}")
        _git(self.src, "commit", "-q", "-m", "src: mode")
        self.head = _git(self.src, "rev-parse", "HEAD").strip()

    def run(self, *argv: str) -> "tuple[int, str]":
        self.activate()
        rc = publish.main(["row-a", *argv])
        out, err = self.capsys.readouterr()
        row_text, self.row_out = self.row_out.getvalue(), io.StringIO()
        return rc, out + err + row_text

    def union(self) -> "dict[str, tuple[str, bytes]]":
        listing = _git(self.dest, "ls-tree", "-r", "HEAD").splitlines()
        tree: "dict[str, tuple[str, bytes]]" = {}
        for line in listing:
            meta, path = line.split("\t", 1)
            mode, _kind, sha = meta.split()
            blob = subprocess.run(
                ["git", "cat-file", "blob", sha], cwd=str(self.dest), capture_output=True, check=True,
                creationflags=_NO_WINDOW,
            ).stdout
            tree[path] = (mode, blob)
        return tree


def _add(side: Side) -> None:
    side.commit(lambda root: (_write(root / "new.py", f"# {_LEAK}\nNEW = 1\n"), _write(root / "bulk/f99.txt", "99\n")))


def _modify(side: Side) -> None:
    side.commit(lambda root: _write(root / "mod.py", f"# {_LEAK}\nMOD = 1\n"))


def _delete(side: Side) -> None:
    side.commit(lambda root: (root / "gone.txt").unlink())


def _rename(side: Side) -> None:
    side.commit(lambda root: (root / "old_name.txt").rename(root / "new_name.txt"))


def _mode_flip(side: Side) -> None:
    side.set_mode("tool.sh", False)
    side.set_mode("keep.py", True)


def _scrub_and_denylisted(side: Side) -> None:
    side.commit(
        lambda root: (
            _write(root / "scrubbed.md", f"leaks {_LEAK} twice: {_LEAK}\n"),
            _write(root / "ignored/new.txt", "ignored\n"),
            _write(root / "denied.txt", "denylisted\n"),
        )
    )


def _modify_under_a_new_identity(side: Side) -> None:
    side.commit(lambda root: _write(root / "mod.py", f"# {_LEAK}\nMOD = 2\n"))
    side.section = _section("renamed-widget")
    side.signature = _SIG_B


def _cover_a_published_path_with_an_ignore_rule(side: Side) -> None:
    side.commit(lambda root: _write(root.parent / ".percolate-ignore", "pkg/ignored/\npkg/reapme.txt\n"))


def _assert_unions_match(seeded: Side, reference: Side) -> "dict[str, tuple[str, bytes]]":
    left, right = seeded.union(), reference.union()
    assert sorted(left) == sorted(right)
    for path in left:
        if not path.endswith(("_engine_stamp", "expected-manifest.json")):
            assert left[path] == right[path], path
    stamp = "out/_engine_stamp"
    assert left[stamp] == right[stamp], (
        f"seeded stamp {left[stamp][1]!r} != full-pass {right[stamp][1]!r}; heads {seeded.head} {reference.head}; "
        f"seeded log {_git(seeded.src, 'log', '--format=%H %s', '--', 'coordinator_core')!r}; "
        f"reference log {_git(reference.src, 'log', '--format=%H %s', '--', 'coordinator_core')!r}"
    )
    manifest = next(p for p in left if p.endswith("expected-manifest.json"))
    lpaths, rpaths = json.loads(left[manifest][1])["paths"], json.loads(right[manifest][1])["paths"]
    assert {k: (lpaths.get(k), rpaths.get(k)) for k in set(lpaths) | set(rpaths) if lpaths.get(k) != rpaths.get(k)} == {}
    assert left[manifest][0] == right[manifest][0]
    for side in (seeded, reference):
        assert _git(side.dest, "status", "--porcelain").strip() == ""
    assert not any(_LEAK.encode() in data for _mode, data in left.values())
    assert not any(p.startswith("out/pkg/denied") or "/ignored/" in p for p in left)
    return left


def test_every_delta_kind_lands_the_same_union_as_a_cold_cache_full_pass(tmp_path, monkeypatch, capsys):
    seeded = Side(tmp_path / "seeded", monkeypatch, capsys)
    reference = Side(tmp_path / "reference", monkeypatch, capsys)
    for side in (seeded, reference):
        rc, out = side.run()
        assert rc == 0, out
        assert "[seed]" in out
        assert (side.seed_cache / "row-a").is_dir(), "round one must leave a seed to take"
        assert list(side.memo_dir.glob("index-*.json")), "round one must leave a transform memo"
    previous_stamp = seeded.union()["out/_engine_stamp"][1]

    steps = [
        ("add", _add, True),
        ("modify", _modify, True),
        ("delete", _delete, True),
        ("rename", _rename, True),
        ("mode-flip", _mode_flip, True),
        ("scrub-and-denylisted", _scrub_and_denylisted, True),
        ("identity-change", _modify_under_a_new_identity, False),
        ("ignore-reap", _cover_a_published_path_with_an_ignore_rule, False),
    ]
    for name, edit, seed_taken in steps:
        for side in (seeded, reference):
            edit(side)
        assert seeded.head == reference.head, f"{name}: both sides must publish the same source commit"
        reference.wipe_caches()
        rc_seeded, out_seeded = seeded.run("--full-sweep")
        rc_reference, out_reference = reference.run("--full-sweep")
        assert rc_seeded == 0, (name, out_seeded)
        assert rc_reference == 0, (name, out_reference)
        if seed_taken:
            assert "staging seeded from source" in out_seeded, (name, out_seeded)
        assert "staging seeded from source" not in out_reference, (name, out_reference)
        reference_reason = "full pass (rule-path-changed" if name == "ignore-reap" else "full pass (no seed cached"
        assert reference_reason in out_reference, (name, out_reference)

        union = _assert_unions_match(seeded, reference)
        stamp = union["out/_engine_stamp"][1]
        assert stamp != previous_stamp, f"{name}: the delta must move the engine stamp"
        previous_stamp = stamp

        if name == "delete":
            assert "out/pkg/gone.txt" not in union
        if name == "rename":
            assert "out/pkg/old_name.txt" not in union and union["out/pkg/new_name.txt"][1] == b"rename me\n"
        if name == "mode-flip":
            assert union["out/pkg/tool.sh"][0] == "100644" and union["out/pkg/keep.py"][0] == "100755"
        if name == "scrub-and-denylisted":
            assert union["out/pkg/scrubbed.md"][1] == b"leaks public-widget twice: public-widget\n"
        if name == "identity-change":
            assert "full pass (seed transform identity differs" in out_seeded, out_seeded
            assert b"renamed-widget" in union["out/pkg/keep.py"][1]
        if name == "ignore-reap":
            assert "rule-path-changed" in out_seeded, out_seeded
            assert "out/pkg/reapme.txt" not in union
            assert "ignore-covered path(s)" in out_seeded and "ignore-covered path(s)" in out_reference
