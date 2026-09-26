"""Tests for coordinator_core.ops.review_findings_ledger — the
reviewer-applies-own-findings ledger op (DoE-claude
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
    merged = m.targets_add(tmp_path, "session-1", ["a/b.py", "c/d.py", "a/b.py"])
    assert merged == ["a/b.py", "c/d.py"]
    again = m.targets_add(tmp_path, "session-1", ["e/f.py"])
    assert again == ["a/b.py", "c/d.py", "e/f.py"]
    target_file = tmp_path / ".git" / "coordinator-sessions" / "session-1" / "review-targets.txt"
    assert target_file.read_text(encoding="utf-8").splitlines() == ["a/b.py", "c/d.py", "e/f.py"]


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
