"""recommend_level and the version-bump rule: levels, tag collisions, reuse once stamped."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from coordinator_core import merge_assemble
from coordinator_core.win_portability import no_console_creationflags
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


def _raw_entry(root: Path, name: str, frontmatter: str, month: str = "2026-10") -> None:
    d = root / "archive" / "completed" / month
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}.md").write_text(f"---\n{frontmatter}---\n\nbody\n", encoding="utf-8")


@pytest.mark.spawns_process
def test_recommend_level_imports_neither_heavy_module(tmp_path):
    _header(tmp_path, "2026-10-05")
    _entry(tmp_path, "a", "roadmap")
    code = (
        "import sys\n"
        "from pathlib import Path\n"
        "from coordinator_core.workweek_complete.release import version_rule\n"
        f"r = version_rule.recommend_level(Path({str(tmp_path)!r}))\n"
        "assert r and r['disposition'] == 'minor', r\n"
        "heavy = [m for m in ('coordinator_core.merge_assemble',"
        " 'coordinator_core.ops.ceremony.records_query', 'coordinator_core.ops.records_query',"
        " 'coordinator_core.workweek_start.header', 'yaml') if m in sys.modules]\n"
        "assert not heavy, heavy\n"
    )
    subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        cwd=Path(__file__).resolve().parents[4],
        **no_console_creationflags(),
    )


def test_week_start_literals_match_header():
    from coordinator_core.workweek_start import header

    assert version_rule._WEEK_DIR == header.WEEK_DIR
    assert version_rule._BARE_DATE_RE.pattern == header._BARE_DATE_RE.pattern


@pytest.mark.parametrize(
    "frontmatter",
    [
        'title: "t"\ncreated: 2026-10-12\nnature: roadmap\nstatus: pending-release\n',
        'title: "t"\ncreated: "2026-10-12"\nnature: "roadmap"\nstatus: "pending-release"\n',
        "created: '2026-10-12'\nnature: 'roadmap'\nstatus: 'pending-release'\n",
        "created: 2026-10-12\nnature: roadmap  # one of: roadmap|bugfix|tech-debt|infra\n"
        "status: pending-release\n",
        "created: 2026-10-12  # today\nnature: roadmap\nstatus: pending-release  # unreleased\n",
        "created: 2026-10-12\r\nnature: roadmap\r\nstatus: pending-release\r\n",
        "loe:\n  status: released\n  nature: bugfix\ncreated: 2026-10-12\nnature: roadmap\n"
        "status: pending-release\n",
    ],
)
def test_writer_shapes_parse(tmp_path, frontmatter):
    _header(tmp_path, "2026-10-05")
    _raw_entry(tmp_path, "a", frontmatter)
    result = version_rule.recommend_level(tmp_path)
    assert result is not None and result["disposition"] == "minor"
    assert "1 pending-release entries (roadmap=1)" in result["rationale"]


def test_missing_nature_counts_unknown_and_missing_created_is_skipped_with_week_start(tmp_path):
    _header(tmp_path, "2026-10-05")
    _raw_entry(tmp_path, "a", "created: 2026-10-12\nstatus: pending-release\n")
    _raw_entry(tmp_path, "b", "nature: roadmap\nstatus: pending-release\n")
    result = version_rule.recommend_level(tmp_path)
    assert result["disposition"] == "patch" and "unknown=1" in result["rationale"]


def test_months_before_week_start_are_not_read(tmp_path):
    _header(tmp_path, "2026-10-05")
    _raw_entry(
        tmp_path,
        "old",
        "created: 2026-09-30\nnature: roadmap\nstatus: pending-release\n",
        month="2026-09",
    )
    assert version_rule.recommend_level(tmp_path) is None


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
