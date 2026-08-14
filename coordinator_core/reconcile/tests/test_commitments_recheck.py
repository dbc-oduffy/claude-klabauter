"""
coordinator_core.reconcile.tests.test_commitments_recheck — coverage for the
`state/cross-repo-commitments/` ledger re-resolver (C12b).

Coverage:
  (a) the named F10 pathology, reproduced against a fixture: a record whose
      OWN title reads "(now satisfied)" beside `status: open` and whose
      `evidence:` resolves truthy MUST surface `actionable: True` — and the
      ledger record on disk must be byte-identical before/after (never
      auto-flipped, D5).
  (b) `evidence:` unset reports `resolvable: False` ("not yet resolvable"),
      never a false `actionable: False` that looks identical to "resolved and
      not satisfied".
  (c) `file:` evidence, including the known C12a corpus quirk of a leading
      repo-name segment that must be stripped before resolution.
  (d) `symbol:<module.qualname>` projects onto a `file_exists` check over the
      qualname's own containing module file.
  (e) an unmapped `committed_by` is "not yet resolvable", never a repo-id guess.
  (f) a sibling read failure (unresolvable repo root) is "not yet resolvable",
      never a false negative.
  (g) a record already `status: fulfilled` whose evidence resolves truthy is
      resolvable but NOT actionable — no mismatch to surface.
  (h) unparseable ledger YAML is included, not silently dropped.

Spec backlink: pln-structured-sibling-evidence-ga-6e2ceb § C12b
"""

from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path
from typing import List

import pytest

from coordinator_core import sibling_fact
from coordinator_core.reconcile import commitments_recheck
from coordinator_core.reconcile.commitments_recheck import recheck_commitments


# ---------------------------------------------------------------------------
# Sibling git-repo fixture (mirrors coordinator_core/tests/test_sibling_fact.py)
# ---------------------------------------------------------------------------


