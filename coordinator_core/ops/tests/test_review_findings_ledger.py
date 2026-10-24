"""Tests for coordinator_core.ops.review_findings_ledger — the
reviewer-applies-own-findings ledger op (coordinator-content-repo
docs/plans/2026-09-26-retire-review-integrator.md, row M2).
"""

from __future__ import annotations

import hashlib
import json

import pytest

from coordinator_core.ops import review_findings_ledger as m


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_sidecar(tmp_path, *, baseline: dict, findings_count: int, rows: list) -> "object":
    baseline_lines = "\n".join(f"  {path}: {digest}" for path, digest in baseline.items())
    findings_body = "\n".join(f"### Finding {i}\nSomething.\n" for i in range(1, findings_count + 1))
    text = (
        "---\n"
        "agent_type: coordinator:code-reviewer\n"
        f"baseline_sha256:\n{baseline_lines}\n"
        "---\n"
        "# Review\n\n"
        "## Findings\n\n"
        f"{findings_body}\n"
        "## Findings Ledger\n\n"
        "```json\n"
        f"{json.dumps(rows, indent=2)}\n"
        "```\n"
    )
    sidecar_dir = tmp_path / "state" / "subagent-share" / "sess-1"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    sidecar = sidecar_dir / "sidecar.md"
    sidecar.write_text(text, encoding="utf-8")
    return sidecar


def test_verify_passes_a_clean_replacement(tmp_path):
    target = tmp_path / "src.py"
    baseline_content = "x = 1\n"
    target.write_text("x = 2\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256(baseline_content)},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2"}],
    )
    outcome = m.verify(sidecar, repo_root=tmp_path)
    assert outcome.ok, outcome.failures
    assert outcome.stamp["applied"] == 1
    stamped_text = sidecar.read_text(encoding="utf-8")
    assert "findings_ledger:" in stamped_text
    rows = m._parse_ledger_rows(stamped_text)
    assert rows[0]["verified"] is True


def _capture_rung_fires(monkeypatch) -> list:
    from coordinator_core.ops import plan_status_transition

    calls: list = []
    monkeypatch.setattr(plan_status_transition, "main", lambda argv: calls.append(argv) or 0)
    return calls


def test_verify_fires_stamp_reviewed_on_a_reviewed_plan(tmp_path, monkeypatch):
    calls = _capture_rung_fires(monkeypatch)
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nstatus: draft\n---\nnew text\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"docs/plans/p.md": _sha256("---\nstatus: draft\n---\nold text\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "docs/plans/p.md", "before": "old text", "after": "new text"}],
    )
    assert m.verify(sidecar, repo_root=tmp_path).ok
    assert calls == [["stamp-reviewed", "--plan", str(plan)]]


