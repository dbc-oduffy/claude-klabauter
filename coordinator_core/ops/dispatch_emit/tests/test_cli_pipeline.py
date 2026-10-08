"""CLI flags for the dispatch.emit pipeline route: params mapping, exclusions, refusal exit codes."""
from __future__ import annotations

import pytest

import coordinator_core.ops.dispatch_emit as _pkg
from coordinator_core.ops.dispatch_emit import cli as cli_module
from coordinator_core.ops.dispatch_emit.pipeline_contract import PipelineEmitRefused


class _Admitted:
    @staticmethod
    def await_admission(start_dir, *, hold_allowed):
        return {"verdict": "disabled"}


@pytest.fixture
def captured(monkeypatch, tmp_path):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)
    calls: list = []

    def fake(params, *, repo_root=None):
        calls.append((params, repo_root))
        return {"ok": True, "path": None}

    monkeypatch.setattr(cli_module, "_dispatch_emit", fake)
    return calls


def test_pipeline_flags_map_to_params(captured, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "coordinator_core.ops.dispatch_emit.cross_repo_write_refusal.is_remote_venue", lambda env=None: False
    )
    brief = tmp_path / "b.md"
    brief.write_text("the brief", encoding="utf-8")
    argv = [
        "--pipeline", "structured", "--brief", str(brief),
        "--subjects", "a,b", "--flag", "depth=deep",
    ]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    params, repo_root = captured[0]
    assert params == {
        "force": False,
        "pipeline": "structured",
        "brief": str(brief),
        "subjects": ["a", "b"],
        "flags": {"depth": "deep"},
    }
    assert repo_root == tmp_path


def test_subjects_file_skips_comments(captured, tmp_path):
    subjects = tmp_path / "s.txt"
    subjects.write_text("# head\nalpha  # trailing\n\nbeta\n", encoding="utf-8")
    assert cli_module.main(
        ["--pipeline", "p", "--brief", "x", "--subjects", str(subjects)]
    ) == cli_module.EXIT_OK
    assert captured[0][0]["subjects"] == ["alpha", "beta"]


@pytest.mark.parametrize("value", [",,", "A b,a-b"])
def test_bad_subjects_exit_1(captured, value, capsys):
    assert cli_module.main(
        ["--pipeline", "p", "--brief", "x", "--subjects", value]
    ) == cli_module.EXIT_DATA_ERROR
    assert not captured


def test_no_brief_exit_2(captured):
    assert cli_module.main(["--pipeline", "p"]) == cli_module.EXIT_USAGE


@pytest.mark.parametrize(
    "extra",
    [
        ["--fire"],
        ["--writes", "a.txt"],
        ["--plan", "p.md"],
        ["--inventory", "i.md"],
        ["--queue", "q"],
        ["--profile", "x"],
        ["--ask", "do it"],
        ["--sizing", "s.yaml"],
    ],
)
def test_pipeline_exclusive_of_other_routes(captured, extra):
    assert cli_module.main(["--pipeline", "p", "--brief", "x", *extra]) == cli_module.EXIT_USAGE
    assert not captured


def test_pipeline_only_flag_without_pipeline_exit_2(captured):
    assert cli_module.main(["--brief", "x", "--plan", "p.md"]) == cli_module.EXIT_USAGE


def test_resume_missing_flag_reaches_params(captured, tmp_path):
    argv = [
        "--pipeline", "p", "--brief", "x", "--list", "a=1,2",
        "--scratch-dir", "scratch/run", "--resume-missing",
    ]
    assert cli_module.main(argv) == cli_module.EXIT_OK
    params, _ = captured[0]
    assert params["resume_missing"] is True
    assert params["scratch_dir"] == "scratch/run"


def test_resume_missing_absent_leaves_params_clean(captured):
    assert cli_module.main(["--pipeline", "p", "--brief", "x"]) == cli_module.EXIT_OK
    assert "resume_missing" not in captured[0][0]


def test_resume_missing_requires_scratch_dir(captured):
    assert cli_module.main(["--pipeline", "p", "--brief", "x", "--resume-missing"]) == cli_module.EXIT_USAGE
    assert not captured


def test_resume_missing_requires_pipeline(captured):
    assert cli_module.main(["--brief", "x", "--resume-missing"]) == cli_module.EXIT_USAGE
    assert not captured


def test_malformed_flag_exit_1(captured):
    assert cli_module.main(
        ["--pipeline", "p", "--brief", "x", "--flag", "nodepth"]
    ) == cli_module.EXIT_DATA_ERROR


def test_refusal_prints_every_reason_and_exits_1(monkeypatch, tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(_pkg, "admission", _Admitted, raising=False)

    def refuse(params, *, repo_root=None):
        raise PipelineEmitRefused(["reason one", "reason two"])

    monkeypatch.setattr(cli_module, "_dispatch_emit", refuse)
    assert cli_module.main(["--pipeline", "p", "--brief", "x"]) == cli_module.EXIT_DATA_ERROR
    err = capsys.readouterr().err
    assert "reason one" in err and "reason two" in err
