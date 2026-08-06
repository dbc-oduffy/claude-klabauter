"""Characterization tests for coordinator_core.ops.normalize_claimed_frontmatter.

Golden-oracle-derived: cases 1-5 mirror
coordinator/bin/normalize-consumed-frontmatter.test.js (example-doctrine-repo) 1:1,
proving the write-path shape guard (C4b) survived the port byte-identically.
Cases 6+ cover the CLI-level (main()) behavior the JS unit test never
exercised directly (parseArgs, git-tracked-file gating, walkDir), hand-run
against the node CLI during the port to pin exit codes and stdout shape.

DR-084 C5: fixtures/assertions moved to the post-cutover status/claimed_at/
shipped_in vocabulary; the body-comment marker convention
(`<!-- consumed: ... -->`) is a separately-scoped, never-renamed surface.

Port source: coordinator/bin/normalize-consumed-frontmatter.js (example-doctrine-repo)
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from coordinator_core.ops.normalize_claimed_frontmatter import main, normalize_one


def _write_fixture(dir_: Path, name: str, marker: str) -> Path:
    content = (
        "---\nstatus: open\ndeployment_state: in_flight\nclaimed_at: 2026-06-14\n---\n"
        "\n# Body content\n\n"
        f"<!-- consumed: 2026-06-14 {marker} -->\n"
    )
    p = dir_ / name
    p.write_text(content, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# normalize_one — positive: bare SHA / sanctioned token notes get written
# ---------------------------------------------------------------------------

def test_bare_sha_marker_notes_written_to_shipped_in(tmp_path):
    p = _write_fixture(tmp_path, "sha.md", "abc1234f")
    out = normalize_one(str(p))
    assert out is not None
    assert "shipped_in: abc1234f" in out["rebuilt"]
    assert "shipped_in_kind: ship-commit" in out["rebuilt"]


def test_all_numeric_sha_marker_notes_written_as_quoted_string(tmp_path):
    """example-doctrine-repo declined to widen `shipped_in` to accept ints (schema 2.1.0): an
    all-numeric abbreviated SHA is a write-side quoting defect on our surface,
    not a reader-schema gap. Pins that ``insert_fm_field`` writes an
    all-numeric SHA with ``numeric_quoting=True`` so it round-trips through
    ``yaml.safe_load`` as ``str``, never ``int``.
    """
    p = _write_fixture(tmp_path, "numeric-sha.md", "8155357")
    out = normalize_one(str(p))
    assert out is not None
    parsed = yaml.safe_load(out["rebuilt"].split("---")[1])
    assert isinstance(parsed["shipped_in"], str)
    assert parsed["shipped_in"] == "8155357"


def test_sanctioned_no_commit_token_written_to_shipped_in(tmp_path):
    p = _write_fixture(tmp_path, "token.md", "substantively-shipped-no-commit:2026-07-08")
    out = normalize_one(str(p))
    assert out is not None
    assert "shipped_in:" in out["rebuilt"]
    assert "substantively-shipped-no-commit:2026-07-08" in out["rebuilt"]
    assert "shipped_in_kind: no-commit" in out["rebuilt"]


# ---------------------------------------------------------------------------
# normalize_one — negative: prose / annotated-SHA notes must NOT be written
# ---------------------------------------------------------------------------

def test_prose_rationale_notes_not_written_write_path_gated(tmp_path):
    p = _write_fixture(
        tmp_path, "prose.md", "shipped as part of the bigger refactor no single commit"
    )
    out = normalize_one(str(p))
    # status/deployment_state/claimed_at still flip -- only the shipped_in
    # insert is refused.
    if out is not None:
        assert "shipped_in:" not in out["rebuilt"]


def test_annotated_sha_notes_not_written(tmp_path):
    p = _write_fixture(tmp_path, "annotated.md", "[~/.claude] f4f150a1d")
    out = normalize_one(str(p))
    if out is not None:
        assert "shipped_in:" not in out["rebuilt"]


def test_malformed_shipped_in_refusal_recorded_in_changes(tmp_path):
    p = _write_fixture(tmp_path, "prose2.md", "shipped elsewhere, see chat")
    out = normalize_one(str(p))
    assert out is not None, "other fields (status/claimed_at) still change"
    noted = any(
        "shipped_in" in c.lower() or "shipped_in" in c
        for c in out["changes"]
        if any(kw in c.lower() for kw in ("refus", "reject", "skip", "invalid"))
    )
    assert noted, f"Expected a refusal note in changes[], got: {out['changes']}"


# ---------------------------------------------------------------------------
# C1c regression: byte-identical to the pre-migration (node) snapshot, moved
# to new vocabulary
# ---------------------------------------------------------------------------

def test_normalize_one_output_byte_identical_to_node_oracle_snapshot(tmp_path):
    # DR-096 (example-doctrine-repo 2026-07-26 ruling): no longer byte-identical to the
    # pre-DR-096 node oracle -- this pass adds a lockstep `shipped_in_kind:`
    # insert immediately after `shipped_in:` that the oracle (predating the
    # kind discriminant entirely) never wrote. The snapshot below is pinned
    # to the current, post-ruling shape; see git history for the literal
    # byte-identical-to-oracle version this test asserted before DR-096.
    p = _write_fixture(tmp_path, "sha-snapshot.md", "abc1234f")
    out = normalize_one(str(p))
    assert out is not None
    expected = (
        "---\n"
        "status: claimed\n"
        "deployment_state: shipped\n"
        "claimed_at: 2026-06-14\n"
        "shipped_in: abc1234f\n"
        "shipped_in_kind: ship-commit\n"
        "---\n"
        "\n"
        "# Body content\n"
        "\n"
        "<!-- consumed: 2026-06-14 abc1234f -->\n"
    )
    assert out["rebuilt"] == expected


# ---------------------------------------------------------------------------
# normalize_one — no-drift / terminal-state guards
# ---------------------------------------------------------------------------

def test_no_marker_no_change(tmp_path):
    p = tmp_path / "plain.md"
    p.write_text("---\nstatus: open\n---\n\nNo marker here.\n", encoding="utf-8")
    assert normalize_one(str(p)) is None


def test_terminal_status_preserved(tmp_path):
    content = (
        "---\nstatus: claimed\nclosed_reason: stale\ndeployment_state: closed\n"
        "claimed_at: 2026-06-14\n---\n"
        "\n<!-- consumed: 2026-06-14 abc1234f -->\n"
    )
    p = tmp_path / "terminal.md"
    p.write_text(content, encoding="utf-8")
    out = normalize_one(str(p))
    # status/deployment_state are already terminal and claimed_at already
    # present -- only shipped_in is new, so a change IS reported, but the
    # terminal fields must not be touched.
    assert out is not None
    assert "status: claimed" in out["rebuilt"]
    assert "deployment_state: closed" in out["rebuilt"]


def test_gate_dependency_retired_to_blocking_notes_on_flip(tmp_path):
    content = (
        "---\nstatus: open\ndeployment_state: awaiting_gate\n"
        "claimed_at: 2026-06-14\ngate_dependency: some blocker\n---\n"
        "\n<!-- consumed: 2026-06-14 abc1234f -->\n"
    )
    p = tmp_path / "gated.md"
    p.write_text(content, encoding="utf-8")
    out = normalize_one(str(p))
    assert out is not None
    # gate_dependency is gone (schema forbids it at the terminal shipped state)
    # but the prose survives -- moved to blocking_notes, not silently dropped.
    assert "gate_dependency:" not in out["rebuilt"]
    assert "blocking_notes:" in out["rebuilt"]
    assert "some blocker" in out["rebuilt"]
    assert any("gate_dependency: retired to blocking_notes" in c for c in out["changes"])


def test_gate_dependency_retirement_appends_not_overwrites_existing_blocking_notes(tmp_path):
    # A node may already carry advisory blocking_notes unrelated to the gate --
    # retirement must APPEND, never clobber the pre-existing note.
    content = (
        "---\nstatus: open\ndeployment_state: awaiting_gate\n"
        "claimed_at: 2026-06-14\ngate_dependency: some blocker\n"
        "blocking_notes: pre-existing advisory note\n---\n"
        "\n<!-- consumed: 2026-06-14 abc1234f -->\n"
    )
    p = tmp_path / "gated-with-notes.md"
    p.write_text(content, encoding="utf-8")
    out = normalize_one(str(p))
    assert out is not None
    assert "gate_dependency:" not in out["rebuilt"]
    assert "pre-existing advisory note" in out["rebuilt"]
    assert "some blocker" in out["rebuilt"]


def test_gate_dependency_retirement_preserves_value_longer_than_former_60_char_truncation(tmp_path):
    # AC11: the former changes-log echo truncated at 60 chars + an ellipsis --
    # exactly the case that silently lost data. The full value must now survive
    # in blocking_notes even when it is well past that old truncation point.
    long_value = "waiting on sibling repo X to ship PR 4821, see DR-148 for the full cross-repo rationale and timeline"
    assert len(long_value) > 60
    # On-disk YAML value is comma-bearing prose -- serialize_yaml_scalar would
    # single-quote a value like this; fixture mirrors that shape.
    quoted_value = "'" + long_value.replace("'", "''") + "'"
    content = (
        "---\nstatus: open\ndeployment_state: awaiting_gate\n"
        f"claimed_at: 2026-06-14\ngate_dependency: {quoted_value}\n---\n"
        "\n<!-- consumed: 2026-06-14 abc1234f -->\n"
    )
    p = tmp_path / "gated-long.md"
    p.write_text(content, encoding="utf-8")
    out = normalize_one(str(p))
    assert out is not None
    assert long_value in out["rebuilt"]
    assert "…" not in out["rebuilt"]


# ---------------------------------------------------------------------------
# main() — CLI-level behavior (git-tracked gating, --dry-run, --type, exit codes)
# ---------------------------------------------------------------------------

def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)


def test_main_flips_tracked_file_and_reports_on_stdout(tmp_path, capsys):
    _init_git_repo(tmp_path)
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    p = _write_fixture(handoffs, "sha.md", "abc1234f")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    rc = main(["--root", str(tmp_path), "--type", "handoff"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Updated state/handoffs/sha.md" in out
    assert "shipped_in: + abc1234f" in out
    assert "shipped_in: abc1234f" in p.read_text(encoding="utf-8")


def test_main_dry_run_does_not_write(tmp_path, capsys):
    _init_git_repo(tmp_path)
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    p = _write_fixture(handoffs, "sha.md", "abc1234f")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    original = p.read_text(encoding="utf-8")

    rc = main(["--root", str(tmp_path), "--type", "handoff", "--dry-run"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Would update state/handoffs/sha.md" in out
    assert p.read_text(encoding="utf-8") == original


def test_main_skips_untracked_file(tmp_path, capsys):
    _init_git_repo(tmp_path)
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    _write_fixture(handoffs, "untracked.md", "abc1234f")
    # deliberately NOT `git add`-ed — the Staff Engineer F5 gate

    rc = main(["--root", str(tmp_path), "--type", "handoff"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "No drift between body markers and frontmatter." in err


def test_main_no_drift_prints_stderr_message(tmp_path, capsys):
    _init_git_repo(tmp_path)
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    (handoffs / "plain.md").write_text(
        "---\nstatus: open\n---\n\nNo marker.\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    rc = main(["--root", str(tmp_path), "--type", "handoff"])
    assert rc == 0
    assert "No drift between body markers and frontmatter." in capsys.readouterr().err


def test_main_unknown_flag_exits_1(tmp_path, capsys):
    with pytest.raises(SystemExit) as exc_info:
        main(["--bogus"])
    assert exc_info.value.code == 1
    assert "Unknown argument: --bogus" in capsys.readouterr().err


def test_main_block_scalar_field_errors_but_continues_scan(tmp_path, capsys):
    # Rule 6 (porter-brief addendum): the block-scalar guard is a platform/
    # edge-case fix class -- this test pins the fixture that exercises it.
    _init_git_repo(tmp_path)
    handoffs = tmp_path / "state" / "handoffs"
    handoffs.mkdir(parents=True)
    bad = handoffs / "block-scalar.md"
    bad.write_text(
        "---\nstatus: >\n  folded scalar value\ndeployment_state: in_flight\n"
        "claimed_at: 2026-06-14\n---\n\n<!-- consumed: 2026-06-14 abc1234f -->\n",
        encoding="utf-8",
    )
    good = _write_fixture(handoffs, "good.md", "abc1234f")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)

    rc = main(["--root", str(tmp_path), "--type", "handoff"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "ERROR state/handoffs/block-scalar.md" in err
    assert "shipped_in: abc1234f" in good.read_text(encoding="utf-8")
