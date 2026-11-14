"""halt_split_tree: stat-only split-tree detection and live G-EM alert seam."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import halt_split_tree as hst


def _pkg(root: Path, rel: str, *, build=True, main=None, lock_age=0, entry_age=100, entry=True):
    d = root / rel
    (d / "dist").mkdir(parents=True, exist_ok=True)
    body = {"name": "srv"}
    if build:
        body["scripts"] = {"build": "tsc"}
    if main:
        body["main"] = main
    (d / "package.json").write_text(json.dumps(body))
    (d / "node_modules").mkdir(exist_ok=True)
    lock = d / "node_modules" / ".package-lock.json"
    lock.write_text("{}")
    now = time.time()
    os.utime(lock, (now - lock_age, now - lock_age))
    if entry:
        e = d / (main or "dist/index.js")
        e.parent.mkdir(parents=True, exist_ok=True)
        e.write_text("x")
        os.utime(e, (now - entry_age, now - entry_age))
    return d


def test_split_detected_with_relative_dir_and_name(tmp_path):
    _pkg(tmp_path, "control/server")
    [hit] = hst.find_split_trees(tmp_path)
    assert hit["package_dir"] == "control/server"
    assert hit["package_name"] == "srv"
    assert hit["tree_mtime"] > hit["build_mtime"]


def test_entry_newer_than_lock_is_not_split(tmp_path):
    _pkg(tmp_path, "control/server", lock_age=100, entry_age=0)
    assert hst.find_split_trees(tmp_path) == []


def test_no_build_script_is_not_split(tmp_path):
    _pkg(tmp_path, "control/server", build=False)
    assert hst.find_split_trees(tmp_path) == []


def test_missing_entry_is_not_split(tmp_path):
    _pkg(tmp_path, "control/server", entry=False)
    assert hst.find_split_trees(tmp_path) == []


def test_main_field_is_the_entry_point(tmp_path):
    _pkg(tmp_path, "a", main="lib/main.js")
    assert [h["package_dir"] for h in hst.find_split_trees(tmp_path)] == ["a"]


def test_skips_node_modules_and_depth_limit(tmp_path):
    _pkg(tmp_path, "node_modules/x")
    _pkg(tmp_path, "a/b/c")
    assert hst.find_split_trees(tmp_path) == []


def test_missing_root_returns_empty(tmp_path):
    assert hst.find_split_trees(tmp_path / "nope") == []


def test_200_siblings_under_50ms_process_time(tmp_path):
    for i in range(200):
        (tmp_path / f"d{i}").mkdir()
    start = time.process_time()
    assert hst.find_split_trees(tmp_path) == []
    assert time.process_time() - start < 0.05


def test_visits_are_bounded_whatever_the_tree_holds(tmp_path, monkeypatch):
    for i in range(40):
        for j in range(40):
            (tmp_path / f"d{i:03d}" / f"s{j:03d}").mkdir(parents=True)
    for corpus in ("archive", "docs", ".coordinator-local"):
        (tmp_path / corpus / "x" / "y").mkdir(parents=True)
    visited = []
    real = hst._check_package
    monkeypatch.setattr(hst, "_check_package", lambda p: (visited.append(p), real(p))[1])
    assert hst.find_split_trees(tmp_path) == []
    assert len(visited) <= 1 + 32 + 32 * 32
    assert not any(p.parts[len(tmp_path.parts)] in ("archive", "docs", ".coordinator-local") for p in visited if p != tmp_path)


def test_real_package_still_detected_beside_corpora(tmp_path):
    for corpus in ("archive", "docs", "state"):
        (tmp_path / corpus / "x").mkdir(parents=True)
    _pkg(tmp_path, "control/server")
    _pkg(tmp_path, "top")
    assert sorted(h["package_dir"] for h in hst.find_split_trees(tmp_path)) == ["control/server", "top"]


def test_a_dot_directory_is_skipped_and_its_sibling_package_is_detected(tmp_path):
    _pkg(tmp_path, ".tools/pkg")
    _pkg(tmp_path, "tools/pkg")
    assert [h["package_dir"] for h in hst.find_split_trees(tmp_path)] == ["tools/pkg"]


def test_a_package_inside_a_skipped_corpus_is_not_visited(tmp_path):
    _pkg(tmp_path, "archive/pkg")
    assert hst.find_split_trees(tmp_path) == []


def _stub_identify(monkeypatch, verdict):
    from coordinator_core.group_em import nomination

    monkeypatch.setattr(nomination, "identify", lambda root: verdict)


SPLITS = [{"package_dir": "control/server", "package_name": "srv", "tree_mtime": 2.0, "build_mtime": 1.0}]


def test_alert_sends_once_to_live_holder(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(hst, "_ALERT_SENDER", lambda n, t: sent.append((n, t)))
    _stub_identify(monkeypatch, {"state": "live", "holder": {"session_id": "s", "name": "group-em-1"}})
    hst.alert_live_gem(tmp_path, "ask-1", SPLITS)
    assert len(sent) == 1
    name, text = sent[0]
    assert name == "group-em-1"
    assert "ask-1" in text and "control/server" in text and "srv" in text and "LIVE BREAKAGE" in text


@pytest.mark.parametrize("verdict", [{"state": "none"}, {"state": "stale", "holder": {"name": "x"}}, {"state": "live"}])
def test_alert_skipped_without_live_holder(tmp_path, monkeypatch, verdict):
    sent = []
    monkeypatch.setattr(hst, "_ALERT_SENDER", lambda n, t: sent.append((n, t)))
    _stub_identify(monkeypatch, verdict)
    hst.alert_live_gem(tmp_path, "ask-1", SPLITS)
    assert sent == []


def test_no_process_spawned_under_pytest_without_seam(tmp_path, monkeypatch):
    from coordinator_core.ops.ceremony import detached_spawn

    calls = []
    monkeypatch.setattr(hst, "_ALERT_SENDER", None)
    monkeypatch.setattr(detached_spawn, "spawn_detached", lambda *a, **k: calls.append(a))
    _stub_identify(monkeypatch, {"state": "live", "holder": {"name": "g"}})
    hst.alert_live_gem(tmp_path, "ask-1", SPLITS)
    assert calls == []


def test_alert_never_raises(tmp_path, monkeypatch):
    from coordinator_core.group_em import nomination

    def boom(root):
        raise RuntimeError("x")

    monkeypatch.setattr(nomination, "identify", boom)
    hst.alert_live_gem(tmp_path, "ask-1", SPLITS)


def test_real_alert_spawns_unimatrix_send_with_sender_target_and_text(tmp_path, monkeypatch):
    from coordinator_core.ops.ceremony import detached_spawn

    calls = []
    monkeypatch.setattr(hst, "_ALERT_SENDER", None)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(detached_spawn, "spawn_detached", lambda *a, **k: calls.append(a))
    _stub_identify(monkeypatch, {"state": "live", "holder": {"name": "group-em-1"}})
    hst.alert_live_gem(tmp_path, "ask-1", SPLITS)
    [(_cwd, script, argv)] = calls
    assert script.endswith("unimatrix-send.py") and os.path.isfile(script)
    assert argv[:4] == ["--as", "halted-run", "--to", "group-em-1"]
    assert "LIVE BREAKAGE" in argv[4] and "ask-1" in argv[4] and len(argv) == 5
