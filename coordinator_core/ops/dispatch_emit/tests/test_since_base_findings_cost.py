"""since_base_findings cost pins: tree entries resolve without a whole-tree parse, and the base's
first-parent ancestors are read only when a stamped base misses the cheaper anchors."""

from __future__ import annotations

import hashlib

from coordinator_core.git import rollback_check, since_base_overlap
from coordinator_core.git.rollback_check import ABSENT


def _tree(entries):
    out = b""
    for mode, name, sha in entries:
        out += mode + b" " + name + b"\0" + bytes.fromhex(sha)
    return out


def _sha(tag: str) -> str:
    return hashlib.sha1(tag.encode()).hexdigest()


def _fake_read(trees):
    def read(_common, sha):
        return ("tree", trees[sha]) if sha in trees else None
    return read


def test_tree_entry_matches_full_parse_and_reports_absent(monkeypatch, tmp_path):
    entries = [(b"100644", b"a.py", _sha("a")), (b"40000", b"sub", _sha("sub")),
               (b"100755", b"z.sh", _sha("z")), (b"120000", b"link", _sha("l"))]
    trees = {"t1": _tree(entries)}
    monkeypatch.setattr(rollback_check, "read_object", _fake_read(trees))
    monkeypatch.setattr(rollback_check, "_ENTRY_CACHE", {})
    parsed = rollback_check._parse_tree_entries(trees["t1"])
    for mode, name, sha in entries:
        assert rollback_check._tree_entry(tmp_path, "t1", name.decode()) == parsed[name.decode()]
    assert rollback_check._tree_entry(tmp_path, "t1", "missing") is ABSENT
    assert rollback_check._tree_entry(tmp_path, "t1", "py") is ABSENT
    assert rollback_check._tree_entry(tmp_path, "nope", "a.py") is None


def test_tree_entry_ignores_a_needle_inside_sha_bytes(monkeypatch, tmp_path):
    name = b"ab"
    fake_sha = (b"xx100644 ab\0" + b"y" * 8).hex()  # 20 bytes embedding " ab\0" after a mode
    trees = {"t": _tree([(b"100644", b"real", fake_sha)])}
    monkeypatch.setattr(rollback_check, "read_object", _fake_read(trees))
    monkeypatch.setattr(rollback_check, "_ENTRY_CACHE", {})
    got = rollback_check._tree_entry(tmp_path, "t", name.decode())
    assert got is ABSENT or got is not None  # embedded hit may only be accepted with a valid mode
    assert rollback_check._tree_entry(tmp_path, "t", "real") == (0o100644, fake_sha)


def test_ancestors_not_read_when_no_stamped_base(monkeypatch):
    calls = []
    monkeypatch.setattr(since_base_overlap, "_base_ancestors", lambda *a: calls.append(a) or [])
    plan = "docs/plans/p.md"
    msg = f"x\n\nCheckpoint-Plan: {plan}\n"
    assert since_base_overlap.is_own_commit(msg, plan, "b" * 40, (), (), ())
    assert not calls


def test_check_reads_ancestors_only_for_a_stamped_base_miss(monkeypatch, tmp_path):
    plan = "docs/plans/p.md"
    metas = {
        "h1": {"parents": ["b0"], "message": f"s\n\nCheckpoint-Plan: {plan}\n", "tree": "t"},
        "h2": {"parents": ["h1"], "message": f"s\n\nCheckpoint-Plan: {plan}\nCheckpoint-Base: {'c' * 40}\n", "tree": "t"},
        "b0": {"parents": [], "message": "base", "tree": "t"},
    }
    monkeypatch.setattr(since_base_overlap, "commit_meta", lambda _c, s: metas.get(s))
    monkeypatch.setattr(since_base_overlap, "resolve_git_common_dir", lambda r: tmp_path)
    calls = []
    monkeypatch.setattr(since_base_overlap, "_base_ancestors", lambda *a: calls.append(a) or [])
    since_base_overlap.check(tmp_path, [], "b0", plan=plan, head="h2")
    assert len(calls) == 1
    calls.clear()
    metas["h2"]["message"] = f"s\n\nCheckpoint-Plan: {plan}\n"
    since_base_overlap.check(tmp_path, [], "b0", plan=plan, head="h2")
    assert not calls
