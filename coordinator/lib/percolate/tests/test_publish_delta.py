from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from coordinator_core.git.git_state import format_publish_trailers  # noqa: E402
from percolate import publish_delta  # noqa: E402
from percolate.publish_delta import (  # noqa: E402
    ColdReason,
    PublishBase,
    WarmPlan,
    find_publish_base,
    materialize_paths,
    materialize_subtree,
    plan_publish_round,
)

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NC = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
SIG = "a" * 64


def _git(root, *args):
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, check=True, **_NC
    ).stdout.strip()


def _repo(tmp_path, name):
    root = tmp_path / name
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@local")
    _git(root, "config", "user.name", "t")
    _git(root, "config", "core.autocrlf", "false")
    return root


def _commit(root, files, msg, delete=()):
    for rel, data in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    for rel in delete:
        (root / rel).unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", msg)
    return _git(root, "rev-parse", "HEAD")


@pytest.fixture
def spawn_counter(monkeypatch):
    calls = []
    real = subprocess.Popen.__init__

    def counting(self, *a, **k):
        calls.append(a)
        real(self, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", counting)
    return calls


def test_rule_tuple_pinned():
    assert publish_delta.TRANSFORM_RULE_PATHS == (
        "setup/percolate-hooks/",
        "setup/publish-targets.portable",
        "setup/publish-allowlist-declarations.yaml",
        "coordinator/bin/publish.py",
        "coordinator/lib/percolate/",
        "coordinator_core/percolate/",
        "coordinator_core/ops/percolate_run.py",
    )
    assert publish_delta.TRANSFORM_RULE_BASENAMES == frozenset({".percolate-ignore"})


def test_base_found_and_cold_reasons(tmp_path, spawn_counter):
    dest = _repo(tmp_path, "d")
    head = "b" * 40
    stamped = _commit(
        dest, {"a": b"1"}, "round" + format_publish_trailers(round_id="r1", source_head=head, signature=SIG)
    )
    n = len(spawn_counter)
    assert find_publish_base(dest) == PublishBase(stamped, head, SIG)
    assert len(spawn_counter) == n

    partial = _commit(
        dest, {"a": b"2"}, "p" + format_publish_trailers(round_id="r2", source_head=None, signature=None)
    )
    r = find_publish_base(dest)
    assert r == ColdReason("partial-round", partial[:12])

    foreign = _commit(dest, {"a": b"3"}, "hand edit")
    r = find_publish_base(dest)
    assert r.code == "foreign-commit" and r.detail == f"{foreign[:12]} hand edit"


def test_no_stamp_and_unreadable(tmp_path):
    empty = _repo(tmp_path, "e")
    assert find_publish_base(empty).code == "no-stamp"
    dest = _repo(tmp_path, "d")
    sha = _commit(dest, {"a": b"1"}, "x")
    loose = dest / ".git" / "objects" / sha[:2] / sha[2:]
    _git(dest, "gc", "-q")
    for p in (dest / ".git" / "objects").rglob("*"):
        if p.is_file() and p.parent.name != "info" and p.parent.name != "pack":
            p.chmod(0o666)
            p.unlink()
    for p in (dest / ".git" / "objects" / "pack").glob("*"):
        p.chmod(0o666)
        p.unlink()
    assert not loose.exists()
    assert find_publish_base(dest).code == "object-unreadable"


def _src(tmp_path):
    src = _repo(tmp_path, "src")
    files = {f"root/f{i}.txt": b"0" for i in range(30)}
    files.update({f"sub/g{i}.txt": b"0" for i in range(30)})
    files["setup/other.txt"] = b"0"
    base = _commit(src, files, "base")
    return src, base


PREFIXES = {"rootrow": ("",), "subrow": ("sub",)}


def test_plan_buckets_50_files_across_rows(tmp_path, spawn_counter):
    src, b = _src(tmp_path)
    edits = {f"root/f{i}.txt": b"1" for i in range(25)}
    edits.update({f"sub/g{i}.txt": b"1" for i in range(25)})
    head = _commit(src, edits, "edit", delete=["root/f29.txt", "sub/g29.txt"])
    n = len(spawn_counter)
    plan = plan_publish_round(
        source_toplevel=src,
        head_sha=head,
        base=PublishBase("d" * 40, b, SIG),
        signature=SIG,
        row_source_prefixes=PREFIXES,
    )
    assert len(spawn_counter) == n
    assert isinstance(plan, WarmPlan)
    sub = {f"sub/g{i}.txt" for i in range(25)}
    root = {f"root/f{i}.txt" for i in range(25)}
    assert plan.changed["subrow"] == sub
    assert plan.changed["rootrow"] == root | sub
    assert plan.deleted["subrow"] == {"sub/g29.txt"}
    assert plan.deleted["rootrow"] == {"sub/g29.txt", "root/f29.txt"}


def test_plan_cold_reasons(tmp_path):
    src, b = _src(tmp_path)
    kw = dict(source_toplevel=src, row_source_prefixes=PREFIXES)
    base = PublishBase("d" * 40, b, SIG)
    head = _commit(src, {"root/f0.txt": b"1"}, "e")
    assert plan_publish_round(head_sha=head, base=base, signature="c" * 64, **kw).code == "signature-changed"
    cold = ColdReason("requested", "x")
    assert plan_publish_round(head_sha=head, base=cold, signature=SIG, **kw) is cold
    bad = PublishBase("d" * 40, "0" * 40, SIG)
    assert plan_publish_round(head_sha=head, base=bad, signature=SIG, **kw).code == "object-unreadable"
    for rel in ("coordinator/lib/percolate/x.py", "sub/deep/.percolate-ignore", "setup/publish-targets.portable"):
        prev = _git(src, "rev-parse", "HEAD")
        h = _commit(src, {rel: b"r"}, "rule")
        r = plan_publish_round(head_sha=h, base=PublishBase("d" * 40, prev, SIG), signature=SIG, **kw)
        assert r == ColdReason("rule-path-changed", rel)


def test_materialize_only_named_blobs(tmp_path, spawn_counter):
    src, _ = _src(tmp_path)
    (src / "bin").mkdir()
    (src / "bin" / "run.sh").write_bytes(b"#!/bin/sh\n")
    _git(src, "update-index", "--add", "--chmod=+x", "bin/run.sh")
    _git(src, "commit", "-q", "-m", "exec")
    head = _git(src, "rev-parse", "HEAD")
    out = tmp_path / "out"
    n = len(spawn_counter)
    modes = materialize_paths(src, head, ["bin/run.sh", "sub/g3.txt"], out)
    assert len(spawn_counter) == n
    assert modes == {"bin/run.sh": 0o100755, "sub/g3.txt": 0o100644}
    written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert written == ["bin/run.sh", "sub/g3.txt"]
    assert (out / "sub" / "g3.txt").read_bytes() == b"0"
    with pytest.raises(RuntimeError):
        materialize_paths(src, head, ["nope.txt"], out)


def test_materialize_symlink_as_text(tmp_path):
    src, _ = _src(tmp_path)
    blob = subprocess.run(
        ["git", "-C", str(src), "hash-object", "-w", "--stdin"],
        input=b"root/f0.txt", capture_output=True, check=True, **_NC,
    ).stdout.decode().strip()
    _git(src, "update-index", "--add", "--cacheinfo", f"120000,{blob},link")
    _git(src, "commit", "-q", "-m", "link")
    head = _git(src, "rev-parse", "HEAD")
    out = tmp_path / "out"
    assert materialize_paths(src, head, ["link"], out) == {"link": 0o120000}
    assert (out / "link").read_bytes() == b"root/f0.txt"


def test_materialize_subtree_writes_only_that_subtree(tmp_path, spawn_counter):
    src, _ = _src(tmp_path)
    _commit(src, {"sub/deep/h.txt": b"h"}, "deep")
    head = _git(src, "rev-parse", "HEAD")
    out = tmp_path / "out"
    n = len(spawn_counter)
    modes = materialize_subtree(src, head, "sub", out)
    assert len(spawn_counter) == n
    written = sorted(p.relative_to(out).as_posix() for p in out.rglob("*") if p.is_file())
    assert written == sorted([f"sub/g{i}.txt" for i in range(30)] + ["sub/deep/h.txt"])
    assert set(modes) == set(written)
    assert (out / "sub" / "deep" / "h.txt").read_bytes() == b"h"
    assert materialize_subtree(src, head, "setup/other.txt", tmp_path / "one") == {"setup/other.txt": 0o100644}
    with pytest.raises(RuntimeError):
        materialize_subtree(src, head, "nope", tmp_path / "none")
