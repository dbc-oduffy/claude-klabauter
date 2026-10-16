"""Tests for coordinator_core.ops.review_mint.wave_bookkeeping — the
zero-integration-stage MECHANICAL bookkeeping step (2026-09-28 PM order,
step b')."""

from __future__ import annotations

import hashlib
import json

import pytest
import yaml

from coordinator_core.ops.review_mint.wave_bookkeeping import (
    bookkeep_wave,
    review_wave_bookkeeping_stem,
)


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_wave_sidecar(
    tmp_path,
    *,
    session_id: str,
    name: str,
    applied: int,
    brief_rows: list,
    baseline: dict,
    ledger_rows: list,
):
    baseline_lines = "\n".join(f"  {path}: {digest}" for path, digest in baseline.items())
    findings_body = "\n".join(
        f"### Finding {i}\nSomething.\n" for i in range(1, len(ledger_rows) + 1)
    )
    brief_yaml = yaml.safe_dump(brief_rows, default_flow_style=True).strip()
    text = (
        "---\n"
        "agent_type: coordinator:code-reviewer\n"
        f"applied: {applied}\n"
        f"brief_conformance: {brief_yaml}\n"
        f"baseline_sha256:\n{baseline_lines}\n"
        "---\n"
        "# Review\n\n"
        "## Findings\n\n"
        f"{findings_body}\n"
        "## Findings Ledger\n\n"
        "```json\n"
        f"{json.dumps(ledger_rows, indent=2)}\n"
        "```\n"
    )
    sidecar_dir = tmp_path / ".coordinator-local" / "subagent-share" / session_id
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    sidecar = sidecar_dir / name
    sidecar.write_text(text, encoding="utf-8")
    return sidecar


