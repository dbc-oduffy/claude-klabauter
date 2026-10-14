"""A declared touch batch rewrites meta.json's last_activity once, not per path."""

from __future__ import annotations

from coordinator_core import ipc
from coordinator_core.session import core, scope
from coordinator_core.tests.git_seed import seeded_repo


def _count_activity_writes(monkeypatch):
    calls = []
    real = core.update_meta_field

    def spy(sdir, field, value):
        calls.append(field)
        return real(sdir, field, value)

    monkeypatch.setattr(core, "update_meta_field", spy)
    return calls


def test_touch_refresh_activity_false_skips_meta_rewrite(tmp_path, monkeypatch):
    repo = seeded_repo(tmp_path, readme="x")
    core.init("s1", cwd=str(repo))
    calls = _count_activity_writes(monkeypatch)
    scope.touch("s1", "a.py", cwd=str(repo), refresh_activity=False)
    assert calls == []


def test_declared_batch_rewrites_last_activity_once(tmp_path, monkeypatch):
    repo = seeded_repo(tmp_path, readme="x")
    root = str(repo)
    names = [f"f{i}.txt" for i in range(5)]
    for n in names:
        (repo / n).write_text("x")
    core.init("s-batch", cwd=root)
    sdir = core.session_dir("s-batch", root)
    core.update_meta_field(sdir, "last_activity", "2000-01-01T00:00:00Z")

    monkeypatch.setattr(core, "attributable_session_id", lambda _cwd: "s-batch")
    calls = _count_activity_writes(monkeypatch)

    ipc._record_self_reported_touches(
        {ipc._SCOPE_TOUCH_PATHS_KEY: [str(repo / n) for n in names]}, root
    )

    assert calls == ["last_activity"]
    assert core.read_meta_field(sdir, "last_activity") != "2000-01-01T00:00:00Z"
    record = scope.os.path.join(sdir, "touch-record.jsonl")
    with open(record, encoding="utf-8") as fh:
        assert len([ln for ln in fh if ln.strip()]) == len(names)