def test_verify_refuses_when_reviewed_plan_frontmatter_no_longer_parses(tmp_path, monkeypatch):
    """F24b (gh-klabauter#71): a reviewer's edit that broke the reviewed
    plan's own YAML frontmatter must refuse `verify` -- not stamp
    `status: reviewed` over a document nothing can read frontmatter off."""
    calls = _capture_rung_fires(monkeypatch)
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nstatus: draft\nauthor: [unclosed\n---\nnew text\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"docs/plans/p.md": _sha256("---\nstatus: draft\n---\nold text\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "docs/plans/p.md", "before": "old text", "after": "new text"}],
    )
    outcome = m.verify(sidecar, repo_root=tmp_path)
    assert not outcome.ok
    assert any("frontmatter no longer parses" in f for f in outcome.failures), outcome.failures
    assert calls == []
    assert "findings_ledger:" not in sidecar.read_text(encoding="utf-8")


def test_verify_fires_no_rung_for_a_non_plan_target(tmp_path, monkeypatch):
    calls = _capture_rung_fires(monkeypatch)
    (tmp_path / "src.py").write_text("x = 2\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("x = 1\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2"}],
    )
    assert m.verify(sidecar, repo_root=tmp_path).ok
    assert calls == []


def test_verify_fails_on_row_count_mismatch(tmp_path):
    target = tmp_path / "src.py"
    target.write_text("x = 2\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("x = 1\n")},
        findings_count=2,
        rows=[{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2"}],
    )
    outcome = m.verify(sidecar, repo_root=tmp_path)
    assert not outcome.ok
    assert any("declared 2" in f for f in outcome.failures)


def test_verify_fails_when_after_text_absent(tmp_path):
    target = tmp_path / "src.py"
    target.write_text("x = 999\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("x = 1\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2"}],
    )
    outcome = m.verify(sidecar, repo_root=tmp_path)
    assert not outcome.ok
    assert any("not found" in f for f in outcome.failures)


def test_verify_accepts_em_rejected_row_with_reason(tmp_path):
    target = tmp_path / "src.py"
    target.write_text("x = 1\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("x = 1\n")},
        findings_count=1,
        rows=[
            {
                "id": "finding-1",
                "file": "src.py",
                "before": "x = 1",
                "after": "x = 2",
                "status": "em-rejected",
                "reason": "not a real finding",
            }
        ],
    )
    outcome = m.verify(sidecar, repo_root=tmp_path)
    assert outcome.ok, outcome.failures
    assert outcome.stamp["em_rejected"] == 1


def test_verify_refuses_em_rejected_row_with_no_reason(tmp_path):
    target = tmp_path / "src.py"
    target.write_text("x = 1\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("x = 1\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2", "status": "em-rejected"}],
    )
    outcome = m.verify(sidecar, repo_root=tmp_path)
    assert not outcome.ok
    assert any("no `reason`" in f for f in outcome.failures)


def test_verify_accepts_suspended_row_under_premise_verdict(tmp_path):
    target = tmp_path / "src.py"
    target.write_text("x = 1\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("x = 1\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "src.py", "before": "", "after": "", "status": "suspended"}],
    )
    outcome = m.verify(sidecar, repo_root=tmp_path)
    assert outcome.ok, outcome.failures
    assert outcome.stamp["suspended"] == 1


def test_verify_reruns_clean_after_a_prior_verify(tmp_path):
    target = tmp_path / "src.py"
    target.write_text("x = 2\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("x = 1\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2"}],
    )
    first = m.verify(sidecar, repo_root=tmp_path)
    assert first.ok
    second = m.verify(sidecar, repo_root=tmp_path)
    assert second.ok


def test_reject_restores_before_and_records_reason(tmp_path):
    target = tmp_path / "src.py"
    target.write_text("x = 2\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("x = 1\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2"}],
    )
    touched = m.reject(sidecar, "finding-1", "premise was wrong", repo_root=tmp_path)
    assert touched == "src.py"
    assert target.read_text(encoding="utf-8") == "x = 1\n"
    rows = m._parse_ledger_rows(sidecar.read_text(encoding="utf-8"))
    assert rows[0]["status"] == "em-rejected"
    assert rows[0]["reason"] == "premise was wrong"


def test_reject_refuses_empty_reason(tmp_path):
    target = tmp_path / "src.py"
    target.write_text("x = 2\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("x = 1\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2"}],
    )
    with pytest.raises(m.LedgerError):
        m.reject(sidecar, "finding-1", "  ", repo_root=tmp_path)


def test_reject_refuses_ambiguous_multiple_occurrences(tmp_path):
    target = tmp_path / "src.py"
    target.write_text("x = 2\nx = 2\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("x = 1\nx = 1\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2"}],
    )
    with pytest.raises(m.LedgerError, match="occurs 2 times"):
        m.reject(sidecar, "finding-1", "no longer needed", repo_root=tmp_path)


def test_targets_add_dedupes_and_persists(tmp_path):
    (tmp_path / ".git").mkdir()
    merged = m.targets_add(tmp_path, "session-1", ["a/b.py", "c/d.py", "a/b.py"])
    assert merged == ["a/b.py", "c/d.py"]
    again = m.targets_add(tmp_path, "session-1", ["e/f.py"])
    assert again == ["a/b.py", "c/d.py", "e/f.py"]
    target_file = tmp_path / ".git" / "coordinator-sessions" / "session-1" / "review-targets.txt"
    assert target_file.read_text(encoding="utf-8").splitlines() == ["a/b.py", "c/d.py", "e/f.py"]


def test_targets_add_agent_key_writes_a_separate_per_dispatch_file(tmp_path):
    (tmp_path / ".git").mkdir()
    m.targets_add(tmp_path, "session-1", ["a.py"])
    merged = m.targets_add(tmp_path, "session-1", ["b.py"], agent_key="agent-x")
    assert merged == ["b.py"]
    base = tmp_path / ".git" / "coordinator-sessions" / "session-1"
    assert (base / "review-targets.agent-x.txt").read_text(encoding="utf-8") == "b.py\n"
    assert (base / "review-targets.txt").read_text(encoding="utf-8") == "a.py\n"


def test_targets_add_refuses_a_root_that_is_not_a_repo(tmp_path):
    with pytest.raises(m.LedgerError, match="not a git repository root"):
        m.targets_add(tmp_path, "session-1", ["a.py"])
    assert not (tmp_path / ".git").exists()


def test_targets_add_refuses_a_path_shaped_agent_key(tmp_path):
    with pytest.raises(m.LedgerError, match="agent-key"):
        m.targets_add(tmp_path, "session-1", ["a.py"], agent_key="../evil")


def test_targets_add_refuses_absolute_path(tmp_path):
    with pytest.raises(m.LedgerError):
        m.targets_add(tmp_path, "session-1", ["/etc/passwd"])


def test_targets_add_refuses_drive_relative_path(tmp_path):
    with pytest.raises(m.LedgerError):
        m.targets_add(tmp_path, "session-1", ["C:foo.py"])


def test_verify_refuses_sidecar_outside_subagent_share(tmp_path):
    target = tmp_path / "src.py"
    target.write_text("x = 2\n", encoding="utf-8")
    text = (
        "---\nagent_type: coordinator:code-reviewer\n---\n"
        "## Findings\n\n### Finding 1\nSomething.\n\n"
        "## Findings Ledger\n\n```json\n"
        + json.dumps([{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2"}])
        + "\n```\n"
    )
    sidecar = tmp_path / "not-under-the-right-tree.md"
    sidecar.write_text(text, encoding="utf-8")
    with pytest.raises(m.LedgerError, match="subagent-share"):
        m.verify(sidecar, repo_root=tmp_path)


def test_verify_refuses_non_reviewer_agent_type(tmp_path):
    target = tmp_path / "src.py"
    target.write_text("x = 2\n", encoding="utf-8")
    text = (
        "---\nagent_type: coordinator:executor\n---\n"
        "## Findings\n\n### Finding 1\nSomething.\n\n"
        "## Findings Ledger\n\n```json\n"
        + json.dumps([{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2"}])
        + "\n```\n"
    )
    sidecar_dir = tmp_path / "state" / "subagent-share" / "sess-1"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    sidecar = sidecar_dir / "sidecar.md"
    sidecar.write_text(text, encoding="utf-8")
    with pytest.raises(m.LedgerError, match="agent_type"):
        m.verify(sidecar, repo_root=tmp_path)


@pytest.mark.parametrize(
    "on_disk",
    [
        b"a = 1\r\nb = 2\r\nc = 3\r\n",
        b"a = 1\nb = 2\r\nc = 3\r\n",
        b"a = 1\r\nb = 2\nc = 3\n",
    ],
    ids=["crlf", "mixed-lf-first", "mixed-crlf-first"],
)
def test_verify_accepts_multiline_after_in_crlf_or_mixed_file(tmp_path, on_disk):
    target = tmp_path / "src.py"
    target.write_bytes(on_disk)
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("a = 0\nb = 0\nc = 3\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "src.py", "before": "a = 0\nb = 0", "after": "a = 1\nb = 2"}],
    )
    outcome = m.verify(sidecar, repo_root=tmp_path)
    assert outcome.ok, outcome.failures


def test_verify_still_refuses_a_multiline_after_that_is_absent_from_a_crlf_file(tmp_path):
    target = tmp_path / "src.py"
    target.write_bytes(b"a = 1\r\nb = 9\r\nc = 3\r\n")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("a = 0\nb = 0\nc = 3\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "src.py", "before": "a = 0\nb = 0", "after": "a = 1\nb = 2"}],
    )
    outcome = m.verify(sidecar, repo_root=tmp_path)
    assert not outcome.ok
    assert "not found" in outcome.failures[0]


def test_cli_verify_help_prints_verify_usage(capsys):
    with pytest.raises(SystemExit) as exc:
        m.main(["verify", "-h"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    assert "review-findings-ledger verify" in out
    assert "--sidecar" in out


def test_cli_verify_exit_codes(tmp_path):
    target = tmp_path / "src.py"
    target.write_text("x = 2\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("x = 1\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2"}],
    )
    assert m.main(["--root", str(tmp_path), "verify", "--sidecar", str(sidecar)]) == 0


def test_cli_reject_and_targets(tmp_path):
    (tmp_path / ".git").mkdir()
    target = tmp_path / "src.py"
    target.write_text("x = 2\n", encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"src.py": _sha256("x = 1\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "src.py", "before": "x = 1", "after": "x = 2"}],
    )
    assert (
        m.main(
            ["--root", str(tmp_path), "reject", "--sidecar", str(sidecar), "--finding", "finding-1", "--reason", "no"]
        )
        == 0
    )
    assert m.main(["--root", str(tmp_path), "targets", "--add", "x.py", "--session-id", "s1"]) == 0


def test_cli_targets_add_accepts_multiple_paths_in_one_flag(tmp_path):
    (tmp_path / ".git").mkdir()
    assert (
        m.main(
            ["--root", str(tmp_path), "targets", "--add", "a.py", "b.py", "c.py", "--session-id", "s1"]
        )
        == 0
    )
    target_file = tmp_path / ".git" / "coordinator-sessions" / "s1" / "review-targets.txt"
    assert target_file.read_text(encoding="utf-8").splitlines() == ["a.py", "b.py", "c.py"]


def test_cli_targets_add_defaults_session_id_from_hook_env(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("COORDINATOR_SESSION_ID", "env-session-9")

    assert m.main(["--root", str(tmp_path), "targets", "--add", "x.py"]) == 0

    target_file = tmp_path / ".git" / "coordinator-sessions" / "env-session-9" / "review-targets.txt"
    assert target_file.is_file()


def test_cli_targets_add_without_session_id_or_env_refuses(tmp_path, monkeypatch):
    monkeypatch.delenv("COORDINATOR_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)

    assert m.main(["--root", str(tmp_path), "targets", "--add", "x.py"]) == 2


_PLAN_TMPL = """# P

## Tasks

```yaml plan-tasks
- id: A
  title: a
  surface: s
  writes: [x/a.py, x/b.py]
- id: B
  title: b
  surface: s
  writes: [x/b.py, y/c.py]
%s```
"""


def _plan(tmp_path, extra=""):
    p = tmp_path / "plan.md"
    p.write_text(_PLAN_TMPL % extra, encoding="utf-8")
    return p


def test_plan_targets_is_the_plan_then_the_deduped_union_of_row_writes(tmp_path):
    plan = _plan(tmp_path)
    assert m.plan_targets(plan) == [plan.as_posix(), "x/a.py", "x/b.py", "y/c.py"]


def test_plan_targets_fails_loud_naming_an_undeclared_row(tmp_path):
    plan = _plan(tmp_path, "- id: C\n  title: c\n  surface: s\n")
    with pytest.raises(m.LedgerError, match="C"):
        m.plan_targets(plan)


def test_targets_cli_from_plan_merges_with_explicit_add(tmp_path, capsys):
    (tmp_path / ".git").mkdir()
    plan = _plan(tmp_path)
    rc = m.main(["--root", str(tmp_path), "targets", "--from-plan", str(plan),
                 "--add", "z.py", "--session-id", "s1"])
    assert rc == 0
    f = tmp_path / ".git" / "coordinator-sessions" / "s1" / "review-targets.txt"
    assert f.read_text(encoding="utf-8").split() == [
        "z.py", "plan.md", "x/a.py", "x/b.py", "y/c.py"]


def test_verify_refuses_a_plan_spine_with_consumes_and_reads_at_head_overlap(tmp_path, monkeypatch):
    calls = _capture_rung_fires(monkeypatch)
    spine = (
        "## Tasks\n\n```yaml plan-tasks\n"
        '- id: C1\n  title: "t"\n  change_kind: doc-edit\n  surface: "docs/a.md"\n'
        '  writes: []\n  consumes:\n    - "docs/a.md"\n  reads_at_head:\n    - "docs/a.md"\n```\n'
    )
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True)
    plan.write_text("---\nstatus: draft\n---\nnew text\n" + spine, encoding="utf-8")
    sidecar = _write_sidecar(
        tmp_path,
        baseline={"docs/plans/p.md": _sha256("---\nstatus: draft\n---\nold text\n")},
        findings_count=1,
        rows=[{"id": "finding-1", "file": "docs/plans/p.md", "before": "old text", "after": "new text"}],
    )
    outcome = m.verify(sidecar, repo_root=tmp_path)
    assert not outcome.ok
    assert any("both" in f and "reads_at_head" in f for f in outcome.failures), outcome.failures
    assert calls == []


def test_read_ledger_reports_missing_stamp_and_never_writes(tmp_path):
    sidecar = _write_sidecar(tmp_path, baseline={}, findings_count=1, rows=[{"id": "F1"}])
    before = sidecar.read_bytes()
    assert m.read_ledger(sidecar) == (False, [{"id": "F1"}])
    assert sidecar.read_bytes() == before


def test_read_ledger_reports_a_present_stamp(tmp_path):
    sidecar = _write_sidecar(tmp_path, baseline={}, findings_count=1, rows=[{"id": "F1"}])
    sidecar.write_text(
        sidecar.read_text(encoding="utf-8").replace(
            "---\n# Review", "findings_ledger: {rows: 1}\n---\n# Review", 1
        ),
        encoding="utf-8",
    )
    assert m.read_ledger(sidecar) == (True, [{"id": "F1"}])


def test_read_ledger_returns_none_when_unparseable_or_absent(tmp_path):
    sidecar = _write_sidecar(tmp_path, baseline={}, findings_count=1, rows=[])
    sidecar.write_text(
        sidecar.read_text(encoding="utf-8").replace("[]", "{not json"), encoding="utf-8"
    )
    assert m.read_ledger(sidecar) is None
    assert m.read_ledger(tmp_path / "nope.md") is None


def _plan_reviewer_sidecar(tmp_path, agent_type: str):
    plan = tmp_path / "docs" / "plans" / "p.md"
    plan.parent.mkdir(parents=True, exist_ok=True)
    plan.write_text("fixture now patches the root\n", encoding="utf-8")
    baseline = _sha256("old text\n")
    text = (
        "---\n"
        "status: open\n"
        f"agent_type: {agent_type}\n"
        "kind: review-findings\n"
        f"reviewer: {agent_type}\n"
        "verdict: WARN\n"
        "plan: docs/plans/p.md\n"
        "target_plan: null\n"
        "divergence:\n  diverged: false\n"
        "commits: []\n"
        f"baseline_sha256:\n  docs/plans/p.md: {baseline}\n"
        "---\n\n"
        "## Findings\n\n- [P2] fixture omits the monkeypatch. Applied.\n\n"
        "## Findings Ledger\n\n```json\n"
        + json.dumps([{"id": "finding-1", "file": "docs/plans/p.md", "before": "old text", "after": "fixture now patches the root"}])
        + "\n```\n"
    )
    sidecar_dir = tmp_path / ".coordinator-local" / "subagent-share" / "sess-1"
    sidecar_dir.mkdir(parents=True, exist_ok=True)
    sidecar = sidecar_dir / "coordinator-plan-reviewer.md"
    sidecar.write_text(text, encoding="utf-8")
    return sidecar


@pytest.mark.parametrize("agent_type", ["game-dev:executor", "coordinator:staff-unknown", "game-dev:"])
def test_scope_refuses_non_reviewer_plugin_agent_types(tmp_path, agent_type):
    sidecar = _plan_reviewer_sidecar(tmp_path, agent_type)
    with pytest.raises(m.LedgerError, match="agent_type"):
        m._check_sidecar_scope(sidecar, tmp_path, sidecar.read_text(encoding="utf-8"))


@pytest.mark.parametrize("agent_type", ["coordinator:plan-reviewer", "review-findings", "game-dev:staff-game-dev"])
def test_verify_accepts_a_plan_reviewer_sidecar_of_either_shape(tmp_path, monkeypatch, agent_type):
    from coordinator_core.ops import plan_status_transition

    monkeypatch.setattr(plan_status_transition, "main", lambda argv: 0)
    sidecar = _plan_reviewer_sidecar(tmp_path, agent_type)
    m._check_sidecar_scope(sidecar, tmp_path, sidecar.read_text(encoding="utf-8"))
    outcome = m.verify(sidecar, repo_root=tmp_path)
    assert outcome.ok, outcome.failures


@pytest.mark.parametrize("status", [None, "open", "applied", "em_rejected"])
def test_verify_keeps_an_out_of_ask_row_and_it_reaches_the_peer_hunk_gate(tmp_path, status):
    from coordinator_core.ops.dispatch_emit import peer_hunk_gate

    row = {"id": "finding-1", "file": "peer/x.py", "out_of_ask": True}
    if status:
        row["status"] = status
    sidecar = _write_sidecar(tmp_path, baseline={}, findings_count=1, rows=[row])
    outcome = m.verify(sidecar, repo_root=tmp_path)
    assert outcome.ok, outcome.failures
    text = sidecar.read_text(encoding="utf-8")
    kept = m._parse_ledger_rows(text)[0]
    assert kept["out_of_ask"] is True and kept["verified"] is True
    assert kept.get("status") == status
    unresolved = peer_hunk_gate._unresolved_out_of_ask(text)
    assert bool(unresolved) == (status in (None, "open"))
    if unresolved:
        assert unresolved[0][1] == "peer/x.py"


def test_verify_still_refuses_an_out_of_ask_row_with_an_unknown_status(tmp_path):
    row = {"id": "finding-1", "file": "peer/x.py", "out_of_ask": True, "status": "bogus"}
    sidecar = _write_sidecar(tmp_path, baseline={}, findings_count=1, rows=[row])
    assert not m.verify(sidecar, repo_root=tmp_path).ok


def test_verify_still_refuses_open_status_on_a_normal_row(tmp_path):
    row = {"id": "finding-1", "file": "a.py", "before": "x", "after": "y", "status": "open"}
    sidecar = _write_sidecar(tmp_path, baseline={}, findings_count=1, rows=[row])
    assert not m.verify(sidecar, repo_root=tmp_path).ok
