"""
coordinator_core.tests.test_chain_ancestry_waivers

Coverage for `chain_ancestry_waivers.chain_reached_terminal_close` — the
retention predicate W2's (next wave) reaper is built on: a chain's minted
waivers are reapable only once THAT chain_id has itself reached a terminal
`closed` disposition, per the ratified DR-084 vocabulary
(`open`/`claimed`/`continued`/`closed`).

This predicate is a thin, deliberate reuse of
`coordinator_core.ops.session.resolve_chain_terminal_disposition`'s
classification core (`classify_chain_terminal_disposition`, the public
wrapper this chunk added) via its `param_sid` tier — NOT a re-derivation of
the archived-handoff `deployment_state` read. See that module's own
docstring for the full dual-detector contract this predicate rides on top
of.

Spec backlink: pln-kill-the-n-1-git-spawn-class-a-88897a § W1

Negative-spec:
  - Does NOT exercise the reaper (W2) — that op does not exist yet and this
    chunk does not write it.
  - Does NOT duplicate the archive-frontmatter read logic — every fixture
    here exists solely to drive `classify_chain_terminal_disposition`'s
    existing dual-detector classification, never a hand-rolled parse.
  - Does NOT delete any waiver file under state/review-trail/ — this module
    reads a predicate only.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from coordinator_core.chain_ancestry_waivers import (
    chain_ancestry_waived_shas,
    chain_ancestry_waiver_records,
    chain_reached_terminal_close,
)

# _make_repo spawns real git per test (init/config/add/commit) — declared to
# the spawn ratchet rather than grandfathered in its frozen baseline. See
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [
    pytest.mark.cadence,
    pytest.mark.spawns_process,
]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    (tmp_path / "README.md").write_text("x")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)
    return tmp_path


def _archive_handoff(repo, chain_id, deployment_state):
    archive_dir = repo / "archive" / "handoffs"
    archive_dir.mkdir(parents=True, exist_ok=True)
    (archive_dir / f"{chain_id}.md").write_text(
        "---\n"
        "predecessor: none\n"
        f"claimed_by: {chain_id}\n"
        f"deployment_state: {deployment_state}\n"
        "---\n"
        "body\n",
        encoding="utf-8",
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", f"archive handoff for {chain_id}"],
        cwd=repo,
        check=True,
    )


class TestChainReachedTerminalClose:
    def test_closed_disposition_is_terminal(self, tmp_path):
        repo = _make_repo(tmp_path)
        chain_id = "closed-chain-id"
        _archive_handoff(repo, chain_id, "closed")

        assert chain_reached_terminal_close(str(repo), chain_id) is True

    def test_continued_disposition_is_not_terminal(self, tmp_path):
        """The chain handed off to a successor under a DIFFERENT chain_id —
        a later close under the successor's own id does not license reaping
        THIS chain_id's waivers."""
        repo = _make_repo(tmp_path)
        chain_id = "continued-chain-id"
        _archive_handoff(repo, chain_id, "continued")

        assert chain_reached_terminal_close(str(repo), chain_id) is False

    def test_no_archived_record_is_not_terminal(self, tmp_path):
        """No claimed/archived handoff at all for this chain_id — the
        classification core's own 'open'/not-terminal branch. Must fail
        closed (never reapable), not raise."""
        repo = _make_repo(tmp_path)

        assert chain_reached_terminal_close(str(repo), "never-seen-chain-id") is False

    def test_classification_error_fails_closed_not_terminal(self, tmp_path):
        """A CC-7 structured-error classification (banned/unknown
        deployment_state token) must read as NON-terminal, never as
        'safe to reap' — the requirement W2's reaper is built on."""
        repo = _make_repo(tmp_path)
        chain_id = "abandoned-chain-id"
        _archive_handoff(repo, chain_id, "abandoned")

        assert chain_reached_terminal_close(str(repo), chain_id) is False


def _waiver_dir(repo: Path, chain_id: str) -> Path:
    return repo / "state" / "review-trail" / "chain-ancestry-waivers" / chain_id


def _write_waiver(repo: Path, chain_id: str, sha: str, body: dict) -> None:
    chain_dir = _waiver_dir(repo, chain_id)
    chain_dir.mkdir(parents=True, exist_ok=True)
    (chain_dir / f"{sha}.json").write_text(json.dumps(body), encoding="utf-8")


