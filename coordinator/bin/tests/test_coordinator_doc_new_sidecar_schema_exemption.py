"""test_coordinator_doc_new_sidecar_schema_exemption.py -- sidecar output under
docs/plans/ must not be validated against the `plan` schema (2026-08-19).

Purpose: `_assert_scaffold_content_valid`'s own docstring claims sidecar types
(review, prior-art-check, plan-coverage-check, docs-check) "have no schema in
Claude-klabauter's own ~14-schema corpus and resolve to no match, so this check is a
no-op for them" -- but that was false. A sidecar's output path
(`docs/plans/<stem>.<suffix>.md`) falls through `match_schema`'s glob
fallback to `docs/plans/*.md`, which resolves to the `plan` schema, so every
sidecar scaffold was validated as if it were a plan and unconditionally
failed (missing `title`, `status: pending` not a plan-schema enum value).
The fix reuses `_lint_is_sidecar_file` (the lint layer's own canonical
sidecar recognizer, already exempting these same paths from parent-directory
schema validation) to skip the check for sidecar output, restoring the
docstring's claim rather than adding new behaviour.

Spec backlink: dispatch brief 2026-08-19, diagnosis by team-lead session.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same load idiom as test_coordinator_doc_new_predecessor.py.

CLI subprocess calls here are single, non-looped invocations (one per test
method), the same shape already used by `CliTypeScopingTest` in
test_coordinator_doc_new_summary_gated_open.py -- not the per-item
amplification pattern the spawn-count gate polices.

Run:
    pytest coordinator/bin/tests/test_coordinator_doc_new_sidecar_schema_exemption.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path

from coordinator_core.win_portability import no_console_creationflags

_BIN_DIR = Path(__file__).resolve().parent.parent
_CLI_PATH = _BIN_DIR / "coordinator-doc-new.py"
_REPO_ROOT = _BIN_DIR.parent.parent  # coordinator/bin/tests -> coordinator/bin -> coordinator -> repo root


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_sidecar_schema_exemption_test", str(_CLI_PATH)
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_sidecar_schema_exemption_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()

_SIDECAR_TYPES = sorted(_cli._SIDECAR_TYPES)


class SidecarTypesScaffoldSuccessfullyTest(unittest.TestCase):
    """AC1 (regression): each sidecar type this CLI scaffolds under
    docs/plans/ must exit 0 and actually write its file -- this is the exact
    repro that failed before the fix (see the module docstring)."""

    def _run(self, tmp_path: Path, doc_type: str) -> tuple[subprocess.CompletedProcess, Path]:
        out_path = tmp_path / f"2026-08-19-test-stem.{_cli._SIDECAR_SUFFIXES[doc_type]}.md"
        proc = subprocess.run(
            [
                sys.executable, str(_CLI_PATH),
                "--type", doc_type,
                "--plan", "2026-08-19-test-stem",
                "--out", str(out_path),
            ],
            capture_output=True,
            text=True,
            cwd=str(_REPO_ROOT),
            **no_console_creationflags(),
        )
        return proc, out_path

    def test_each_sidecar_type_scaffolds_successfully(self):
        for doc_type in _SIDECAR_TYPES:
            with self.subTest(doc_type=doc_type):
                import tempfile
                with tempfile.TemporaryDirectory() as td:
                    proc, out_path = self._run(Path(td), doc_type)
                    self.assertEqual(
                        proc.returncode, 0,
                        f"{doc_type} scaffold failed: stdout={proc.stdout!r} stderr={proc.stderr!r}",
                    )
                    self.assertTrue(
                        out_path.exists(),
                        f"{doc_type} scaffold reported success but wrote no file at {out_path}",
                    )
                    content = out_path.read_text(encoding="utf-8")
                    self.assertIn("2026-08-19-test-stem.md", content)
                    self.assertTrue(content.startswith("---\n"))


class PlanSchemaStillEnforcedTest(unittest.TestCase):
    """Negative spec: the sidecar exemption must not have widened into
    "docs/plans/ is unchecked" -- a genuinely invalid plan (the same shape
    the pre-fix bug report reproduced with) must still refuse to write."""

    def test_invalid_plan_frontmatter_under_docs_plans_still_refused(self):
        invalid_plan_content = (
            "---\n"
            "kind: plan\n"
            "status: pending\n"
            "---\n"
            "# body\n"
        )
        out_path = str(_REPO_ROOT / "docs" / "plans" / "2026-08-19-not-a-real-plan.md")
        with self.assertRaises(SystemExit):
            _cli._assert_scaffold_content_valid(
                invalid_plan_content, out_path, str(_REPO_ROOT)
            )

    def test_sidecar_path_with_the_same_invalid_frontmatter_is_exempt(self):
        # Same invalid frontmatter shape, but a sidecar-suffixed path -- must
        # NOT raise, since it is validated as a sidecar (no schema), not a plan.
        invalid_plan_content = (
            "---\n"
            "kind: prior-art-check\n"
            "status: pending\n"
            "---\n"
            "# body\n"
        )
        out_path = str(
            _REPO_ROOT / "docs" / "plans" / "2026-08-19-test-stem.prior-art-check.md"
        )
        try:
            _cli._assert_scaffold_content_valid(
                invalid_plan_content, out_path, str(_REPO_ROOT)
            )
        except SystemExit as exc:  # pragma: no cover -- failure path, assert message on fail
            self.fail(f"sidecar path was not exempted from plan-schema validation: {exc}")


if __name__ == "__main__":
    unittest.main()
