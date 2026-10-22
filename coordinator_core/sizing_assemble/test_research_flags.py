"""Tests for `sizing-assemble --research-*`: the flags write a schema-valid `research:` block."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

import coordinator_core.sizing_assemble as sa
from coordinator_core.frontmatter.schema_validate import validate_frontmatter
from coordinator_core.session import record_homes

_NAME = "2026-10-08-r.yaml"
_SCHEMA = (
    Path(sa.__file__).resolve().parent.parent / "frontmatter" / "schemas" / "sizing-object.schema.json"
)
_DRAFT = """schema: sizing-object
intent: "Research the thing"
estimate:
  tshirt: XS
  provisional: true
route: dispatch
detents: []
fork: null
xl_exit: null
status: draft
premise:
  provenance: unrecorded
  evidence: PLACEHOLDER - cite the evidence
deliverable_id: null
"""


@pytest.fixture()
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    (tmp_path / ".git").mkdir()
    Path(record_homes.home_dir(str(tmp_path), "sizings")).mkdir(parents=True)
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _run(repo: Path, *extra: str) -> tuple[int, dict]:
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(_DRAFT, encoding="utf-8")
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    code = sa.main(["--tshirt", "XS", "--interaction-mode", "pm", "--write", rel, "--json", *extra])
    return code, yaml.safe_load(path.read_text(encoding="utf-8"))


def test_full_flag_set_writes_a_schema_valid_block(repo: Path) -> None:
    code, doc = _run(
        repo,
        "--research-class", "corpus",
        "--research-appetite", "large",
        "--research-source", "web",
        "--research-source", "repo",
        "--research-source", "web",
        "--research-depth", "deeper",
        "--research-target", "repo=coordinator_core/ops",
        "--research-target", "web=https://example.com/a=b",
    )
    assert code == sa.EXIT_OK
    assert doc["research"] == {
        "value_class": "corpus",
        "appetite": "large",
        "sources": ["web", "repo"],
        "depth": "deeper",
        "targets": [
            {"source": "repo", "ref": "coordinator_core/ops"},
            {"source": "web", "ref": "https://example.com/a=b"},
        ],
    }
    assert validate_frontmatter(doc, _SCHEMA) == []


def test_only_given_keys_are_written(repo: Path) -> None:
    code, doc = _run(repo, "--research-class", "scouts")
    assert code == sa.EXIT_OK
    assert doc["research"] == {"value_class": "scouts"}


def test_research_appetite_never_touches_top_level_appetite(repo: Path) -> None:
    code, doc = _run(repo, "--research-class", "deep", "--research-appetite", "small")
    assert code == sa.EXIT_OK
    assert "appetite" not in doc
    assert doc["research"]["appetite"] == "small"


def test_no_research_flags_writes_no_research_key(repo: Path) -> None:
    code, doc = _run(repo)
    assert code == sa.EXIT_OK
    assert "research" not in doc


@pytest.mark.parametrize(
    "flags",
    [
        ("--research-class", "bogus"),
        ("--research-class", "deep", "--research-appetite", "huge"),
        ("--research-class", "deep", "--research-source", "dns"),
        ("--research-class", "deep", "--research-depth", "abyssal"),
        ("--research-class", "deep", "--research-target", "dns=x"),
        ("--research-class", "deep", "--research-target", "web"),
        ("--research-appetite", "small"),
    ],
)
def test_bad_values_are_refused_with_usage_exit_and_nothing_written(
    repo: Path, flags: tuple[str, ...]
) -> None:
    code, doc = _run(repo, *flags)
    assert code == sa.EXIT_USAGE
    assert "research" not in doc
    assert doc["status"] == "draft"


def test_research_flags_without_write_are_refused(
    capsys: pytest.CaptureFixture[str], repo: Path
) -> None:
    code = sa.main(["--tshirt", "XS", "--interaction-mode", "pm", "--research-class", "deep"])
    assert code == sa.EXIT_USAGE
    assert "--research-* requires --write" in capsys.readouterr().err


def test_one_refusal_per_bad_value(capsys: pytest.CaptureFixture[str], repo: Path) -> None:
    code, _ = _run(
        repo, "--research-class", "x", "--research-depth", "y", "--research-source", "z"
    )
    assert code == sa.EXIT_USAGE
    assert len(capsys.readouterr().err.strip().splitlines()) == 3


def test_a_research_sizing_routes_to_the_research_fire_not_plan(repo: Path, capsys) -> None:
    code, _ = _run(repo, "--research-class", "corpus", "--research-source", "repo")
    assert code == sa.EXIT_OK
    reply = json.loads(capsys.readouterr().out)
    assert reply["stages"]["rows"] == list(sa.RESEARCH_STAGES)
    assert "emit-dispatch-workflow --from-sizing" in reply["next_move"]
    assert "Route to" not in reply["next_move"]


def test_amend_research_changes_one_field_and_keeps_the_rest(repo: Path, capsys) -> None:
    _run(repo, "--research-class", "corpus", "--research-source", "repo", "--research-target", "repo=a/b")
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    assert sa.main(["--amend-research", rel, "--research-depth", "deeper"]) == sa.EXIT_OK
    doc = yaml.safe_load(Path(record_homes.record_path(str(repo), "sizings", _NAME)).read_text(encoding="utf-8"))
    assert doc["research"] == {
        "value_class": "corpus", "sources": ["repo"], "depth": "deeper",
        "targets": [{"source": "repo", "ref": "a/b"}],
    }
    assert sa.main(["--amend-research", rel, "--research-class", "nope"]) == sa.EXIT_BUSINESS_FAIL


def test_research_questions_are_written_and_amended(repo: Path) -> None:
    code, doc = _run(repo, "--research-class", "corpus", "--research-question", "What is X?", "--research-question", "Why Y?")
    assert code == sa.EXIT_OK
    assert doc["research"]["questions"] == ["What is X?", "Why Y?"]
    assert validate_frontmatter(doc, _SCHEMA) == []
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    assert sa.main(["--amend-research", rel, "--research-question", "Only Z?"]) == sa.EXIT_OK
    doc = yaml.safe_load(Path(record_homes.record_path(str(repo), "sizings", _NAME)).read_text(encoding="utf-8"))
    assert doc["research"] == {"value_class": "corpus", "questions": ["Only Z?"]}


def test_amend_research_from_a_subdirectory_resolves_the_repo_root(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _run(repo, "--research-class", "corpus")
    sub = repo / "scratch" / "deep"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    absolute = str(Path(record_homes.record_path(str(repo), "sizings", _NAME)))
    assert sa.main(["--amend-research", absolute, "--research-depth", "deeper"]) == sa.EXIT_OK
    assert sa.main(["--amend-research", rel, "--research-depth", "deepest"]) == sa.EXIT_OK
    doc = yaml.safe_load(Path(absolute).read_text(encoding="utf-8"))
    assert doc["research"]["depth"] == "deepest"


def test_research_destination_is_written_amended_and_validated(repo: Path) -> None:
    code, doc = _run(repo, "--research-class", "corpus", "--research-destination", "local-only")
    assert code == sa.EXIT_OK
    assert doc["research"]["destination"] == "local-only"
    assert validate_frontmatter(doc, _SCHEMA) == []
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    assert sa.main(["--amend-research", rel, "--research-depth", "deeper"]) == sa.EXIT_OK
    doc = yaml.safe_load(Path(record_homes.record_path(str(repo), "sizings", _NAME)).read_text(encoding="utf-8"))
    assert doc["research"]["destination"] == "local-only"
    assert sa.main(["--amend-research", rel, "--research-destination", "elsewhere"]) == sa.EXIT_BUSINESS_FAIL


def test_seeds_are_written_through_the_real_path_and_validate(repo: Path) -> None:
    code, doc = _run(
        repo, "--research-class", "corpus",
        "--research-seed", "video=https://youtu.be/x", "--research-seed", "repo=https://h/o/r",
    )
    assert code == sa.EXIT_OK
    assert doc["research"]["seeds"] == [
        {"kind": "video", "url": "https://youtu.be/x"},
        {"kind": "repo", "url": "https://h/o/r"},
    ]
    assert validate_frontmatter(doc, _SCHEMA) == []


def test_seeds_build_in_order_with_repeats_dropped_and_equals_kept() -> None:
    block, errors = sa.build_research_block(
        "corpus", None, [], None, [], None, None, None, None,
        [
            "video=https://youtu.be/x?t=1",
            "repo=git@host:o/r.git",
            "video=https://youtu.be/x?t=1",
            "doc=https://example.com/a",
        ],
    )
    assert errors == []
    assert block["seeds"] == [
        {"kind": "video", "url": "https://youtu.be/x?t=1"},
        {"kind": "repo", "url": "git@host:o/r.git"},
        {"kind": "doc", "url": "https://example.com/a"},
    ]
    assert "targets" not in block


@pytest.mark.parametrize(
    "seed", ["bogus=https://x", "https://x", "doc=", "doc=   ", "doc=a b"]
)
def test_bad_seeds_are_refused_with_usage_exit(repo: Path, seed: str) -> None:
    code, doc = _run(repo, "--research-class", "corpus", "--research-seed", seed)
    assert code == sa.EXIT_USAGE
    assert "research" not in doc


def test_seed_without_research_class_is_refused(repo: Path) -> None:
    code, doc = _run(repo, "--research-seed", "doc=https://x")
    assert code == sa.EXIT_USAGE
    assert "research" not in doc


def test_amend_forwards_seeds_to_build_research_block(repo: Path, monkeypatch) -> None:
    captured = {}
    real = sa.build_research_block

    def spy(*args):
        captured["args"] = args
        return real(*args)

    monkeypatch.setattr(sa, "build_research_block", spy)
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(_DRAFT + "research:\n  value_class: scouts\n", encoding="utf-8")
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    sa.main(["--amend-research", rel, "--research-seed", "repo=https://h/o/r"])
    assert captured["args"][9] == ["repo=https://h/o/r"]


def test_amend_reopens_the_research_obligation(repo: Path, monkeypatch) -> None:
    import coordinator_core.hooks.watchdog_undischarged_next_move as wd

    opened = []
    monkeypatch.setattr(wd, "open_sizing_routed", lambda root, rel, sid: opened.append(rel))
    monkeypatch.setattr("coordinator_core.session.touch_record.append_touch_claims", lambda *a, **k: None)
    _run(repo, "--research-class", "corpus")
    opened.clear()
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    assert sa.main(["--amend-research", rel, "--research-depth", "deeper"]) == sa.EXIT_OK
    assert opened == [rel]


_QS = ["a?", "b?", "c?"]


def _block(mode=None, targets=(), questions=_QS):
    return sa.build_research_block("scouts", None, [], None, [], list(questions), None, mode, list(targets))


def test_question_mode_and_target_build_the_block() -> None:
    block, errors = _block("combine", ["2=repo:.", "3=local:claude-json-keys", "1=web"])
    assert errors == []
    assert block["question_mode"] == "combine"
    assert block["question_targets"] == [
        {"question": 2, "source": "repo", "ref": "."},
        {"question": 3, "source": "local", "ref": "claude-json-keys"},
        {"question": 1, "source": "web"},
    ]


def test_question_target_ref_keeps_a_colon() -> None:
    block, errors = _block(targets=["1=repo:drive:sub/x"])
    assert errors == []
    assert block["question_targets"] == [{"question": 1, "source": "repo", "ref": "drive:sub/x"}]


@pytest.mark.parametrize(
    "mode,targets,fragment",
    [
        ("both", [], "--research-question-mode must be one of"),
        (None, ["0=web"], "positive integer"),
        (None, ["-1=web"], "positive integer"),
        (None, ["x=web"], "positive integer"),
        (None, ["4=web"], "exceeds"),
        (None, ["1=both"], "source must be one of"),
        (None, ["1=repo"], "needs a ref"),
        (None, ["1=local:"], "needs a ref"),
        (None, ["1=web:x"], "takes no ref"),
        (None, ["web"], "expected <n>="),
    ],
)
def test_question_target_bad_values_are_refused(mode, targets, fragment) -> None:
    block, errors = _block(mode, targets)
    assert block is None
    assert len(errors) == 1 and fragment in errors[0]


def test_question_target_without_questions_is_refused() -> None:
    block, errors = _block(targets=["1=web"], questions=())
    assert block is None
    assert any("needs --research-question" in e for e in errors)


def test_question_mode_repeat_and_bad_target_are_cli_refusals(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(_DRAFT, encoding="utf-8")
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    base = ["--tshirt", "XS", "--interaction-mode", "pm", "--write", rel, "--research-class", "scouts"]
    code = sa.main([*base, "--research-question", "q?", "--research-question-mode", "split",
                    "--research-question-mode", "combine"])
    assert code == sa.EXIT_USAGE
    capsys.readouterr()
    code = sa.main([*base, "--research-question", "q?", "--research-question-target", "1=repo"])
    assert code == sa.EXIT_USAGE
    assert "needs a ref" in capsys.readouterr().err
    assert "research" not in yaml.safe_load(path.read_text(encoding="utf-8"))


def test_amend_question_target_merges_prior_targets_back_to_flag_spelling(repo: Path, monkeypatch) -> None:
    import coordinator_core.sizing_assemble as mod

    captured = {}
    real = mod.build_research_block

    def spy(*args):
        captured["args"] = args
        return real(*args)

    monkeypatch.setattr(mod, "build_research_block", spy)
    path = Path(record_homes.record_path(str(repo), "sizings", _NAME))
    path.write_text(
        _DRAFT
        + "research:\n  value_class: scouts\n  questions: ['a?', 'b?']\n  question_mode: split\n"
        "  question_targets:\n    - {question: 2, source: repo, ref: .}\n",
        encoding="utf-8",
    )
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    sa.main(["--amend-research", rel, "--research-question-mode", "combine"])
    assert captured["args"][7] == "combine"
    assert captured["args"][8] == ["2=repo:."]


def _seeded_video_sizing(repo: Path) -> tuple[str, Path]:
    _run(repo, "--research-class", "corpus", "--research-source", "notebooklm",
         "--research-seed", "video=https://youtu.be/x", "--research-seed", "repo=https://h/o/r")
    rel = Path(record_homes.record_path("", "sizings", _NAME)).as_posix()
    return rel, Path(record_homes.record_path(str(repo), "sizings", _NAME))


def test_amend_source_excluding_a_seed_source_is_refused_naming_seeds_and_the_flag(
    repo: Path, capsys
) -> None:
    rel, path = _seeded_video_sizing(repo)
    capsys.readouterr()
    assert sa.main(["--amend-research", rel, "--research-source", "repo"]) == sa.EXIT_BUSINESS_FAIL
    err = capsys.readouterr().err
    assert "video=https://youtu.be/x" in err and "--clear-research-seeds" in err
    assert len(yaml.safe_load(path.read_text(encoding="utf-8"))["research"]["seeds"]) == 2


def test_clear_research_seeds_drops_them_and_lets_the_source_amend_land(repo: Path) -> None:
    rel, path = _seeded_video_sizing(repo)
    code = sa.main(["--amend-research", rel, "--clear-research-seeds",
                    "--research-source", "repo", "--research-target", "repo=a/b"])
    assert code == sa.EXIT_OK
    research = yaml.safe_load(path.read_text(encoding="utf-8"))["research"]
    assert "seeds" not in research and research["sources"] == ["repo"]


def test_research_seed_on_amend_replaces_the_prior_seeds(repo: Path) -> None:
    rel, path = _seeded_video_sizing(repo)
    code = sa.main(["--amend-research", rel, "--research-seed", "repo=https://h/o/r2",
                    "--research-source", "repo"])
    assert code == sa.EXIT_OK
    research = yaml.safe_load(path.read_text(encoding="utf-8"))["research"]
    assert research["seeds"] == [{"kind": "repo", "url": "https://h/o/r2"}]
