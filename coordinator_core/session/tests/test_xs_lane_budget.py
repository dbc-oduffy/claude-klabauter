"""Tests for the XS lane / residue budget leaf. Repos are hand-built loose git objects, so no test
spawns a process."""

from __future__ import annotations

import hashlib
import subprocess
import time
import zlib
from pathlib import Path

import pytest

from coordinator_core.session import xs_lane_budget as xb


def _obj(git: Path, kind: str, payload: bytes) -> str:
    raw = f"{kind} {len(payload)}".encode() + b"\0" + payload
    sha = hashlib.sha1(raw).hexdigest()
    p = git / "objects" / sha[:2] / sha[2:]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(zlib.compress(raw))
    return sha


def _tree(git: Path, entries: dict) -> str:
    """entries: name -> ('blob'|'tree', sha). Sorted per git (dirs sort with trailing '/')."""
    def key(item):
        name, (kind, _) = item
        return name + ("/" if kind == "tree" else "")

    body = b""
    for name, (kind, sha) in sorted(entries.items(), key=key):
        mode = b"40000" if kind == "tree" else b"100644"
        body += mode + b" " + name.encode() + b"\0" + bytes.fromhex(sha)
    return _obj(git, "tree", body)


def _commit(git: Path, tree: str) -> str:
    return _obj(git, "commit", f"tree {tree}\nauthor a <a@b> 0 +0000\ncommitter a <a@b> 0 +0000\n\nm\n".encode())


def _make_repo(tmp_path: Path, files: dict) -> tuple[Path, str]:
    root = tmp_path / "repo"
    git = root / ".git"
    (git / "objects").mkdir(parents=True)
    (git / "HEAD").write_text("ref: refs/heads/main\n")
    sub: dict = {}
    top: dict = {}
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text)
        blob = _obj(git, "blob", text.encode())
        if "/" in rel:
            d, n = rel.split("/", 1)
            sub.setdefault(d, {})[n] = ("blob", blob)
        else:
            top[rel] = ("blob", blob)
    for d, ents in sub.items():
        top[d] = ("tree", _tree(git, ents))
    return root, _commit(git, _tree(git, top))


def _lines(n: int, tag: str = "l") -> str:
    return "".join(f"{tag}{i}\n" for i in range(n))


def _edit(root: Path, rel: str, old: str, new: str, **kw) -> dict:
    return {"file_path": str(root / rel), "old_string": old, "new_string": new, **kw}


@pytest.fixture
def repo(tmp_path):
    return _make_repo(tmp_path, {"a.py": _lines(10), "b.py": _lines(10), "c.py": _lines(10), "d/e.py": _lines(5)})


def test_open_refuses_reopen_and_residue_is_idempotent(repo):
    root, sha = repo
    assert xb.open_xs_lane(root, "s1", sha) is True
    assert xb.open_xs_lane(root, "s1", sha) is False
    assert xb.open_residue_window(root, "s1", sha) is False
    assert xb.open_residue_window(root, "s2", sha) is True
    assert xb.open_residue_window(root, "s2", sha) is True
    assert xb.read_lane(root, "s2")["kind"] == "residue"
    assert xb.read_lane(root, "absent") is None


def test_record_shape_add_path_and_spent(repo):
    root, sha = repo
    xb.open_xs_lane(root, "s", sha)
    xb.add_path(root, "s", str(root / "a.py"))
    xb.add_path(root, "s", "a.py")
    rec = xb.read_lane(root, "s")
    assert rec == {"kind": "xs", "base_sha": sha, "state": "open", "paths": ["a.py"]}
    xb.mark_spent(root, "s")
    assert xb.read_lane(root, "s")["state"] == "spent"


def test_lane_path_routes_through_shared_sessions_dir_resolver(repo, tmp_path, monkeypatch):
    from coordinator_core.session import core as _core

    root, sha = repo
    assert xb.lane_path(root, "s") == root / ".git" / "coordinator-sessions" / "s" / "xs-lane.json"
    redirected = tmp_path / "elsewhere" / "coordinator-sessions"
    monkeypatch.setattr(_core, "_sessions_dir_resolve", lambda cwd: str(redirected))
    assert xb.open_xs_lane(root, "s", sha) is True
    assert (redirected / "s" / "xs-lane.json").is_file()
    assert not (root / ".git" / "coordinator-sessions" / "s").exists()


def test_corrupt_record_reads_spent_and_blocks_reopen(repo):
    root, sha = repo
    p = xb.lane_path(root, "s")
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert xb.read_lane(root, "s")["state"] == "spent"
    assert xb.open_xs_lane(root, "s", sha) is False


