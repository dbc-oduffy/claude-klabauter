"""upgrade_plan: the structured per-plan result, and the CLI rendering pinned byte-for-byte."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
from contextlib import redirect_stdout
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "mise-prep-upgrade.py"


def _load_cli_module():
    name = "mise_prep_upgrade_upgrade_plan_test"
    loader = importlib.machinery.SourceFileLoader(name, str(_CLI_PATH))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()

_DERIVABLE = '---\ntitle: "A"\nsizing_object: state/sizings/x.yaml\nstatus: draft\n---\n\n# A\n'
_RESIDUE_ONLY = '---\ntitle: "B"\nstatus: draft\n---\n\nThere are 217 things.\n'
_BROKEN = "---\ntitle: [unclosed\n---\n\n# broken\n"

_RESIDUE_LINES = (
    "    AUTHOR: prime_exit_criterion.statement: a falsifiable outcome sentence — authorship\n"
    "    AUTHOR: spine: no `yaml plan-tasks` block — inventing rows would invent scope\n"
)
_B_BLOCK = (
    "  2026-09-08-beta.md\n"
    "    AUTHOR: census: body carries 1 count-shaped claim(s) (217) — each needs "
    "question/command/result, or an argued `census: []`\n"
    "    AUTHOR: prime_exit_criterion.derived_from: no sizing_object to derive from\n"
) + _RESIDUE_LINES
_TAIL = (
    "residue is authorship, not a conversion gap — a census entry or an "
    "exit criterion written by a script would declare what nobody checked.\n"
)


def _golden(verb: str, summary: str) -> str:
    return (
        "  2026-09-08-alpha.md\n"
        f"    {verb}: census, prime_exit_criterion.derived_from\n"
        + _RESIDUE_LINES
        + _B_BLOCK
        + f"\nmise-prep-upgrade: 2 plan(s); {summary}; 2 still need authoring; 0 unreadable\n"
        + "  "
        + _TAIL
    )


def _plan(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8", newline="\n")
    return path


def test_check_writes_nothing_and_reports_changed(tmp_path):
    path = _plan(tmp_path, "2026-09-08-alpha.md", _DERIVABLE)
    result = _cli.upgrade_plan(path, check=True)
    assert path.read_text(encoding="utf-8") == _DERIVABLE
    assert result["changed"] is True
    assert result["error"] is None
    assert result["derived"] == ["census", "prime_exit_criterion.derived_from"]
    assert result["plan"] == path


def test_write_mode_writes_and_reports_changed(tmp_path):
    path = _plan(tmp_path, "2026-09-08-alpha.md", _DERIVABLE)
    result = _cli.upgrade_plan(path, check=False)
    assert result["changed"] is True
    assert "census: []" in path.read_text(encoding="utf-8")
    assert _cli.upgrade_plan(path, check=False)["derived"] == []


def test_nothing_derivable_is_unchanged_with_residue(tmp_path):
    path = _plan(tmp_path, "2026-09-08-beta.md", _RESIDUE_ONLY)
    result = _cli.upgrade_plan(path, check=False)
    assert result["changed"] is False
    assert result["derived"] == []
    assert result["residue"]
    assert path.read_text(encoding="utf-8") == _RESIDUE_ONLY


def test_unparseable_frontmatter_returns_error(tmp_path):
    path = _plan(tmp_path, "2026-09-08-broken.md", _BROKEN)
    result = _cli.upgrade_plan(path, check=False)
    assert result["error"]
    assert result["derived"] == []
    assert result["changed"] is False
    assert path.read_text(encoding="utf-8") == _BROKEN


def _run_main(argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = _cli.main(argv)
    return rc, buf.getvalue()


def test_main_stdout_pinned_check(tmp_path):
    a = _plan(tmp_path, "2026-09-08-alpha.md", _DERIVABLE)
    b = _plan(tmp_path, "2026-09-08-beta.md", _RESIDUE_ONLY)
    rc, out = _run_main(["--check", str(a), str(b)])
    assert rc == 1
    assert out == _golden("would derive", "1 would change")


def test_main_stdout_pinned_write(tmp_path):
    a = _plan(tmp_path, "2026-09-08-alpha.md", _DERIVABLE)
    b = _plan(tmp_path, "2026-09-08-beta.md", _RESIDUE_ONLY)
    rc, out = _run_main([str(a), str(b)])
    assert rc == 1
    assert out == _golden("derived", "1 changed")


def _census_plan(commands):
    items = "".join(
        f"  - question: q{i}\n    command: {c}\n    result: r{i}\n" for i, c in enumerate(commands)
    )
    return f'---\ntitle: "C"\nstatus: draft\ncensus:\n{items}---\n\n# C\n'


def _census_commands(path):
    import yaml

    fm = yaml.safe_load(path.read_text(encoding="utf-8").split("---\n")[1])
    return [e["command"] for e in fm["census"]]


def test_backtick_wrapped_command_is_rewritten_and_screens_clean(tmp_path):
    from coordinator_core.roadmap import census

    path = _plan(tmp_path, "2026-09-08-gamma.md", _census_plan(["'echo `grep -c foo a.txt`'"]))
    result = _cli.upgrade_plan(path, check=False)
    assert result["changed"] is True
    assert "census.commands" in result["derived"]
    (cmd,) = _census_commands(path)
    assert cmd == "grep -c foo a.txt"
    assert census.screen(cmd) is None


def test_wc_file_list_substitution_becomes_a_pipe(tmp_path):
    path = _plan(tmp_path, "2026-09-08-gamma.md", _census_plan(["'wc -l $(find . -name x)'"]))
    _cli.upgrade_plan(path, check=False)
    assert _census_commands(path) == ["find . -name x | wc -l"]


def test_for_loop_is_reported_not_rewritten_and_entry_kept(tmp_path):
    loop = "'for f in a b; do wc -l $f; done'"
    text = _census_plan([loop, "'grep -c x a'"])
    path = _plan(tmp_path, "2026-09-08-gamma.md", text)
    result = _cli.upgrade_plan(path, check=False)
    assert result["changed"] is False
    assert any("census[0].command" in r and "never delete the premise" in r for r in result["residue"])
    assert path.read_text(encoding="utf-8") == text


def test_clean_command_untouched_and_no_entry_removed(tmp_path):
    text = _census_plan(["'grep -c x a'", "'echo $(for f in a; do :; done)'"])
    path = _plan(tmp_path, "2026-09-08-gamma.md", text)
    _cli.upgrade_plan(path, check=False)
    assert len(_census_commands(path)) == 2
    assert _census_commands(path)[0] == "grep -c x a"
