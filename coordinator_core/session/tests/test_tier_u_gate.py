"""
coordinator_core.session.tests.test_tier_u_gate -- tests for
coordinator_core.session.tier_u_gate.enforce_tier_u_gate (R3+R4 shared
resolve-and-execute shape gate).

Spec backlink: cross-repo/inbox/2026-07-25-coordinator-claude-em-validate-tier-u-
shape-ruling.md (R3, R4).

Fixtures mirror test_grant.py's ``_make_repo``/``_live_session`` idiom --
a real ``tmp_path`` git repo with an explicit ``session_id=`` threaded
through rather than relying on env-var session resolution.
"""

from __future__ import annotations

import ast
import inspect
import json
import subprocess
from pathlib import Path

import pytest

from coordinator_core.bash_guards import check_test_suite_invocation as guard
from coordinator_core.session import core, grant, tier_u_gate

# Every test in this file builds its repo via `_make_repo(tmp_path)`, spawning
# real git (init/config/add/commit) because the production code under test --
# `core.git_root()`, consulted by the gate's own repo-root resolution --
# reads real git state that no mock stands in for. `tmp_path` is
# function-scoped and tests write grant/session state under reused session
# ids, so the repo fixture stays per-test rather than hoisted to module
# scope. The spawn ratchet's `_BASELINE` is shrink-only pre-existing residue
# and is explicitly not the route for this file --
# coordinator_core/tests/test_no_new_spawning_tests.py Rule 2.
pytestmark = [pytest.mark.cadence, pytest.mark.spawns_process]


def _make_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        "[tool.pytest.ini_options]\ntestpaths = [\"coordinator_core\"]\n"
    )
    (tmp_path / "coordinator_core").mkdir()
    subprocess.run(["git", "add", "."], cwd=tmp_path)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path)
    return tmp_path


def _live_session(repo, sid):
    sdir = Path(repo) / ".git" / "coordinator-sessions" / sid
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "meta.json").write_text(
        json.dumps({"pid": "999", "last_activity": core.now_iso()}, indent=2) + "\n",
        encoding="utf-8",
    )
    return sdir


