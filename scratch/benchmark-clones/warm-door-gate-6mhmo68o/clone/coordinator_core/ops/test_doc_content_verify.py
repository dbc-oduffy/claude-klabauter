"""
Tests for coordinator_core.ops.doc_content_verify.

Three required groups (docs/plans/2026-07-28-human-facing-doc-staleness-detector.md
§ C6b/AC12/AC13, DoE-claude):
    1. Positive — each v1 citation class is caught when genuinely broken.
    2. Negative — a known-good cross-repo citation produces no finding, plus
       one test per exclusion-set category. This group decides whether the
       gate is ever trusted: measured naively (no negative surface) against
       this repo's own docs, an absence-only predicate fires on the large
       majority of citations, most of them correct cross-repo references.
    3. AC13, both halves: (a) a synthetic fixture reproducing the b644d5a9
       shape is flagged; (b) a historical replay against DoE-claude's real
       history at commit b644d5a9 flags the true positive
       (coordinator/scripts/install-maximalist.py) and does NOT flag the
       correct-but-cross-repo citation present in the same tree
       (coordinator/lib/install-substrate.py, verified absent from DoE-claude
       at that commit and present under the current engine root).
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

import pytest

from coordinator_core.ops.doc_content_verify import (
    Finding,
    extract_citations,
    is_excluded,
    verify_doc,
    verify_doc_on_disk,
)

# Declared, not excused: the AC13 historical-replay group (TestAC13HistoricalReplay)
# spawns real `git show`/`git ls-tree`/`git cat-file` against the actual DoE-claude
# checkout to prove the citation-verifier against real repo history, not a fixture --
# no mock stands in for "did this path genuinely exist at commit b644d5a9". The
# `_doe_repo_available` probe is cached (lru_cache), not per-test, since it fires at
# collection-adjacent time; the replay itself is read-only plumbing, never checkout/
# reset, so no per-test isolation is needed beyond that cache. The spawn ratchet's
# `_BASELINE` is shrink-only pre-existing residue and is explicitly not the route for
# this file -- coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]
from coordinator_core.doe_root_pointer import read_doe_root_pointer
from coordinator_core.engine_root import coordinator_engine_root

_B644D5A9_SHA = "b644d5a9"


def _findings_reasons(findings):
    return {(f.doc, f.token, f.reason) for f in findings}


# ---------------------------------------------------------------------------
# Group 1 — positive surface
# ---------------------------------------------------------------------------


class TestPositiveSurface:
    def test_fenced_command_path_absent_is_flagged(self, tmp_path):
        text = "```bash\npython3 coordinator/scripts/gone.py\n```\n"
        findings = verify_doc(
            "README.md",
            text,
            repo_exists=lambda token: False,
        )
        assert Finding("README.md", 2, "coordinator/scripts/gone.py", "absent") in findings

    def test_fenced_command_path_present_is_not_flagged(self, tmp_path):
        text = "```bash\npython3 coordinator/scripts/live.py\n```\n"
        findings = verify_doc(
            "README.md",
            text,
            repo_exists=lambda token: token == "coordinator/scripts/live.py",
        )
        assert findings == []

    def test_code_span_path_absent_is_flagged(self):
        text = "| `coordinator/lib/gone.py` | does a thing |\n"
        findings = verify_doc("INSTALL.md", text, repo_exists=lambda token: False)
        assert Finding("INSTALL.md", 1, "coordinator/lib/gone.py", "absent") in findings

    def test_code_span_without_separator_is_not_a_citation(self):
        text = "See `install-maximalist` for details.\n"
        citations = extract_citations("README.md", text)
        assert citations == []

    def test_md_link_target_absent_is_flagged(self):
        text = "See [the install doc](docs/GONE.md) for details.\n"
        findings = verify_doc("README.md", text, repo_exists=lambda token: False)
        assert Finding("README.md", 1, "docs/GONE.md", "absent") in findings

    def test_md_link_target_present_is_not_flagged(self):
        text = "See [the install doc](INSTALL.md) for details.\n"
        findings = verify_doc("README.md", text, repo_exists=lambda token: token == "INSTALL.md")
        assert findings == []

    def test_md_link_anchor_missing_is_flagged_as_moved(self):
        text = "See [details](INSTALL.md#section-that-moved) for details.\n"
        target_text = "# Install\n\n## A Different Section\n"
        findings = verify_doc(
            "README.md",
            text,
            repo_exists=lambda token: token == "INSTALL.md",
            read_target_text=lambda token: target_text,
        )
        assert Finding("README.md", 1, "INSTALL.md", "moved") in findings

    def test_md_link_anchor_present_is_not_flagged(self):
        text = "See [details](INSTALL.md#a-real-section) for details.\n"
        target_text = "# Install\n\n## A Real Section\n"
        findings = verify_doc(
            "README.md",
            text,
            repo_exists=lambda token: token == "INSTALL.md",
            read_target_text=lambda token: target_text,
        )
        assert findings == []


# ---------------------------------------------------------------------------
# Group 2 — negative surface
# ---------------------------------------------------------------------------


class TestNegativeSurface:
    @pytest.mark.real_home
    def test_known_good_cross_repo_citation_does_not_produce_finding(self):
        """coordinator/lib/install-substrate.py is absent from a sibling repo's
        tree but present under the engine root — the canonical case AC12 requires
        to resolve as `resolves-cross-repo`, never `absent`."""
        claude_klabauter_root = Path(coordinator_engine_root())
        assert (claude_klabauter_root / "coordinator" / "lib" / "install-substrate.py").exists(), (
            "test fixture assumption: install-substrate.py must exist under the engine root "
            "for this to be a meaningful negative-surface check"
        )

        text = "| `coordinator/lib/install-substrate.py` | Writes the machine-local registry substrate |\n"
        findings = verify_doc(
            "INSTALL.md",
            text,
            repo_exists=lambda token: False,  # absent from the local (sibling) tree
            sibling_checkers=[lambda token: (claude_klabauter_root / token).exists()],
        )
        assert findings == []

    def test_exclusion_slash_command(self):
        assert is_excluded("/coordinator:install") is True

    def test_exclusion_url_with_scheme(self):
        assert is_excluded("https://github.com/dbc-oduffy/DoE-claude") is True

    def test_exclusion_bare_domain(self):
        assert is_excluded("github.com/dbc-oduffy/DoE-claude") is True

    def test_exclusion_glob_metacharacter(self):
        assert is_excluded("coordinator/skills/*/SKILL.md") is True
        assert is_excluded("coordinator/lib/foo?.py") is True
        assert is_excluded("coordinator/lib/[abc].py") is True

    def test_exclusion_placeholder_shape(self):
        assert is_excluded("archive/completed/<YYYY-MM>/report.md") is True
        assert is_excluded("archive/completed/YYYY-MM/report.md") is True
        assert is_excluded("coordinator/.../deep/path.py") is True

    def test_exclusion_dollar_prefixed(self):
        assert is_excluded("$CLAUDE_KLABAUTER_ROOT/coordinator/bin/foo.py") is True

    def test_exclusion_dollar_brace_prefixed(self):
        assert is_excluded("${COORDINATOR_SETTINGS_HOME}/bin/cross-repo-memo") is True

    def test_exclusion_tilde_prefixed(self):
        assert is_excluded("~/.coordinator-claude-settings/bin/cross-repo-memo") is True

    def test_ordinary_repo_relative_path_is_not_excluded(self):
        assert is_excluded("coordinator/scripts/install-maximalist.py") is False

    def test_per_doc_ignore_list_suppresses_finding(self):
        text = "```bash\npython3 coordinator/scripts/genuinely-unresolvable.py\n```\n"
        findings = verify_doc(
            "README.md",
            text,
            repo_exists=lambda token: False,
            ignore=["coordinator/scripts/genuinely-unresolvable.py"],
        )
        assert findings == []

    # Review: code-reviewer — Finding 2 (P2). The four tests above call
    # is_excluded() directly on an un-truncated literal, which passes
    # against a broken end-to-end implementation (Finding 1) — the fenced-
    # code-path extractor could truncate the very prefix these tests
    # exercise before is_excluded() ever sees it. These four route the same
    # text through verify_doc() inside a real fenced bash block, the one
    # path where truncation happens, so a regression on Finding 1's fix
    # fails here even if the unit-level is_excluded() tests still pass.
    def test_exclusion_dollar_prefixed_end_to_end(self):
        text = "```bash\npython3 $CLAUDE_KLABAUTER_ROOT/coordinator/bin/foo.py\n```\n"
        findings = verify_doc("README.md", text, repo_exists=lambda token: False)
        assert findings == []

    def test_exclusion_dollar_brace_prefixed_end_to_end(self):
        text = "```bash\n${COORDINATOR_SETTINGS_HOME}/bin/cross-repo-memo\n```\n"
        findings = verify_doc("README.md", text, repo_exists=lambda token: False)
        assert findings == []

    def test_exclusion_tilde_prefixed_end_to_end(self):
        text = "```bash\n~/.coordinator-claude-settings/bin/cross-repo-memo\n```\n"
        findings = verify_doc("README.md", text, repo_exists=lambda token: False)
        assert findings == []

    def test_exclusion_glob_metacharacter_end_to_end(self):
        text = "```bash\nrm -rf tasks/scratch-*/notes.md\n```\n"
        findings = verify_doc("README.md", text, repo_exists=lambda token: False)
        assert findings == []

    def test_excluded_token_produces_no_finding_even_when_absent_everywhere(self):
        text = "```bash\n/coordinator:install\n```\n"
        # A bare slash-command has no internal path separator so it is not
        # even extracted as a fenced-code-path citation; assert directly on
        # is_excluded to pin the mechanism regardless of extraction shape.
        assert is_excluded("/coordinator:install") is True
        findings = verify_doc("README.md", text, repo_exists=lambda token: False)
        assert findings == []


# ---------------------------------------------------------------------------
# Group 3 — AC13: the motivating incident, synthetic + historical
# ---------------------------------------------------------------------------


class TestDocRelativeResolution:
    """Plugin-root false-positive class: a doc in a subdirectory (e.g.
    `coordinator/README.md`) cites paths relative to ITS OWN directory, not
    the repo root. Measured against DoE-claude's real `coordinator/README.md`
    this produced 24 of 26 spurious `absent` findings before this piece of
    the resolution ladder existed."""

    def test_plugin_root_doc_relative_citation_present_is_not_flagged(self, tmp_path):
        repo_root = tmp_path / "repo"
        (repo_root / "coordinator" / "docs" / "wiki").mkdir(parents=True)
        (repo_root / "coordinator" / "docs" / "wiki" / "delegate-execution.md").write_text(
            "# Delegate Execution\n"
        )
        (repo_root / "coordinator" / "README.md").write_text(
            "See [delegate execution](docs/wiki/delegate-execution.md) for details.\n"
        )

        findings = verify_doc_on_disk(repo_root, "coordinator/README.md")

        assert findings == []

    def test_plugin_root_doc_relative_citation_absent_is_still_flagged(self, tmp_path):
        repo_root = tmp_path / "repo"
        (repo_root / "coordinator").mkdir(parents=True)
        (repo_root / "coordinator" / "README.md").write_text(
            "See [lessons](state/lessons.md) for details.\n"
        )
        # coordinator/state/lessons.md deliberately not created, and no
        # repo-root state/lessons.md either — genuinely absent both ways.

        findings = verify_doc_on_disk(repo_root, "coordinator/README.md")

        assert Finding("coordinator/README.md", 1, "state/lessons.md", "absent") in findings

    def test_doc_relative_traversal_cannot_escape_repo_root(self, tmp_path):
        repo_root = tmp_path / "repo"
        (repo_root / "coordinator").mkdir(parents=True)
        outside = tmp_path / "outside-secret.txt"
        outside.write_text("secret\n")
        # A `../`-escaping token that WOULD resolve if containment were not
        # enforced (repo_root/coordinator/../../outside-secret.txt).
        (repo_root / "coordinator" / "README.md").write_text(
            "See `../../outside-secret.txt` for details.\n"
        )

        findings = verify_doc_on_disk(repo_root, "coordinator/README.md")

        assert Finding(
            "coordinator/README.md", 1, "../../outside-secret.txt", "absent"
        ) in findings


class TestAC13MotivatingIncident:
    def test_synthetic_b644d5a9_shape_is_flagged(self, tmp_path):
        """A doc cites a path that existed when authored and a later commit
        emptied — the exact shape of the incident that prompted this plan."""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "README.md").write_text(
            "```bash\npython3 coordinator/scripts/install-maximalist.py\n```\n"
        )
        # coordinator/scripts/install-maximalist.py deliberately not created —
        # simulates the post-b644d5a9 state where the path was emptied.

        findings = verify_doc_on_disk(repo_root, "README.md")

        assert (
            Finding(
                "README.md", 2, "coordinator/scripts/install-maximalist.py", "absent"
            )
            in findings
        )


def _doe_root() -> str:
    return read_doe_root_pointer()


def _git_show(repo_root: str, sha: str, path: str) -> str:
    result = subprocess.run(
        ["git", "show", f"{sha}:{path}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout


def _git_ls_tree_exists(repo_root: str, sha: str, path: str) -> bool:
    result = subprocess.run(
        ["git", "ls-tree", sha, "--", path],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    return result.returncode == 0 and bool(result.stdout.strip())


@functools.lru_cache()
def _doe_repo_available() -> bool:
    """Cached lazily (not evaluated at class-decoration time) — the `git
    cat-file` subprocess this performs would otherwise fire on every pytest
    collection, including --collect-only and -k-filtered runs that never
    execute a test in this class."""
    root = _doe_root()
    if not root or not Path(root).is_dir():
        return False
    result = subprocess.run(
        ["git", "cat-file", "-e", f"{_B644D5A9_SHA}^{{commit}}"],
        cwd=root,
        capture_output=True,
    )
    return result.returncode == 0


class TestAC13HistoricalReplay:
    """Read-only replay via git plumbing (git show / git ls-tree) — never
    checks out, resets, or otherwise mutates the live DoE-claude working
    tree, which other sessions may be using concurrently."""

    pytestmark = pytest.mark.real_home

    @pytest.fixture(autouse=True)
    def _require_doe_repo(self):
        """Runtime (not collection-time) gate — mirrors the former
        class-level skipif but defers the git-plumbing probe to first test
        setup so collection never spawns a process (see
        _doe_repo_available's docstring)."""
        if not _doe_repo_available():
            pytest.skip(
                "DoE-claude repo not resolvable/cloned on this machine, or commit b644d5a9 missing"
            )

    @staticmethod
    def _repo_exists_at_commit(doe_root: str, sha: str) -> "callable":
        def _check(token: str) -> bool:
            return _git_ls_tree_exists(doe_root, sha, token)

        return _check

    def test_true_positive_flagged_install_maximalist_when_no_sibling_root_resolvable(self):
        """DISK-TRUTH ADAPTATION from the plan's stated feasibility note.

        The plan's C6b/AC13 text asserts `coordinator/scripts/install-maximalist.py`
        is "absent everywhere" at b644d5a9 — absent from DoE-claude AND absent
        under the engine root. The first half holds (verified below via
        `git ls-tree`); the second half does not: `git log --follow --diff-filter=A`
        against claude-klabauter shows the file landed there at commit `8a28a6ca`,
        2026-07-22T16:28:43+01:00 — five minutes BEFORE b644d5a9 itself
        (2026-07-22T16:33:51+01:00) — and has existed there continuously since,
        including at the current engine root checked by this test suite. So with
        a resolvable sibling root, this citation legitimately resolves
        `resolves-cross-repo`, exactly like `install-substrate.py` below — the
        mechanism cannot tell the two apart, because at the moment b644d5a9
        landed there was no gap: the executable surface migration added the
        claude-klabauter-side file before removing the DoE-side one.

        What actually made this a live defect on 2026-07-28 was that the
        README's cold-bootstrap section is explicitly for a machine with NO
        engine root configured yet ("a genuinely fresh machine... `/coordinator:
        install` doesn't exist yet") — i.e. exactly the case where NO sibling
        root is resolvable at all, so cross-repo re-resolution has nothing to
        check against. That is the faithful replay: no sibling_checkers, matching
        a bare clone with no machine-local registry entry for claude-klabauter.
        Recorded in this plan's C8 wiki chunk as a v1 limitation, not silently
        dropped: on a machine where the engine root IS already configured, this
        specific citation resolves cross-repo and is correctly NOT flagged —
        the doc-level distinction between "cold-bootstrap, must be standalone"
        and "post-install, engine root expected" is a prose-context judgment,
        explicitly out of v1's mechanical scope (see module docstring's
        anti-scope)."""
        doe_root = _doe_root()
        text = _git_show(doe_root, _B644D5A9_SHA, "README.md")

        # Feasibility pinned in the plan: git ls-tree at this commit for
        # coordinator/scripts/ returns empty — the path was genuinely absent
        # from the DoE-claude tree.
        assert not _git_ls_tree_exists(
            doe_root, _B644D5A9_SHA, "coordinator/scripts/install-maximalist.py"
        )

        findings = verify_doc(
            "README.md",
            text,
            repo_exists=self._repo_exists_at_commit(doe_root, _B644D5A9_SHA),
            sibling_checkers=[],  # no sibling root resolvable — the fresh-clone case
        )

        assert (
            "coordinator/scripts/install-maximalist.py",
            "absent",
        ) in {(f.token, f.reason) for f in findings}

    def test_install_maximalist_resolves_cross_repo_when_sibling_root_available(self):
        """Companion to the test above: on a machine WITH the engine root resolvable
        (this test's own machine), the same citation legitimately resolves
        cross-repo and must NOT be reported as a finding — this is the disk
        fact that makes install-maximalist.py an imperfect AC13 exemplar (see
        the sibling test's docstring) rather than an "absent everywhere" one."""
        doe_root = _doe_root()
        text = _git_show(doe_root, _B644D5A9_SHA, "README.md")
        claude_klabauter_root = Path(coordinator_engine_root())
        assert (claude_klabauter_root / "coordinator" / "scripts" / "install-maximalist.py").exists()

        findings = verify_doc(
            "README.md",
            text,
            repo_exists=self._repo_exists_at_commit(doe_root, _B644D5A9_SHA),
            sibling_checkers=[lambda token: (claude_klabauter_root / token).exists()],
        )

        flagged_tokens = {f.token for f in findings}
        assert "coordinator/scripts/install-maximalist.py" not in flagged_tokens

    def test_negative_cross_repo_citation_not_flagged_install_substrate(self):
        doe_root = _doe_root()
        text = _git_show(doe_root, _B644D5A9_SHA, "INSTALL.md")

        # Confirmed absent from DoE-claude's tree at this commit...
        assert not _git_ls_tree_exists(
            doe_root, _B644D5A9_SHA, "coordinator/lib/install-substrate.py"
        )
        # ...but present under the current engine root — the correct-as-written
        # cross-repo citation this replay must not flag.
        claude_klabauter_root = Path(coordinator_engine_root())
        assert (claude_klabauter_root / "coordinator" / "lib" / "install-substrate.py").exists()

        findings = verify_doc(
            "INSTALL.md",
            text,
            repo_exists=self._repo_exists_at_commit(doe_root, _B644D5A9_SHA),
            sibling_checkers=[lambda token: (claude_klabauter_root / token).exists()],
        )

        flagged_tokens = {f.token for f in findings}
        assert "coordinator/lib/install-substrate.py" not in flagged_tokens
