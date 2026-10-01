"""test_coordinator_doc_new_spinoff_resolvable_fields.py -- standing gate:
`_scaffold_spinoff` (coordinator-doc-new.py --type spinoff) must never emit
the literal 'PLACEHOLDER' in `authoring_session:` or `workstream:` (2026-08-21).

Purpose: prior behavior hand-typed 'PLACEHOLDER' into both fields whenever
they were unresolvable, and the EM Edit'd the values in afterward every
time -- a state/handoffs/2026-08-21-scaffold-knows-the-session.md baton
ruled the hand-edit itself is the defect. Fixed by:

  - `authoring_session`: an unresolvable session id (`_resolve_session_id()
    == "em-unknown"`) now REFUSES the scaffold loudly (`sys.exit(1)`) rather
    than degrading to a placeholder. This field is a machine-trustworthy
    fact or nothing -- `coordinator_core.baton_assemble
    ._adopt_prior_attempt_scaffold_path` gates cross-authorship adoption on
    it (see `_scaffold_handoff`'s docstring).
  - `workstream` and the origin ancestry: resolved read-only off the baton
    the current session holds, via the `_resolve_spinoff_origin` helper
    (`coordinator_core.ops.handoff_author_fork._resolve_origin_handoff` +
    `coordinator_core.ops._fm_util.extract_frontmatter_scalar`), which
    returns a `SpinoffOrigin(origin_handoff, origin_handoff_id, workstream)`.
    When no workstream resolves, the key is OMITTED entirely -- not
    re-emitted as a placeholder. `origin_session` / `origin_handoff` /
    `origin_handoff_id` are always emitted (explicit null when unresolved).

FAST TIER ONLY: no subprocess spawn, no git spawn. `_scaffold_spinoff` and
`_resolve_spinoff_origin` are called in-process with every engine seam
mocked at the point of use -- no live repo, no live handoff corpus, no
`git` invocation. Contrast with `test_coordinator_doc_new_category_flag.py`
/ `test_coordinator_doc_new_emitter_parity.py`, which are `spawns_process` +
`cadence` and invoke the real CLI as a subprocess.

Loaded by file path (`importlib.machinery.SourceFileLoader`) since
`coordinator-doc-new` is an extensionless polyglot entrypoint, not a `.py`
module -- same idiom as this directory's sibling suites (see
`test_coordinator_doc_new_plan_author_session.py`'s docstring).

Run:
    python3 -m pytest coordinator/bin/tests/test_coordinator_doc_new_spinoff_resolvable_fields.py -v
"""
from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

from coordinator_core.frontmatter import schema_validate

_BIN_DIR = Path(__file__).resolve().parent.parent


