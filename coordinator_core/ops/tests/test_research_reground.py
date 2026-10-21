"""research.reground: moves a stale grounding.md aside and emits a grounding-only script."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops import _research_grounding as rg
from coordinator_core.ops.dispatch_emit import op as op_module
from coordinator_core.ops.research_close import _handler as close_handler
from coordinator_core.ops.research_reground import _handler

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]

_NOWIN = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_FIXTURE = Path(op_module.__file__).parent / "tests" / "fixtures" / "pipeline_research"
_STAGE = """  - id: grounding
    phase: Ground
    agent_type: general-purpose
    model: sonnet
    template: grounding-template.md
    depends_on: [scout]
    web_caller: true
    output: "{{scratch_dir}}/grounding.md"
"""
_GOOD = (
    "## digest-a.md\n- **grounded** a holds\n## digest-b.md\n- **grounded** b holds\n"
    "## Contradictions\n- **real** sources disagree\n"
)
_BAD = _GOOD + "- **contradicted** a claim its source denies\n"
RUN = "run1"


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false", *args],
        cwd=repo, capture_output=True, text=True, check=True, creationflags=_NOWIN,
    )
    return r.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path, monkeypatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q")
    (r / "seed.txt").write_text("seed\n")
    _git(r, "add", "seed.txt")
    _git(r, "commit", "-q", "-m", "seed")
    root = tmp_path / "content"
    shutil.copytree(_FIXTURE, root)
    pdir = root / "pipelines" / "deep-research"
    manifest = pdir / "scouts.manifest.yaml"
    manifest.write_text(manifest.read_text(encoding="utf-8").rstrip("\n") + "\n" + _STAGE, encoding="utf-8")
    (pdir / "grounding-template.md").write_text(
        "Your brief is the file `{{brief}}`. Ground the digests under `{{scratch_dir}}`.\n", encoding="utf-8"
    )
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: root)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "sess-reground")
    return r


@pytest.fixture
def scratch(repo: Path) -> Path:
    s = repo / "scratch" / "warp" / RUN
    s.mkdir(parents=True)
    (s / "digest-a.md").write_text("alpha\n")
    (s / "digest-b.md").write_text("beta\n")
    (s / "ask.md").write_text("What is X?\n")
    (s / "grounding.md").write_text(_BAD, encoding="utf-8")
    return s


def _reground(repo: Path, **extra) -> dict:
    params = {"run_id": RUN}
    params.update(extra)
    return asyncio.run(_handler(params, repo / ".git"))


def _snapshot(repo: Path) -> dict:
    return {
        p.relative_to(repo).as_posix(): p.read_bytes()
        for p in (repo / "scratch").rglob("*") if p.is_file()
    }


def test_happy_path_moves_report_aside_and_emits_script(repo, scratch):
    out = _reground(repo)
    assert out["exit_code"] == 0, out
    assert out["generation"] == 1 and out["stale"] == "grounding.stale-1.md"
    assert not (scratch / "grounding.md").exists()
    assert (scratch / "grounding.stale-1.md").read_text(encoding="utf-8") == _BAD
    brief = (scratch / "reground-1.md").read_text(encoding="utf-8")
    assert brief.startswith("What is X?\n") and "generation 1" in brief and "grounding.stale-1.md" in brief
    script = repo / "scratch" / "warp" / f"{RUN}.reground-1.workflow.mjs"
    assert out["script"] == script.as_posix() and "\\" not in out["script"]
    assert out["next_action"] == {"tool": "Workflow", "scriptPath": out["script"]}
    assert out["then"]["op"] == "research.close"
    assert out["brief"] == f"scratch/warp/{RUN}/reground-1.md"
    text = script.read_text(encoding="utf-8")
    assert text.count("agent(") == 1 and out["brief"] in text
    receipt = json.loads(Path(str(script) + ".emitted.json").read_text(encoding="utf-8"))
    assert receipt["session_id"] == "sess-reground" and receipt["sha256"]


def test_second_generation_leaves_the_first_untouched(repo, scratch):
    first = _reground(repo)
    gen1 = {k: v for k, v in _snapshot(repo).items() if "reground-1" in k or "stale-1" in k}
    (scratch / "grounding.md").write_text(_BAD + "\n", encoding="utf-8")
    (scratch / "digest-a.md").write_text("alpha changed\n", encoding="utf-8")
    second = _reground(repo)
    assert second["generation"] == 2 and second["stale"] == "grounding.stale-2.md"
    after = _snapshot(repo)
    assert all(after[k] == v for k, v in gen1.items())
    assert first["script"] != second["script"]


def test_absent_grounding_report_still_emits_with_null_stale(repo, scratch):
    (scratch / "grounding.md").unlink()
    out = _reground(repo)
    assert out["exit_code"] == 0 and out["stale"] is None and out["generation"] == 1
    assert not list(scratch.glob("grounding.stale-*"))


def test_unfired_generation_still_advances(repo, scratch):
    assert _reground(repo)["generation"] == 1
    assert _reground(repo)["generation"] == 2


def test_prompt_differs_per_generation(repo, scratch):
    one = _reground(repo)
    two = _reground(repo)
    s1 = Path(one["script"]).read_text(encoding="utf-8")
    s2 = Path(two["script"]).read_text(encoding="utf-8")
    assert one["brief"] in s1 and two["brief"] in s2
    assert one["brief"] not in s2 and two["brief"] not in s1
    assert f"{RUN}/ask.md" not in s1


def test_refusals_write_nothing(repo, scratch, tmp_path):
    def refused(**params):
        before = _snapshot(repo)
        out = asyncio.run(_handler({"run_id": RUN, **params}, repo / ".git"))
        assert out["exit_code"] == 1 and out["error"], out
        assert _snapshot(repo) == before
        assert not list((repo / "scratch" / "warp").glob("*.mjs*"))
        return out

    refused(run_id="../x")
    refused(scratch_dir=str(tmp_path / "missing"))
    refused(brief=str(tmp_path / "nope.md"))
    refused(scratch_dir="relative/dir")
    (scratch / "ask.md").unlink()
    refused()
    (scratch / "ask.md").write_text("What is X?\n")
    for digest in scratch.glob("digest-*.md"):
        digest.unlink()
    assert "digest" in refused()["error"]


def test_manifest_without_grounding_stage_refuses_naming_manifest(repo, scratch, monkeypatch):
    monkeypatch.setattr(op_module, "_pipeline_content_root", lambda: _FIXTURE)
    before = _snapshot(repo)
    out = _reground(repo)
    assert out["exit_code"] == 1 and "grounding" in out["error"]
    assert _snapshot(repo) == before


def test_contradicted_then_reground_then_fresh_grounding_closes(repo, scratch):
    def close() -> dict:
        params = {
            "scratch_dir": scratch.as_posix(), "tier": "scouts", "run_id": RUN,
            "topic_slug": "topic", "require_grounding": True,
        }
        return asyncio.run(close_handler(params, repo / ".git"))

    refused = close()
    assert refused["exit_code"] == 1 and refused["deliverable"] is False
    assert refused["grounding"]["contradicted"] == 1

    out = _reground(repo, scratch_dir=scratch.as_posix())
    assert out["exit_code"] == 0 and out["stale"] == rg.stale_report_name(1)
    absent = close()
    assert absent["exit_code"] == 1 and "grounding.md" in absent["error"]

    (scratch / "grounding.md").write_text(_GOOD, encoding="utf-8")
    closed = close()
    assert closed["exit_code"] == 0 and closed["deliverable"] is True, closed


_NAMES = ["digest-a.md", "digest-b.md"]
_NO_RULING = _GOOD.replace("- **real** sources disagree\n", "No contradictions found.\n")


def _graded(repo, scratch, report: str = _GOOD) -> None:
    """Run the first (full) reground, then play the grader by writing ``report``."""
    assert _reground(repo)["generation"] == 1
    (scratch / "grounding.md").write_text(report, encoding="utf-8")


def _shas(scratch: Path) -> dict[str, str]:
    return {p.name: rg.digest_sha(p.read_text(encoding="utf-8")) for p in scratch.glob("digest-*.md")}


def _carry(scratch: Path) -> dict:
    return json.loads((scratch / "grounding.carry.json").read_text(encoding="utf-8"))


def test_ruling_rewritten_before_next_generation_is_dropped(repo, scratch):
    report = (
        _GOOD.replace("- **real** sources disagree\n", "")
        + "- **invented** digest-a.md misreads its source\n- **invented** the tension was misread\n"
    )
    _graded(repo, scratch, report)
    (scratch / "digest-a.md").write_text("alpha rewritten\n", encoding="utf-8")
    out = _reground(repo)
    assert out["generation"] == 2 and out["regraded"] == _NAMES
    assert _carry(scratch)["rulings"] == []


def test_keyed_ruling_on_unchanged_digests_survives_two_regrounds(repo, scratch):
    report = _GOOD.replace("- **real** sources disagree\n", "- **real** digest-a.md sources disagree\n")
    _graded(repo, scratch, report)
    (scratch / "digest-b.md").write_text("beta two\n", encoding="utf-8")
    assert _reground(repo)["regraded"] == ["digest-b.md"]
    first = _carry(scratch)["rulings"]
    assert first == [
        rg.key_ruling(
            "- **real** digest-a.md sources disagree", {"digest-a.md": rg.digest_sha("alpha\n")},
            grader=rg.GRADER_VERSION,
        )
    ]
    (scratch / "grounding.md").write_text(
        "## digest-b.md\n- **grounded** b holds\n## Contradictions\nNo contradictions found.\n", encoding="utf-8"
    )
    (scratch / "digest-b.md").write_text("beta three\n", encoding="utf-8")
    out = _reground(repo)
    assert out["regraded"] == ["digest-b.md"] and out["generation"] == 3
    assert _carry(scratch)["rulings"] == first


def test_legacy_string_rulings_are_not_a_noop_and_regrade_the_named_digest(repo, scratch):
    _graded(repo, scratch)
    assert _reground(repo)["generation"] is None
    (scratch / "grounding.carry.json").write_text(
        json.dumps({
            "generation": 1, "carried": {}, "rulings": ["- **invented** digest-a.md misread"], "flagged": {},
            "grader": rg.GRADER_VERSION,
        }),
        encoding="utf-8",
    )
    out = _reground(repo)
    assert out["generation"] == 2 and out["regraded"] == ["digest-a.md"]
    assert all(isinstance(r, dict) for r in _carry(scratch)["rulings"])


def test_brief_states_the_ruling_vocabulary(repo, scratch):
    _reground(repo)
    brief = (scratch / "reground-1.md").read_text(encoding="utf-8")
    assert "Name the digest file(s) each ruling line judges." in brief
    assert "real = the sources themselves disagree" in brief
    assert "invented = the digest's own text claims two sources disagree, and they do not" in brief
    assert "dismissed = every other tension that does not hold" in brief
    assert "it is dismissed, never invented." in brief


def test_carry_unchanged_digests_is_a_noop_with_zero_fresh(repo, scratch):
    _graded(repo, scratch)
    first = _reground(repo)
    before = _snapshot(repo)
    for _ in range(5):
        again = _reground(repo)
        assert again == first
        assert _snapshot(repo) == before
        assert rg.next_stale_index(p.name for p in scratch.iterdir()) == 2
    assert first["exit_code"] == 0 and first["generation"] is None
    assert first["fresh"] == 0 and first["regraded"] == []
    assert first["carried"] == sum(rg.grounding_counts(_GOOD).values()) == 3
    assert first["problems"] == rg.grounding_problems(_GOOD, _NAMES) == []
    assert first["next_action"] == {"op": "research.close"}
    assert not list((repo / "scratch" / "warp").glob("*.reground-2.*"))


def test_carry_noop_reports_a_blocking_grade_unchanged(repo, scratch):
    bad = _GOOD.replace("- **grounded** a holds", "- **contradicted** a claim its source denies")
    _graded(repo, scratch, bad)
    out = _reground(repo)
    assert out["generation"] is None and out["fresh"] == 0
    assert out["problems"] == rg.grounding_problems(bad, _NAMES) != []


def test_carry_noop_ignores_touch_and_line_endings(repo, scratch):
    (scratch / "digest-a.md").write_bytes(b"alpha\r\nmore\r\n")
    _graded(repo, scratch)
    (scratch / "digest-a.md").write_bytes(b"alpha\nmore\n")
    (scratch / "digest-b.md").touch()
    assert _reground(repo)["regraded"] == []


def test_carry_ungraded_digest_is_regraded_not_stranded(repo, scratch):
    _graded(repo, scratch, "## digest-a.md\n- **grounded** a holds\n## Contradictions\nNo contradictions found.\n")
    out = _reground(repo)
    assert out["generation"] == 2 and out["regraded"] == ["digest-b.md"]
    assert sorted(json.loads((scratch / "grounding.carry.json").read_text(encoding="utf-8"))["carried"]) == [
        "digest-a.md"
    ]


def test_carry_changed_line_is_always_regraded(repo, scratch):
    (scratch / "digest-a.md").write_text("one\ntwo\nthree\n")
    _graded(repo, scratch, _NO_RULING)
    (scratch / "digest-a.md").write_text("one\nTWO\nthree\n")
    out = _reground(repo)
    assert out["generation"] == 2 and out["regraded"] == ["digest-a.md"] and out["fresh"] is None
    assert out["carried"] == 1
    carry = json.loads((scratch / "grounding.carry.json").read_text(encoding="utf-8"))
    assert sorted(carry["carried"]) == ["digest-b.md"] and carry["generation"] == 2
    brief = (scratch / "reground-2.md").read_text(encoding="utf-8")
    assert "digest-a.md: changed lines 2" in brief and "Carried, not to be graded: digest-b.md" in brief
    assert (scratch / "grounding.stale-2.md").is_file()
    basis = json.loads((scratch / "grounding.basis.json").read_text(encoding="utf-8"))
    assert basis["digest-a.md"] == rg.line_hashes("one\nTWO\nthree\n")


def test_carry_flagged_claims_and_rulings_stay_in_the_carry(repo, scratch):
    report = (
        "## digest-a.md\n- **grounded** a holds\n"
        "## digest-b.md\n- **contradicted** b is denied\n"
        "## Contradictions\n- **invented** digest-a.md was misread\n"
        "- **real** digest-a.md sources disagree\n"
    )
    _graded(repo, scratch, report)
    (scratch / "digest-a.md").write_text("alpha changed\n", encoding="utf-8")
    _reground(repo)
    carry = json.loads((scratch / "grounding.carry.json").read_text(encoding="utf-8"))
    assert carry["flagged"] == {"digest-b.md": ["- **contradicted** b is denied"]}
    assert carry["rulings"] == []
    eff = rg.effective_report("", carry, set(), _shas(scratch))
    assert "contradicted" in eff
    again = _reground(repo)
    assert again["regraded"] == ["digest-a.md"] and again["generation"] == 3


def test_carry_flagged_lines_of_a_changed_digest_are_named_in_the_brief(repo, scratch):
    _graded(repo, scratch, _NO_RULING.replace("- **grounded** a holds", "- **ungrounded** a is unsupported"))
    (scratch / "digest-a.md").write_text("alpha changed\n", encoding="utf-8")
    _reground(repo)
    brief = (scratch / "reground-2.md").read_text(encoding="utf-8")
    assert "previously flagged: - **ungrounded** a is unsupported" in brief


@pytest.mark.parametrize("victim", ["grounding.basis.json", "grounding.carry.json"])
def test_carry_unreadable_state_file_refuses_and_writes_nothing(repo, scratch, victim):
    _graded(repo, scratch)
    (scratch / victim).write_text("{not json", encoding="utf-8")
    before = _snapshot(repo)
    out = _reground(repo)
    assert out["exit_code"] == 1 and victim in out["error"]
    assert _snapshot(repo) == before


def test_carry_first_generation_writes_a_basis_and_a_versioned_empty_carry(repo, scratch):
    out = _reground(repo)
    assert out["carried"] == 0 and out["regraded"] == _NAMES
    basis = json.loads((scratch / "grounding.basis.json").read_text(encoding="utf-8"))
    assert basis == {"digest-a.md": rg.line_hashes("alpha\n"), "digest-b.md": rg.line_hashes("beta\n")}
    carry = _carry(scratch)
    assert carry["carried"] == {} and carry["rulings"] == [] and carry["flagged"] == {}
    assert carry["grader"] == rg.GRADER_VERSION and carry["generation"] == 1


_INVENTED = _GOOD.replace("- **real** sources disagree\n", "- **invented** digest-a.md the sources do not disagree\n")


def test_stale_grader_ruling_over_unchanged_digests_is_regraded(repo, scratch):
    _graded(repo, scratch, _INVENTED)
    carry = _carry(scratch)
    del carry["grader"]
    (scratch / "grounding.carry.json").write_text(json.dumps(carry), encoding="utf-8")
    out = _reground(repo)
    assert out["generation"] == 2 and out["regraded"] == ["digest-a.md"]
    after = _carry(scratch)
    assert after["rulings"] == [] and after["grader"] == rg.GRADER_VERSION


def test_current_grader_ruling_over_unchanged_digests_survives_as_a_noop(repo, scratch):
    _graded(repo, scratch, _INVENTED)
    out = _reground(repo)
    assert out["generation"] is None and out["regraded"] == []
    assert out["problems"] == ["invented: - **invented** digest-a.md the sources do not disagree"]


def _close(repo: Path, scratch: Path) -> dict:
    params = {
        "scratch_dir": scratch.as_posix(), "tier": "scouts", "run_id": RUN,
        "topic_slug": "topic", "require_grounding": True,
    }
    return asyncio.run(close_handler(params, repo / ".git"))


def _stub_grader(stale: str) -> str:
    """Stands in for the grader: the sources do not disagree is a dismissal, not an invention."""
    rulings = [
        "- **dismissed**" + ln.split("**invented**", 1)[1]
        for ln in stale.splitlines() if "**invented**" in ln and "do not disagree" in ln
    ]
    rulings += [
        "- **dismissed**" + ln.split("**invented**", 1)[1]
        for ln in stale.splitlines() if "**invented**" in ln and "not a contradiction" in ln
    ]
    return (
        "## digest-a.md\n- **grounded** a holds\n## digest-b.md\n- **grounded** b holds\n"
        "## Contradictions\n" + "\n".join(rulings) + "\n"
    )


def test_stale_grader_invented_rulings_reground_then_close(repo, scratch):
    assert _reground(repo)["generation"] == 1
    example_game_repo_report = (
        "## digest-a.md\n- **grounded** a holds\n"
        "## Contradictions\n"
        "- **invented** digest-a.md the sources do not disagree\n"
        "- **invented** a recommendation, not a contradiction\n"
    )
    (scratch / "grounding.md").write_text(example_game_repo_report, encoding="utf-8")
    (scratch / "grounding.carry.json").write_text(
        json.dumps({
            "generation": 1,
            "carried": {"digest-b.md": ["- **grounded** b holds"]},
            "rulings": ["- **invented** the tension was misread"],
            "flagged": {},
        }),
        encoding="utf-8",
    )

    refused = _close(repo, scratch)
    assert refused["exit_code"] == 1 and refused["deliverable"] is False, refused
    assert "research.reground" in refused["error"]
    action = refused["next_action"]
    assert action["op"] == "research.reground"

    out = _reground(repo, **action["params"])
    assert out["exit_code"] == 0 and out["generation"] is not None, out
    assert out["regraded"] == _NAMES
    carry = _carry(scratch)
    assert carry["grader"] == rg.GRADER_VERSION and carry["rulings"] == []
    brief = (scratch / f"reground-{out['generation']}.md").read_text(encoding="utf-8")
    assert brief.endswith(rg.RULING_NOTE)

    stale = (scratch / out["stale"]).read_text(encoding="utf-8")
    (scratch / "grounding.md").write_text(_stub_grader(stale), encoding="utf-8")
    assert "**dismissed**" in (scratch / "grounding.md").read_text(encoding="utf-8")
    closed = _close(repo, scratch)
    assert closed["exit_code"] == 0 and closed["deliverable"] is True, closed


def test_brief_ends_with_the_ruling_note(repo, scratch):
    _reground(repo)
    assert (scratch / "reground-1.md").read_text(encoding="utf-8").endswith(rg.RULING_NOTE)
