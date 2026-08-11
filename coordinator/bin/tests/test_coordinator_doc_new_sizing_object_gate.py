"""test_coordinator_doc_new_sizing_object_gate.py -- coverage for C2 of the
plan sizing-citation gate (2026-08-06): threading `sizing_object` through
`_scaffold_plan` and `coordinator-doc-new --type plan --sizing-object PATH`.

Purpose: a plan's `sizing_object:` frontmatter key was hand-written on 17
plans and declared by nothing on the write path -- `_scaffold_plan` took no
sizing parameter, so the scaffolder could not emit it, and nothing checked
that a supplied path actually resolved before the file landed on disk. This
suite pins the scaffolder half of the fix (C1 declares the schema field
separately):

1. `--sizing-object PATH` resolving on disk emits a real `sizing_object:`
   frontmatter key (AC2).
2. Omitting BOTH `--sizing-object` and `--no-sizing-object` for `--type plan`
   exits 1 and writes no file -- INVERTED by
   docs/plans/2026-08-06-sizing-citation-absence-is-checkable.md § C1: the
   commented-optional-key-skeleton-unchanged behaviour this docstring
   originally described no longer holds. An explicit sizing answer is now
   REQUIRED for `--type plan`; the old negative-spec case (bare omission
   passing silently) is exactly the failure mode
   `assert_plan_sizing_citation`'s absence leg exists to close, so it is
   refused here at write time instead.
3. `--sizing-object PATH` that does NOT resolve on disk fails loud (exit 1,
   naming the unresolvable path and `coordinator:sizing` as the route) and
   writes no file -- the write-time half of the gate, and the reason this
   chunk exists at all (AC3).
4. `--no-sizing-object` emits a literal unquoted `sizing_object: null` --
   parses via `yaml.safe_load` to real Python `None`, not the string
   `"null"`, since a quoted null would silently become a dangling citation
   under the absence-plan's sweep (AC2 of the absence plan).
5. `--sizing-object` and `--no-sizing-object` together exit 1 as mutually
   exclusive, writing no file (AC3 of the absence plan).

Spec backlink: docs/plans/2026-08-06-plan-sizing-citation-gate.md § C2, AC2, AC3
Spec backlink: docs/plans/2026-08-06-sizing-citation-absence-is-checkable.md § C1, AC1-AC4

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_predecessor.py.
`_tmp_git_repo`/`_init_git_repo` mirror
test_coordinator_doc_new_roadmap_baton_self_validation.py's fixture exactly.

Negative-spec: does NOT cover AC1 (schema declaration -- C1's surface) or
AC4/AC5 (the corpus-wide assert op and the write-guard nudge -- C3/C4's
surfaces). Scoped to the scaffolder only.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_sizing_object_gate.py -v
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

from coordinator_core.win_portability import no_console_creationflags

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new"

_NO_CONSOLE = no_console_creationflags()


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_sizing_object_gate_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_sizing_object_gate_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init", "-q", str(root)], capture_output=True, **_NO_CONSOLE)
    subprocess.run(
        [
            "git", "-C", str(root), "-c", "user.email=test@test", "-c", "user.name=Test",
            "commit", "-q", "--allow-empty", "-m", "init",
        ],
        capture_output=True,
        **_NO_CONSOLE,
    )


@contextlib.contextmanager
def _tmp_git_repo():
    """Temp git repo plus a default plan out_path.

    Negative-spec on the yielded `out_path`: it sits at the repo ROOT, so it
    canNOT satisfy the sizing schema's `plan` pattern `^docs/plans/.+\\.md$`.
    Any test that exercises the reverse-edge mutation (`--sizing-object`, which
    writes the `plan` FK back onto the cited sizing) must override it with a
    path under `docs/plans/` and mkdir the parent — see
    `test_full_cli_emits_real_deliverable_id_and_validates` and
    `test_resolving_path_emits_key_and_writes_file` for the idiom. Tests that
    only scaffold, and never mutate a sizing, can use it as-is. The default is
    left root-relative rather than "fixed" because three callers deliberately
    pass their own path already; changing it here would silently re-point them.

    Load note: `_init_git_repo` shells out to `git init` per fixture. On this
    machine that is a real cost — see the repo's load norm — and
    `coordinator/tests/_fake_git.py` builds the same state as plain files with
    no subprocess. Prefer it for any fixture added here that does not genuinely
    need a live git binary; the reverse-edge mutation path does, because
    `locked_write.locked_rmw` resolves its lock directory through git.
    """
    with tempfile.TemporaryDirectory() as td:
        repo = Path(td) / "testrepo"
        repo.mkdir()
        _init_git_repo(repo)
        out_path = repo / "custom-out.md"
        yield repo, out_path


class ScaffoldPlanEmitsSizingObjectTest(unittest.TestCase):
    """AC2: `_scaffold_plan(sizing_object=...)` emits a real frontmatter key."""

    def test_supplied_sizing_object_is_emitted_as_real_key(self):
        content = _cli._scaffold_plan(
            title="t",
            branch="b",
            author="test-author",
            sizing_object="state/sizings/2026-08-06-example.yaml",
        )
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertEqual(
            fields.get("sizing_object"), "state/sizings/2026-08-06-example.yaml"
        )

    def test_omitted_sizing_object_leaves_commented_skeleton_unchanged(self):
        """`_scaffold_plan` itself is unchanged for the no-sizing-object
        case -- the helper still emits the commented-optional-key skeleton
        when called with no `sizing_object` kwarg. C1 moved the REQUIREMENT
        that `--type plan` supply an explicit sizing answer into main()'s
        CLI-level validation (see FullCliSizingObjectRequiredTest below),
        not into this helper -- `_scaffold_plan` on its own still permits
        omission; the CLI is what now refuses it."""
        content = _cli._scaffold_plan(title="t", branch="b", author="test-author")
        self.assertNotIn("sizing_object:", content)
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertNotIn("sizing_object", fields)
        # The pre-existing commented-optional-key block is untouched.
        self.assertIn(
            "# Optional keys — uncomment and fill as needed (promoted de-facto keys, D1):",
            content,
        )

    def test_no_sizing_object_emits_unquoted_null(self):
        """AC2 of the absence plan: `sizing_object="null"` (the sentinel
        threaded from `--no-sizing-object`) emits a literal unquoted
        `sizing_object: null` -- parses to real Python None, not the string
        "null"."""
        content = _cli._scaffold_plan(
            title="t", branch="b", author="test-author", sizing_object="null"
        )
        self.assertIn("sizing_object: null", content)
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertIsNone(fields.get("sizing_object"))
        self.assertNotEqual(fields.get("sizing_object"), "null")


class FullCliSizingObjectResolvesTest(unittest.TestCase):
    """AC2/AC3 end-to-end: the real CLI surface, not just the scaffolder helper."""

    def test_resolving_path_emits_key_and_writes_file(self):
        with _tmp_git_repo() as (repo, _out_path):
            out_path = repo / "docs" / "plans" / "custom-out.md"
            out_path.parent.mkdir(parents=True)
            sizing_dir = repo / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-06-example.yaml"
            sizing_file.write_text(
                "schema: sizing-object\n"
                "intent: \"example intent for the CLI sizing-object gate test\"\n"
                "estimate:\n"
                "  tshirt: S\n"
                "  provisional: true\n"
                "route: spec-dispatch\n"
                "detents: []\n"
                "fork: null\n"
                "xl_exit: null\n"
                "status: sized\n"
                "premise:\n"
                "  provenance: executed\n"
                "  evidence: \"exercised for the CLI sizing-object gate test\"\n"
            )
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Sizing object gate test plan",
                    "--sizing-object", "state/sizings/2026-08-06-example.yaml",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.exists())
            fm_text = out_path.read_text().split("---", 2)[1]
            fields = yaml.safe_load(fm_text)
            self.assertEqual(
                fields.get("sizing_object"), "state/sizings/2026-08-06-example.yaml"
            )


class FullCliSizingObjectDanglingTest(unittest.TestCase):
    """AC3: a supplied path that does not resolve fails loud, no file written."""

    def test_dangling_path_exits_1_and_writes_no_file(self):
        with _tmp_git_repo() as (repo, out_path):
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Sizing object gate test plan (dangling)",
                    "--sizing-object", "state/sizings/2026-08-06-does-not-exist.yaml",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("state/sizings/2026-08-06-does-not-exist.yaml", result.stderr)
            self.assertIn("coordinator:sizing", result.stderr)
            self.assertFalse(out_path.exists())


class FullCliSizingObjectRequiredTest(unittest.TestCase):
    """AC1: neither flag supplied -> exit 1, no file, coordinator:sizing named."""

    def test_neither_flag_exits_1_and_writes_no_file(self):
        with _tmp_git_repo() as (repo, out_path):
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Sizing object gate test plan (neither flag)",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("coordinator:sizing", result.stderr)
            self.assertFalse(out_path.exists())


class FullCliNoSizingObjectFlagTest(unittest.TestCase):
    """AC2 end-to-end: --no-sizing-object writes the plan with a real None."""

    def test_no_sizing_object_flag_writes_file_with_real_none(self):
        with _tmp_git_repo() as (repo, out_path):
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Sizing object gate test plan (no-sizing-object)",
                    "--no-sizing-object",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.exists())
            fm_text = out_path.read_text().split("---", 2)[1]
            fields = yaml.safe_load(fm_text)
            self.assertIn("sizing_object", fields)
            self.assertIsNone(fields.get("sizing_object"))
            self.assertNotEqual(fields.get("sizing_object"), "null")


class FullCliBothFlagsMutuallyExclusiveTest(unittest.TestCase):
    """AC3: both flags together -> exit 1, no file."""

    def test_both_flags_exit_1_and_write_no_file(self):
        with _tmp_git_repo() as (repo, out_path):
            sizing_dir = repo / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-06-example.yaml"
            sizing_file.write_text("id: example\n")
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Sizing object gate test plan (both flags)",
                    "--sizing-object", "state/sizings/2026-08-06-example.yaml",
                    "--no-sizing-object",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(out_path.exists())


class ScaffoldSizingMintsDeliverableIdTest(unittest.TestCase):
    """AC1/AC2/AC5: `_scaffold_sizing` mints a real, schema-valid
    `deliverable_id` via the CLI's `--type sizing-object` path, reusing
    `_mint_deliverable_id` -- never a second mint formula.

    Spec backlink: docs/plans/2026-08-10-a-sizing-mints-the-join-key-that-closes-it.md § C1
    """

    def test_full_cli_emits_real_deliverable_id_and_validates(self):
        with _tmp_git_repo() as (repo, _out_path):
            out_path = repo / "state" / "sizings" / "custom-out.yaml"
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "sizing-object",
                    "--title", "A sizing mints its own join key",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(out_path.exists())
            doc = yaml.safe_load(out_path.read_text(encoding="utf-8"))
            deliverable_id = doc.get("deliverable_id")
            self.assertIsNotNone(deliverable_id)
            self.assertRegex(
                deliverable_id,
                r"^dlv-(?!placeholder-replace-with)[0-9a-zA-Z][0-9a-zA-Z.-]*$",
            )
            # AC2 — validates against the vendored schema with zero hand edits.
            import jsonschema

            schema_path = (
                _BIN_DIR.parent.parent
                / "coordinator_core" / "frontmatter" / "schemas"
                / "sizing-object.schema.json"
            )
            import json

            schema = json.loads(schema_path.read_text(encoding="utf-8"))
            jsonschema.validate(doc, schema)

    def test_deliverable_id_flag_is_carried_verbatim_never_reminted(self):
        """AC3: `--deliverable-id <id>` on the sizing type is a carry --
        emitted unchanged."""
        with _tmp_git_repo() as (repo, _out_path):
            out_path = repo / "state" / "sizings" / "carried.yaml"
            carried_id = "dlv-carried-abc123"
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "sizing-object",
                    "--title", "A carried sizing id",
                    "--deliverable-id", carried_id,
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            doc = yaml.safe_load(out_path.read_text(encoding="utf-8"))
            self.assertEqual(doc.get("deliverable_id"), carried_id)

    def test_scaffold_sizing_helper_direct_carry_and_mint(self):
        """Unit-level: `_scaffold_sizing(deliverable_id=...)` emits the
        supplied value verbatim, and omitting it emits `null`."""
        content = _cli._scaffold_sizing(title="t", deliverable_id="dlv-direct-000000")
        doc = yaml.safe_load(content)
        self.assertEqual(doc.get("deliverable_id"), "dlv-direct-000000")

        content_no_id = _cli._scaffold_sizing(title="t")
        doc_no_id = yaml.safe_load(content_no_id)
        self.assertIsNone(doc_no_id.get("deliverable_id"))

    def test_status_comment_lists_full_terminal_set(self):
        """AC5: the `status:` line's enum comment names `shipped` and
        `declined`, not just the stale `draft | sized | routed | superseded`
        set."""
        content = _cli._scaffold_sizing(title="t")
        status_line = next(
            line for line in content.splitlines() if line.startswith("status:")
        )
        self.assertIn("shipped", status_line)
        self.assertIn("declined", status_line)


class SiblingScaffoldsUnaffectedTest(unittest.TestCase):
    """AC7: sibling whole-document-YAML scaffolds and a representative `.md`
    type emit byte-identical output to before this change."""

    def test_goal_scaffold_unchanged(self):
        content = _cli._scaffold_goal(title="An unrelated goal")
        self.assertNotIn("deliverable_id", content)

    def test_strategic_self_description_scaffold_unchanged(self):
        content = _cli._scaffold_strategic_self_description(title="repo-x")
        self.assertNotIn("deliverable_id", content)

    def test_plan_scaffold_still_emits_its_own_deliverable_id_field(self):
        """The plan type's own (pre-existing) deliverable_id emission is
        untouched by this change -- still driven by its own resolved id."""
        content = _cli._scaffold_plan(
            title="t",
            branch="b",
            author="test-author",
            deliverable_id="dlv-plan-000000",
            sizing_object="null",
        )
        fm_text = content.split("---", 2)[1]
        fields = yaml.safe_load(fm_text)
        self.assertEqual(fields.get("deliverable_id"), "dlv-plan-000000")


class EndToEndSizingCascadeClosesTest(unittest.TestCase):
    """AC4: scaffold a sizing, scaffold a plan carrying the same
    deliverable_id, then fire the registered `deliverable.cascade_terminal`
    op (the SAME mechanism `plan_status_transition` invokes on a plan's
    `implemented` stamp -- see `deliverable_cascade.py`'s own module
    docstring) and confirm the sizing reaches `status: shipped` with its
    `plan` FK written. Demonstrated entirely in a temp git fixture; no
    existing `state/sizings/*.yaml` record is touched.
    """

    def test_cascade_flips_scaffolded_sizing_to_shipped(self):
        import asyncio

        import coordinator_core.ops.deliverable_cascade as cascade_mod
        import coordinator_core.ipc as ipc_mod

        with _tmp_git_repo() as (repo, _out_path):
            sizing_out = repo / "state" / "sizings" / "2026-08-10-e2e.yaml"
            sizing_result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "sizing-object",
                    "--title", "End to end cascade closure sizing",
                    "--out", str(sizing_out),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(sizing_result.returncode, 0, sizing_result.stderr)
            sizing_doc = yaml.safe_load(sizing_out.read_text(encoding="utf-8"))
            minted_id = sizing_doc["deliverable_id"]
            self.assertIsNotNone(minted_id)

            plan_out = repo / "docs" / "plans" / "2026-08-10-e2e-plan.md"
            plan_result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "End to end cascade closure plan",
                    "--deliverable-id", minted_id,
                    "--sizing-object", "state/sizings/2026-08-10-e2e.yaml",
                    "--out", str(plan_out),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(plan_result.returncode, 0, plan_result.stderr)

            plan_relpath = "docs/plans/2026-08-10-e2e-plan.md"
            handler = ipc_mod.get_op_handler("deliverable.cascade_terminal")
            self.assertIsNotNone(handler)

            result = asyncio.run(
                handler(
                    {
                        "deliverable_id": minted_id,
                        "source_kind": "plan",
                        "source_path": plan_relpath,
                        "target_kind": "sizing",
                    },
                    repo_root=repo / ".git",
                )
            )
            self.assertEqual(result["exit_code"], 0, result)
            self.assertEqual(len(result["advanced"]), 1, result)

            after = yaml.safe_load(sizing_out.read_text(encoding="utf-8"))
            self.assertEqual(after["status"], "shipped")
            self.assertEqual(after["plan"], plan_relpath)

            errors = cascade_mod._validate_sizing_fm(
                sizing_out.read_text(encoding="utf-8")
            )
            self.assertEqual(errors, [])


class PlanCarriesCitedSizingDeliverableIdTest(unittest.TestCase):
    """2026-08-10 fork-remediation follow-up: a plan citing a sizing that
    already carries a `deliverable_id` must CARRY that id verbatim, never
    mint a second, forked one -- `deliverable.cascade_terminal` joins by
    exact-string match, so a fork makes the pair unreachable by the cascade.
    """

    def test_plan_carries_id_from_cited_sizing(self):
        with _tmp_git_repo() as (repo, _out_path):
            out_path = repo / "docs" / "plans" / "carried-from-sizing.md"
            out_path.parent.mkdir(parents=True)
            sizing_dir = repo / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-10-carrier.yaml"
            sizing_file.write_text(
                "schema: sizing-object\n"
                "deliverable_id: dlv-sizing-carrier-abc123\n"
                "intent: \"example intent for the deliverable-id carry test\"\n"
                "estimate:\n"
                "  tshirt: S\n"
                "  provisional: true\n"
                "route: spec-dispatch\n"
                "detents: []\n"
                "fork: null\n"
                "xl_exit: null\n"
                "status: sized\n"
                "premise:\n"
                "  provenance: executed\n"
                "  evidence: \"exercised for the deliverable-id carry test\"\n"
            )
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Plan carrying a cited sizing's deliverable_id",
                    "--sizing-object", "state/sizings/2026-08-10-carrier.yaml",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("cited sizing-object", result.stderr)
            fm_text = out_path.read_text().split("---", 2)[1]
            fields = yaml.safe_load(fm_text)
            self.assertEqual(fields.get("deliverable_id"), "dlv-sizing-carrier-abc123")

    def test_plan_mints_when_cited_sizing_has_no_id(self):
        """Most of the 225-record corpus is pre-C1: no `deliverable_id` key
        at all. Must still mint as before, not crash or emit null."""
        with _tmp_git_repo() as (repo, _out_path):
            out_path = repo / "docs" / "plans" / "no-id-sizing.md"
            out_path.parent.mkdir(parents=True)
            sizing_dir = repo / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-10-no-id.yaml"
            sizing_file.write_text(
                "schema: sizing-object\n"
                "intent: \"pre-C1 sizing with no deliverable_id key\"\n"
                "estimate:\n"
                "  tshirt: S\n"
                "  provisional: true\n"
                "route: spec-dispatch\n"
                "detents: []\n"
                "fork: null\n"
                "xl_exit: null\n"
                "status: sized\n"
                "premise:\n"
                "  provenance: executed\n"
                "  evidence: \"exercised for the no-id sizing test\"\n"
            )
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Plan citing an id-less sizing",
                    "--sizing-object", "state/sizings/2026-08-10-no-id.yaml",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            fm_text = out_path.read_text().split("---", 2)[1]
            fields = yaml.safe_load(fm_text)
            self.assertIsNotNone(fields.get("deliverable_id"))
            self.assertTrue(fields.get("deliverable_id").startswith("dlv-"))

    def test_explicit_deliverable_id_flag_wins_over_cited_sizing(self):
        with _tmp_git_repo() as (repo, _out_path):
            out_path = repo / "docs" / "plans" / "explicit-wins.md"
            out_path.parent.mkdir(parents=True)
            sizing_dir = repo / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-10-ignored.yaml"
            sizing_file.write_text(
                "schema: sizing-object\n"
                "deliverable_id: dlv-should-be-ignored-000000\n"
                "intent: \"sizing whose id must NOT win over an explicit flag\"\n"
                "estimate:\n"
                "  tshirt: S\n"
                "  provisional: true\n"
                "route: spec-dispatch\n"
                "detents: []\n"
                "fork: null\n"
                "xl_exit: null\n"
                "status: sized\n"
                "premise:\n"
                "  provenance: executed\n"
                "  evidence: \"exercised for the explicit-flag-wins test\"\n"
            )
            result = subprocess.run(
                [
                    sys.executable, str(_CLI_PATH), "--type", "plan",
                    "--title", "Plan with explicit flag over a cited sizing",
                    "--deliverable-id", "dlv-explicit-flag-000000",
                    "--sizing-object", "state/sizings/2026-08-10-ignored.yaml",
                    "--out", str(out_path),
                ],
                cwd=str(repo),
                capture_output=True,
                text=True,
                timeout=30,
                **_NO_CONSOLE,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            fm_text = out_path.read_text().split("---", 2)[1]
            fields = yaml.safe_load(fm_text)
            self.assertEqual(fields.get("deliverable_id"), "dlv-explicit-flag-000000")

    def test_malformed_cited_sizing_degrades_to_mint_not_crash(self):
        """Unit-level, against `_resolve_cited_sizing_deliverable_id`
        directly rather than the full CLI: a malformed sizing file also
        trips the PRE-EXISTING, unrelated plan<->sizing reverse-edge writer
        (`_write_sizing_reverse_edge`, unconditional whenever
        `--sizing-object` resolves on disk) -- that writer's own post-
        mutation-parse failure is a separate, out-of-scope failure surface
        this fix does not touch. This test isolates the read-and-degrade
        step this fix DOES own, with no reverse-edge write involved."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sizing_dir = root / "state" / "sizings"
            sizing_dir.mkdir(parents=True)
            sizing_file = sizing_dir / "2026-08-10-malformed.yaml"
            # Not a YAML mapping -- a bare scalar. `_read_sizing_meta` raises
            # ValueError on this; the helper must degrade to None, not crash.
            sizing_file.write_text("not a mapping, just a scalar string\n")

            import io
            import contextlib as _contextlib

            _stderr_capture = io.StringIO()
            with _contextlib.redirect_stderr(_stderr_capture):
                result = _cli._resolve_cited_sizing_deliverable_id(
                    "state/sizings/2026-08-10-malformed.yaml", str(root)
                )
            self.assertIsNone(result)
            self.assertIn("degrading to mint-from-slug", _stderr_capture.getvalue())


if __name__ == "__main__":
    unittest.main()
