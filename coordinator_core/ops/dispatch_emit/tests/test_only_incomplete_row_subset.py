"""`--only`: an --only-incomplete re-emit restricted to named incomplete rows; chunks[] omits landed/coded rows."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.dispatch_emit import cli
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

_PLAN = "docs/plans/p.md"


def _plan() -> str:
    rows = [("C0", None, "coded"), ("C1", None, "open"), ("C2", None, "open"),
            ("C3", "C2", "open"), ("C4", None, "open"), ("C5", None, "open")]
    out = ""
    for rid, dep, disp in rows:
        edge = f"  depends_on:\n    - chunk: {dep}\n      gate_kind: output-consumption-runtime\n" if dep else ""
        out += (
            f"- id: {rid}\n  title: t{rid}\n  change_kind: doc-edit\n  surface: docs/x/{rid}.md\n"
            f"  writes:\n    - docs/x/{rid}.md\n  queue_scope: project\n  disposition: {disp}\n"
            f"{edge}  body: |\n    Do {rid}.\n"
        )
    return (
        '---\ntitle: "p"\nsizing_object: null\n---\n\n# p\n\n## Goal\n\ng\n\n'
        f"## Tasks\n\n```yaml plan-tasks\n{out}```\n"
    )


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, **no_console_creationflags())


@pytest.fixture
def repo(tmp_path, monkeypatch) -> Path:
    r = tmp_path / "repo"
    (r / "docs" / "plans").mkdir(parents=True)
    _git(r, "init", "-q")
    _git(r, "config", "user.email", "t@t.example")
    _git(r, "config", "user.name", "t")
    (r / _PLAN).write_text(_plan(), encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "seed")
    monkeypatch.chdir(r)
    return r


def _run_text(repo: Path, body: str = "nothing landed\n") -> str:
    p = repo.parent / "run.txt"
    p.write_text(body, encoding="utf-8")
    return str(p)


def _emit(repo: Path, *extra: str) -> tuple:
    out = repo / "p.workflow.mjs"
    out.unlink(missing_ok=True)
    code = cli.main(["--plan", str(repo / _PLAN), "--out", str(out), *extra])
    return code, (out.read_text(encoding="utf-8") if out.exists() else "")


def _registered(script: str) -> set:
    return set(re.findall(r"_rows\['(\w+)'\] = _runRow\(", script))


def _chunks(script: str) -> set:
    m = re.search(r"chunks:\s*\[([^\]]*)\]", script)
    assert m, "emitted script carries no chunks literal"
    return set(re.findall(r"['\"](\w+)['\"]", m.group(1)))


def test_only_emits_exactly_the_named_incomplete_rows(repo):
    code, script = _emit(repo, "--only-incomplete", _run_text(repo), "--only", "C1,C4")

    assert code == cli.EXIT_OK
    assert _registered(script) == {"C1", "C4"}


def test_only_with_landed_rows_in_the_run_text_composes(repo):
    text = _run_text(repo, "abc checkpoint(wave 1): 1 rows — C1\n")

    code, script = _emit(repo, "--only-incomplete", text, "--only", "C4")

    assert code == cli.EXIT_OK
    assert _registered(script) == {"C4"}


def test_only_refuses_an_id_that_is_not_a_plan_row(repo, capsys):
    code, script = _emit(repo, "--only-incomplete", _run_text(repo), "--only", "C1,ZZ")

    assert code == cli.EXIT_USAGE and script == ""
    err = capsys.readouterr().err
    assert "ZZ" in err and err.count("\n") == 1


def test_only_refuses_a_landed_or_coded_row(repo, capsys):
    text = _run_text(repo, "abc checkpoint(wave 1): 1 rows — C1\n")

    assert _emit(repo, "--only-incomplete", text, "--only", "C1")[0] == cli.EXIT_USAGE
    assert _emit(repo, "--only-incomplete", text, "--only", "C0")[0] == cli.EXIT_USAGE
    assert "C1" in capsys.readouterr().err


def test_only_refuses_a_row_whose_predecessor_is_left_out(repo, capsys):
    code, _ = _emit(repo, "--only-incomplete", _run_text(repo), "--only", "C3")

    assert code == cli.EXIT_USAGE
    assert "C3 depend on" in capsys.readouterr().err


def test_only_with_its_predecessor_named_emits_both(repo):
    code, script = _emit(repo, "--only-incomplete", _run_text(repo), "--only", "C2,C3")

    assert code == cli.EXIT_OK
    assert _registered(script) == {"C2", "C3"}


def test_only_needs_only_incomplete_and_excludes_hold(repo, capsys):
    assert _emit(repo, "--only", "C1")[0] == cli.EXIT_USAGE
    assert "--only is accepted only with --only-incomplete" in capsys.readouterr().err
    code, _ = _emit(repo, "--only-incomplete", _run_text(repo), "--only", "C1", "--hold", "C4", "--hold-reason", "r")
    assert code == cli.EXIT_USAGE
    assert "exclusive of --hold" in capsys.readouterr().err


def test_rows_is_still_review_only(repo, capsys):
    code, _ = _emit(repo, "--rows", "C1")

    assert code == cli.EXIT_USAGE
    assert "--rows is accepted only with --review-only" in capsys.readouterr().err


def test_a_reemit_chunks_list_omits_landed_and_coded_rows(repo):
    text = _run_text(repo, "abc checkpoint(wave 1): 2 rows — C1, C4\n")

    code, script = _emit(repo, "--only-incomplete", text)

    assert code == cli.EXIT_OK
    assert _chunks(script) == {"C2", "C3", "C5"}