class TestChainAncestryWaiverRecords:
    """AC1 — `chain_ancestry_waiver_records(cwd, chain_id)`: parsed waiver
    bodies keyed by sha. Missing dir, unreadable file, shape-invalid
    chain_id, and unparseable body each omit that sha and never raise."""

    def test_well_formed_record_round_trips(self, tmp_path):
        chain_id = "abc123"
        _write_waiver(
            tmp_path,
            chain_id,
            "deadbeef",
            {"sha": "deadbeef", "chain_id": chain_id, "certifies_review": False},
        )

        records = chain_ancestry_waiver_records(str(tmp_path), chain_id)

        assert records == {
            "deadbeef": {"sha": "deadbeef", "chain_id": chain_id, "certifies_review": False}
        }

    def test_missing_directory_omits_everything_never_raises(self, tmp_path):
        records = chain_ancestry_waiver_records(str(tmp_path), "abc999")

        assert records == {}

    def test_shape_invalid_chain_id_omits_everything_never_raises(self, tmp_path):
        # Path separator makes this fail `_CHAIN_ID_RE`'s directory-name-safety
        # shape check (`chain_waiver_dir` returns None for it).
        records = chain_ancestry_waiver_records(str(tmp_path), "../escape")

        assert records == {}

    def test_unreadable_file_is_omitted_sibling_survives(self, tmp_path, monkeypatch):
        chain_id = "aaa111"
        _write_waiver(tmp_path, chain_id, "goodsha", {"certifies_review": False})
        _write_waiver(tmp_path, chain_id, "badsha", {"certifies_review": False})

        real_open = Path.open

        def _flaky_open(self, *args, **kwargs):
            if self.name == "badsha.json":
                raise OSError("simulated unreadable file")
            return real_open(self, *args, **kwargs)

        monkeypatch.setattr(Path, "open", _flaky_open)

        records = chain_ancestry_waiver_records(str(tmp_path), chain_id)

        assert set(records) == {"goodsha"}

    def test_unparseable_json_body_is_omitted_sibling_survives(self, tmp_path):
        chain_id = "bbb222"
        _write_waiver(tmp_path, chain_id, "goodsha", {"certifies_review": False})
        chain_dir = _waiver_dir(tmp_path, chain_id)
        (chain_dir / "corruptsha.json").write_text("{not valid json", encoding="utf-8")

        records = chain_ancestry_waiver_records(str(tmp_path), chain_id)

        assert set(records) == {"goodsha"}


class TestAC2NoBehaviourChange:
    """AC2 — `chain_ancestry_waived_shas`'s function body is byte-unchanged by
    this plan's addition of `chain_ancestry_waiver_records`. This module
    changes what is READABLE (a second, body-driven accessor), never what a
    waiver PERMITS (the stem-driven relaxation set `_guard_foreign_session_
    range` / `_narrow_foreign_session_scope` consume)."""

    # Pinned sha256 of `inspect.getsource(chain_ancestry_waived_shas)` as of
    # this plan's C1 landing. A change to this function's body (including its
    # docstring) is a behaviour-change candidate this plan's Anti-scope
    # forbids and must fail this test rather than pass silently.
    _PINNED_BODY_SHA256 = "7ddc873a5760a9e09b26314a7bae054d007ede11c9a3597586e1811ae79529e0"

    def test_waived_shas_body_is_byte_unchanged(self):
        source = inspect.getsource(chain_ancestry_waived_shas)

        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()

        assert digest == self._PINNED_BODY_SHA256, (
            "chain_ancestry_waived_shas' body changed — AC2 requires it stay "
            "byte-unchanged by this plan; re-implementing it over the new "
            "records() parser would silently narrow what a waiver permits."
        )

    def test_accessors_agree_on_key_set_for_well_formed_records(self, tmp_path):
        chain_id = "ccc333"
        for sha in ("aaa111", "bbb222", "ccc333"):
            _write_waiver(tmp_path, chain_id, sha, {"certifies_review": False})

        stems = chain_ancestry_waived_shas(str(tmp_path), chain_id)
        records = chain_ancestry_waiver_records(str(tmp_path), chain_id)

        assert stems == frozenset(records)

    def test_corrupt_body_drops_from_records_only_waived_shas_unaffected(self, tmp_path):
        """The named asymmetry, pinned explicitly: a corrupt body must drop
        that sha from `chain_ancestry_waiver_records` (body-driven, strict)
        while `chain_ancestry_waived_shas` (stem-driven, permissive) still
        counts it as waived — narrowing the relaxation set on a parse
        failure would change what `_guard_foreign_session_range` accepts."""
        chain_id = "ddd444"
        _write_waiver(tmp_path, chain_id, "goodsha", {"certifies_review": False})
        chain_dir = _waiver_dir(tmp_path, chain_id)
        (chain_dir / "corruptsha.json").write_text("{not valid json", encoding="utf-8")

        stems = chain_ancestry_waived_shas(str(tmp_path), chain_id)
        records = chain_ancestry_waiver_records(str(tmp_path), chain_id)

        assert stems == frozenset({"goodsha", "corruptsha"})
        assert set(records) == {"goodsha"}