class TestEnforceTierUGate:
    def test_scoped_tier_f_command_refuses_without_grant_and_ignores_declaration(
        self, tmp_path, monkeypatch
    ):
        """TIER-F EXTENSION (2026-08-04): a genuinely Tier-F-classified
        command (forced via a configured ``fast_test_cmd``, same technique
        as ``test_scoped_single_key_command_not_refused_no_tie_needed``
        below) now REFUSES absent a live grant -- the fast tier's blanket
        exemption from the grant leg is gone. It also does NOT consult
        ``check_tier_u_grant``'s R6 declaration sibling
        (``_fast_tier_unscoped_declaration_covers``) -- spied to prove the
        Tier-F leg never reaches it, per PM ruling 2026-08-04 (no
        declaration-based Tier-F escape hatch)."""
        import unittest.mock as mock

        repo = _make_repo(tmp_path)
        scoped_cmd = "pytest coordinator_core/sub/test_x.py"

        def _fail_if_called(*a, **k):
            raise AssertionError(
                "Tier-F leg must never consult the R6 declaration exit"
            )

        monkeypatch.setattr(
            tier_u_gate, "_fast_tier_unscoped_declaration_covers", _fail_if_called
        )
        with mock.patch.object(guard, "resolve_git_root", lambda cwd: str(repo)), \
             mock.patch.object(
                 guard, "_configured_test_cmds",
                 lambda root: [guard.ConfiguredCmd("fast_test_cmd", scoped_cmd, 0)],
             ):
            assert guard.classify_command(scoped_cmd, cwd=str(repo))[0].tier == "F"
            result = tier_u_gate.enforce_tier_u_gate(
                scoped_cmd, repo_root=str(repo), session_id="s1"
            )
        assert result.proceed is False
        assert "Tier F" in result.refusal_message
        assert "tier-u-grant-cli grant pm" in result.refusal_message

    def test_scoped_tier_f_command_proceeds_with_live_grant(self, tmp_path):
        """The Tier-F leg's only exit: a live Tier-U grant (PM-ruled
        2026-08-04 escape hatch)."""
        import unittest.mock as mock

        repo = _make_repo(tmp_path)
        scoped_cmd = "pytest coordinator_core/sub/test_x.py"
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "yes, run the fast tier", session_id="s1", cwd=str(repo))
        with mock.patch.object(guard, "resolve_git_root", lambda cwd: str(repo)), \
             mock.patch.object(
                 guard, "_configured_test_cmds",
                 lambda root: [guard.ConfiguredCmd("fast_test_cmd", scoped_cmd, 0)],
             ):
            result = tier_u_gate.enforce_tier_u_gate(
                scoped_cmd, repo_root=str(repo), session_id="s1"
            )
        assert result.proceed is True
        assert result.refusal_message is None

    def test_mixed_tier_u_and_tier_f_match_falls_through_to_tier_u_leg(
        self, tmp_path
    ):
        """Review: coordinator:code-reviewer (Finding 1, tierf-s1-session) --
        a chained command whose FIRST segment satisfies the repo's
        configured ``fast_test_cmd`` (Tier F) and whose SECOND segment is
        a bare, unscoped runner invocation (Tier U) produces a single
        ``classify_command`` call with BOTH ``tier_u_matches`` and
        ``tier_f_matches`` non-empty. None of the three early-return
        branches in ``enforce_tier_u_gate`` fire (each requires one of the
        two lists empty), so this pins that control falls through to the
        final Tier-U leg -- the conservative, correct outcome -- rather
        than relying on branch order alone. Absent a live grant this must
        refuse with the Tier-U message (mentions the R6 declaration exit
        and the grant ceremony), never the Tier-F message."""
        import unittest.mock as mock

        repo = _make_repo(tmp_path)
        scoped_cmd = "pytest coordinator_core/sub/test_x.py"
        mixed_cmd = f"{scoped_cmd} && pytest"

        with mock.patch.object(guard, "resolve_git_root", lambda cwd: str(repo)), \
             mock.patch.object(
                 guard, "_configured_test_cmds",
                 lambda root: [guard.ConfiguredCmd("fast_test_cmd", scoped_cmd, 0)],
             ):
            matches = guard.classify_command(mixed_cmd, cwd=str(repo))
            assert {m.tier for m in matches} == {"F", "U"}
            result = tier_u_gate.enforce_tier_u_gate(
                mixed_cmd, repo_root=str(repo), session_id="s1"
            )
        assert result.proceed is False
        assert "Tier U" in result.refusal_message
        assert "scoped fast_test_cmd" in result.refusal_message
        assert "granted ceremony" in result.refusal_message
        assert "Tier F" not in result.refusal_message

    def test_unscoped_command_refused_without_live_grant(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = tier_u_gate.enforce_tier_u_gate("pytest", repo_root=str(repo), session_id="s1")
        assert result.proceed is False
        assert "Tier U" in result.refusal_message
        assert "scoped fast_test_cmd" in result.refusal_message
        assert "granted ceremony" in result.refusal_message

    def test_unscoped_command_proceeds_with_live_grant(self, tmp_path):
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "yes, run it", session_id="s1", cwd=str(repo))
        result = tier_u_gate.enforce_tier_u_gate("pytest", repo_root=str(repo), session_id="s1")
        assert result.proceed is True
        assert result.refusal_message is None

    def test_never_writes_a_grant(self, tmp_path, monkeypatch):
        """R4: this gate only READS a grant. Assert write_tier_u_grant is
        never invoked, on either the refuse or the proceed-with-grant path."""
        repo = _make_repo(tmp_path)
        _live_session(repo, "s1")
        # Write the grant BEFORE installing the fail-if-called patch, so this
        # setup call itself doesn't trip the guard.
        grant.write_tier_u_grant("pm", "yes", session_id="s1", cwd=str(repo))

        def _fail_if_called(*a, **k):
            raise AssertionError("enforce_tier_u_gate must never write a grant")

        monkeypatch.setattr(grant, "write_tier_u_grant", _fail_if_called)

        # Refuse path (grant present but for a different session -- s2).
        tier_u_gate.enforce_tier_u_gate("pytest", repo_root=str(repo), session_id="s2")

        # Proceed-with-live-grant path (s1's grant, already on disk above).
        tier_u_gate.enforce_tier_u_gate("pytest", repo_root=str(repo), session_id="s1")

    def test_tier_u_gate_module_has_no_write_tier_u_grant_import(self):
        """Static guard: the module's own namespace must never bind
        write_tier_u_grant at all -- belt-and-braces alongside the
        runtime monkeypatch guard above."""
        assert not hasattr(tier_u_gate, "write_tier_u_grant")

    def test_scoped_single_key_command_classifies_tier_f_not_tier_u_no_tie_needed(
        self, tmp_path
    ):
        """The memo's underlying point (cross-repo/inbox/2026-07-25-doe-
        claude-em-validate-tier-u-shape-ruling.md): a genuinely scoped
        command classifies Tier F, not Tier U -- demonstrated here with a
        single fast_test_cmd declaration (no fast/full tie at all), which is
        the uncontroversial case. The TIED variant of this example diverged
        from the memo for a time -- the full_test_cmd leg forced Tier U
        regardless of shape -- and was reconciled in favour of R1 on
        2026-07-30; see
        test_check_test_suite_invocation_public_api.py's
        test_classify_command_identical_scoped_string_under_both_keys_now_classifies_tier_f
        for that resolution.

        TIER-F EXTENSION (2026-08-04): Tier F now requires a grant like
        Tier U, so this test grants the calling session -- what it
        continues to demonstrate is that this command is refused as Tier F
        with a normal grant (the Tier-F leg, not the Tier-U leg with its R6
        declaration exit), never as a tied Tier U."""
        repo = tmp_path
        subprocess.run(["git", "init", "-q"], cwd=repo)
        (repo / "pyproject.toml").write_text(
            "[tool.pytest.ini_options]\ntestpaths = [\"coordinator_core\"]\n"
        )
        (repo / "coordinator_core" / "frontmatter" / "tests").mkdir(parents=True)
        import unittest.mock as mock

        scoped_cmd = "pytest coordinator_core/frontmatter/tests"
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "yes, run it", session_id="s1", cwd=str(repo))
        with mock.patch.object(guard, "resolve_git_root", lambda cwd: str(repo)), \
             mock.patch.object(
                 guard, "_configured_test_cmds",
                 lambda root: [guard.ConfiguredCmd("fast_test_cmd", scoped_cmd, 0)],
             ):
            assert guard.classify_command(scoped_cmd, cwd=str(repo))[0].tier == "F"
            result = tier_u_gate.enforce_tier_u_gate(scoped_cmd, repo_root=str(repo), session_id="s1")
        assert result.proceed is True
        assert result.refusal_message is None

    def _repo_with_declaration(self, tmp_path, *, reason: str, fast_test_cmd: str):
        """A repo whose ``coordinator.local.md`` declares
        ``fast_tier_unscoped_reason`` (R6) alongside a Tier-U-shaped
        ``fast_test_cmd`` -- ``pytest coordinator_core`` matches the
        testpaths root exactly, so it classifies Tier U on shape alone
        (no configured-cmd leg needed)."""
        repo = _make_repo(tmp_path)
        (repo / "coordinator.local.md").write_text(
            "---\n"
            f'fast_test_cmd: "{fast_test_cmd}"\n'
            f'fast_tier_unscoped_reason: "{reason}"\n'
            "---\n",
            encoding="utf-8",
        )
        return repo

    def test_declared_repo_proceeds_on_the_literal_resolved_fast_test_cmd(
        self, tmp_path, monkeypatch
    ):
        """R6: a non-empty declaration discharges the authority check for
        EXACTLY the resolved fast_test_cmd string, and never touches the
        grant machinery to do it."""
        repo = self._repo_with_declaration(
            tmp_path,
            reason="marker-based fast/full split; no path subset is meaningful",
            fast_test_cmd="pytest coordinator_core",
        )
        called = {"n": 0}

        def _spy(*a, **k):
            called["n"] += 1
            return False, None

        monkeypatch.setattr(tier_u_gate, "check_tier_u_grant", _spy)

        result = tier_u_gate.enforce_tier_u_gate(
            "pytest coordinator_core", repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is True
        assert result.refusal_message is None
        assert called["n"] == 0

    def test_declared_repo_still_refuses_a_different_tier_u_command(self, tmp_path):
        """Narrow-reach property: the declaration covers ONLY the literal
        resolved fast_test_cmd -- any other Tier-U command still refuses,
        declaration or not."""
        repo = self._repo_with_declaration(
            tmp_path,
            reason="marker-based fast/full split; no path subset is meaningful",
            fast_test_cmd="pytest coordinator_core",
        )
        result = tier_u_gate.enforce_tier_u_gate(
            "pytest", repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is False
        assert "Tier U" in result.refusal_message

    @pytest.mark.parametrize("reason", ["", "   ", None])
    def test_empty_or_whitespace_or_absent_declaration_still_refuses(
        self, tmp_path, reason
    ):
        """An absent key, an empty string, or a whitespace-only value is NOT
        a declaration -- the refusal stands."""
        if reason is None:
            repo = _make_repo(tmp_path)  # no coordinator.local.md at all
        else:
            repo = self._repo_with_declaration(
                tmp_path, reason=reason, fast_test_cmd="pytest coordinator_core"
            )
        result = tier_u_gate.enforce_tier_u_gate(
            "pytest coordinator_core", repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is False
        assert "Tier U" in result.refusal_message

    def test_refusal_text_names_three_exits(self, tmp_path):
        repo = _make_repo(tmp_path)
        result = tier_u_gate.enforce_tier_u_gate(
            "pytest", repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is False
        assert "scoped fast_test_cmd" in result.refusal_message
        assert "fast_tier_unscoped_reason" in result.refusal_message
        assert "granted ceremony" in result.refusal_message

    # -- Fail-closed default on an UNCLASSIFIABLE command (PM ruling, 2026-07-28) --

    _OPAQUE_WRAPPER = "pnpm run tier:fast"

    def _repo_with_shape(self, tmp_path, *, fast_tier_shape):
        """A repo with no ``fast_test_cmd``/testpaths shape that would
        classify ``_OPAQUE_WRAPPER`` at all -- classify_command returns
        zero matches for it -- optionally declaring ``fast_tier_shape``."""
        repo = _make_repo(tmp_path)
        if fast_tier_shape is not None:
            (repo / "coordinator.local.md").write_text(
                "---\n"
                f'fast_tier_shape: "{fast_tier_shape}"\n'
                "---\n",
                encoding="utf-8",
            )
        return repo

    def test_unclassifiable_command_with_no_declaration_refuses(self, tmp_path):
        repo = self._repo_with_shape(tmp_path, fast_tier_shape=None)
        result = tier_u_gate.enforce_tier_u_gate(
            self._OPAQUE_WRAPPER, repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is False
        assert "fast_tier_shape" in result.refusal_message
        assert "scoped" in result.refusal_message
        assert "unscoped" in result.refusal_message

    @pytest.mark.parametrize("bad_value", ["", "   ", "bogus"])
    def test_unclassifiable_command_with_invalid_declaration_refuses(
        self, tmp_path, bad_value
    ):
        repo = self._repo_with_shape(tmp_path, fast_tier_shape=bad_value)
        result = tier_u_gate.enforce_tier_u_gate(
            self._OPAQUE_WRAPPER, repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is False
        assert "fast_tier_shape" in result.refusal_message

    def test_unclassifiable_command_declared_scoped_proceeds(self, tmp_path, monkeypatch):
        """fast_tier_shape: scoped proceeds WITHOUT ever touching the grant
        machinery -- same discipline as the classified-non-U path."""
        repo = self._repo_with_shape(tmp_path, fast_tier_shape="scoped")
        called = {"n": 0}

        def _spy(*a, **k):
            called["n"] += 1
            return False, None

        monkeypatch.setattr(tier_u_gate, "check_tier_u_grant", _spy)
        result = tier_u_gate.enforce_tier_u_gate(
            self._OPAQUE_WRAPPER, repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is True
        assert result.refusal_message is None
        assert called["n"] == 0

    def test_unclassifiable_command_declared_unscoped_refuses_without_grant(self, tmp_path):
        """fast_tier_shape: unscoped is treated exactly like a detected
        Tier-U command -- refuses when there is no fast_tier_unscoped_reason
        declaration and no live grant."""
        repo = self._repo_with_shape(tmp_path, fast_tier_shape="unscoped")
        result = tier_u_gate.enforce_tier_u_gate(
            self._OPAQUE_WRAPPER, repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is False
        assert "Tier U" in result.refusal_message
        assert "granted ceremony" in result.refusal_message

    def test_unclassifiable_command_declared_unscoped_proceeds_with_live_grant(
        self, tmp_path
    ):
        repo = self._repo_with_shape(tmp_path, fast_tier_shape="unscoped")
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "yes, run it", session_id="s1", cwd=str(repo))
        result = tier_u_gate.enforce_tier_u_gate(
            self._OPAQUE_WRAPPER, repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is True
        assert result.refusal_message is None

    def test_classified_tier_f_command_with_grant_proceeds_absent_fast_tier_shape_declaration(
        self, tmp_path
    ):
        """Regression guard: a command classify_command DOES recognise
        (Tier F here) is unaffected by the UNCLASSIFIABLE-command machinery
        -- it must not be routed through the fast_tier_shape/footprint
        branch just because no fast_tier_shape declaration is present. A
        live grant demonstrates this proceeds via the (now-gated) Tier-F
        leg, not via the unclassifiable branch's footprint narrowing (which
        would proceed even with NO grant -- see
        TestUnclassifiableFootprintNarrowing) -- these are different code
        paths and must not be conflated post-TIER-F-EXTENSION."""
        import unittest.mock as mock

        repo = _make_repo(tmp_path)
        scoped_cmd = "pytest coordinator_core/sub/test_x.py"
        _live_session(repo, "s1")
        grant.write_tier_u_grant("pm", "yes, run it", session_id="s1", cwd=str(repo))
        with mock.patch.object(guard, "resolve_git_root", lambda cwd: str(repo)), \
             mock.patch.object(
                 guard, "_configured_test_cmds",
                 lambda root: [guard.ConfiguredCmd("fast_test_cmd", scoped_cmd, 0)],
             ):
            result = tier_u_gate.enforce_tier_u_gate(
                scoped_cmd, repo_root=str(repo), session_id="s1"
            )
        assert result.proceed is True
        assert result.refusal_message is None

    def test_tier_t_command_proceeds_without_touching_grant_machinery(
        self, tmp_path, monkeypatch
    ):
        """A Tier-T command (scoped by construction, so classify_command
        returns zero matches -- see the FOOTPRINT NARROWING section of the
        module docstring for why this repo's classifier treats a
        positively-scoped, unconfigured runner invocation this way) is
        entirely unaffected by the TIER-F EXTENSION: it proceeds without
        ever calling check_tier_u_grant, exactly as before this dispatch."""
        repo = _make_repo(tmp_path)
        tier_t_cmd = "pytest coordinator_core/session/tests/test_tier_u_gate.py"
        assert guard.classify_command(tier_t_cmd, cwd=str(repo)) == []

        called = {"n": 0}

        def _spy(*a, **k):
            called["n"] += 1
            return False, None

        monkeypatch.setattr(tier_u_gate, "check_tier_u_grant", _spy)
        result = tier_u_gate.enforce_tier_u_gate(
            tier_t_cmd, repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is True
        assert result.refusal_message is None
        assert called["n"] == 0

    def test_this_repos_own_diff_scoped_fast_test_cmd_classifies_tier_f_and_refuses(
        self, tmp_path
    ):
        """AC3b: THIS repo's own configured fast_test_cmd is BARE and
        classifies Tier U (R6-discharged) -- but the DIFF-SCOPED form
        /validate and /workday-complete step-1 actually execute (the bare
        command with a changed-test-path appended, via
        coordinator_core.diff_scoped_tests.append_test_paths) classifies
        Tier F, and is newly gated by this dispatch. Measured live against
        the real repo root, not a synthetic fixture -- the two forms are
        different objects and must not be conflated (see plan's Anti-scope:
        'Name the object precisely')."""
        import os as _os

        from coordinator_core.diff_scoped_tests import append_test_paths
        from coordinator_core.resolve_validation_cmd import cs_resolve_fast_test_cmd

        repo_root = str(Path(__file__).resolve().parents[3])
        assert _os.path.isfile(_os.path.join(repo_root, "pyproject.toml"))

        resolved = cs_resolve_fast_test_cmd(repo_root, _quiet=True)
        assert resolved.exit_code == 0
        bare_cmd = resolved.cmd

        # Bare form: Tier U, R6-discharged -- untouched by this dispatch.
        bare_matches = guard.classify_command(bare_cmd, cwd=repo_root)
        assert any(m.tier == "U" for m in bare_matches)

        # Diff-scoped form: Tier F, newly gated.
        diff_scoped_cmd = append_test_paths(
            bare_cmd, ["coordinator_core/session/tests/test_tier_u_gate.py"]
        )
        diff_scoped_matches = guard.classify_command(diff_scoped_cmd, cwd=repo_root)
        assert diff_scoped_matches and all(m.tier == "F" for m in diff_scoped_matches)

        result = tier_u_gate.enforce_tier_u_gate(
            diff_scoped_cmd, repo_root=repo_root, session_id="s1-c1-ac3b-probe-session"
        )
        assert result.proceed is False
        assert "Tier F" in result.refusal_message

    def test_r6_declaration_does_not_discharge_a_tier_f_command(self, tmp_path):
        """AC4/C1's own guard against the escape-hatch-by-omission the PM
        forbade: a repo carrying a fast_tier_unscoped_reason declaration for
        its BARE fast_test_cmd still requires a grant for the DIFF-SCOPED
        (Tier-F) form of that same command -- the R6 exit covers only the
        literal resolved fast_test_cmd string, and (independent of that
        narrow reach) the Tier-F leg never consults the declaration at all
        (see test_scoped_tier_f_command_refuses_without_grant_and_ignores_declaration)."""
        from coordinator_core.diff_scoped_tests import append_test_paths

        bare_cmd = "pytest coordinator_core"
        repo = self._repo_with_declaration(
            tmp_path,
            reason="marker-based fast/full split; no path subset is meaningful",
            fast_test_cmd=bare_cmd,
        )
        diff_scoped_cmd = append_test_paths(bare_cmd, ["coordinator_core/sub/test_x.py"])

        matches = guard.classify_command(diff_scoped_cmd, cwd=str(repo))
        assert matches and all(m.tier == "F" for m in matches)

        result = tier_u_gate.enforce_tier_u_gate(
            diff_scoped_cmd, repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is False
        assert "Tier F" in result.refusal_message


class TestGateCallSitesPinned:
    """AC2/AC3: the resolved-command path for BOTH resolve-and-execute
    callers of ``enforce_tier_u_gate`` is pinned here, at both call sites
    each caller has -- the primary (diff-scoped) call and the
    zero-tests-collected ``gate_full`` fallback (unscoped) call. A future
    refactor that bypasses this gate, or that hands either call site the
    wrong command shape, fails loudly here rather than being caught only by
    a live PreToolUse denial in production.

    Both callers' on-disk filenames are hyphenated (bin/-resident CLIs, not
    importable package members), loaded by explicit file path exactly as
    their own test suites do (coordinator/bin/tests/
    test_validate_fast_and_packageability.py,
    coordinator/bin/tests/test_workday_complete_step1_validate.py) -- this
    module never edits either CLI, only reads and drives them.
    """

    _REPO_ROOT = str(Path(__file__).resolve().parents[3])
    _VFP_CLI = str(Path(_REPO_ROOT) / "coordinator" / "bin" / "validate-fast-and-packageability.py")
    _WCS1_CLI = str(Path(_REPO_ROOT) / "coordinator" / "bin" / "workday-complete-step1-validate.py")

    @staticmethod
    def _load_module(name, path):
        import importlib.util

        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(mod)
        return mod

    def test_validate_fast_and_packageability_run_fast_pins_both_gate_call_sites(self):
        from coordinator_core.diff_scoped_tests import PYTEST_NO_TESTS_COLLECTED, append_test_paths

        mod = self._load_module("_c1_pin_vfp", self._VFP_CLI)

        resolved_cmd = "the-bare-resolved-fast-test-cmd"

        class _FakeResolveResult:
            def __init__(self, stdout, returncode):
                self.stdout = stdout
                self.returncode = returncode

        mod._resolver.resolve_fast_test_cmd = lambda repo_root: _FakeResolveResult(
            resolved_cmd + "\n", 0
        )
        mod.find_changed_test_files = lambda repo_root: ["pkg/test_changed.py"]

        gate_calls: list[str] = []

        def _spy_gate(cmd, repo_root=None):
            gate_calls.append(cmd)
            return tier_u_gate.TierUGateResult(proceed=True)

        mod.enforce_tier_u_gate = _spy_gate

        run_calls: list[str] = []

        def _fake_run_resolved_command(cmd: str) -> int:
            run_calls.append(cmd)
            # First (diff-scoped) run collects zero tests -> triggers the
            # gate_full fallback with the unscoped command; second run
            # (the fallback itself) passes.
            return PYTEST_NO_TESTS_COLLECTED if len(run_calls) == 1 else 0

        mod._run_resolved_command = _fake_run_resolved_command

        validation_result, exit_code = mod.run_fast("/tmp")

        expected_scoped_cmd = append_test_paths(resolved_cmd, ["pkg/test_changed.py"])
        assert gate_calls == [expected_scoped_cmd, resolved_cmd]
        assert run_calls == [expected_scoped_cmd, resolved_cmd]
        assert exit_code == 0
        assert validation_result == "0"

    def test_workday_complete_step1_validate_main_pins_both_gate_call_sites(
        self, monkeypatch, tmp_path
    ):
        from coordinator_core.diff_scoped_tests import PYTEST_NO_TESTS_COLLECTED, append_test_paths

        mod = self._load_module("_c1_pin_wcs1", self._WCS1_CLI)

        # No bin/check-ubt-build-fresh.sh in this cwd -> UBT gate skips,
        # isolating this test to Gate 2 (the fast-test resolver + this
        # module's gate) exactly as production does for a non-UE repo.
        monkeypatch.chdir(tmp_path)

        resolved_cmd = "the-bare-resolved-fast-test-cmd"

        class _FakeResolveResult:
            def __init__(self, stdout, returncode):
                self.stdout = stdout
                self.returncode = returncode

        mod.rvc.resolve_fast_test_cmd = lambda cwd: _FakeResolveResult(resolved_cmd + "\n", 0)
        mod.find_changed_test_files = lambda repo_root: ["pkg/test_changed.py"]

        gate_calls: list[str] = []

        def _spy_gate(cmd, repo_root=None):
            gate_calls.append(cmd)
            return tier_u_gate.TierUGateResult(proceed=True)

        mod.enforce_tier_u_gate = _spy_gate

        run_calls: list[str] = []

        def _fake_run_fast_test_cmd(cmd, env):
            run_calls.append(cmd)
            return (PYTEST_NO_TESTS_COLLECTED, "") if len(run_calls) == 1 else (0, "")

        mod._run_fast_test_cmd = _fake_run_fast_test_cmd

        rc = mod.main()

        expected_scoped_cmd = append_test_paths(resolved_cmd, ["pkg/test_changed.py"])
        assert gate_calls == [expected_scoped_cmd, resolved_cmd]
        assert run_calls == [expected_scoped_cmd, resolved_cmd]
        assert rc == 0


class TestUnclassifiableFootprintNarrowing:
    """FOOTPRINT NARROWING (2026-08-02, see the module docstring section of
    that name): the fail-closed default on an UNCLASSIFIABLE command was
    written for the opaque-wrapper case but fired for every unclassifiable
    command, so a fast tier configured as ``true`` and every diff-scoped
    single-file invocation refused too. The narrowing consults the command's
    runner FOOTPRINT before refusing; the wrapper class still refuses.

    All four cases below run against a repo with NO ``fast_tier_shape`` and
    NO ``fast_tier_unscoped_reason`` declaration -- the narrowing must be
    visible on the no-declaration default itself, not borrowed from a
    declaration path.
    """

    def _plain_repo(self, tmp_path, monkeypatch):
        """A repo with no declaration of any kind, and no configured
        ``fast_test_cmd`` inherited from the ambient environment (the CLI
        tests set ``COORDINATOR_FAST_TEST_CMD``, and a leaked value would
        give these commands a configured-cmd classification they must not
        have here)."""
        monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
        monkeypatch.delenv("COORDINATOR_FULL_TEST_CMD", raising=False)
        return _make_repo(tmp_path)

    @pytest.mark.parametrize("cmd", ["true", "exit 3", "echo hello"])
    def test_no_runner_footprint_proceeds(self, tmp_path, monkeypatch, cmd):
        """Case (a): a command that invokes no test runner at all cannot be
        an unscoped suite run, so this gate was never about it. Measured
        before the narrowing: all three refused, which is why
        ``test_t4_resolved_command_passes`` (``true``) and
        ``test_t5_resolved_command_fails`` (``exit 3``) failed."""
        repo = self._plain_repo(tmp_path, monkeypatch)
        assert guard.classify_command(cmd, cwd=str(repo)) == []
        result = tier_u_gate.enforce_tier_u_gate(
            cmd, repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is True
        assert result.refusal_message is None

    @pytest.mark.parametrize(
        "cmd",
        [
            "pytest coordinator_core/sub/test_x.py",
            "pytest coordinator_core/sub/test_x.py::test_case",
            "python3 -m pytest coordinator_core/sub/test_x.py",
            "pytest -k some_expr",
        ],
    )
    def test_scoped_runner_invocation_proceeds(self, tmp_path, monkeypatch, cmd):
        """Case (b): a runner invocation with an explicit path / node-id /
        ``-k`` footprint is scoped by construction, and is exactly what the
        Bash-layer guard's own refusal message advertises as always allowed.
        Two enforcement layers must not disagree about one command shape."""
        repo = self._plain_repo(tmp_path, monkeypatch)
        assert guard.classify_command(cmd, cwd=str(repo)) == []
        result = tier_u_gate.enforce_tier_u_gate(
            cmd, repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is True
        assert result.refusal_message is None

    @pytest.mark.parametrize(
        "cmd",
        [
            "pytest",
            "pytest -n auto",
            "pytest coordinator_core/",
            "pnpm run tier:fast",
            "bash scripts/run-tests.sh --tier fast",
            "bash run-suite.sh",
            "python dev.py test",
            "pytest coordinator_core/sub/test_x.py; bash run-suite.sh",
        ],
    )
    def test_unscoped_or_opaque_invocation_still_refuses(
        self, tmp_path, monkeypatch, cmd
    ):
        """Case (c) -- THE regression test for this narrowing. Every command
        here could plausibly be a full-suite run: a bare/unscoped runner
        invocation, or an opaque wrapper whose breadth cannot be read off
        its shape. Both classes are the fail-OPEN hole the 2026-07-28 PM
        ruling closed, and both must keep refusing with no declaration and
        no grant.

        The last case is the compound shape that defeats any deny-list of
        wrapper "tells": one positively-scoped segment does NOT launder an
        opaque second segment. ``run-suite.sh`` names neither a known runner
        nor the substring ``test``.
        """
        repo = self._plain_repo(tmp_path, monkeypatch)
        result = tier_u_gate.enforce_tier_u_gate(
            cmd, repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is False
        assert result.refusal_message

    def test_declared_fast_test_cmd_still_proceeds_via_the_declaration_path(
        self, tmp_path, monkeypatch
    ):
        """The R6 declaration exit is untouched by the narrowing: the
        verbatim configured ``fast_test_cmd`` -- an admitted Tier-U shape --
        still proceeds because the repo declared
        ``fast_tier_unscoped_reason``, NOT because of any footprint verdict.
        Asserted by measuring the footprint as UNPROVEN in the same test."""
        repo = _make_repo(tmp_path)
        unscoped_cmd = "pytest"
        (repo / "coordinator.local.md").write_text(
            "---\n"
            'fast_tier_unscoped_reason: "this repo has no scoped tier"\n'
            f'fast_test_cmd: "{unscoped_cmd}"\n'
            "---\n",
            encoding="utf-8",
        )
        monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
        assert (
            guard.classify_runner_footprint(unscoped_cmd, cwd=str(repo))
            == guard.RUNNER_FOOTPRINT_UNPROVEN
        )
        result = tier_u_gate.enforce_tier_u_gate(
            unscoped_cmd, repo_root=str(repo), session_id="s1"
        )
        assert result.proceed is True
        assert result.refusal_message is None

    def test_footprint_is_consulted_only_after_the_declaration(
        self, tmp_path, monkeypatch
    ):
        """Ordering contract: ``fast_tier_shape: unscoped`` still routes an
        unclassifiable command into the declaration-then-grant check even
        though its footprint says UNPROVEN, and ``fast_tier_shape: scoped``
        still proceeds -- the footprint leg narrows only the NO-declaration
        default and can never override a declaration in either direction."""
        monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
        opaque = "pnpm run tier:fast"

        (tmp_path / "a").mkdir()
        declared_unscoped = _make_repo(tmp_path / "a")
        (declared_unscoped / "coordinator.local.md").write_text(
            '---\nfast_tier_shape: "unscoped"\n---\n', encoding="utf-8"
        )
        result = tier_u_gate.enforce_tier_u_gate(
            opaque, repo_root=str(declared_unscoped), session_id="s1"
        )
        assert result.proceed is False
        assert "Tier U" in result.refusal_message

        (tmp_path / "b").mkdir()
        declared_scoped = _make_repo(tmp_path / "b")
        (declared_scoped / "coordinator.local.md").write_text(
            '---\nfast_tier_shape: "scoped"\n---\n', encoding="utf-8"
        )
        assert tier_u_gate.enforce_tier_u_gate(
            opaque, repo_root=str(declared_scoped), session_id="s1"
        ).proceed is True


class TestClassifyRunnerFootprint:
    """Direct coverage of the classifier-side helper the narrowing above
    consumes. Kept beside its only consumer's tests deliberately -- the
    verdicts are only meaningful as the gate's three cases."""

    @pytest.mark.parametrize(
        "cmd,expected",
        [
            ("true", guard.RUNNER_FOOTPRINT_NONE),
            ("exit 3", guard.RUNNER_FOOTPRINT_NONE),
            ("echo hello", guard.RUNNER_FOOTPRINT_NONE),
            ("", guard.RUNNER_FOOTPRINT_NONE),
            ("pytest sub/test_x.py", guard.RUNNER_FOOTPRINT_SCOPED),
            ("pytest -k expr && echo done", guard.RUNNER_FOOTPRINT_SCOPED),
            ("sh -c 'pytest sub/test_x.py'", guard.RUNNER_FOOTPRINT_SCOPED),
            ("pytest", guard.RUNNER_FOOTPRINT_UNPROVEN),
            ("npm test", guard.RUNNER_FOOTPRINT_UNPROVEN),
            ("go test ./...", guard.RUNNER_FOOTPRINT_UNPROVEN),
            ("sh -c 'pytest'", guard.RUNNER_FOOTPRINT_UNPROVEN),
            ("bash run-suite.sh", guard.RUNNER_FOOTPRINT_UNPROVEN),
            ("python dev.py test", guard.RUNNER_FOOTPRINT_UNPROVEN),
            ("pytest sub/test_x.py; bash run-suite.sh", guard.RUNNER_FOOTPRINT_UNPROVEN),
        ],
    )
    def test_verdicts(self, tmp_path, monkeypatch, cmd, expected):
        monkeypatch.delenv("COORDINATOR_FAST_TEST_CMD", raising=False)
        repo = _make_repo(tmp_path)
        assert guard.classify_runner_footprint(cmd, cwd=str(repo)) == expected

    def test_returns_data_never_a_decision(self):
        """Negative spec: three string verdicts, none of them a permission
        decision -- a caller cannot mistake the return for a bool."""
        for verdict in (
            guard.RUNNER_FOOTPRINT_NONE,
            guard.RUNNER_FOOTPRINT_SCOPED,
            guard.RUNNER_FOOTPRINT_UNPROVEN,
        ):
            assert isinstance(verdict, str)
            assert not isinstance(verdict, bool)


class TestClassifierMustNeverLearnTheDeclarationKey:
    def test_classifier_source_does_not_reference_fast_tier_unscoped_reason(self):
        """R7 hard prohibition, pinned structurally rather than by
        convention: the classifier answers 'what shape is this command';
        the caller (this module) answers 'is this caller authorized to run
        that shape here'. A classifier that references the declaration key
        has crossed that line.

        Review note (2026-07-28, code-reviewer finding 1): this test was
        briefly narrowed to ``inspect.getsource`` on 4 named functions
        (``_classify_command_core``, ``_classify_tokens``,
        ``classify_command``, ``classify_text``). That narrowing was NOT a
        Windows-portability fix -- it carried no separator/permission/
        subprocess concern -- and it silently shrank this test's blast
        radius: ``inspect.getsource(fn)`` returns only ``fn``'s own body,
        not the source of anything it calls, so the seven helper functions
        the enumerated functions delegate to (``_classify_pytest``,
        ``_classify_python_module``, ``_classify_package_manager``,
        ``_classify_js_runner``, ``_classify_cargo``, ``_classify_go``,
        ``_classify_make``) were no longer covered at all. A future
        violation of R7 inside any of those seven (or a new classifier
        helper nobody remembered to add to the enumeration) would have
        passed this test green.

        Restored to whole-module scanning (source minus module docstring,
        as originally written) with the three documented CALLER-AUTHORITY
        functions -- ``check()`` (the PreToolUse hard-deny entrypoint),
        ``_fast_tier_unscoped_declaration``, and
        ``_matches_declared_fast_test_cmd`` -- excluded BY NAME. R6
        legitimately reads the declaration in those three (see each
        function's own docstring, and ``check()``'s own inline comment:
        "The classifier itself (``_classify_command_core``, above) never
        reads this key ... the read lives here, at the caller-authority
        layer, not in the classifier").

        This exclude-by-name form is deliberately preferred over an
        enumerate-the-classifiers form: an exclusion list of known-
        legitimate caller-authority references FAILS SAFE -- a brand new
        classifier helper is covered automatically the instant it's
        added, and must be explicitly, visibly exempted here to escape
        coverage. The enumerate-the-classifiers form FAILS OPEN -- a new
        helper is silently uncovered unless someone remembers to add it to
        the list, which is exactly the shape of bug this test exists to
        prevent. Do not re-narrow this to a named-function enumeration
        under time pressure; the whole-module-minus-named-callers shape is
        the doctrinally-correct one and should stay that way even if a
        future caller-authority function needs adding to the exclusion
        list."""
        tree = ast.parse(inspect.getsource(guard))
        module_docstring = ast.get_docstring(tree) or ""
        full_source = inspect.getsource(guard)
        source_under_test = full_source.replace(module_docstring, "", 1)

        caller_authority_fns = [
            guard.check,
            guard._fast_tier_unscoped_declaration,
            guard._matches_declared_fast_test_cmd,
        ]
        for fn in caller_authority_fns:
            source_under_test = source_under_test.replace(
                inspect.getsource(fn), "", 1
            )

        assert "fast_tier_unscoped_reason" not in source_under_test, (
            "the classifier module references the R6 declaration key "
            "outside the documented caller-authority functions "
            "(check(), _fast_tier_unscoped_declaration, "
            "_matches_declared_fast_test_cmd) -- this belongs at the "
            "caller-authority layer, never in a classifier."
        )

    def test_classifier_does_not_import_the_declaration_reader(self):
        """True-on-current-disk strengthening: the classifier module binds
        no name from either the module that implements the R6 declaration
        exit (``tier_u_gate``) or the frontmatter reader it uses
        (``resolve_validation_cmd``). Import-absence is a cheaper, harder
        guard than a substring check alone -- a future edit that imports
        either module (even without referencing the key by name, e.g. via
        ``getattr``) trips this before it could launder the declaration in
        by indirection."""
        assert "tier_u_gate" not in guard.__dict__
        assert "resolve_validation_cmd" not in guard.__dict__
        assert not any(
            "tier_u_gate" in (getattr(mod, "__name__", "") or "")
            or "resolve_validation_cmd" in (getattr(mod, "__name__", "") or "")
            for mod in vars(guard).values()
            if inspect.ismodule(mod)
        )
