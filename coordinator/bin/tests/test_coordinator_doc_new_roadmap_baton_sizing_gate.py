"""test_coordinator_doc_new_roadmap_baton_sizing_gate — `--type roadmap-baton`
is held to the same explicit-sizing-answer bar as `--type plan`.

A roadmap arrives THROUGH the sizing lobby, and `roadmap-planning` assigns
every stub its own `loe:` at mint — so a roadmap baton is sized work by
construction. The FK simply went unwritten, because writing it depended on a
skill step remembering to pass a flag. Seven `conversion-vehicle` batons
(`ccv-01..07`) carried no `sizing_object` as a result, which made
`coordinator_core.sizing_disposition` read PM-ratified work as `unsized` and
would have bounced it back to the lobby to re-make a size that already
existed.

"The operator remembers" is not an artifact that discharges a rule. The
scaffolder is the artifact: it refuses to write a roadmap baton without an
answer, exactly as it already does for a plan.

Cross-repo ask: `cross-repo/inbox/2026-08-20-doe-claude-em-pickup-brief-
should-emit-the-sizing-disposition.md` (follow-on ask). Sender-side fix that
motivated it: DoE-claude `c34f05d58`.

In-process by construction — `main()` is driven through `sys.argv` rather
than spawned, so this file stays on the fast tier and adds nothing to the
spawn ratchet. The end-to-end CLI spawn is already covered by this
directory's `test_coordinator_doc_new_roadmap_baton_self_validation.py`.

Run: python3 -m pytest coordinator/bin/tests/test_coordinator_doc_new_roadmap_baton_sizing_gate.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "coordinator" / "bin" / "coordinator-doc-new.py"


def _load_cli():
    """The scaffolder as a module. Loaded by path because the file's name is
    hyphenated and it is a CLI, not an importable package member."""
    spec = importlib.util.spec_from_file_location("coordinator_doc_new_under_test", _CLI_PATH)
    assert spec is not None and spec.loader is not None, f"could not load {_CLI_PATH}"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def cli():
    return _load_cli()


def _run(cli, monkeypatch, tmp_path: Path, *args: str) -> int:
    """`main()` under a supplied argv, returning its exit code."""
    monkeypatch.setattr(sys, "argv", ["coordinator-doc-new", *args])
    monkeypatch.chdir(tmp_path)
    try:
        cli.main()
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _baton_args(out_path: Path) -> list[str]:
    return [
        "--type", "roadmap-baton",
        "--title", "a stub",
        "--roadmap-id", "rm-probe",
        "--stub-id", "rm-probe-01",
        "--out", str(out_path),
    ]


def test_refuses_without_an_explicit_sizing_answer(cli, monkeypatch, tmp_path, capsys):
    """The whole point: neither flag supplied is a refusal, not a silent
    omission that surfaces as `unsized` weeks later at pickup."""
    out_path = tmp_path / "baton.md"

    code = _run(cli, monkeypatch, tmp_path, *_baton_args(out_path))

    assert code == 1
    assert not out_path.exists(), "refused invocation must write no file"
    err = capsys.readouterr().err
    assert "roadmap-baton" in err
    assert "coordinator:sizing" in err


def test_refuses_both_flags_together(cli, monkeypatch, tmp_path):
    out_path = tmp_path / "baton.md"

    code = _run(
        cli, monkeypatch, tmp_path,
        *_baton_args(out_path), "--no-sizing-object", "--sizing-object", "state/sizings/x.yaml",
    )

    assert code == 1
    assert not out_path.exists()


def test_refuses_a_sizing_object_that_does_not_resolve(cli, monkeypatch, tmp_path):
    """Same bar as the plan arm: a cited path that resolves to nothing is
    refused at write time rather than written and left to dangle."""
    out_path = tmp_path / "baton.md"

    code = _run(
        cli, monkeypatch, tmp_path,
        *_baton_args(out_path), "--sizing-object", "state/sizings/absent.yaml",
    )

    assert code == 1
    assert not out_path.exists()


def test_resolving_sizing_object_is_emitted_as_a_real_key(cli, monkeypatch, tmp_path):
    sizing_rel = "state/sizings/2026-08-20-a-real-one.yaml"
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / sizing_rel).write_text("id: a-real-one\nroute: roadmap\n", encoding="utf-8")
    out_path = tmp_path / "baton.md"

    code = _run(cli, monkeypatch, tmp_path, *_baton_args(out_path), "--sizing-object", sizing_rel)

    assert code == 0
    body = out_path.read_text(encoding="utf-8")
    assert f'sizing_object: "{sizing_rel}"' in body


def test_no_sizing_object_emits_explicit_null(cli, monkeypatch, tmp_path):
    """Declared absence is checkable; an absent key is not. This is the
    difference the sizing axis reads."""
    out_path = tmp_path / "baton.md"

    code = _run(cli, monkeypatch, tmp_path, *_baton_args(out_path), "--no-sizing-object")

    assert code == 0
    assert "sizing_object: null" in out_path.read_text(encoding="utf-8")


def test_scaffolded_baton_reads_as_sized(cli, monkeypatch, tmp_path):
    """The end the whole change exists for: a scaffolded roadmap baton is
    `sized` to the pickup brief, so a PM-ratified stub is never bounced back
    to the lobby to re-make a size that already exists."""
    from coordinator_core.sizing_disposition import compute_sizing_disposition

    sizing_rel = "state/sizings/2026-08-20-routed-this-roadmap.yaml"
    (tmp_path / "state" / "sizings").mkdir(parents=True)
    (tmp_path / sizing_rel).write_text("id: routed\nroute: roadmap\n", encoding="utf-8")
    out_path = tmp_path / "baton.md"

    assert _run(cli, monkeypatch, tmp_path, *_baton_args(out_path), "--sizing-object", sizing_rel) == 0

    verdict = compute_sizing_disposition(tmp_path, {"sizing_object": sizing_rel})

    assert verdict["value"] == "sized"
    assert verdict["warning"] is None


def test_plan_arm_is_unchanged(cli, monkeypatch, tmp_path):
    """Negative control — widening the gate to roadmap-baton must not have
    relaxed the arm it was copied from."""
    out_path = tmp_path / "a-plan.md"

    code = _run(
        cli, monkeypatch, tmp_path,
        "--type", "plan", "--title", "a plan", "--out", str(out_path),
    )

    assert code == 1
    assert not out_path.exists()
