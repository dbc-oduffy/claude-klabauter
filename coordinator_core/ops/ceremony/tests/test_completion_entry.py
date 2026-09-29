"""
coordinator_core.ops.ceremony.tests.test_completion_entry

Tests for ceremony.completion_entry -- the C7 chunk of the `wsc_tail` rebuild
(docs/plans/2026-07-16-wsc-pure-python-tail-rebuild.md).

Coverage:
  scaffold_completion_entry:
    (a) scaffold_produces_well_formed_entry -- happy path: file lands at the
        deterministic archive/completed/<YYYY-MM>/<YYYY-MM-DD>-<slug>.md path,
        parses as valid frontmatter, and carries the expected field set.
    (b) scaffold_idempotent_skip -- a second call against an already-scaffolded
        title is a clean skip, never a clobber (content byte-identical to the
        first call's output).
    (c) scaffold_chain_field_present_when_supplied -- chain= supplies a real
        `chain:` field instead of the commented-out placeholder line.
    (d) scaffold_no_bash_or_subprocess -- mechanical AC1/AC2 guard: the module
        source contains no `subprocess` import and no shell-out call.

  fill_completion_entry_residues:
    (e) fill_prose_replaces_sentinel -- prose lands where the `<!-- ONE paragraph`
        sentinel was, sentinel fully removed.
    (f) fill_chain_terminal_flips_false_to_true -- chain_terminal=True flips the
        live YAML key to `true`, dropping the trailing comment (parity with the
        OLD prefix-substitution behavior).
    (g) fill_authored_by_uncomments_placeholder -- sid fill replaces the commented
        placeholder line with a live `authored_by: <sid>` key.
    (h) fill_is_idempotent -- a second fill call against an already-filled entry
        is a clean all-False no-op (byte-identical file, no re-write).
    (i) fill_empty_path_returns_all_false -- empty completion_entry_path short-
        circuits to the all-False empty result without touching disk.
    (j) fill_missing_file_returns_all_false -- a nonexistent target degrades to
        all-False rather than raising.
    (k) fill_frontmatter_safe -- the residue fill preserves the file's frontmatter
        parseability (round-trips through split_frontmatter cleanly) and leaves
        untouched fields (title, created, nature, status, commits, loe) verbatim.

Spec backlink: pln-rebuild-the-wsc-commit-ceremon-f7c2a0 § C7 (AC1, AC2).
"""

from __future__ import annotations

import inspect
import subprocess
from pathlib import Path

import pytest

from coordinator_core.completion_record_integrity import HollowCompletionRecordError
from coordinator_core.frontmatter.primitives import read_fm_field, split_frontmatter
from coordinator_core.ops.ceremony import completion_entry
from coordinator_core.win_portability import no_console_creationflags

pytestmark = [pytest.mark.spawns_process, pytest.mark.cadence]