def test_project_edit_replace_all_multiedit_write(repo):
    root, _ = repo
    f = str(root / "a.py")
    assert xb.project(f, _edit(root, "a.py", "l1\n", "X\n")).splitlines()[1] == "X"
    (root / "a.py").write_text("x\nx\n")
    assert xb.project(f, _edit(root, "a.py", "x", "y")) == "y\nx\n"
    assert xb.project(f, _edit(root, "a.py", "x", "y", replace_all=True)) == "y\ny\n"
    me = {"edits": [{"old_string": "x", "new_string": "y"}, {"old_string": "y\nx", "new_string": "z"}]}
    assert xb.project(f, me) == "z\n"
    assert xb.project(f, {"content": "new\n"}) == "new\n"
    assert xb.project(f, {}) is None
    assert xb.project(f, _edit(root, "a.py", "absent", "q")) is None
    assert xb.project(str(root / "nope.py"), _edit(root, "nope.py", "a", "b")) is None


def test_measure_counts_added_plus_deleted_and_trips_third_file(repo):
    root, sha = repo
    xb.open_xs_lane(root, "s", sha)
    rec = xb.read_lane(root, "s")
    u = xb.measure(root, rec, "a.py", _lines(10).replace("l1\n", "X\n"))
    assert (u.files, u.lines, u.over) == (1, 2, False)
    (root / "a.py").write_text(_lines(10).replace("l1\n", "X\n"))
    xb.add_path(root, "s", "a.py")
    (root / "b.py").write_text(_lines(10).replace("l2\n", "Y\n"))
    xb.add_path(root, "s", "b.py")
    rec = xb.read_lane(root, "s")
    u = xb.measure(root, rec, "c.py", _lines(10).replace("l3\n", "Z\n"))
    assert (u.files, u.over) == (3, True)


def test_line_budget_boundary(repo):
    root, sha = repo
    xb.open_xs_lane(root, "s", sha)
    rec = xb.read_lane(root, "s")
    at = _lines(10) + "".join(f"n{i}\n" for i in range(xb.BUDGET_LINES))
    assert xb.measure(root, rec, "a.py", at).over is False
    assert xb.measure(root, rec, "a.py", at + "n\n").over is True


def test_revert_frees_slot_and_new_file_counts_all_lines(repo):
    root, sha = repo
    xb.open_xs_lane(root, "s", sha)
    xb.add_path(root, "s", "a.py")
    xb.add_path(root, "s", "b.py")
    (root / "a.py").write_text(_lines(10))  # equals base: drops out
    (root / "b.py").write_text("changed\n")
    rec = xb.read_lane(root, "s")
    u = xb.measure(root, rec, "new.py", "1\n2\n3\n")
    assert (u.files, u.lines, u.over) == (2, 11 + 3, False)


def test_unreadable_base_raises(repo):
    root, _ = repo
    rec = {"kind": "xs", "base_sha": "0" * 40, "state": "open", "paths": []}
    with pytest.raises(RuntimeError):
        xb.measure(root, rec, "a.py", "x\n")


def test_zero_spawns_and_fast(tmp_path, monkeypatch):
    big = "".join(f"line {i}\n" for i in range(2000))
    root, sha = _make_repo(tmp_path, {"a.py": big, "b.py": big})
    xb.open_xs_lane(root, "s", sha)
    xb.add_path(root, "s", "b.py")
    (root / "b.py").write_text(big.replace("line 7\n", "seven\n"))
    rec = xb.read_lane(root, "s")
    proj = big.replace("line 900\n", "nine\n")

    def boom(*a, **k):
        raise AssertionError("spawn")

    monkeypatch.setattr(subprocess, "run", boom)
    monkeypatch.setattr(subprocess, "Popen", boom)
    t = time.process_time()
    u = xb.measure(root, rec, "a.py", proj)
    dt = time.process_time() - t
    assert (u.files, u.lines) == (2, 4)
    assert dt < 0.5  # AC7 evidence line prints the figure; 50ms is the target on the box
    print(f"measure process_time_ms={dt * 1000:.1f}")


def test_touched_s_or_larger(tmp_path, monkeypatch):
    root, _ = _make_repo(tmp_path, {"a.py": "x\n"})
    from coordinator_core.session import scope

    def fake(touched, degraded=False):
        monkeypatch.setattr(
            scope, "_read_touch_record_as_legacy_lines", lambda sink: ([f"T 2026-01-01T00:00:00+00:00 {t}" for t in touched], degraded)
        )

    z = root / "state" / "sizings"
    z.mkdir(parents=True)
    (z / "xs.yaml").write_text("estimate:\n  tshirt: XS\n")
    (z / "m.yaml").write_text("estimate:\n  tshirt: M\n")
    fake([])
    assert xb.touched_s_or_larger("s", root) is False
    fake(["a.py", "state/sizings/xs.yaml"])
    assert xb.touched_s_or_larger("s", root) is False
    fake(["state/sizings/xs.yaml", "state/sizings/m.yaml"])
    assert xb.touched_s_or_larger("s", root) is True
    fake(["state/sizings/gone.yaml"])
    assert xb.touched_s_or_larger("s", root) is True


def test_lf_file_stays_byte_exact_and_eol_only_change_is_free(repo):
    root, sha = repo
    xb.open_xs_lane(root, "s", sha)
    (root / "a.py").write_bytes(_lines(10).encode().replace(b"\n", b"\r\n"))
    assert xb.measure(root, xb.read_lane(root, "s"), "a.py", _lines(10)).files == 0
    (root / "b.py").write_bytes(b"l1\nl2\n")
    f = str(root / "b.py")
    assert xb.project(f, {"old_string": "l1\nl2", "new_string": "q"}) == "q\n"
    assert xb.project(f, {"old_string": "l1\r\nl2", "new_string": "q"}) is None