def _load_cli_module():
    loader = importlib.machinery.SourceFileLoader(
        "coordinator_doc_new_spinoff_resolvable_fields_test", str(_BIN_DIR / "coordinator-doc-new.py")
    )
    spec = importlib.util.spec_from_loader(
        "coordinator_doc_new_spinoff_resolvable_fields_test", loader
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    loader.exec_module(mod)
    return mod


_cli = _load_cli_module()

_A_UUID = "bc1ca482-6b06-4943-ab49-92c9b35482ad"
_NO_ORIGIN = _cli.SpinoffOrigin(None, None, None)
_RESOLVE_ORIGIN_HANDOFF = "coordinator_core.ops.handoff_author_fork._resolve_origin_handoff"


class ScaffoldSpinoffNeverEmitsPlaceholderTest(unittest.TestCase):
    """The standing gate itself: no PLACEHOLDER, in either field, ever."""

    @staticmethod
    def _identity_lines(content: str) -> list[str]:
        """The two fields this gate governs -- excludes `summary:`, whose
        PLACEHOLDER text is a deliberately untouched, separate convention
        (this change scopes to authoring identity, not every scaffold
        placeholder)."""
        return [
            line for line in content.splitlines()
            if line.startswith("authoring_session:") or line.startswith("workstream:")
        ]

    def test_resolvable_session_and_workstream_emit_no_placeholder(self):
        with mock.patch.object(
            _cli, "_resolve_session_id", return_value=_A_UUID
        ), mock.patch.object(
            _cli, "_resolve_session_display_name", return_value="claude-klabauter-51"
        ), mock.patch.object(
            _cli,
            "_resolve_spinoff_origin",
            return_value=_cli.SpinoffOrigin(None, None, "sat-04-completion-axis"),
        ):
            content = _cli._scaffold_spinoff(title="t", branch="b")
        identity_lines = self._identity_lines(content)
        self.assertEqual(len(identity_lines), 2)
        for line in identity_lines:
            self.assertNotIn("PLACEHOLDER", line)
        self.assertIn(f'authoring_session: "{_A_UUID}"', content)
        self.assertIn('workstream: "sat-04-completion-axis"', content)

    def test_unresolvable_workstream_omits_the_key_not_a_placeholder(self):
        """workstream unresolvable -> key OMITTED entirely, never
        're-emitted' as PLACEHOLDER. authoring_session stays resolvable so
        this test isolates the workstream arm."""
        with mock.patch.object(
            _cli, "_resolve_session_id", return_value=_A_UUID
        ), mock.patch.object(
            _cli, "_resolve_session_display_name", return_value=None
        ), mock.patch.object(
            _cli, "_resolve_spinoff_origin", return_value=_NO_ORIGIN
        ):
            content = _cli._scaffold_spinoff(title="t", branch="b")
        identity_lines = self._identity_lines(content)
        self.assertEqual(len(identity_lines), 1)
        self.assertNotIn("PLACEHOLDER", identity_lines[0])
        for line in content.splitlines():
            self.assertFalse(line.startswith("workstream:"), f"workstream key present: {line!r}")


class ScaffoldSpinoffLoudFailureTest(unittest.TestCase):
    """authoring_session's em-unknown arm: fail loud, never PLACEHOLDER."""

    def test_em_unknown_session_id_raises_systemexit_1(self):
        with mock.patch.object(_cli, "_resolve_session_id", return_value="em-unknown"):
            with self.assertRaises(SystemExit) as ctx:
                _cli._scaffold_spinoff(title="t", branch="b")
        self.assertEqual(ctx.exception.code, 1)

    def test_em_unknown_session_id_never_reaches_placeholder_emission(self):
        """Regression guard for the exact defect this baton names: the
        em-unknown arm must raise BEFORE any content is built, not build a
        PLACEHOLDER-carrying string and then (hypothetically) discard it."""
        with mock.patch.object(
            _cli, "_resolve_spinoff_origin"
        ) as _origin_mock:
            with mock.patch.object(_cli, "_resolve_session_id", return_value="em-unknown"):
                with self.assertRaises(SystemExit):
                    _cli._scaffold_spinoff(title="t", branch="b")
        _origin_mock.assert_not_called()


class ResolveSpinoffOriginTest(unittest.TestCase):

    def test_resolves_workstream_off_the_held_baton(self):
        with tempfile.TemporaryDirectory(prefix="spinoff-workstream-") as tmpdir:
            handoffs_dir = Path(tmpdir) / "state" / "handoffs"
            handoffs_dir.mkdir(parents=True)
            baton = handoffs_dir / "held.md"
            baton.write_text(
                "---\ntitle: held\nworkstream: sat-04-completion-axis\n---\nbody\n",
                encoding="utf-8",
            )
            with mock.patch.object(
                _cli, "_resolve_session_id", return_value="sess-1"
            ), mock.patch.object(
                _cli, "_current_repo_root", return_value=tmpdir
            ), mock.patch(
                "coordinator_core.ops.handoff_author_fork._resolve_origin_handoff",
                return_value=("state/handoffs/held.md", "hnd-held-abc123"),
            ):
                result = _cli._resolve_spinoff_origin()
        self.assertEqual(
            result,
            _cli.SpinoffOrigin(
                "state/handoffs/held.md", "hnd-held-abc123", "sat-04-completion-axis"
            ),
        )

    def test_no_held_baton_returns_no_origin(self):
        with tempfile.TemporaryDirectory(prefix="spinoff-workstream-") as tmpdir:
            with mock.patch.object(
                _cli, "_resolve_session_id", return_value="sess-1"
            ), mock.patch.object(
                _cli, "_current_repo_root", return_value=tmpdir
            ), mock.patch(
                "coordinator_core.ops.handoff_author_fork._resolve_origin_handoff",
                return_value=(None, None),
            ):
                result = _cli._resolve_spinoff_origin()
        self.assertEqual(result, _NO_ORIGIN)

    def test_held_baton_with_no_workstream_field_returns_null_workstream(self):
        with tempfile.TemporaryDirectory(prefix="spinoff-workstream-") as tmpdir:
            handoffs_dir = Path(tmpdir) / "state" / "handoffs"
            handoffs_dir.mkdir(parents=True)
            baton = handoffs_dir / "held.md"
            baton.write_text("---\ntitle: held\n---\nbody\n", encoding="utf-8")
            with mock.patch.object(
                _cli, "_resolve_session_id", return_value="sess-1"
            ), mock.patch.object(
                _cli, "_current_repo_root", return_value=tmpdir
            ), mock.patch(
                "coordinator_core.ops.handoff_author_fork._resolve_origin_handoff",
                return_value=("state/handoffs/held.md", None),
            ):
                result = _cli._resolve_spinoff_origin()
        self.assertEqual(result, _cli.SpinoffOrigin("state/handoffs/held.md", None, None))

    def test_em_unknown_session_id_returns_no_origin_without_engine_touch(self):
        with mock.patch.object(
            _cli, "_resolve_session_id", return_value="em-unknown"
        ), mock.patch(
            "coordinator_core.ops.handoff_author_fork._resolve_origin_handoff"
        ) as _resolve_mock:
            result = _cli._resolve_spinoff_origin()
        self.assertEqual(result, _NO_ORIGIN)
        _resolve_mock.assert_not_called()

    def test_unresolvable_repo_root_returns_no_origin(self):
        with mock.patch.object(
            _cli, "_resolve_session_id", return_value="sess-1"
        ), mock.patch.object(
            _cli, "_current_repo_root", return_value=None
        ):
            result = _cli._resolve_spinoff_origin()
        self.assertEqual(result, _NO_ORIGIN)

    def test_ambiguous_claim_returns_nulls_and_prints_one_stderr_line(self):
        with tempfile.TemporaryDirectory(prefix="spinoff-origin-") as tmpdir, mock.patch.object(
            _cli, "_resolve_session_id", return_value="sess-1"
        ), mock.patch.object(
            _cli, "_current_repo_root", return_value=tmpdir
        ), mock.patch(
            _RESOLVE_ORIGIN_HANDOFF, side_effect=RuntimeError("two live claims")
        ), mock.patch("sys.stderr", new_callable=io.StringIO) as err:
            result = _cli._resolve_spinoff_origin()
        self.assertEqual(result, _NO_ORIGIN)
        self.assertEqual(len(err.getvalue().strip().splitlines()), 1)

    def test_origin_handoff_is_forward_slash_relative_never_absolute(self):
        with tempfile.TemporaryDirectory(prefix="spinoff-origin-") as tmpdir:
            (Path(tmpdir) / "state" / "handoffs").mkdir(parents=True)
            (Path(tmpdir) / "state" / "handoffs" / "held.md").write_text(
                "---\ntitle: held\n---\n", encoding="utf-8"
            )
            with mock.patch.object(
                _cli, "_resolve_session_id", return_value="sess-1"
            ), mock.patch.object(
                _cli, "_current_repo_root", return_value=tmpdir
            ), mock.patch(
                _RESOLVE_ORIGIN_HANDOFF, return_value=("state/handoffs/held.md", None)
            ):
                result = _cli._resolve_spinoff_origin()
        self.assertEqual(result.origin_handoff, "state/handoffs/held.md")
        self.assertNotIn("\\", result.origin_handoff)
        self.assertNotIn(tmpdir, result.origin_handoff)


class ScaffoldSpinoffOriginAncestryTest(unittest.TestCase):
    """The origin_* frontmatter lines `_scaffold_spinoff` stamps."""

    @staticmethod
    def _scaffold(origin, **kwargs) -> tuple[str, dict]:
        with mock.patch.object(
            _cli, "_resolve_session_id", return_value=_A_UUID
        ), mock.patch.object(
            _cli, "_resolve_session_display_name", return_value=None
        ), mock.patch.object(
            _cli, "_resolve_spinoff_origin", return_value=origin
        ):
            content = _cli._scaffold_spinoff(title="t", branch="b", **kwargs)
        return content, yaml.safe_load(content.split("---", 2)[1])

    def test_held_baton_stamps_origin_session_handoff_and_id(self):
        _, fm = self._scaffold(
            _cli.SpinoffOrigin("state/handoffs/held.md", "hnd-held-abc123", "ws")
        )
        self.assertEqual(fm["origin_session"], _A_UUID)
        self.assertEqual(fm["origin_handoff"], "state/handoffs/held.md")
        self.assertEqual(fm["origin_handoff_id"], "hnd-held-abc123")
        self.assertEqual(fm["origin_session"], fm["authoring_session"])

    def test_nothing_held_emits_explicit_nulls(self):
        _, fm = self._scaffold(_NO_ORIGIN)
        self.assertEqual(fm["origin_session"], _A_UUID)
        self.assertIn("origin_handoff", fm)
        self.assertIsNone(fm["origin_handoff"])
        self.assertIn("origin_handoff_id", fm)
        self.assertIsNone(fm["origin_handoff_id"])

    def test_runtime_error_from_resolver_degrades_to_nulls(self):
        with mock.patch.object(
            _cli, "_resolve_session_id", return_value=_A_UUID
        ), mock.patch.object(
            _cli, "_resolve_session_display_name", return_value=None
        ), mock.patch.object(
            _cli, "_current_repo_root", return_value="/nonexistent-root"
        ), mock.patch(
            _RESOLVE_ORIGIN_HANDOFF, side_effect=RuntimeError("ambiguous")
        ), mock.patch("sys.stderr", new_callable=io.StringIO):
            content = _cli._scaffold_spinoff(title="t", branch="b")
        fm = yaml.safe_load(content.split("---", 2)[1])
        self.assertIsNone(fm["origin_handoff"])
        self.assertIsNone(fm["origin_handoff_id"])

    def test_explicit_origin_handoff_id_wins_and_is_emitted_once(self):
        content, fm = self._scaffold(
            _cli.SpinoffOrigin("state/handoffs/held.md", "hnd-resolved-111111", None),
            origin_handoff_id="hnd-explicit-222222",
        )
        self.assertEqual(fm["origin_handoff_id"], "hnd-explicit-222222")
        self.assertEqual(content.count("origin_handoff_id:"), 1)

    def test_explicit_origin_handoff_id_wins_over_null_resolution(self):
        content, fm = self._scaffold(_NO_ORIGIN, origin_handoff_id="hnd-explicit-222222")
        self.assertEqual(fm["origin_handoff_id"], "hnd-explicit-222222")
        self.assertEqual(content.count("origin_handoff_id:"), 1)

    def test_frontmatter_with_origin_keys_validates_against_handoff_schema(self):
        for origin in (
            _cli.SpinoffOrigin("state/handoffs/held.md", "hnd-held-abc123", "ws"),
            _NO_ORIGIN,
        ):
            _, fm = self._scaffold(origin)
            result = schema_validate.validate("handoff", fm)
            self.assertTrue(result["ok"], result.get("errors"))

    def test_emitted_origin_handoff_has_no_backslash_or_absolute_root(self):
        content, fm = self._scaffold(
            _cli.SpinoffOrigin("state/handoffs/held.md", None, None)
        )
        self.assertEqual(fm["origin_handoff"], "state/handoffs/held.md")
        self.assertNotIn("\\", fm["origin_handoff"])
        self.assertFalse(fm["origin_handoff"].startswith("/"))


if __name__ == "__main__":
    unittest.main()