def _make_git_repo(tmp_path: Path) -> Path:
    """Create a minimal git repo and return its worktree root.

    ``locked_rmw`` (via ``git_common_dir``) requires a real git repository --
    a bare ``tmp_path`` is not one. Mirrors the fixture pattern used by
    ``coordinator_core/ops/tests/test_handoff_stamp.py``.
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    def _git(*args: str) -> None:
        subprocess.run(
            ["git"] + list(args),
            cwd=str(repo),
            capture_output=True,
            check=True,
            **no_console_creationflags(),
        )

    _git("init", "-b", "main")
    _git("config", "user.email", "completion-entry-test@claude-klabauter.test")
    _git("config", "user.name", "Completion Entry Test")
    _git("config", "commit.gpgsign", "false")
    (repo / ".gitkeep").write_text("", encoding="utf-8")
    _git("add", "-A")
    _git("commit", "-m", "chore: initial skeleton")

    return repo


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    return _make_git_repo(tmp_path)


# ---------------------------------------------------------------------------
# scaffold_completion_entry
# ---------------------------------------------------------------------------


def test_scaffold_produces_well_formed_entry(repo: Path) -> None:
    result = completion_entry.scaffold_completion_entry(repo, "Ship the wsc tail rebuild")

    assert result["failed"] == []
    assert result["skipped"] == []
    assert len(result["acted"]) == 1
    rel_path = result["completion_entry_path"]
    assert rel_path

    abs_path = repo / rel_path
    assert abs_path.is_file()
    text = abs_path.read_text()

    split = split_frontmatter(text)
    assert split is not None
    assert read_fm_field(split.fm_text, "title") == '"Ship the wsc tail rebuild"'
    assert read_fm_field(split.fm_text, "nature") is not None
    assert read_fm_field(split.fm_text, "nature").split()[0] == "infra"
    assert read_fm_field(split.fm_text, "status") == "pending-release"
    assert read_fm_field(split.fm_text, "chain_terminal").split()[0] == "false"
    assert read_fm_field(split.fm_text, "commits").split()[0] == "[]"
    assert "# authored_by: PLACEHOLDER" in split.fm_text
    assert "<!-- ONE paragraph" in split.body_with_leading_newline


def test_scaffold_idempotent_skip(repo: Path) -> None:
    first = completion_entry.scaffold_completion_entry(repo, "Idempotent scaffold test")
    abs_path = repo / first["completion_entry_path"]
    original_text = abs_path.read_text()

    second = completion_entry.scaffold_completion_entry(repo, "Idempotent scaffold test")

    assert second["acted"] == []
    assert second["failed"] == []
    assert len(second["skipped"]) == 1
    assert second["completion_entry_path"] == first["completion_entry_path"]
    assert abs_path.read_text() == original_text


def test_scaffold_chain_field_present_when_supplied(repo: Path) -> None:
    result = completion_entry.scaffold_completion_entry(
        repo, "Scaffold with chain", chain="docs/plans/2026-07-16-example.md"
    )
    abs_path = repo / result["completion_entry_path"]
    split = split_frontmatter(abs_path.read_text())
    assert split is not None
    assert read_fm_field(split.fm_text, "chain") == '"docs/plans/2026-07-16-example.md"'
    assert "# chain: null" not in split.fm_text


def test_scaffold_no_bash_or_subprocess() -> None:
    """Mechanical AC1/AC2 guard: the module never imports `subprocess` and never
    shells out -- docstring/negative-spec prose is free to NAME the OLD
    subprocess-based seam this module replaces ("subprocess with a stdout
    PATH-RETURN contract"), so this checks for the concrete import/invocation
    tokens rather than the bare substring "subprocess"."""
    source = inspect.getsource(completion_entry)
    assert "import subprocess" not in source
    assert "subprocess.run(" not in source
    assert "subprocess.Popen(" not in source
    assert '"bash"' not in source
    assert "'bash'" not in source


# ---------------------------------------------------------------------------
# fill_completion_entry_residues
# ---------------------------------------------------------------------------


def _scaffold(repo: Path, title: str = "Fill residues test") -> tuple[Path, str]:
    result = completion_entry.scaffold_completion_entry(repo, title)
    return repo / result["completion_entry_path"], result["completion_entry_path"]


def test_fill_prose_replaces_sentinel(repo: Path) -> None:
    abs_path, rel_path = _scaffold(repo)
    prose = "<!-- ONE paragraph -->\nShipped the native completion-entry tail port."

    filled = completion_entry.fill_completion_entry_residues(
        repo, rel_path, prose, chain_terminal=False, sid=""
    )

    assert filled["prose"] is True
    assert filled["chain_terminal"] is False
    assert filled["authored_by"] is False
    text = abs_path.read_text()
    assert "Shipped the native completion-entry tail port." in text
    assert "<!-- ONE paragraph (" not in text


def test_fill_chain_terminal_flips_false_to_true(repo: Path) -> None:
    abs_path, rel_path = _scaffold(repo)

    # chain_terminal=True is now a finalize signal (2026-09-28 EM
    # correction: ANY hollow check failing at finalize is refused) -- prose
    # must be supplied in the SAME call, matching the production shape
    # (this module has no way to author prose in a later call other than
    # passing it here).
    filled = completion_entry.fill_completion_entry_residues(
        repo, rel_path, "<!-- ONE paragraph -->\nShipped it.", chain_terminal=True, sid=""
    )

    assert filled == {"prose": True, "chain_terminal": True, "authored_by": False}
    split = split_frontmatter(abs_path.read_text())
    assert split is not None
    # The scaffold authors chain_terminal with a trailing inline comment, and the
    # flip PRESERVES it (2026-08-01, example-cockpit-repo memo) -- so the raw read is
    # `true  # <comment>`, not a bare `true`. The production check at
    # completion_entry._CHAIN_TERMINAL_FALSE_TOKEN is already token-anchored for
    # exactly this reason; this asserts value and surviving comment separately.
    raw = read_fm_field(split.fm_text, "chain_terminal")
    assert raw is not None
    assert raw.split()[0] == "true"
    assert "chain-terminal entry" in raw


def test_fill_authored_by_uncomments_placeholder(repo: Path) -> None:
    abs_path, rel_path = _scaffold(repo)

    filled = completion_entry.fill_completion_entry_residues(
        repo, rel_path, "", chain_terminal=False, sid="sess-abc123"
    )

    assert filled == {"prose": False, "chain_terminal": False, "authored_by": True}
    split = split_frontmatter(abs_path.read_text())
    assert split is not None
    assert read_fm_field(split.fm_text, "authored_by") == "sess-abc123  # forensic tracing only"
    assert "# authored_by: PLACEHOLDER" not in split.fm_text


def test_fill_is_idempotent(repo: Path) -> None:
    abs_path, rel_path = _scaffold(repo)
    # Deliberately does NOT reuse the "<!-- ONE paragraph" sentinel text inside the
    # prose itself -- if it did, the second fill call would re-find that literal
    # substring in the already-filled body and re-replace it, masking a real
    # non-idempotency bug as a false-pass.
    prose = "Idempotent fill body, no sentinel text embedded."

    completion_entry.fill_completion_entry_residues(
        repo, rel_path, prose, chain_terminal=True, sid="sess-xyz"
    )
    filled_text = abs_path.read_text()

    second = completion_entry.fill_completion_entry_residues(
        repo, rel_path, prose, chain_terminal=True, sid="sess-xyz"
    )

    assert second == {"prose": False, "chain_terminal": False, "authored_by": False}
    assert abs_path.read_text() == filled_text


def test_fill_empty_path_returns_all_false(repo: Path) -> None:
    filled = completion_entry.fill_completion_entry_residues(
        repo, "", "some prose", chain_terminal=True, sid="sess-1"
    )
    assert filled == {"prose": False, "chain_terminal": False, "authored_by": False}


def test_fill_missing_file_returns_all_false(repo: Path) -> None:
    filled = completion_entry.fill_completion_entry_residues(
        repo, "archive/completed/2026-07/does-not-exist.md", "prose",
        chain_terminal=True, sid="sess-1",
    )
    assert filled == {"prose": False, "chain_terminal": False, "authored_by": False}


def test_fill_frontmatter_safe(repo: Path) -> None:
    abs_path, rel_path = _scaffold(repo, title="Frontmatter safety test")
    prose = "<!-- ONE paragraph -->\nFrontmatter safety body."

    completion_entry.fill_completion_entry_residues(
        repo, rel_path, prose, chain_terminal=True, sid="sess-fm-safe"
    )

    text = abs_path.read_text()
    split = split_frontmatter(text)
    assert split is not None
    assert read_fm_field(split.fm_text, "title") == '"Frontmatter safety test"'
    assert read_fm_field(split.fm_text, "created") is not None
    assert read_fm_field(split.fm_text, "nature").split()[0] == "infra"
    assert read_fm_field(split.fm_text, "status") == "pending-release"
    assert read_fm_field(split.fm_text, "commits").split()[0] == "[]"
    assert "agent_dispatches: null" in split.fm_text
    assert "opus_dispatches: null" in split.fm_text


# ---------------------------------------------------------------------------
# fill_completion_entry_residues -- hollow-record refusal (2026-09-28)
# ---------------------------------------------------------------------------


def test_chain_terminal_fill_refuses_on_placeholder_prose_alone(repo: Path) -> None:
    """2026-09-28 EM correction: ANY of the checks failing at finalize is
    refused, not only the all-three shape. Plan landed, prose left as a
    placeholder alone is enough."""
    slug = "2026-09-27-single-axis-refusal-test"
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "plans" / f"{slug}.md").write_text(
        "---\nstatus: implemented\n---\nbody\n", encoding="utf-8"
    )
    abs_path, rel_path = _scaffold(repo, title="Single axis refusal test")
    text = abs_path.read_text()
    text = text.replace("---\n", f'---\nchain: "{slug}"\n', 1)
    abs_path.write_text(text, encoding="utf-8")

    with pytest.raises(HollowCompletionRecordError):
        completion_entry.fill_completion_entry_residues(
            repo, rel_path, "", chain_terminal=True, sid="sess-hollow"
        )


def test_chain_terminal_fill_never_gates_on_empty_commits(repo: Path) -> None:
    """This module has NO mechanism to ever populate `commits:` itself
    (a separate op's job) -- gating chain_terminal finalize on commits would
    make it permanently unsatisfiable through this module. Prose authored
    and plan landed, commits still `[]` -- must NOT refuse."""
    slug = "2026-09-27-commits-excluded-test"
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "plans" / f"{slug}.md").write_text(
        "---\nstatus: landed\n---\nbody\n", encoding="utf-8"
    )
    abs_path, rel_path = _scaffold(repo, title="Commits excluded test")
    text = abs_path.read_text()
    text = text.replace("---\n", f'---\nchain: "{slug}"\n', 1)
    abs_path.write_text(text, encoding="utf-8")
    assert 'commits: []' in abs_path.read_text()

    filled = completion_entry.fill_completion_entry_residues(
        repo, rel_path, "<!-- ONE paragraph -->\nShipped it.", chain_terminal=True, sid="sess-1"
    )
    assert filled["chain_terminal"] is True
    assert 'commits: []' in abs_path.read_text()


def test_chain_terminal_fill_refuses_a_fully_hollow_record(repo: Path) -> None:
    """The chain_terminal flip IS this ceremony's own finalize signal.
    Refusing it when the record would still fail every integrity check --
    unfilled prose placeholder, `commits: []`, and a governing plan that
    never landed -- pins the exact defect shape (2026-09-20-doe-holds-no-
    scripts-188007.md: exactly this combination, and nothing refused it)."""
    slug = "2026-09-27-hollow-fill-refusal-test"
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "plans" / f"{slug}.md").write_text(
        "---\nstatus: executing\n---\nbody\n", encoding="utf-8"
    )
    abs_path, rel_path = _scaffold(repo, title="Hollow fill refusal test")
    # Splice a chain: field in directly -- scaffold_completion_entry's own
    # `chain=` kwarg accepts any string verbatim; use the governing plan slug
    # so `hollow_reasons`' chain->plan lookup resolves the file above.
    text = abs_path.read_text()
    text = text.replace("---\n", f'---\nchain: "{slug}"\n', 1)
    abs_path.write_text(text, encoding="utf-8")

    with pytest.raises(HollowCompletionRecordError):
        completion_entry.fill_completion_entry_residues(
            repo, rel_path, "", chain_terminal=True, sid="sess-hollow"
        )

    # Refused BEFORE the write landed -- chain_terminal is still false on disk.
    split = split_frontmatter(abs_path.read_text())
    assert split is not None
    raw = read_fm_field(split.fm_text, "chain_terminal")
    assert raw is not None
    assert raw.split()[0] == "false"


def test_chain_terminal_fill_passes_when_not_fully_hollow(repo: Path) -> None:
    """Same finalize signal, but the record carries real commits and a
    landed governing plan -- the refusal must not fire on a genuinely
    ready record."""
    slug = "2026-09-27-hollow-fill-pass-test"
    (repo / "docs" / "plans").mkdir(parents=True, exist_ok=True)
    (repo / "docs" / "plans" / f"{slug}.md").write_text(
        "---\nstatus: implemented\n---\nbody\n", encoding="utf-8"
    )
    abs_path, rel_path = _scaffold(repo, title="Hollow fill pass test")
    text = abs_path.read_text()
    text = text.replace("---\n", f'---\nchain: "{slug}"\n', 1)
    text = text.replace("commits: []", 'commits:\n  - "deadbeef"')
    abs_path.write_text(text, encoding="utf-8")

    filled = completion_entry.fill_completion_entry_residues(
        repo, rel_path, "shipped the thing.", chain_terminal=True, sid="sess-ready"
    )

    assert filled["chain_terminal"] is True
    split = split_frontmatter(abs_path.read_text())
    assert split is not None
    raw = read_fm_field(split.fm_text, "chain_terminal")
    assert raw is not None
    assert raw.split()[0] == "true"
