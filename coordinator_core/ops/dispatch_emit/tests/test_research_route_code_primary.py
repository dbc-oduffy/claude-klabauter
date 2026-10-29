"""dispatch.emit --from-sizing with research.primary=repo emits repo segments first, then web segments briefed from the repo syntheses."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.dispatch_emit.tests.test_cli_research_route import _write_sizing
from coordinator_core.ops.dispatch_emit.tests.test_research_emit_script_cap import _Admitted
import coordinator_core.ops.dispatch_emit as _pkg

_FIXTURES = Path(__file__).parent / "fixtures"
_DIR = Path("pipelines") / "deep-research"


def _content_root(tmp_path: Path) -> Path:
    root = tmp_path / "content"
    shutil.copytree(_FIXTURES / "pipeline_research_multi_repo", root)
    for src in (_FIXTURES / "pipeline_research" / _DIR).glob("web*"):
        shutil.copy(src, root / _DIR / src.name)
    return root


def _emit(monkeypatch, tmp_path, capsys, refs, primary):
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: _content_root(tmp_path))
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-code-primary")
    flags = ["--research-class", "corpus", "--research-source", "repo", "--research-source", "web"]
    for ref in refs:
        flags += ["--research-target", f"repo={(tmp_path / 'targets' / ref).as_posix()}"]
    rel = _write_sizing(tmp_path, *flags)
    if primary:
        path = tmp_path / rel
        text = path.read_text(encoding="utf-8")
        assert "\nresearch:\n" in text
        path.write_text(text.replace("\nresearch:\n", "\nresearch:\n  primary: repo\n", 1), encoding="utf-8")
    capsys.readouterr()
    assert cli_module.main(["--from-sizing", rel]) == cli_module.EXIT_OK
    reply = json.loads(capsys.readouterr().out)
    return reply


def test_primary_repo_orders_repo_segments_before_web_and_briefs_web_from_syntheses(monkeypatch, tmp_path, capsys):
    reply = _emit(monkeypatch, tmp_path, capsys, ("alpha", "beta"), primary=True)
    assert [s["segment"] for s in reply["segments"]] == ["repo-1-alpha", "repo-2-beta", "repo-compare", "web"]
    script = Path(reply["path"]).read_text(encoding="utf-8")
    gap = tmp_path / reply["scratch_dir"] / "web-after-repo" / "brief.md"
    text = gap.read_text(encoding="utf-8")
    assert "repo-1-alpha/synthesis.md" in text and "repo-2-beta/synthesis.md" in text
    assert "Open Questions" in text
    assert "Rank code claims above web claims" in text
    assert "web-after-repo/brief.md" in script
    synthesis_brief = (tmp_path / reply["scratch_dir"] / "brief.md").read_text(encoding="utf-8")
    assert "Rank code claims above web claims" in synthesis_brief


def test_primary_repo_with_one_target_gets_a_keyed_segment(monkeypatch, tmp_path, capsys):
    reply = _emit(monkeypatch, tmp_path, capsys, ("alpha",), primary=True)
    assert [s["segment"] for s in reply["segments"]] == ["repo-1-alpha", "web"]


def test_without_primary_a_lone_repo_stays_unkeyed_and_no_gap_brief_is_written(monkeypatch, tmp_path, capsys):
    reply = _emit(monkeypatch, tmp_path, capsys, ("alpha",), primary=False)
    assert "repo-1-alpha" not in [s["segment"] for s in reply["segments"]]
    assert not (tmp_path / reply["scratch_dir"] / "web-after-repo").exists()
    brief = tmp_path / reply["scratch_dir"] / "brief.md"
    assert not brief.exists() or "Rank code claims" not in brief.read_text(encoding="utf-8")