@pytest.fixture
def two_wave_sidecars(tmp_path):
    (tmp_path / ".git").mkdir()
    target = tmp_path / "src.py"
    target.write_text("x = 2\n", encoding="utf-8")

    s1 = _write_wave_sidecar(
        tmp_path,
        session_id="sess-1",
        name="wave-code-reviewer.md",
        applied=2,
        brief_rows=[
            {"brief_item": "a", "requirement": "r", "file_line": "f", "verified_how": "v", "status": "met"},
            {"brief_item": "b", "requirement": "r", "file_line": "f", "verified_how": "v", "status": "unmet"},
        ],
        baseline={"src.py": _sha256("x = 1\n")},
        ledger_rows=[{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2"}],
    )
    s2 = _write_wave_sidecar(
        tmp_path,
        session_id="sess-1",
        name="wave-kira.md",
        applied=1,
        brief_rows=[
            {"brief_item": "c", "requirement": "r", "file_line": "f", "verified_how": "v", "status": "met"},
        ],
        baseline={},
        ledger_rows=[],
    )
    return tmp_path, [s1, s2]


def test_bookkeep_wave_writes_one_record_with_merged_fields(two_wave_sidecars):
    repo_root, sidecars = two_wave_sidecars
    stem = review_wave_bookkeeping_stem("pln-example-a1b2c3", "sess-1")
    (repo_root / ".coordinator-local/subagent-share/sess-1/prep.md").write_text("x", encoding="utf-8")

    record = bookkeep_wave(
        sidecars,
        repo_root=repo_root,
        session_id="sess-1",
        plan_id="pln-example-a1b2c3",
        prep_sidecar=".coordinator-local/subagent-share/sess-1/prep.md",
        record_stem=stem,
    )

    assert record["integrated_from"] == ["wave-code-reviewer", "wave-kira"]
    assert record["fixes_applied"] == 3
    assert record["confinement_violations"] == 0
    assert record["unresolved"] == []
    assert record["brief_conformance"] == {"items": 3, "met": 2, "unmet": 1}
    assert record["slices"] == 2

    record_path = repo_root / ".coordinator-local" / "subagent-share" / "sess-1" / f"{stem}.md"
    assert record_path.is_file()
    text = record_path.read_text(encoding="utf-8")
    assert text.startswith("---\n")
    written = yaml.safe_load(text.split("---\n")[1])
    assert written["plan_id"] == "pln-example-a1b2c3"
    assert written["prep_sidecar"] == ".coordinator-local/subagent-share/sess-1/prep.md"
    assert written["integrated_from"] == ["wave-code-reviewer", "wave-kira"]


def test_bookkeep_wave_stamps_plan_id_onto_every_wave_sidecar(two_wave_sidecars):
    repo_root, sidecars = two_wave_sidecars
    stem = review_wave_bookkeeping_stem("pln-example-a1b2c3", "sess-1")

    bookkeep_wave(
        sidecars,
        repo_root=repo_root,
        session_id="sess-1",
        plan_id="pln-example-a1b2c3",
        prep_sidecar=".coordinator-local/subagent-share/sess-1/prep.md",
        record_stem=stem,
    )
    for sidecar in sidecars:
        text = sidecar.read_text(encoding="utf-8")
        assert 'plan_id: "pln-example-a1b2c3"' in text


def test_bookkeep_wave_never_raises_on_ledger_verify_failure(tmp_path):
    """§ module docstring point 1: a ledger-verify failure is surfaced in
    `ledger_failures`, never raised -- a bookkeeping step must not block a
    run it cannot itself repair."""
    (tmp_path / ".git").mkdir()
    sidecar_dir = tmp_path / ".coordinator-local" / "subagent-share" / "sess-2"
    sidecar_dir.mkdir(parents=True)
    bad_sidecar = sidecar_dir / "bad.md"
    bad_sidecar.write_text(
        "---\nagent_type: coordinator:code-reviewer\n---\nno ledger heading here\n",
        encoding="utf-8",
    )
    record = bookkeep_wave(
        [bad_sidecar],
        repo_root=tmp_path,
        session_id="sess-2",
        plan_id="pln-x",
        prep_sidecar=".coordinator-local/subagent-share/sess-2/prep.md",
        record_stem="pln-x.review-wave-bookkeeping",
    )
    assert "bad" in record["ledger_failures"]
    assert record["unresolved"] == []


def _plain_sidecar(share, name, fm_lines=()):
    share.mkdir(parents=True, exist_ok=True)
    body = "---\n" + "\n".join(fm_lines) + "\n---\nbody\n" if fm_lines else "# Delivery verification\nVerdict PASS.\n"
    path = share / name
    path.write_text(body, encoding="utf-8")
    return path


def test_two_plans_share_dir_binds_only_own_delivery(tmp_path):
    (tmp_path / ".git").mkdir()
    share = tmp_path / ".coordinator-local" / "subagent-share" / "sess-2"
    mine = _plain_sidecar(share, "roadmap-blitz-workflow-skill.delivery.md")
    other = _plain_sidecar(share, "install-dogfood-fixes-2026-10-02.delivery.md")
    other_plan = _plain_sidecar(
        share, "coordinator-code-reviewer.a1.md", ["agent_type: review-findings", "plan_id: pln-install-dogfood-fixes-close-ev-e12ab2"]
    )
    own_slice = _plain_sidecar(share, "coordinator-code-reviewer.a2.md", ["agent_type: review-findings"])

    record = bookkeep_wave(
        [mine, other, other_plan, own_slice],
        repo_root=tmp_path,
        session_id="sess-2",
        plan_id="pln-roadmap-blitz-workflow-skill-a-9177a4",
        prep_sidecar=None,
        record_stem="rec",
    )

    assert record["integrated_from"] == [
        "roadmap-blitz-workflow-skill.delivery",
        "coordinator-code-reviewer.a2",
    ]
    assert sorted(record["excluded_sidecars"]) == [
        "coordinator-code-reviewer.a1",
        "install-dogfood-fixes-2026-10-02.delivery",
    ]
    assert record["slices"] == 2
    assert "plan_id" not in other.read_text()
    assert "pln-roadmap" not in other_plan.read_text()


def test_delivery_binds_by_plan_stem(tmp_path):
    (tmp_path / ".git").mkdir()
    share = tmp_path / ".coordinator-local" / "subagent-share" / "s"
    d = _plain_sidecar(share, "2026-10-02-my-plan.delivery.md")
    record = bookkeep_wave(
        [d], repo_root=tmp_path, session_id="s", plan_id="pln-zzz-aaaaaa",
        prep_sidecar=None, record_stem="rec", plan_stem="2026-10-02-my-plan",
    )
    assert record["integrated_from"] == ["2026-10-02-my-plan.delivery"]


def test_phantom_prep_sidecar_resolves_to_real_runner_sidecar(tmp_path):
    (tmp_path / ".git").mkdir()
    share = tmp_path / ".coordinator-local" / "subagent-share" / "sess-3"
    _plain_sidecar(share, "coordinator-test-runner.aaa.blocks.md")
    real = _plain_sidecar(share, "coordinator-test-runner.aaa.md", ["status: complete"])
    phantom = ".coordinator-local/subagent-share/sess-3/review-coordinator-test-runner.md"

    record = bookkeep_wave(
        [], repo_root=tmp_path, session_id="sess-3", plan_id="pln-p-aaaaaa",
        prep_sidecar=phantom, record_stem="rec",
    )

    assert record["prep_sidecar"] == real.relative_to(tmp_path).as_posix()
    assert (tmp_path / record["prep_sidecar"]).is_file()


def test_phantom_prep_sidecar_picks_this_runs_tests_sidecar(tmp_path):
    (tmp_path / ".git").mkdir()
    share = tmp_path / ".coordinator-local" / "subagent-share" / "sess-4"
    _plain_sidecar(share, "coordinator-test-runner.old.md", ["status: complete"])
    mine = _plain_sidecar(share, "coordinator-test-runner.mine.md", ["status: complete"])
    rel = mine.relative_to(tmp_path).as_posix()
    record = bookkeep_wave(
        [], repo_root=tmp_path, session_id="sess-4", plan_id="pln-p-aaaaaa",
        prep_sidecar=".coordinator-local/subagent-share/sess-4/review-coordinator-test-runner.md",
        record_stem="rec", stage_returns={"tests": {"sidecar": rel}},
    )
    assert record["prep_sidecar"] == rel


def test_phantom_prep_sidecar_with_no_real_file_is_recorded_absent(tmp_path):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".coordinator-local" / "subagent-share" / "sess-5").mkdir(parents=True)
    record = bookkeep_wave(
        [], repo_root=tmp_path, session_id="sess-5", plan_id="pln-p-aaaaaa",
        prep_sidecar=".coordinator-local/subagent-share/sess-5/review-coordinator-test-runner.md",
        record_stem="rec",
    )
    assert record["prep_sidecar"] is None


_FIRING_KEYS = ["leg", "plan_id", "findings_count", "max_severity", "applied"]


def _firing_sidecar(tmp_path, name, findings, ledger=None, agent="coordinator:code-reviewer", eol="\n"):
    parts = ["---\nagent_type: " + agent + "\n---\n# Review\n\n## Findings\n\n" + findings + "\n"]
    if ledger is not None:
        parts.append("## Findings Ledger\n\n```json\n" + json.dumps(ledger, indent=2) + "\n```\n")
    path = tmp_path / name
    path.write_bytes("".join(parts).replace("\n", eol).encode("utf-8"))
    return path


def test_review_firing_rows_code_reviewer_applied_p1(tmp_path):
    from coordinator_core.ops.review_mint.wave_bookkeeping import review_firing_rows

    p = _firing_sidecar(
        tmp_path, "a.md", "### Item A\n- **Severity:** P1\n",
        ledger=[{"id": "f1", "file": "src.py", "status": "applied"}],
    )
    rows = review_firing_rows([p], "pln-x")
    assert rows == [{"leg": "coordinator:code-reviewer", "plan_id": "pln-x",
                     "findings_count": 1, "max_severity": "P1", "applied": 1}]
    assert list(rows[0]) == _FIRING_KEYS


def test_review_firing_rows_mixed_p2_nit_is_p2(tmp_path):
    from coordinator_core.ops.review_mint.wave_bookkeeping import review_firing_rows

    p = _firing_sidecar(
        tmp_path, "a.md",
        "### Item A\n- **Severity:** nit\n### Item B\n- **Severity:** P2\n",
    )
    assert review_firing_rows([p], "pln-x")[0]["max_severity"] == "P2"


def test_review_firing_rows_json_critical_minor_is_p0(tmp_path):
    from coordinator_core.ops.review_mint.wave_bookkeeping import review_firing_rows

    payload = json.dumps({"findings": [{"severity": "minor"}, {"severity": "critical"}]})
    p = _firing_sidecar(tmp_path, "a.md", "```json\n" + payload + "\n```\n")
    row = review_firing_rows([p], "pln-x")[0]
    assert row["max_severity"] == "P0"
    assert row["findings_count"] == 2


def test_review_firing_rows_off_vocabulary_is_unranked(tmp_path):
    from coordinator_core.ops.review_mint.wave_bookkeeping import review_firing_rows

    payload = json.dumps({"findings": [{"severity": "info"}]})
    p = _firing_sidecar(tmp_path, "a.md", "```json\n" + payload + "\n```\n")
    assert review_firing_rows([p], "pln-x")[0]["max_severity"] == "unranked"


def test_review_firing_rows_no_findings_is_zero_and_null(tmp_path):
    from coordinator_core.ops.review_mint.wave_bookkeeping import review_firing_rows

    p = _firing_sidecar(tmp_path, "a.md", "None.\n")
    row = review_firing_rows([p], "pln-x")[0]
    assert (row["findings_count"], row["max_severity"], row["applied"]) == (0, None, 0)


def test_review_firing_rows_crlf_parses_same(tmp_path):
    from coordinator_core.ops.review_mint.wave_bookkeeping import review_firing_rows

    args = ("### Item A\n- **Severity:** P1\n",)
    kw = {"ledger": [{"id": "f1", "status": "applied"}]}
    lf = review_firing_rows([_firing_sidecar(tmp_path, "lf.md", *args, **kw)], "pln-x")
    crlf = review_firing_rows([_firing_sidecar(tmp_path, "crlf.md", *args, eol="\r\n", **kw)], "pln-x")
    assert lf == crlf and lf[0]["max_severity"] == "P1"


def test_review_firing_rows_unreadable_sidecar_gives_zero_row(tmp_path):
    from coordinator_core.ops.review_mint.wave_bookkeeping import review_firing_rows

    assert review_firing_rows([tmp_path / "gone.md"], "pln-x") == [
        {"leg": "gone", "plan_id": "pln-x", "findings_count": 0, "max_severity": None, "applied": 0}
    ]


def test_review_firing_rows_bookkeep_wave_one_row_per_kept_sidecar(tmp_path):
    (tmp_path / ".git").mkdir()
    sid = tmp_path / ".coordinator-local" / "subagent-share" / "s"
    sid.mkdir(parents=True)
    mine = _firing_sidecar(sid, "mine.md", "### Item A\n- **Severity:** P2\n")
    other = sid / "other-plan.delivery.md"
    other.write_text("---\nagent_type: coordinator:delivery-verifier\n---\n", encoding="utf-8")
    record = bookkeep_wave(
        [mine, other], repo_root=tmp_path, session_id="s", plan_id="pln-mine-aaaaaa",
        prep_sidecar=None, record_stem="rec",
    )
    assert record["excluded_sidecars"] == ["other-plan.delivery"]
    assert [r["leg"] for r in record["review_firings"]] == ["coordinator:code-reviewer"]
    assert record["review_firings"][0]["max_severity"] == "P2"
