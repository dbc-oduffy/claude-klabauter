"""
Tests for coordinator_core.ops.queue_family — the queue-family read seam.

Spec backlink: pln-queue-triage-terminus-ops-clus-043c40 § C1

Covers: family normalization (all three families + unknown-family error),
delegated loading via query_records() for each family, the per-family field
table, and the AC2 mechanical gate asserting the not-yet-authored
queue_cluster.py / queue_age_ping.py / queue_scaffold_baton.py modules (when
they land) do not reimplement directory-walking/YAML-parsing and DO import
their resolver from this module.
"""

from __future__ import annotations

import ast
import subprocess
import textwrap
from pathlib import Path

import pytest

# `_init_repo`/`_seed` spawn real git because `load_family_records` resolves
# real worktree/common-dir layout (see `test_load_family_records_with_git_
# common_dir_finds_records`) — no mock stands in for git's own common-dir
# discovery. Each test seeds and commits its own repo, so the fixture is not
# hoisted to module scope.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]

from coordinator_core.ops.queue_family import (
    FAMILY_FIELDS,
    FAMILY_TO_RECORD_TYPE,
    UnknownQueueFamilyError,
    fields_for_family,
    load_family_records,
    normalize_family,
)


def _init_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=root, check=True, capture_output=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True, capture_output=True)


def _seed(root: Path, rel_dir: str, name: str, body: str) -> Path:
    d = root / rel_dir
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", f"seed {name}"], cwd=root, check=True, capture_output=True
    )
    return p


@pytest.mark.parametrize(
    "family, record_type",
    [
        ("improvement-queue", "improvement"),
        ("debt-backlog", "debt"),
        ("bug-backlog", "bug"),
    ],
)
def test_normalize_family(family: str, record_type: str) -> None:
    assert normalize_family(family) == record_type


def test_normalize_family_unknown_raises_clear_error() -> None:
    with pytest.raises(UnknownQueueFamilyError, match="unknown queue family 'not-a-family'"):
        normalize_family("not-a-family")


def test_family_to_record_type_is_closed_three_entry_set() -> None:
    assert FAMILY_TO_RECORD_TYPE == {
        "improvement-queue": "improvement",
        "debt-backlog": "debt",
        "bug-backlog": "bug",
    }


def test_fields_for_family_returns_required_and_optional_tuples() -> None:
    for family in FAMILY_TO_RECORD_TYPE:
        fields = fields_for_family(family)
        assert set(fields) == {"required", "optional"}
        assert isinstance(fields["required"], tuple)
        assert isinstance(fields["optional"], tuple)
        assert fields["required"] == FAMILY_FIELDS[family]["required"]


def test_fields_for_family_unknown_raises() -> None:
    with pytest.raises(UnknownQueueFamilyError):
        fields_for_family("nope")


