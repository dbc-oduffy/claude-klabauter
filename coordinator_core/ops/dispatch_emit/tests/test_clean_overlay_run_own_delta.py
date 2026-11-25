"""`--export-run` overlays the run's own delta: its declared paths plus the paths its own agents wrote, never a peer's."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core.ops import review_reverify as rr
from coordinator_core.ops.pre_review_dir import pre_review_dir
from coordinator_core.ops.dispatch_emit import clean_overlay_verify as cov
from coordinator_core.ops.dispatch_emit import land_run, review_edits
from coordinator_core.ops.review_slice_id import run_prep_slice_prefix
from coordinator_core.session.machinery_paths import share_dir
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

RUN = "run1"
SESSION = "sess1"
AGENT = "agent1"
PEER = "src/peer.py"


@pytest.fixture(autouse=True)
def _isolated_system_temp(tmp_path, monkeypatch):
    import tempfile

    (tmp_path / "systemp").mkdir()
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "systemp"))


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), "-c", "user.name=t", "-c", "user.email=t@t", *args],
        check=True, capture_output=True, text=True, **no_console_creationflags(),
    ).stdout.strip()


def _world(tmp_path, monkeypatch):
    """A repo whose worktree holds the run's declared edit, a test and a helper only its own agent wrote, and a
    peer's declared dirty file; the session's write record names all three non-declared paths."""
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "__init__.py").write_text("", encoding="utf-8")
    (root / "src" / "mod.py").write_text("from src import peer\nA = 1\n", encoding="utf-8")
    (root / PEER).write_text("P = 1\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "base")
    base = _git(root, "rev-parse", "HEAD")

    (root / "src" / "mod.py").write_text("from src import peer\nA = 1\nB = 2\n", encoding="utf-8")
    (root / "src" / "helper.py").write_text("C = 3\n", encoding="utf-8")
    (root / "tests").mkdir()
    (root / "tests" / "test_new.py").write_text(
        "from src.mod import B\nfrom src.helper import C\n\n\ndef test_new():\n    assert (B, C) == (2, 3)\n",
        encoding="utf-8",
    )
    # Exact bytes: write_text turns \n into \r\n on Windows, and the leg mirror is compared byte-for-byte.
    (root / PEER).write_bytes(b"P = 99\n")

    record = cov.measured_rev_record_path(root, RUN).parent
    record.mkdir(parents=True)
    (record / "manifest.json").write_text(json.dumps({"review_declared_paths": ["src/mod.py"]}), encoding="utf-8")
    (root / "scratch" / "warp" / RUN).mkdir(parents=True)
    (root / "scratch" / "warp" / RUN / "run-base").write_text(base, encoding="utf-8")

    share = share_dir(str(root), SESSION)
    snap = pre_review_dir(Path(share), run_prep_slice_prefix(RUN, base))
    snap.mkdir(parents=True)
    (snap / "manifest.json").write_text(
        json.dumps({"base": base, "files": {"src/mod.py": "x"}, "recorded_at": "2000-01-01T00:00:00Z"}), encoding="utf-8",
    )
    writes = {"tests/test_new.py": AGENT, "src/helper.py": AGENT, PEER: "peer-agent"}
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: dict(writes))
    monkeypatch.setattr(review_edits, "live_sibling_declared", lambda *_a, **_k: {PEER: "peer-run"})
    monkeypatch.setattr(rr, "_run_agents", lambda *_a, **_k: {AGENT})
    monkeypatch.setattr(land_run, "_firing_sessions", lambda *_a, **_k: {SESSION})
    return root


def _main(root, *cmd):
    return cov.main(["--export-run", "--run-id", RUN, "--repo-root", str(root), "--", *cmd])


def test_the_run_s_uncommitted_test_and_helper_are_collected_and_pass_in_the_mirror(tmp_path, monkeypatch):
    root = _world(tmp_path, monkeypatch)

    assert _main(root, sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_new.py") == 0


def test_a_peer_declared_dirty_path_stays_at_its_base_bytes_in_the_mirror(tmp_path, monkeypatch):
    root = _world(tmp_path, monkeypatch)
    probe = "import src.peer as p, src.helper as h; raise SystemExit(0 if (p.P, h.C) == (1, 3) else 3)"

    assert _main(root, sys.executable, "-c", probe) == 0


def test_the_own_delta_is_declared_plus_own_written_minus_sibling_declared(tmp_path, monkeypatch):
    root = _world(tmp_path, monkeypatch)

    got = cov.run_own_delta(root, RUN, ["src/mod.py"])

    assert sorted(got) == ["src/helper.py", "src/mod.py", "tests/test_new.py"]


def test_a_path_shared_with_a_peer_reaches_the_mirror_at_head_plus_the_run_s_own_hunks(tmp_path, monkeypatch):
    root = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(cov, "own_hunk_overrides", lambda *_a, **_k: {PEER: b"P = 5\n"})
    probe = "import src.peer as p; raise SystemExit(0 if p.P == 5 else 3)"

    paths, overrides = cov.own_delta(root, RUN, ["src/mod.py", PEER])

    assert PEER in paths and overrides == {PEER: b"P = 5\n"}
    ran = cov.run_in_export(root, RUN, paths, [sys.executable, "-c", probe], overrides)

    assert ran.returncode == 0, ran.stderr


def test_a_shared_path_with_no_splittable_own_hunks_stays_at_head_never_the_peer_bytes(tmp_path, monkeypatch):
    root = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(cov, "own_hunk_overrides", lambda *_a, **_k: {})

    paths, overrides = cov.own_delta(root, RUN, ["src/mod.py"])

    assert PEER not in paths and overrides == {}


def _leg_tree(root):
    import contextlib

    from coordinator_core.ops import test_leg_run

    record = cov.measured_rev_record_path(root, RUN).parent
    (record / "manifest.json").write_text(
        json.dumps({"review_declared_paths": ["src/mod.py", PEER]}), encoding="utf-8",
    )
    result: dict = {}
    with contextlib.ExitStack() as stack:
        tree = Path(test_leg_run._own_tree(stack, root, None, result, RUN, (), RUN))
        assert "own_tree" in result, result.get("own_tree_skipped")
        return {p: (tree / p).read_bytes() if (tree / p).is_file() else None
                for p in ("src/mod.py", "src/helper.py", "tests/test_new.py", PEER)}


def test_the_test_leg_mirror_carries_a_shared_path_at_head_plus_the_run_s_own_hunks(tmp_path, monkeypatch):
    root = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(cov, "own_hunk_overrides", lambda *_a, **_k: {PEER: b"P = 5\n"})

    held = _leg_tree(root)

    assert held[PEER] == b"P = 5\n"
    assert b"B = 2" in held["src/mod.py"] and held["src/helper.py"] and held["tests/test_new.py"]


def test_the_test_leg_mirror_holds_an_unsplittable_shared_path_the_run_declares_at_worktree_bytes(tmp_path, monkeypatch):
    root = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(cov, "own_hunk_overrides", lambda *_a, **_k: {})

    held = _leg_tree(root)

    assert held[PEER] == b"P = 99\n"
    assert b"B = 2" in held["src/mod.py"]


def _snapshot_world(tmp_path, monkeypatch):
    """`_world` plus a module the run created that no declared path and no review-agent write names, only the
    review-prep snapshot's `files` (the reverify's `edited`), a peer-declared snapshot path, and an unowned stray."""
    root = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    (root / "src" / "seam.py").write_text("S = 7\n", encoding="utf-8")
    (root / "src" / "stray.py").write_text("Z = 0\n", encoding="utf-8")
    (root / "tests" / "test_seam.py").write_text(
        "from src.seam import S\n\n\ndef test_seam():\n    assert S == 7\n", encoding="utf-8",
    )
    base = _git(root, "rev-parse", "HEAD")
    snap = pre_review_dir(Path(share_dir(str(root), SESSION)), run_prep_slice_prefix(RUN, base))
    files = {p: "x" for p in ("src/mod.py", "src/seam.py", "tests/test_seam.py", PEER)}
    (snap / "manifest.json").write_text(
        json.dumps({"base": base, "files": files, "recorded_at": "2000-01-01T00:00:00Z"}), encoding="utf-8",
    )
    return root


def test_an_untracked_module_only_the_review_snapshot_names_is_in_the_reverify_mirror_and_its_test_runs(tmp_path, monkeypatch):
    root = _snapshot_world(tmp_path, monkeypatch)

    got = cov.run_own_delta(root, RUN, ["tests/test_seam.py"])

    assert "src/seam.py" in got
    assert _main(root, sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/test_seam.py") == 0


def test_an_unowned_untracked_module_and_a_peer_declared_snapshot_path_stay_out_of_the_reverify_mirror(tmp_path, monkeypatch):
    root = _snapshot_world(tmp_path, monkeypatch)

    got = cov.run_own_delta(root, RUN, ["tests/test_seam.py"])

    assert "src/stray.py" not in got and PEER not in got
    probe = "import importlib.util as u, sys; sys.exit(0 if u.find_spec('src.stray') is None else 3)"
    assert _main(root, sys.executable, "-c", probe) == 0


def test_an_untracked_module_named_only_by_the_widened_run_write_set_is_in_the_own_delta(tmp_path, monkeypatch):
    from coordinator_core.ops.dispatch_emit.ask_contract import LEG_FOOTPRINT_FILE, record_dir

    root = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    new = "tools/likeness/named_layer/gate.py"
    (root / new).parent.mkdir(parents=True)
    (root / new).write_text("G = 1\n", encoding="utf-8")
    (root / "src" / "stray.py").write_text("Z = 0\n", encoding="utf-8")
    base = record_dir(root, RUN)
    base.mkdir(parents=True, exist_ok=True)
    (base / LEG_FOOTPRINT_FILE).write_text(json.dumps({"writes": [new]}), encoding="utf-8")

    paths, _ = cov.own_delta(root, RUN, ["src/mod.py"])

    assert new in paths and "src/stray.py" not in paths and PEER not in paths


def _widened_world(tmp_path, monkeypatch, writes):
    from coordinator_core.ops.dispatch_emit.ask_contract import LEG_FOOTPRINT_FILE, record_dir

    root = _world(tmp_path, monkeypatch)
    monkeypatch.setattr(review_edits, "review_writes", lambda *_a, **_k: {})
    base = record_dir(root, RUN)
    base.mkdir(parents=True, exist_ok=True)
    (base / LEG_FOOTPRINT_FILE).write_text(json.dumps({"writes": writes}), encoding="utf-8")
    return root


def test_a_peer_held_path_in_the_widened_write_set_with_no_own_hunks_stays_in_the_delta_at_worktree_bytes(tmp_path, monkeypatch):
    root = _widened_world(tmp_path, monkeypatch, [PEER])
    monkeypatch.setattr(cov, "own_hunk_overrides", lambda *_a, **_k: {})

    paths, overrides = cov.own_delta(root, RUN, ["src/mod.py"])

    assert PEER in paths and overrides == {}


def test_a_peer_held_path_in_the_widened_write_set_reaches_the_mirror_at_the_run_s_own_hunks_only(tmp_path, monkeypatch):
    root = _widened_world(tmp_path, monkeypatch, [PEER])
    monkeypatch.setattr(cov, "own_hunk_overrides", lambda *_a, **_k: {PEER: b"P = 5\n"})

    paths, overrides = cov.own_delta(root, RUN, ["src/mod.py"])

    assert PEER in paths and overrides == {PEER: b"P = 5\n"}


def test_an_unreadable_own_written_source_keeps_the_manifest_declared_source_in_the_mirror(tmp_path, monkeypatch, capsys):
    root = _world(tmp_path, monkeypatch)

    def broken(*_a, **_k):
        raise OSError("no pre-review snapshot")

    monkeypatch.setattr(cov, "_own_written", broken)

    paths, _ = cov.own_delta(root, RUN, ["tests/test_new.py"])

    assert "src/mod.py" in paths and PEER not in paths
    assert "run's own delta not read" in capsys.readouterr().err


def test_an_unreadable_peer_declaration_keeps_the_run_s_own_source_and_drops_a_snapshot_only_path(tmp_path, monkeypatch):
    root = _snapshot_world(tmp_path, monkeypatch)

    def broken(*_a, **_k):
        raise OSError("sibling record unreadable")

    monkeypatch.setattr(review_edits, "live_sibling_declared", broken)

    paths, overrides = cov.own_delta(root, RUN, ["tests/test_seam.py"])

    assert "src/mod.py" in paths and "src/seam.py" not in paths and PEER not in paths and overrides == {}


def test_an_own_blob_equal_to_head_is_no_override_but_a_changed_blob_is_kept(tmp_path, monkeypatch):
    from coordinator_core.git import checkpoint_hunks

    root = _world(tmp_path, monkeypatch)
    head_blob = _git(root, "rev-parse", "HEAD:src/mod.py")
    proc = subprocess.run(
        ["git", "-C", str(root), "hash-object", "-w", "--stdin"], input=b"A = 1\nB = 2\n", check=True,
        capture_output=True, **no_console_creationflags(),
    )
    changed = proc.stdout.decode().strip()
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_own_blobs", lambda *_a, **_k: ({"src/mod.py": head_blob}, []))

    assert cov.own_hunk_overrides(root, RUN, ["src/mod.py"]) == {}

    monkeypatch.setattr(checkpoint_hunks, "checkpoint_own_blobs", lambda *_a, **_k: ({"src/mod.py": changed}, []))

    assert cov.own_hunk_overrides(root, RUN, ["src/mod.py"]) == {"src/mod.py": b"A = 1\nB = 2\n"}


def test_an_unreadable_head_keeps_every_own_blob_as_an_override(tmp_path, monkeypatch):
    from coordinator_core.git import checkpoint_hunks

    root = _world(tmp_path, monkeypatch)
    head_blob = _git(root, "rev-parse", "HEAD:src/mod.py")
    monkeypatch.setattr(checkpoint_hunks, "checkpoint_own_blobs", lambda *_a, **_k: ({"src/mod.py": head_blob}, []))

    def _boom(*_a, **_k):
        raise OSError("HEAD unreadable")

    monkeypatch.setattr(cov, "_head_sources", _boom)

    blob = subprocess.run(
        ["git", "-C", str(root), "cat-file", "blob", head_blob], check=True, capture_output=True,
        **no_console_creationflags(),
    ).stdout

    assert cov.own_hunk_overrides(root, RUN, ["src/mod.py"]) == {"src/mod.py": blob}


def test_an_unreadable_own_write_set_leaves_the_declared_seed_and_says_so(tmp_path, monkeypatch, capsys):
    root = _world(tmp_path, monkeypatch)

    def broken(*_a, **_k):
        raise OSError("record unreadable")

    monkeypatch.setattr(land_run, "_firing_sessions", broken)

    assert cov.run_own_delta(root, RUN, ["src/mod.py"]) == ["src/mod.py"]
    assert "run's own delta not read" in capsys.readouterr().err
