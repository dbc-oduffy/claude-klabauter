"""queue.promote writes under a caller-resolved ``doe_root`` param when one is given.

The CLI validates ``--target-wiki`` against the DoE root it resolved (``DOE_ROOT``
honoured); the op must write under that same root instead of re-resolving from
the warm server's own env and registry (claude-klabauter#33).
"""

from __future__ import annotations

import coordinator_core.ops  # noqa: F401 — populates _REGISTRY (import guard, mirrors sibling tests)

from coordinator_core.ops import queue_promote
from coordinator_core.ops.queue_promote import _queue_promote_handler


def test_doe_root_param_routes_the_write(tmp_path, monkeypatch):
    monkeypatch.delenv("LESSON_PROMOTE_OUTBOX_ROOT", raising=False)
    registered = tmp_path / "registered"
    caller = tmp_path / "caller"
    monkeypatch.setattr(queue_promote, "coordinator_doe_root", lambda: str(registered))

    result = _queue_promote_handler({
        "title": "Doe root param routes the write",
        "body": "body",
        "change_kind": "doctrine-edit",
        "target_wiki": "docs/wiki/x.md",
        "from_repo": "test-em",
        "doe_root": str(caller),
    })

    assert result["out_path"].startswith(str(caller / "state" / "lessons-outbox"))
    assert not registered.exists()


def test_absent_param_keeps_registry_resolution(tmp_path, monkeypatch):
    monkeypatch.delenv("LESSON_PROMOTE_OUTBOX_ROOT", raising=False)
    registered = tmp_path / "registered"
    monkeypatch.setattr(queue_promote, "coordinator_doe_root", lambda: str(registered))

    result = _queue_promote_handler({
        "title": "Absent param keeps registry resolution",
        "body": "body",
        "change_kind": "doctrine-edit",
        "target_wiki": "docs/wiki/x.md",
        "from_repo": "test-em",
    })

    assert result["out_path"].startswith(str(registered / "state" / "lessons-outbox"))


def _seed_oss_mirror_marker(root):
    marker_dir = root / ".claude-plugin"
    marker_dir.mkdir(parents=True)
    (marker_dir / "plugin.json").write_text("{}\n", encoding="utf-8")


def test_registry_resolved_oss_mirror_refuses_write(tmp_path, monkeypatch):
    monkeypatch.delenv("LESSON_PROMOTE_OUTBOX_ROOT", raising=False)
    mirror = tmp_path / "coordinator-claude-mirror"
    _seed_oss_mirror_marker(mirror)
    monkeypatch.setattr(queue_promote, "coordinator_doe_root", lambda: str(mirror))

    result = _queue_promote_handler({
        "title": "Registry-resolved OSS mirror refuses write",
        "body": "body",
        "change_kind": "doctrine-edit",
        "target_wiki": "docs/wiki/x.md",
        "from_repo": "test-em",
    })

    assert result == {"skipped": True, "reason": result["reason"]}
    assert ".claude-plugin/plugin.json" in result["reason"]
    assert "repos.doe_claude machine-local registry key" in result["reason"]
    assert not (mirror / "state" / "lessons-outbox").exists()


def test_doe_root_param_resolved_oss_mirror_refuses_write(tmp_path, monkeypatch):
    monkeypatch.delenv("LESSON_PROMOTE_OUTBOX_ROOT", raising=False)
    mirror = tmp_path / "caller-resolved-mirror"
    _seed_oss_mirror_marker(mirror)
    monkeypatch.setattr(queue_promote, "coordinator_doe_root", lambda: str(tmp_path / "unused"))

    result = _queue_promote_handler({
        "title": "Caller-resolved OSS mirror refuses write",
        "body": "body",
        "change_kind": "doctrine-edit",
        "target_wiki": "docs/wiki/x.md",
        "from_repo": "test-em",
        "doe_root": str(mirror),
    })

    assert result == {"skipped": True, "reason": result["reason"]}
    assert ".claude-plugin/plugin.json" in result["reason"]
    assert "caller-resolved doe_root param" in result["reason"]
    assert not (mirror / "state" / "lessons-outbox").exists()


def test_is_oss_publish_mirror_marker_probe(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert queue_promote._is_oss_publish_mirror(str(plain)) is False

    mirror = tmp_path / "mirror"
    _seed_oss_mirror_marker(mirror)
    assert queue_promote._is_oss_publish_mirror(str(mirror)) is True