# ---------------------------------------------------------------------------
# Structural grep guard: no production module may derive a relaxation/
# crediting set from `chain_ancestry_waiver_records`. The reader added by
# this chunk must never become a second, silently-narrower path into what
# `review_trail_write._guard_foreign_session_range` or
# `coverage._narrow_foreign_session_scope` credit — that set is, and stays,
# `chain_ancestry_waived_shas`'s alone. Convention follows
# coordinator_core/frontmatter/tests/test_no_node_schema_shellout.py: `ast`,
# not regex, so a docstring/comment mentioning the function name can never
# trip the detector, and a planted-fixture test proves the gate has teeth.
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SCAN_ROOT = _REPO_ROOT / "coordinator_core"

_CREDITING_SET_WRAPPERS = {"frozenset", "set"}


def _is_excluded_source_path(path: Path) -> bool:
    if path.name.startswith("test_"):
        return True
    return "tests" in path.parts


def _calls_chain_ancestry_waiver_records(node: ast.expr) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id == "chain_ancestry_waiver_records"
    if isinstance(func, ast.Attribute):
        return func.attr == "chain_ancestry_waiver_records"
    return False


class RecordsIntoCreditingSet(ast.NodeVisitor):
    """Flags `frozenset(chain_ancestry_waiver_records(...))`-shaped calls (or
    the same wrapped around `.keys()`) — the shape of turning the records
    mapping into a bare sha-set, the exact shape `chain_ancestry_waived_shas`
    already owns."""

    def __init__(self) -> None:
        self.violations: list[int] = []

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        wrapper_name = func.id if isinstance(func, ast.Name) else None
        if wrapper_name in _CREDITING_SET_WRAPPERS and node.args:
            arg = node.args[0]
            target = arg
            if (
                isinstance(arg, ast.Call)
                and isinstance(arg.func, ast.Attribute)
                and arg.func.attr in ("keys", "items")
            ):
                target = arg.func.value
            if _calls_chain_ancestry_waiver_records(target):
                self.violations.append(node.lineno)
        self.generic_visit(node)


def find_records_fed_into_crediting_set(root: Path) -> list[tuple[str, int]]:
    violations: list[tuple[str, int]] = []
    for path in sorted(root.rglob("*.py")):
        if _is_excluded_source_path(path):
            continue
        try:
            relpath = path.resolve().relative_to(_REPO_ROOT).as_posix()
        except ValueError:
            relpath = path.resolve().relative_to(root.resolve()).as_posix()
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            continue
        visitor = RecordsIntoCreditingSet()
        visitor.visit(tree)
        for lineno in visitor.violations:
            violations.append((relpath, lineno))
    return violations


def test_no_production_module_derives_a_crediting_set_from_records():
    violations = find_records_fed_into_crediting_set(_SCAN_ROOT)
    assert violations == [], (
        "Found chain_ancestry_waiver_records() fed directly into "
        "frozenset()/set() — a crediting/relaxation set derivation. That set "
        f"is chain_ancestry_waived_shas' alone: {violations}"
    )


def test_gate_detects_a_planted_crediting_set_derivation(tmp_path):
    fixture = tmp_path / "fixture_reintroduced_crediting_set.py"
    fixture.write_text(
        textwrap.dedent(
            """
            from coordinator_core.chain_ancestry_waivers import chain_ancestry_waiver_records

            def _sneaky_relaxation_set(cwd, chain_id):
                return frozenset(chain_ancestry_waiver_records(cwd, chain_id).keys())
            """
        ),
        encoding="utf-8",
    )

    violations = find_records_fed_into_crediting_set(tmp_path)

    assert len(violations) == 1
    relpath, lineno = violations[0]
    assert relpath.endswith("fixture_reintroduced_crediting_set.py")
    assert lineno == 5


def test_gate_ignores_the_sanctioned_per_entry_read_pattern(tmp_path):
    """Negative control: the shape C7 actually uses — iterating records and
    reading `.get("certifies_review")` per entry — must never trip the gate."""
    fixture = tmp_path / "fixture_benign_per_entry_read.py"
    fixture.write_text(
        textwrap.dedent(
            """
            from coordinator_core.chain_ancestry_waivers import chain_ancestry_waiver_records

            def _decorate(cwd, chain_id, shas):
                records = chain_ancestry_waiver_records(cwd, chain_id)
                return [
                    {"sha": sha, "certifies_review": bool(records.get(sha, {}).get("certifies_review"))}
                    for sha in shas
                ]
            """
        ),
        encoding="utf-8",
    )

    violations = find_records_fed_into_crediting_set(tmp_path)

    assert violations == []