def _git(args: List[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git"] + args,
        cwd=str(cwd),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )


def _init_repo(path: Path) -> None:
    _git(["init", "-b", "main"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["config", "user.name", "Test"], path)


def _make_commit(repo: Path, message: str) -> str:
    _git(["commit", "--allow-empty", "-m", message], repo)
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(repo),
        capture_output=True,
        encoding="utf-8",
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def sibling_repo(tmp_path: Path) -> Path:
    """A real git repo standing in for the DoE-claude clone, one commit deep."""
    root = tmp_path / "doe-claude-sibling"
    root.mkdir()
    _init_repo(root)
    _make_commit(root, "first")
    return root


@pytest.fixture
def as_doe_claude(monkeypatch: pytest.MonkeyPatch, sibling_repo: Path) -> Path:
    """Route `repo: doe_claude` at `sibling_repo` via the same monkeypatch
    seam `test_sibling_fact.py` uses — never a registry file write."""
    monkeypatch.setattr(sibling_fact, "read_doe_root_pointer", lambda: str(sibling_repo))
    return sibling_repo


@pytest.fixture
def ledger_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "cross-repo-commitments"
    directory.mkdir()
    return directory


def _write_record(directory: Path, name: str, **fields: object) -> Path:
    lines = [f"{key}: {value}" for key, value in fields.items() if value is not None]
    path = directory / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# (a) The named F10 pathology: stale title, evidence resolves, no auto-flip
# ---------------------------------------------------------------------------


def test_stale_title_status_mismatch_surfaces_actionable_without_auto_flip(
    ledger_dir: Path, as_doe_claude: Path
) -> None:
    sha = _make_commit(as_doe_claude, "second")
    record_path = _write_record(
        ledger_dir,
        "stale.yaml",
        title='"DoE to clear the gate (now satisfied)"',
        status="open",
        committed_by="doe-claude-em",
        evidence=f'"commit-sha:{sha}"',
    )
    before = record_path.read_text(encoding="utf-8")

    result = recheck_commitments(commitments_dir=ledger_dir)

    assert result["checked"] == 1
    assert len(result["actionable"]) == 1
    entry = result["actionable"][0]
    assert entry["entry"] == "stale.yaml"
    assert entry["status"] == "open"
    assert entry["resolvable"] is True
    assert entry["observation"]["read_ok"] is True
    assert entry["observation"]["observed"] is True

    after = record_path.read_text(encoding="utf-8")
    assert after == before, "recheck_commitments must never mutate a ledger record"
    assert "status: open" in after


# ---------------------------------------------------------------------------
# (b) evidence unset -> not-yet-resolvable, never resolved-false
# ---------------------------------------------------------------------------


def test_evidence_unset_is_not_yet_resolvable_not_resolved_false(ledger_dir: Path) -> None:
    _write_record(
        ledger_dir,
        "no-evidence.yaml",
        title="Some open commitment with no evidence yet",
        status="open",
        committed_by="doe-claude-em",
    )

    result = recheck_commitments(commitments_dir=ledger_dir)

    assert result["checked"] == 1
    assert result["actionable"] == []
    assert len(result["not_yet_resolvable"]) == 1
    entry = result["not_yet_resolvable"][0]
    assert entry["resolvable"] is False
    assert entry["actionable"] is False
    assert entry["observation"] is None
    assert "not yet resolvable" in entry["reason"]


def test_evidence_null_is_also_not_yet_resolvable(ledger_dir: Path) -> None:
    _write_record(
        ledger_dir,
        "null-evidence.yaml",
        title="Open commitment with an explicit null evidence",
        status="open",
        committed_by="doe-claude-em",
        evidence="null",
    )

    result = recheck_commitments(commitments_dir=ledger_dir)

    assert result["not_yet_resolvable"][0]["resolvable"] is False
    assert result["actionable"] == []


# ---------------------------------------------------------------------------
# (c) file: evidence + the known leading-repo-name-segment corpus quirk
# ---------------------------------------------------------------------------


def test_file_evidence_resolves_true_when_present(ledger_dir: Path, as_doe_claude: Path) -> None:
    target = as_doe_claude / "coordinator" / "docs" / "wiki"
    target.mkdir(parents=True)
    (target / "tripwires.md").write_text("content\n", encoding="utf-8")

    _write_record(
        ledger_dir,
        "file-evidence.yaml",
        title="claude-central-em to repoint a doc citation",
        status="open",
        committed_by="claude-central-em",
        evidence='"file:coordinator/docs/wiki/tripwires.md"',
    )

    result = recheck_commitments(commitments_dir=ledger_dir)

    assert len(result["actionable"]) == 1
    assert result["actionable"][0]["observation"]["observed"] is True


def test_file_evidence_strips_known_leading_repo_name_segment(
    ledger_dir: Path, as_doe_claude: Path
) -> None:
    target = as_doe_claude / "coordinator" / "hooks" / "scripts"
    target.mkdir(parents=True)
    (target / "enforce-agent-dispatch-mode.py").write_text("# stub\n", encoding="utf-8")

    _write_record(
        ledger_dir,
        "prefixed-file-evidence.yaml",
        title="DoE to land the consuming half",
        status="open",
        committed_by="claude-central-em",
        evidence='"file:DoE-claude/coordinator/hooks/scripts/enforce-agent-dispatch-mode.py"',
    )

    result = recheck_commitments(commitments_dir=ledger_dir)

    assert len(result["actionable"]) == 1
    assert result["actionable"][0]["observation"]["observed"] is True


# ---------------------------------------------------------------------------
# (d) symbol: evidence projects onto a file_exists check on the module file
# ---------------------------------------------------------------------------


def test_symbol_evidence_resolves_via_module_file_existence(
    ledger_dir: Path, as_doe_claude: Path
) -> None:
    target = as_doe_claude / "coordinator_core" / "reconcile"
    target.mkdir(parents=True)
    (target / "gate_eval.py").write_text("def evaluate_gate(): ...\n", encoding="utf-8")

    _write_record(
        ledger_dir,
        "symbol-evidence.yaml",
        title="doe-claude-em to land evaluate_gate",
        status="open",
        committed_by="doe-claude-em",
        evidence='"symbol:coordinator_core.reconcile.gate_eval.evaluate_gate"',
    )

    result = recheck_commitments(commitments_dir=ledger_dir)

    assert len(result["actionable"]) == 1
    assert result["actionable"][0]["observation"]["observed"] is True


# ---------------------------------------------------------------------------
# (e) unmapped committed_by -> not yet resolvable, never a repo-id guess
# ---------------------------------------------------------------------------


def test_unmapped_committed_by_is_not_yet_resolvable(ledger_dir: Path) -> None:
    _write_record(
        ledger_dir,
        "unmapped.yaml",
        title="A sibling this table has never seen",
        status="open",
        committed_by="some-new-sibling-em",
        evidence='"commit-sha:deadbeef"',
    )

    result = recheck_commitments(commitments_dir=ledger_dir)

    assert result["actionable"] == []
    entry = result["not_yet_resolvable"][0]
    assert entry["resolvable"] is False
    assert "no known sibling repo-id mapping" in entry["reason"]


# ---------------------------------------------------------------------------
# (f) a sibling read failure is not-yet-resolvable, never a false negative
# ---------------------------------------------------------------------------


def test_unresolvable_sibling_repo_is_not_yet_resolvable(
    monkeypatch: pytest.MonkeyPatch, ledger_dir: Path
) -> None:
    monkeypatch.setattr(sibling_fact, "read_doe_root_pointer", lambda: "")

    _write_record(
        ledger_dir,
        "unresolvable-sibling.yaml",
        title="No DoE clone on this machine",
        status="open",
        committed_by="doe-claude-em",
        evidence='"commit-sha:deadbeef"',
    )

    result = recheck_commitments(commitments_dir=ledger_dir)

    assert result["actionable"] == []
    entry = result["not_yet_resolvable"][0]
    assert entry["resolvable"] is False
    assert entry["observation"]["read_ok"] is False


# ---------------------------------------------------------------------------
# (g) resolvable, already fulfilled -> resolvable but not actionable
# ---------------------------------------------------------------------------


def test_fulfilled_record_with_resolving_evidence_is_not_actionable(
    ledger_dir: Path, as_doe_claude: Path
) -> None:
    sha = _make_commit(as_doe_claude, "second")
    _write_record(
        ledger_dir,
        "already-fulfilled.yaml",
        title="Already closed out",
        status="fulfilled",
        committed_by="doe-claude-em",
        evidence=f'"commit-sha:{sha}"',
    )

    result = recheck_commitments(commitments_dir=ledger_dir)

    assert result["actionable"] == []
    record = result["records"][0]
    assert record["resolvable"] is True
    assert record["actionable"] is False


# ---------------------------------------------------------------------------
# (h) unparseable ledger YAML is included, not silently dropped
# ---------------------------------------------------------------------------


def test_unparseable_ledger_record_is_included_not_dropped(ledger_dir: Path) -> None:
    bad = ledger_dir / "broken.yaml"
    bad.write_text(
        textwrap.dedent(
            """\
            title: "unterminated quote
            status: open
            """
        ),
        encoding="utf-8",
    )

    result = recheck_commitments(commitments_dir=ledger_dir)

    assert result["checked"] == 1
    assert result["not_yet_resolvable"][0]["entry"] == "broken.yaml"


# ---------------------------------------------------------------------------
# Empty / absent ledger directory
# ---------------------------------------------------------------------------


def test_absent_ledger_directory_yields_zero_checked(tmp_path: Path) -> None:
    result = recheck_commitments(commitments_dir=tmp_path / "does-not-exist")
    assert result == {
        "checked": 0,
        "records": [],
        "actionable": [],
        "not_yet_resolvable": [],
    }


def test_default_commitments_dir_points_at_state_cross_repo_commitments() -> None:
    assert commitments_recheck.DEFAULT_COMMITMENTS_DIR.parts[-2:] == (
        "state",
        "cross-repo-commitments",
    )
