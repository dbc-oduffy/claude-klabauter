"""recommend_level and the version-bump rule: levels, tag collisions, reuse once stamped."""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinator_core import merge_assemble
from coordinator_core.workweek_complete.release import version_rule
from coordinator_core.workweek_complete.release.contract import ReleaseState


def _entry(root: Path, name: str, nature: str, status: str = "pending-release") -> None:
    d = root / "archive" / "completed" / "2026-10"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(
        f'---\ntitle: "{name}"\ncreated: 2026-10-12\nnature: {nature}\nstatus: {status}\n---\n\nbody\n',
        encoding="utf-8",
    )


def _header(root: Path, week: str) -> None:
    d = root / "state" / "week-changelog"
    d.mkdir(parents=True, exist_ok=True)
    (d / "HEADER.md").write_text(f"**Week starting:** {week}\n", encoding="utf-8")


def test_roadmap_entry_gives_minor(tmp_path):
    _header(tmp_path, "2026-10-05")
    _entry(tmp_path, "a", "roadmap")
    _entry(tmp_path, "b", "bugfix")
    result = version_rule.recommend_level(tmp_path)
    assert result["disposition"] == "minor"
    assert "roadmap=1" in result["rationale"] and "bugfix=1" in result["rationale"]


def test_no_roadmap_gives_patch_never_major(tmp_path):
    _entry(tmp_path, "a", "infra")
    assert version_rule.recommend_level(tmp_path)["disposition"] == "patch"


def test_no_pending_entry_gives_none(tmp_path):
    _header(tmp_path, "2026-10-05")
    _entry(tmp_path, "a", "roadmap", status="released")
    assert version_rule.recommend_level(tmp_path) is None


def test_entry_before_week_start_is_ignored(tmp_path):
    _header(tmp_path, "2026-10-13")
    _entry(tmp_path, "a", "roadmap")
    assert version_rule.recommend_level(tmp_path) is None


@pytest.fixture
def tags(monkeypatch):
    state = {"latest": "v1.2.3", "origin": set()}
    monkeypatch.setattr(
        version_rule,
        "local_release_tags",
        lambda root, tag_prefix: [state["latest"]] if state["latest"] else [],
    )
    monkeypatch.setattr(merge_assemble, "origin_tag_names", lambda root: state["origin"])
    return state


def _point(level=None):
    return {"recommendation": {"disposition": level, "rationale": "r"} if level else None}


UNSTAMPED = ReleaseState("unstamped")


def test_patch_and_minor_resolve(tmp_path, tags):
    patch = version_rule.evaluate(tmp_path, point=_point("patch"), tag_prefix="v", state=UNSTAMPED)
    assert patch.passed and patch.evidence["version"] == "1.2.4" and patch.evidence["tag"] == "v1.2.4"
    assert patch.evidence["latest_tag"] == "v1.2.3"
    minor = version_rule.evaluate(tmp_path, point=_point("minor"), tag_prefix="v", state=UNSTAMPED)
    assert minor.evidence["version"] == "1.3.0"


def test_no_recommendation(tmp_path, tags):
    r = version_rule.evaluate(tmp_path, point=_point(), tag_prefix="v", state=UNSTAMPED)
    assert not r.passed and r.reason == "no-recommendation"


def test_tag_exists_on_origin(tmp_path, tags):
    tags["origin"] = {"v1.2.4"}
    r = version_rule.evaluate(tmp_path, point=_point("patch"), tag_prefix="v", state=UNSTAMPED)
    assert not r.passed and r.reason == "tag-exists"


def test_not_an_increase(tmp_path, tags, monkeypatch):
    monkeypatch.setattr(version_rule, "_bump", lambda parts, level: parts)
    r = version_rule.evaluate(tmp_path, point=_point("patch"), tag_prefix="v", state=UNSTAMPED)
    assert not r.passed and r.reason == "not-an-increase"


def test_stamped_state_reuses_version(tmp_path, tags):
    tags["origin"] = {"v1.2.4"}
    state = ReleaseState("stamped", version="1.2.4", tag="v1.2.4")
    r = version_rule.evaluate(tmp_path, point=_point(), tag_prefix="v", state=state)
    assert r.passed and r.evidence["version"] == "1.2.4"


def test_first_release_starts_from_zero(tmp_path, tags):
    tags["latest"] = None
    r = version_rule.evaluate(tmp_path, point=_point("patch"), tag_prefix="v", state=UNSTAMPED)
    assert r.passed and r.evidence["version"] == "0.0.1"
    r = version_rule.evaluate(tmp_path, point=_point("minor"), tag_prefix="v", state=UNSTAMPED)
    assert r.evidence["version"] == "0.1.0"