def test_load_family_records_improvement_queue(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed(
        tmp_path,
        "state/improvement-queue",
        "2026-07-01-example.yaml",
        """
        created: 2026-07-01
        title: Example improvement
        body: Something worth doing.
        status: open
        surface: coordinator_core/ops/example.py
        proposed_action: do the thing
        from_repo: claude-klabauter
        change_kind: code-edit
        """,
    )
    records = load_family_records("improvement-queue", tmp_path)
    assert len(records) == 1
    assert records[0]["frontmatter"]["title"] == "Example improvement"


def test_load_family_records_debt_backlog(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed(
        tmp_path,
        "state/debt-backlog",
        "2026-07-01-example.yaml",
        """
        created: 2026-07-01
        title: Example debt
        body: Some debt.
        status: open
        source: review
        risk: it might break
        proposed_action: pay it down
        """,
    )
    records = load_family_records("debt-backlog", tmp_path)
    assert len(records) == 1
    assert records[0]["frontmatter"]["title"] == "Example debt"


def test_load_family_records_bug_backlog(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed(
        tmp_path,
        "state/bug-backlog",
        "2026-07-01-example.yaml",
        """
        created: 2026-07-01
        title: Example bug
        body: Something is broken.
        status: open
        surface: coordinator_core/ops/example.py
        severity: P2
        """,
    )
    records = load_family_records("bug-backlog", tmp_path)
    assert len(records) == 1
    assert records[0]["frontmatter"]["title"] == "Example bug"


def test_load_family_records_with_git_common_dir_finds_records(tmp_path: Path) -> None:
    """Regression for the 2026-07-23 silent-empty-result bug.

    A ``common_dir``-scoped op handler (``queue.cluster``, ``queue.age_ping``)
    receives ``git_common_dir(caller_worktree)`` — i.e. ``<worktree>/.git`` —
    from the IPC engine's ``resolve_op_repo_key``, never the worktree root
    itself. This test exercises exactly that shape: it hands
    ``load_family_records`` the fixture's actual ``.git`` directory (not
    ``tmp_path``) and asserts the records are still found. Before the fix,
    this silently returned ``[]`` because the un-derived common dir has no
    ``state/`` subdirectory of its own.
    """
    _init_repo(tmp_path)
    _seed(
        tmp_path,
        "state/improvement-queue",
        "2026-07-01-example.yaml",
        """
        created: 2026-07-01
        title: Example improvement
        body: Something worth doing.
        status: open
        surface: coordinator_core/ops/example.py
        proposed_action: do the thing
        from_repo: claude-klabauter
        change_kind: code-edit
        """,
    )
    common_dir = tmp_path / ".git"
    assert common_dir.is_dir()  # sanity: standard (non-worktree) layout

    records = load_family_records("improvement-queue", common_dir)
    assert len(records) == 1
    assert records[0]["frontmatter"]["title"] == "Example improvement"


def test_load_family_records_plain_dir_is_a_worktree_root_not_an_error(tmp_path: Path) -> None:
    """A plain directory with no git structure is trusted as an already-worktree
    root and yields ``[]`` — NOT an error.

    A plain path is indistinguishable from a fresh worktree root that has no
    ``state/<family>/`` yet (a fresh consumer repo), so it must resolve to an
    empty result, never a raise. The loud path is reserved for a *common-dir-
    shaped* root whose derivation fails verification — covered by
    ``test_load_family_records_bare_repo_shaped_common_dir_raises`` below. This
    is the ratified "unresolvable root (loud) vs. no matching records (empty)"
    split: only the former raises.
    """
    plain_root = tmp_path / "not-a-git-anything"
    plain_root.mkdir()
    assert load_family_records("improvement-queue", plain_root) == []


def test_load_family_records_bare_repo_shaped_common_dir_raises(tmp_path: Path) -> None:
    """A bare-repo-shaped common dir (HEAD/objects/refs present, NOT named ``.git``,
    and whose parent is not itself a worktree root) must raise loud rather than
    silently falling through to "treat as already-worktree-root" — the exact
    non-standard-name shape the rejected ``.name == ".git"`` heuristic missed.
    """
    bare_dir = tmp_path / "somewhere" / "project.git"
    bare_dir.mkdir(parents=True)
    (bare_dir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (bare_dir / "objects").mkdir()
    (bare_dir / "refs").mkdir()
    # bare_dir.parent ("somewhere") has no .git entry -> derived root fails verification.
    with pytest.raises(ValueError, match="refusing to guess"):
        load_family_records("improvement-queue", bare_dir)


def test_load_family_records_unknown_family_raises(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    with pytest.raises(UnknownQueueFamilyError):
        load_family_records("not-a-family", tmp_path)


def test_load_family_records_missing_optional_field_is_not_an_error(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _seed(
        tmp_path,
        "state/bug-backlog",
        "2026-07-01-no-tags.yaml",
        """
        created: 2026-07-01
        title: No tags or initiative on this one
        body: Real entries frequently omit optional fields.
        status: open
        surface: coordinator_core/ops/example.py
        severity: P3
        """,
    )
    records = load_family_records("bug-backlog", tmp_path)
    assert len(records) == 1
    fm = records[0]["frontmatter"]
    assert "tags" not in fm
    assert "initiative" not in fm


# --- AC2 mechanical gate -----------------------------------------------------
#
# queue_cluster.py / queue_age_ping.py / queue_scaffold_baton.py do not exist
# yet (authored in a later wave). This gate is written to SKIP (never fail)
# when a target module is absent, so it goes green now and converts into a
# real enforcement gate the moment each module lands — per the C1 brief,
# this is what turns AC2 from convention into a mechanical check.
#
# The gate parses each target module's CODE with ``ast`` rather than
# substring-scanning its raw text. A raw-text scan for tokens like "glob" also
# trips on a docstring/comment that correctly documents the ABSENCE of a glob
# call (e.g. "Does NOT glob a directory") — punishing precise negative-spec
# prose for using the exact word the gate is named after. AST inspection sees
# only real attribute accesses and call expressions, so docstrings, comments,
# and string literals can never trip it.

_OPS_DIR = Path(__file__).resolve().parents[1]
_FORBIDDEN_ATTRS = ("glob", "rglob", "iterdir")
_FORBIDDEN_CALL_NAMES = ("safe_load",)
_AC2_TARGET_MODULES = (
    "queue_cluster.py",
    "queue_age_ping.py",
    "queue_scaffold_baton.py",
)


def _find_forbidden_read_calls(source: str, module_name: str) -> list[str]:
    """Return one description per real glob/rglob/iterdir/yaml.safe_load use in ``source``.

    Only actual ``ast.Attribute`` accesses (``p.glob``, ``Path.iterdir``, etc.) and
    ``ast.Call`` nodes whose callee resolves to ``safe_load`` (bare or attribute-qualified,
    e.g. ``yaml.safe_load(...)``) count — a docstring, comment, or string literal
    containing the same word is not an AST node of either shape and can never match.
    Each description names the offending line so a future author sees the actual call
    site, not a whole-file verdict.
    """
    tree = ast.parse(source, filename=module_name)
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in _FORBIDDEN_ATTRS:
            violations.append(
                f"{module_name}:{node.lineno}: attribute access '.{node.attr}' — "
                "delegate all record loading to query_records() via "
                "coordinator_core.ops.queue_family instead of calling "
                f"'{node.attr}' directly"
            )
        elif isinstance(node, ast.Call):
            func = node.func
            callee_name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name)
                else None
            )
            if callee_name in _FORBIDDEN_CALL_NAMES:
                violations.append(
                    f"{module_name}:{node.lineno}: call to '{callee_name}(...)' — "
                    "delegate all record loading to query_records() via "
                    "coordinator_core.ops.queue_family instead of parsing YAML directly"
                )
    return violations


@pytest.mark.parametrize("module_name", _AC2_TARGET_MODULES)
def test_ac2_queue_family_consumers_delegate_to_read_seam(module_name: str) -> None:
    module_path = _OPS_DIR / module_name
    if not module_path.exists():
        pytest.skip(
            f"{module_name} does not exist yet (authored in a later wave) — "
            "this gate activates once it lands"
        )

    source = module_path.read_text(encoding="utf-8")

    violations = _find_forbidden_read_calls(source, module_name)
    assert not violations, (
        f"{module_name} reimplements record loading instead of delegating to "
        "query_records() via coordinator_core.ops.queue_family:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )

    assert "from coordinator_core.ops.queue_family import" in source or (
        "coordinator_core.ops.queue_family" in source
    ), (
        f"{module_name} must import its family resolver from "
        "coordinator_core.ops.queue_family"
    )


# --- test-of-the-test: the AC2 gate must scan CODE, never prose -------------


def test_ac2_gate_predicate_ignores_docstring_mentions_of_forbidden_words(tmp_path: Path) -> None:
    """A module whose DOCSTRING mentions glob/iterdir/safe_load but calls none of them
    must produce zero violations — proving the gate scans AST, not raw text."""
    fixture = tmp_path / "fixture_prose_only.py"
    fixture.write_text(
        textwrap.dedent(
            '''
            """
            Negative-spec:
              - Does NOT glob a directory, iterdir a directory, or yaml.safe_load a
                file — all record loading routes through query_records().
            """
            from coordinator_core.ops.queue_family import load_family_records


            def read_records(repo_root):
                return load_family_records("bug-backlog", repo_root)
            '''
        ).lstrip(),
        encoding="utf-8",
    )
    violations = _find_forbidden_read_calls(
        fixture.read_text(encoding="utf-8"), fixture.name
    )
    assert violations == []


@pytest.mark.parametrize(
    "body",
    [
        'Path(".").glob("*.yaml")',
        'Path(".").iterdir()',
        "yaml.safe_load(handle)",
        "safe_load(handle)",
    ],
)
def test_ac2_gate_predicate_catches_real_forbidden_calls(tmp_path: Path, body: str) -> None:
    """A module that actually CALLS a forbidden read primitive must trip the gate."""
    fixture = tmp_path / "fixture_real_call.py"
    fixture.write_text(f"import yaml\nfrom pathlib import Path\n\n\ndef f():\n    return {body}\n")
    violations = _find_forbidden_read_calls(
        fixture.read_text(encoding="utf-8"), fixture.name
    )
    assert violations, f"expected a violation for body={body!r}"
