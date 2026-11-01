"""`since_base_findings` walks the commit chain to base only for declared paths.

Base (HEAD before the change), the `landing_integrity.since_base_findings` leg: a call with no
declared path still resolved HEAD and walked first-parent commits to base (one commit_meta read per
commit since base), and a path declared twice was compared twice. After: no path, no walk;
each path once.
"""

from __future__ import annotations

from coordinator_core.git import since_base_overlap
from coordinator_core.ops.dispatch_emit import landing_integrity


def _spy(monkeypatch) -> list:
    calls: list = []

    def fake(root, paths, base, **kw):
        calls.append(list(paths))
        return []

    monkeypatch.setattr(since_base_overlap, "find_overlaps", fake)
    return calls


def test_no_declared_path_starts_no_walk(tmp_path, monkeypatch):
    calls = _spy(monkeypatch)

    assert landing_integrity.since_base_findings(tmp_path, [], "abc123", run_id="r") == []
    assert calls == []


def test_a_declared_path_is_scanned_once(tmp_path, monkeypatch):
    calls = _spy(monkeypatch)

    landing_integrity.since_base_findings(tmp_path, ["a.py", "b.py", "a.py"], "abc123", run_id="r")

    assert calls == [["a.py", "b.py"]]