def test_project_shell_write_overwrite_append_absent_and_crlf(repo):
    root, _ = repo
    f = str(root / "a.py")  # a.py is LF
    assert xb.project_shell_write(f, "x\ny\n", append=False) == "x\ny\n"
    assert xb.project_shell_write(f, "x\n", append=True) == _lines(10) + "x\n"
    assert xb.project_shell_write(str(root / "new.py"), "x\n", append=True) == "x\n"
    (root / "b.py").write_bytes(b"l1\r\nl2\r\n")
    assert xb.project_shell_write(str(root / "b.py"), "q\r\n", append=True) == "l1\nl2\nq\n"
    assert xb.project_shell_write(str(root / "d"), "q\n", append=True) is None


def test_two_line_shell_write_into_new_file_prices_two_lines_one_file(repo):
    root, sha = repo
    xb.open_xs_lane(root, "s", sha)
    text = xb.project_shell_write(str(root / "new.py"), "a = 1\nb = 2\n", append=False)
    u = xb.measure(root, xb.read_lane(root, "s"), "new.py", text)
    assert (u.files, u.lines, u.over) == (1, 2, False)


def test_deny_count_absent_field_reads_zero(repo):
    root, sha = repo
    xb.open_xs_lane(root, "s", sha)
    assert xb.deny_count(xb.read_lane(root, "s")) == 0
    assert xb.deny_count({"kind": "xs", "state": "open", "base_sha": "", "paths": []}) == 0


def test_deny_count_record_deny_increments_and_persists(repo):
    root, sha = repo
    xb.open_xs_lane(root, "s", sha)
    assert xb.record_deny(root, "s") == 1
    assert xb.record_deny(root, "s") == 2
    assert xb.deny_count(xb.read_lane(root, "s")) == 2
    assert xb.GOLF_DENY_LIMIT == 2


def test_deny_count_record_deny_absent_and_corrupt_write_nothing(repo):
    root, sha = repo
    assert xb.record_deny(root, "none") == 0
    assert xb.read_lane(root, "none") is None
    p = xb.lane_path(root, "bad")
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert xb.record_deny(root, "bad") == xb.GOLF_DENY_LIMIT
    assert p.read_text() == "{not json"
    assert xb.deny_count(xb.read_lane(root, "bad")) == xb.GOLF_DENY_LIMIT


@pytest.mark.parametrize("bad", [-1, "2", 1.5, True, None, [1]])
def test_deny_count_malformed_field_is_corrupt(repo, bad):
    import json

    root, sha = repo
    p = xb.lane_path(root, "s")
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"kind": "xs", "state": "open", "base_sha": sha, "paths": [], "denies": bad}))
    rec = xb.read_lane(root, "s")
    assert rec["corrupt"] and rec["state"] == "spent"
    assert xb.deny_count(rec) == xb.GOLF_DENY_LIMIT


def test_deny_count_supersede_xs_replaces_xs_and_refuses_corrupt(repo):
    root, sha = repo
    xb.open_xs_lane(root, "s", sha)
    xb.add_path(root, "s", "a.py")
    xb.record_deny(root, "s")
    assert xb.open_residue_window(root, "s", sha) is False
    assert xb.read_lane(root, "s")["kind"] == "xs"
    assert xb.open_residue_window(root, "s", sha, supersede_xs=True) is True
    assert xb.read_lane(root, "s") == {"kind": "residue", "base_sha": sha, "state": "open", "paths": []}
    assert xb.open_residue_window(root, "s", sha, supersede_xs=True) is True
    p = xb.lane_path(root, "bad")
    p.parent.mkdir(parents=True)
    p.write_text("{not json")
    assert xb.open_residue_window(root, "bad", sha, supersede_xs=True) is False
    assert p.read_text() == "{not json"


def test_deny_count_does_not_affect_measure(repo):
    root, sha = repo
    xb.open_xs_lane(root, "s", sha)
    base = xb.measure(root, xb.read_lane(root, "s"), "a.py", "x\n" + _lines(10))
    xb.record_deny(root, "s")
    xb.record_deny(root, "s")
    assert xb.measure(root, xb.read_lane(root, "s"), "a.py", "x\n" + _lines(10)) == base


def test_record_deny_with_usage_keeps_files_and_lines(repo):
    root, sha = repo
    xb.open_xs_lane(root, "s", sha)
    xb.record_deny(root, "s", xb.Usage(2, 45, True), ["a.py", "b.py"])
    assert xb.read_lane(root, "s")["deny_usage"] == {"files": ["a.py", "b.py"], "lines": 45}
    xb.record_deny(root, "s")
    assert xb.read_lane(root, "s")["deny_usage"]["lines"] == 45
    assert xb.deny_count(xb.read_lane(root, "s")) == 2
