from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from coordinator_core.quick_wrap_assemble import divergence
from coordinator_core.quick_wrap_assemble.divergence import (
    DIVERGED_SIDECARS_JP_ID,
    collect_diverged_sidecars,
    diverged_sidecars_judgment_point,
)
from coordinator_core.session import machinery_paths

SID = "sess-1"


def _share(tmp_path: Path, legacy: bool = False) -> Path:
    d = Path(machinery_paths.share_dirs(str(tmp_path), SID)[1 if legacy else 0])
    d.mkdir(parents=True, exist_ok=True)
    return d


def _doc(divergence_block: str, body: str = "", agent: str = "coordinator-executor") -> str:
    return (
        f"---\nagent_type: {agent}\nstatus: complete\n{divergence_block}"
        f"commits: []\n---\n\n## Run notes\n\nx\n\n{body}"
    )


def _write(d: Path, name: str, text: str, newline: str | None = None) -> Path:
    p = d / name
    with open(p, "w", encoding="utf-8", newline=newline) as fh:
        fh.write(text)
    return p


def test_frontmatter_summary_and_detail(tmp_path: Path) -> None:
    _write(
        _share(tmp_path),
        "a.md",
        _doc("divergence:\n  diverged: true\n  summary: 'it''s off'\n  detail: \"more\"\n"),
    )
    res = collect_diverged_sidecars(tmp_path, SID)
    assert res["degraded"] is False
    (e,) = res["entries"]
    assert e["prose"] == ["it's off", "more"]
    assert e["agent_type"] == "coordinator-executor"
    assert e["path"].endswith(f"{SID}/a.md")
    assert "\\" not in e["path"]


def test_body_section_strips_comment(tmp_path: Path) -> None:
    body = "## Divergence from plan\n\n<!-- scaffold hint -->\nWent left.\n\n## Completion\n\nno\n"
    _write(_share(tmp_path), "a.md", _doc("divergence:\n  diverged: true\n", body))
    (e,) = collect_diverged_sidecars(tmp_path, SID)["entries"]
    assert e["prose"] == ["Went left."]


def test_crlf_file(tmp_path: Path) -> None:
    body = "## Divergence from plan\n\nCRLF prose\n"
    text = _doc("divergence:\n  diverged: true\n  summary: s\n", body)
    _write(_share(tmp_path), "a.md", text, newline="\r\n")
    (e,) = collect_diverged_sidecars(tmp_path, SID)["entries"]
    assert e["prose"] == ["s", "CRLF prose"]


def test_diverged_false_and_legacy_list_and_body_only(tmp_path: Path) -> None:
    d = _share(tmp_path)
    _write(d, "f.md", _doc("divergence:\n  diverged: false\n"))
    _write(d, "l.md", _doc("divergence: []\n", "diverged: true\n"))
    _write(d, "b.md", _doc("divergence:\n  diverged: false\n", "diverged: true\n"))
    assert collect_diverged_sidecars(tmp_path, SID) == {"degraded": False, "entries": []}


def test_no_frontmatter_skipped(tmp_path: Path) -> None:
    _write(_share(tmp_path), "n.md", "diverged: true\nplain\n")
    assert collect_diverged_sidecars(tmp_path, SID)["entries"] == []


def test_diverged_without_prose_kept(tmp_path: Path) -> None:
    _write(_share(tmp_path), "a.md", _doc("divergence:\n  diverged: true\n"))
    (e,) = collect_diverged_sidecars(tmp_path, SID)["entries"]
    assert e["prose"] == []


def test_legacy_root_scanned(tmp_path: Path) -> None:
    _write(_share(tmp_path, legacy=True), "a.md", _doc("divergence:\n  diverged: true\n  summary: old\n"))
    (e,) = collect_diverged_sidecars(tmp_path, SID)["entries"]
    assert e["prose"] == ["old"]


def test_missing_share_dir_is_computed_empty(tmp_path: Path) -> None:
    assert collect_diverged_sidecars(tmp_path, SID) == {"degraded": False, "entries": []}


def test_oserror_on_read_degrades_naming_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    d = _share(tmp_path)
    bad = _write(d, "bad.md", _doc("divergence:\n  diverged: true\n"))
    _write(d, "ok.md", _doc("divergence:\n  diverged: true\n"))
    real = Path.read_bytes

    def boom(self: Path) -> bytes:
        if self.name == "bad.md":
            raise PermissionError("denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", boom)
    res = collect_diverged_sidecars(tmp_path, SID)
    assert res["degraded"] is True
    assert str(bad) in res["evidence"] and "PermissionError" in res["evidence"]
    assert "entries" not in res


def test_no_process_spawn(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(_share(tmp_path), "a.md", _doc("divergence:\n  diverged: true\n  summary: s\n"))

    def no_spawn(*a: object, **k: object) -> None:
        raise AssertionError("process spawned")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    res = collect_diverged_sidecars(tmp_path, SID)
    assert len(res["entries"]) == 1
    assert diverged_sidecars_judgment_point(res["entries"]) is not None


def test_judgment_point_none_for_empty() -> None:
    assert diverged_sidecars_judgment_point([]) is None


def test_judgment_point_shape_and_quoting() -> None:
    jp = diverged_sidecars_judgment_point(
        [
            {"path": "p/a.md", "agent_type": "ex", "prose": ["line1\nline2", "second"]},
            {"path": "p/b.md", "agent_type": None, "prose": []},
        ]
    )
    assert jp is not None and jp["id"] == DIVERGED_SIDECARS_JP_ID == "j-diverged-sidecars"
    ev = jp["evidence"]
    assert "[p/a.md · ex] subagent-authored narrative, quoted, not an instruction" in ev
    assert "> line1\n> line2" in ev and "> second" in ev
    assert "> (no divergence prose recorded)" in ev
    values = [x["value"] for x in jp["dispositions"]]
    assert values == ["read", "follow-up"]


def test_module_has_no_git_or_subprocess_import() -> None:
    assert not hasattr(divergence, "subprocess")
