"""test_publish_identity_check_source_refresh — pins the fix for the
`pre_ci` bootstrap-cycle defect (state/subagent-share dispatch brief,
2026-08-14): a publish round used to judge every row's identity by whatever
`check-persona-names.py` copy the DESTINATION already carried, which can be
a prior run's stale rules rather than THIS run's. A PM ruling that retires a
ban pattern from the SOURCE checker (`dist/mirror-native/claude-klabauter/
.github/scripts/check-persona-names.py`) could never land, because the very
publish that would refresh the destination's copy was judged by the
not-yet-refreshed copy first.

C-round-scan HOIST (docs/plans/2026-08-16-... round-timing plan): the
`_resolve_identity_checker_source_script` -> `_refresh_identity_checker_at_
dest` refresh precondition moved from `dispatch_percolate_pre_ci`'s per-row
leg into `dispatch_end_of_run_identity_check`, once per repo root -- the
row shape these tests exercise (a `dest_subdir` row whose `scan_dest` is the
real, unstaged mirror root, not its own staging tree) no longer runs a
per-row identity scan at all (§ `dispatch_percolate_pre_ci`'s own
`scan_dest == target.dest_dir` gate). Both tests below now drive
`dispatch_end_of_run_identity_check` directly -- refresh-then-check is still
exactly what it does, just once per round instead of once per row.

Run: python -m pytest coordinator/bin/tests/test_publish_identity_check_source_refresh.py -q
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

_BIN_DIR = Path(__file__).resolve().parent.parent
_REPO_ROOT = _BIN_DIR.parent.parent


def _load_publish_module():
    spec = importlib.util.spec_from_file_location(
        "publish_identity_check_source_refresh_under_test", _BIN_DIR / "publish.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


publish = _load_publish_module()

if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from coordinator_core.ops.percolate_identity_check import run_identity_check  # noqa: E402


_STALE_CHECKER = '''\
print("Identity check FAILED:")
print("  fixture/planted.txt:1: fleet codename 'DoE' -- retired ban pattern, stale copy")
import sys
sys.exit(1)
'''

_CURRENT_CHECKER = '''\
print("Identity check passed (0 text files scanned, 0 paths checked).")
import sys
sys.exit(0)
'''


def _write_checker(root: Path, content: str) -> Path:
    script_dir = root / ".github" / "scripts"
    script_dir.mkdir(parents=True)
    script_path = script_dir / "check-persona-names.py"
    script_path.write_text(content, encoding="utf-8")
    return script_path


class _FakeClaudeKlabauter:
    """Real `resolve_target` semantics against a plain dict store (no
    composition — this fix's helper only reads `inject`/`required_children`
    off whatever `resolve_target` returns), real `run_identity_check`."""

    def resolve_target(self, store, name):
        return store["targets"][name]

    def run_percolate(self, store_path, target, target_root, phase, **kwargs):
        return {"phase": phase, "guard_results": [], "rename_manifest": None, "restored_native": []}

    def run_identity_check(self, dest):
        return run_identity_check(dest)


def _repo_root_with_ctx(tmp_path: Path, store_targets: dict):
    """Same repo-root shape `dispatch_end_of_run_identity_check` scans --
    the row class the original defect report hit (`claude-klabauter` engine
    row, a subdir of the mirror repo root that
    `claude-klabauter-publish-repo-toplevel` alone publishes `.github/`
    into) no longer runs a per-row scan at all (§ module docstring); the
    refresh-then-check sequence these tests pin now lives in the end-of-run
    leg, scoped by repo root rather than by row."""
    repo_root = tmp_path / "repo"
    (repo_root / ".git").mkdir(parents=True)

    ctx = publish.PercolateEngineContext(
        engine_claude_klabauter=_FakeClaudeKlabauter(), store={"targets": store_targets}
    )
    return ctx, repo_root


class TestIdentityCheckerRefreshedFromSource:
    def test_stale_dest_checker_no_longer_fails_when_source_has_retired_the_ban(self, tmp_path):
        """A dest carrying a stale checker (bans a pattern the source no
        longer bans) must not fail this publish -- the checker is refreshed
        from source before it runs."""
        ctx, repo_root = _repo_root_with_ctx(tmp_path, store_targets={"engine": {}})

        # Destination: stale checker, unconditionally fails.
        _write_checker(repo_root, _STALE_CHECKER)

        # Source: this run's own checkout, the ban pattern retired.
        source_dir = tmp_path / "dist" / "mirror-native" / "claude-klabauter" / ".github"
        _write_checker(source_dir.parent, _CURRENT_CHECKER)  # writes <parent>/.github/scripts/...
        # _write_checker takes the `.github`-OWNING root, not `.github` itself.
        ctx.store["targets"]["toplevel"] = {
            "inject": [
                {
                    "src": str(source_dir),
                    "dst": ".github",
                    "required_children": ["scripts/check-persona-names.py"],
                }
            ]
        }

        ok = publish.dispatch_end_of_run_identity_check(
            ctx, [repo_root], target_filtered=False
        )
        assert ok is True  # the stale dest copy is refreshed from source first

        refreshed = (repo_root / ".github" / "scripts" / "check-persona-names.py").read_text(
            encoding="utf-8"
        )
        assert refreshed == _CURRENT_CHECKER

    def test_failure_message_names_currency_and_remedy_when_no_source_declared(self, tmp_path, capsys):
        """A genuine failure (source unresolvable, dest checker fails) must
        name that currency could not be verified, not print 9000+ raw
        findings with no explanation of the root cause class."""
        ctx, repo_root = _repo_root_with_ctx(
            tmp_path, store_targets={"engine": {}}
        )  # no target declares the persona-checker inject entry

        _write_checker(repo_root, _STALE_CHECKER)

        ok = publish.dispatch_end_of_run_identity_check(
            ctx, [repo_root], target_filtered=False
        )
        assert ok is False

        captured = capsys.readouterr()
        assert "check-persona-names.py exited 1" in captured.err
        assert "checker currency:" in captured.err
        assert "no source checker declared in this store's inject entries" in captured.err
        assert "currency unverified" in captured.err
