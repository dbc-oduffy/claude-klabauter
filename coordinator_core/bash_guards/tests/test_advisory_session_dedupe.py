"""Tests for the per-session, per-(guard,shape) advisory dedupe (item 7,
state/handoffs/2026-07-30-boot-context-bloat-non-orientation-surfaces.md;
baseline: state/audits/2026-08-14-boot-payload-baseline.md § "Item 7 --
bash-spawn guard advisories").

Exercises `dispatch.evaluate_payload_json`'s advisory-return seam end-to-end,
isolating a single fake `GuardEntry` via `dispatch._build_guard_chain`
monkeypatched -- the same isolation technique `test_advisory_value_host_
default.py` and `test_advisory_fire_counter.py` already established -- plus
unit coverage of `_advisory_dedupe` itself for the fail-open branches that
are awkward to induce through the full dispatcher (an unwritable dedupe
directory, a raising fingerprint).
"""

from __future__ import annotations

import os
import sys

import pytest

from coordinator_core.bash_guards import _advisory_dedupe, dispatch, guard_plumbing_and_loops
from coordinator_core.bash_guards._advisory_value import AdvisoryValue
from coordinator_core.bash_guards.dispatch import GuardBand, GuardEntry

_ADVISORY_ENVELOPE = {
    "hookSpecificOutput": {
        "permissionDecision": "allow",
        "permissionDecisionReason": "advisory note",
        "additionalContext": "BASH-SPAWN ADVISORY (non-blocking): shape X.",
    }
}

_ADVISORY_ENVELOPE_OTHER_SHAPE = {
    "hookSpecificOutput": {
        "permissionDecision": "allow",
        "permissionDecisionReason": "advisory note",
        "additionalContext": "BASH-SPAWN ADVISORY (non-blocking): shape Y.",
    }
}

_DENY_ENVELOPE = {
    "hookSpecificOutput": {
        "permissionDecision": "deny",
        "permissionDecisionReason": "hard deny",
    }
}


def _payload(cmd="echo probe", session_id="sess-dedupe", cwd="/tmp"):
    return {
        "tool_name": "Bash",
        "tool_input": {"command": cmd},
        "session_id": session_id,
        "cwd": cwd,
    }


def _advisory_entry(name, envelope):
    return GuardEntry(
        name,
        lambda: dict(envelope),
        False,
        GuardBand.ADVISORY_REWRITE,
        AdvisoryValue.HOST_INDEPENDENT,
    )


def _deny_entry(name="fake-hard-deny-guard"):
    return GuardEntry(
        name,
        lambda: dict(_DENY_ENVELOPE),
        True,
        GuardBand.CONFINEMENT_DENY,
        AdvisoryValue.NOT_COST_ARGUED,
    )


class TestFirstFiringDeliversFullText:
    def test_first_call_returns_envelope(self, tmp_path, monkeypatch):
        entry = _advisory_entry("fake-guard", _ADVISORY_ENVELOPE)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        payload = _payload(cwd=str(tmp_path))
        out = dispatch.evaluate_payload_json(__import__("json").dumps(payload))

        assert out == _ADVISORY_ENVELOPE


class TestSecondIdenticalFiringSuppressed:
    def test_same_session_same_shape_second_call_falls_open_no_cue_text(self, tmp_path, monkeypatch):
        """`_ADVISORY_ENVELOPE` carries no cue word (no "Use instead"/
        "Example:"/bare "instead"), so its terse alternative cannot be
        isolated -- degradation fails open to the FULL envelope rather than
        silence (module docstring, "FAIL OPEN, UNCONDITIONALLY"). See
        `TestDegradeNotSilence` for the case where a cue word IS present.
        """
        entry = _advisory_entry("fake-guard", _ADVISORY_ENVELOPE)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        payload = _payload(cwd=str(tmp_path))
        raw = __import__("json").dumps(payload)
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        assert first == _ADVISORY_ENVELOPE
        assert second == _ADVISORY_ENVELOPE


class TestDifferentShapeStillFires:
    def test_same_guard_different_text_both_fire(self, tmp_path, monkeypatch):
        calls = [dict(_ADVISORY_ENVELOPE), dict(_ADVISORY_ENVELOPE_OTHER_SHAPE)]

        def _chain(*a, **k):
            envelope = calls.pop(0)
            return [GuardEntry(
                "fake-guard",
                lambda envelope=envelope: dict(envelope),
                False,
                GuardBand.ADVISORY_REWRITE,
                AdvisoryValue.HOST_INDEPENDENT,
            )]

        monkeypatch.setattr(dispatch, "_build_guard_chain", _chain)
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        payload = _payload(cwd=str(tmp_path))
        raw = __import__("json").dumps(payload)
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        assert first == _ADVISORY_ENVELOPE
        assert second == _ADVISORY_ENVELOPE_OTHER_SHAPE


class TestNewSessionFiresAgain:
    def test_different_session_id_not_suppressed(self, tmp_path, monkeypatch):
        entry = _advisory_entry("fake-guard", _ADVISORY_ENVELOPE)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        import json

        first = dispatch.evaluate_payload_json(
            json.dumps(_payload(session_id="sess-a", cwd=str(tmp_path)))
        )
        second = dispatch.evaluate_payload_json(
            json.dumps(_payload(session_id="sess-b", cwd=str(tmp_path)))
        )

        assert first == _ADVISORY_ENVELOPE
        assert second == _ADVISORY_ENVELOPE


class TestBlockNeverSuppressed:
    def test_hard_deny_fires_every_time(self, tmp_path, monkeypatch):
        entry = _deny_entry()
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        import json

        raw = json.dumps(_payload(cwd=str(tmp_path)))
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        assert first["hookSpecificOutput"]["permissionDecision"] == "deny"
        assert second["hookSpecificOutput"]["permissionDecision"] == "deny"


class TestFailOpenPaths:
    def test_no_session_id_never_suppresses(self, tmp_path, monkeypatch):
        entry = _advisory_entry("fake-guard", _ADVISORY_ENVELOPE)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        import json

        raw = json.dumps(_payload(session_id="", cwd=str(tmp_path)))
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        assert first == _ADVISORY_ENVELOPE
        assert second == _ADVISORY_ENVELOPE

    def test_unresolvable_gitdir_never_suppresses(self, tmp_path, monkeypatch):
        entry = _advisory_entry("fake-guard", _ADVISORY_ENVELOPE)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: None)

        import json

        raw = json.dumps(_payload(cwd=str(tmp_path)))
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        assert first == _ADVISORY_ENVELOPE
        assert second == _ADVISORY_ENVELOPE

    def test_dedupe_key_raises_never_suppresses(self, tmp_path, monkeypatch):
        entry = _advisory_entry("fake-guard", _ADVISORY_ENVELOPE)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        def _boom(*a, **k):
            raise RuntimeError("boom")

        monkeypatch.setattr(dispatch, "_advisory_dedupe_key", _boom)

        import json

        raw = json.dumps(_payload(cwd=str(tmp_path)))
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        assert first == _ADVISORY_ENVELOPE
        assert second == _ADVISORY_ENVELOPE

    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits only")
    def test_unwritable_dedupe_dir_never_suppresses(self, tmp_path, monkeypatch):
        entry = _advisory_entry("fake-guard", _ADVISORY_ENVELOPE)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        (tmp_path / "advisory-dedupe").mkdir()
        os.chmod(tmp_path / "advisory-dedupe", 0o000)
        try:
            import json

            raw = json.dumps(_payload(cwd=str(tmp_path)))
            first = dispatch.evaluate_payload_json(raw)
            second = dispatch.evaluate_payload_json(raw)

            assert first == _ADVISORY_ENVELOPE
            assert second == _ADVISORY_ENVELOPE
        finally:
            os.chmod(tmp_path / "advisory-dedupe", 0o755)


class TestAdvisoryDedupeKeyUnit:
    def test_none_when_no_additional_context(self):
        assert _advisory_dedupe.advisory_dedupe_key("g", {"hookSpecificOutput": {}}) is None

    def test_none_on_non_dict_envelope(self):
        assert _advisory_dedupe.advisory_dedupe_key("g", None) is None
        assert _advisory_dedupe.advisory_dedupe_key("g", "not-a-dict") is None

    def test_same_text_same_key(self):
        env = {"hookSpecificOutput": {"additionalContext": "same text"}}
        a = _advisory_dedupe.advisory_dedupe_key("g", env)
        b = _advisory_dedupe.advisory_dedupe_key("g", dict(env))
        assert a == b

    def test_different_guard_name_different_key(self):
        env = {"hookSpecificOutput": {"additionalContext": "same text"}}
        a = _advisory_dedupe.advisory_dedupe_key("g1", env)
        b = _advisory_dedupe.advisory_dedupe_key("g2", env)
        assert a != b

    def test_same_shape_different_command_same_key(self):
        """Finding 1 -- two firings of the SAME shape against DIFFERENT
        commands (the `_generic_advisory` `Command:` line varying) must
        collide onto one dedupe key, or the HEAD_TAIL_PLUMBING/FOR_LOOP/
        WHILE_READ_LOOP family never dedupes on repeated shape at all.
        """
        env_a = {
            "hookSpecificOutput": {
                "additionalContext": (
                    "BASH-SPAWN ADVISORY (non-blocking): `for-loop`-shaped "
                    "command spawns a subprocess per iteration/pipe stage.\n\n"
                    "  Command:  for f in *.py; do wc -l $f; done\n\n"
                    "Use instead: a single in-process pass\n"
                    "  Example:  python3 -c '...'\n"
                )
            }
        }
        env_b = {
            "hookSpecificOutput": {
                "additionalContext": (
                    "BASH-SPAWN ADVISORY (non-blocking): `for-loop`-shaped "
                    "command spawns a subprocess per iteration/pipe stage.\n\n"
                    "  Command:  for f in *.txt; do cat $f; done\n\n"
                    "Use instead: a single in-process pass\n"
                    "  Example:  python3 -c '...'\n"
                )
            }
        }
        a = _advisory_dedupe.advisory_dedupe_key("guard-plumbing-and-loops", env_a)
        b = _advisory_dedupe.advisory_dedupe_key("guard-plumbing-and-loops", env_b)
        assert a == b

    def test_genuinely_different_shape_different_key(self):
        """The other half of Finding 1's required coverage: two DIFFERENT
        shapes from the same guard must still mint different keys and both
        fire -- normalization must not over-collapse.
        """
        env_for_loop = {
            "hookSpecificOutput": {
                "additionalContext": (
                    "BASH-SPAWN ADVISORY (non-blocking): `for-loop`-shaped "
                    "command spawns a subprocess per iteration/pipe stage.\n\n"
                    "  Command:  for f in *.py; do wc -l $f; done\n\n"
                    "Use instead: a single in-process pass\n"
                    "  Example:  python3 -c '...'\n"
                )
            }
        }
        env_while_read = {
            "hookSpecificOutput": {
                "additionalContext": (
                    "BASH-SPAWN ADVISORY (non-blocking): `while-read`-shaped "
                    "command spawns a subprocess per iteration/pipe stage.\n\n"
                    "  Command:  while read -r line; do echo $line; done < f\n\n"
                    "Use instead: a single in-process pass\n"
                    "  Example:  python3 -c '...'\n"
                )
            }
        }
        a = _advisory_dedupe.advisory_dedupe_key("guard-plumbing-and-loops", env_for_loop)
        b = _advisory_dedupe.advisory_dedupe_key("guard-plumbing-and-loops", env_while_read)
        assert a != b

    def test_platform_verdict_style_context_unaffected(self):
        """`_platform_verdict.platform_verdict_for_shape` never echoes the
        command, so normalization must be a no-op there -- two DIFFERENT
        commands producing the SAME (command-free) advisory text still
        collide onto one key, exactly as before Finding 1's fix.
        """
        env = {
            "hookSpecificOutput": {
                "additionalContext": (
                    "BASH-SPAWN ADVISORY (non-blocking): this command matches "
                    "the `grep-via-bash` shape, one of this fleet's "
                    "per-process cold-start-cost drivers on Windows; consider "
                    "in-process search here too so behavior stays consistent "
                    "across the fleet.\n\n"
                    "  Example:  rg -n foo\n"
                )
            }
        }
        a = _advisory_dedupe.advisory_dedupe_key("guard-grep-via-bash", dict(env))
        b = _advisory_dedupe.advisory_dedupe_key("guard-grep-via-bash", dict(env))
        assert a == b


class TestRealBuilderRoundTrip:
    """Every existing dedupe test hand-writes the `Command:`-labeled
    envelope shape rather than calling a real builder -- a relabel at the
    builder site (e.g. `Command:` -> `Cmd:`) would silently revert dedupe
    to command-instance keying with every hand-written test still green.
    This exercises the actual shipped builder
    (`guard_plumbing_and_loops._generic_advisory`) so the wire path from
    builder output to `advisory_dedupe_key` is covered by something that
    would actually break on a relabel.
    """

    def test_generic_advisory_same_shape_different_command_collides(self):
        payload = {"session_id": "round-trip-sess"}
        env_a = guard_plumbing_and_loops._generic_advisory(
            "for-loop", "for f in *.py; do wc -l $f; done", "a single in-process pass",
            "python3 -c '...'", payload,
        )
        env_b = guard_plumbing_and_loops._generic_advisory(
            "for-loop", "for f in *.txt; do cat $f; done", "a single in-process pass",
            "python3 -c '...'", payload,
        )

        key_a = _advisory_dedupe.advisory_dedupe_key("guard-plumbing-and-loops", env_a)
        key_b = _advisory_dedupe.advisory_dedupe_key("guard-plumbing-and-loops", env_b)

        assert key_a is not None
        assert key_a == key_b

    def test_generic_advisory_different_shape_does_not_collide(self):
        payload = {"session_id": "round-trip-sess"}
        env_for_loop = guard_plumbing_and_loops._generic_advisory(
            "for-loop", "for f in *.py; do wc -l $f; done", "a single in-process pass",
            "python3 -c '...'", payload,
        )
        env_while_read = guard_plumbing_and_loops._generic_advisory(
            "while-read-loop", "while read -r line; do echo $line; done < f",
            "a single in-process pass", "python3 -c '...'", payload,
        )

        key_for_loop = _advisory_dedupe.advisory_dedupe_key("guard-plumbing-and-loops", env_for_loop)
        key_while_read = _advisory_dedupe.advisory_dedupe_key("guard-plumbing-and-loops", env_while_read)

        assert key_for_loop != key_while_read


class TestSweepStaleSessionDirs:
    def test_old_sibling_removed_current_kept(self, tmp_path):
        gitdir = tmp_path
        _advisory_dedupe.mark_advised(gitdir, "current-sess", "guard__aaa")
        old_dir = gitdir / "advisory-dedupe" / "old-sess"
        old_dir.mkdir(parents=True)
        (old_dir / "guard__bbb").touch()
        old_time = __import__("time").time() - (60 * 60 * 60)
        os.utime(old_dir / "guard__bbb", (old_time, old_time))
        os.utime(old_dir, (old_time, old_time))

        _advisory_dedupe._sweep_stale_session_dirs(gitdir, "current-sess")

        assert not old_dir.exists()
        assert (gitdir / "advisory-dedupe" / "current-sess" / "guard__aaa").exists()


class TestSweepThrottle:
    """The sweep must not run on every `mark_advised` call, and the
    throttle clock must be governed by a dedicated sentinel that ordinary
    session-directory creation never bumps."""

    def test_first_call_sweeps_once_then_throttles(self, tmp_path, monkeypatch):
        """Nothing to throttle yet on a brand-new root -- the very first
        `mark_advised` call performs the sweep (fail-open on a missing
        sentinel), and only THEN does the throttle window start."""
        gitdir = tmp_path
        calls = []
        real_sweep = _advisory_dedupe._sweep_stale_session_dirs

        def _spy(gd, sess):
            calls.append(1)
            return real_sweep(gd, sess)

        monkeypatch.setattr(_advisory_dedupe, "_sweep_stale_session_dirs", _spy)

        _advisory_dedupe.mark_advised(gitdir, "sess-a", "guard__aaa")
        assert calls == [1]

        _advisory_dedupe.mark_advised(gitdir, "sess-a", "guard__bbb")
        _advisory_dedupe.mark_advised(gitdir, "sess-a", "guard__ccc")

        assert calls == [1], "sweep re-ran before the throttle interval elapsed"

    def test_sweep_not_starved_by_concurrent_sibling_session_creation(self, tmp_path, monkeypatch):
        """Regression for the anti-correlated throttle: minting brand-new
        sibling session directories (continuous traffic under this repo's
        own 50-70-concurrent-session load norm) must never itself reset the
        throttle clock -- only the dedicated `_LAST_SWEEP_SENTINEL` file
        does. This is the exact scenario the old root-mtime-keyed throttle
        got wrong: a burst of new sessions kept bumping `root`'s own mtime,
        so the root was never stale enough to sweep."""
        gitdir = tmp_path
        root = gitdir / "advisory-dedupe"
        calls = []
        real_sweep = _advisory_dedupe._sweep_stale_session_dirs

        def _spy(gd, sess):
            calls.append(1)
            return real_sweep(gd, sess)

        monkeypatch.setattr(_advisory_dedupe, "_sweep_stale_session_dirs", _spy)

        _advisory_dedupe.mark_advised(gitdir, "sess-0", "guard__zzz")
        assert calls == [1]

        old_time = __import__("time").time() - (60 * 60)
        sentinel = root / _advisory_dedupe._LAST_SWEEP_SENTINEL
        os.utime(sentinel, (old_time, old_time))

        for i in range(5):
            _advisory_dedupe.mark_advised(gitdir, "sess-new-%d" % i, "guard__%d" % i)

        assert calls == [1, 1], "sweep starved by concurrent sibling session creation"

    def test_old_root_gets_swept_and_resets_clock(self, tmp_path, monkeypatch):
        gitdir = tmp_path
        root = gitdir / "advisory-dedupe"
        # Pre-create the session's own dir so `mark_advised`'s `mkdir` is a
        # no-op under `root` -- otherwise creating a NEW entry under `root`
        # bumps `root`'s own mtime to "now" before the throttle check runs,
        # which is correct self-throttling behavior but would defeat this
        # test's attempt to force a stale-root sweep.
        (root / "sess-a").mkdir(parents=True)
        old_time = __import__("time").time() - (60 * 60)
        os.utime(root, (old_time, old_time))

        calls = []
        real_sweep = _advisory_dedupe._sweep_stale_session_dirs

        def _spy(gd, sess):
            calls.append(1)
            return real_sweep(gd, sess)

        monkeypatch.setattr(_advisory_dedupe, "_sweep_stale_session_dirs", _spy)

        _advisory_dedupe.mark_advised(gitdir, "sess-a", "guard__aaa")
        assert calls == [1]

        _advisory_dedupe.mark_advised(gitdir, "sess-a", "guard__bbb")
        assert calls == [1], "sweep re-ran before the throttle interval elapsed again"

    def test_current_session_never_reaped_regardless_of_throttle(self, tmp_path):
        """Property the reviewer verified sound must survive the throttle
        change: the current session's own directory is never reaped."""
        gitdir = tmp_path
        root = gitdir / "advisory-dedupe"
        root.mkdir(parents=True)
        old_time = __import__("time").time() - (60 * 60)
        os.utime(root, (old_time, old_time))

        _advisory_dedupe.mark_advised(gitdir, "current-sess", "guard__aaa")

        assert (root / "current-sess" / "guard__aaa").exists()


_ADVISORY_ENVELOPE_WITH_ALT = {
    "hookSpecificOutput": {
        "permissionDecision": "allow",
        "permissionDecisionReason": "advisory note",
        "additionalContext": (
            "BASH-SPAWN ADVISORY (non-blocking): shape X spawns a "
            "subprocess per iteration.\n\n"
            "Use instead: a single in-process pass\n"
            "  Example:  python3 -c '...'\n"
        ),
    }
}


class TestDegradeNotSilence:
    """A repeat firing must degrade to the terse alternative, never fall
    fully silent -- and the (guard, shape) slot returned must still be the
    higher-precedence guard's, not a lower one that used to win it via the
    old `continue`."""

    def test_repeat_firing_returns_alternative_not_prose(self, tmp_path, monkeypatch):
        entry = _advisory_entry("fake-guard", _ADVISORY_ENVELOPE_WITH_ALT)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        payload = _payload(cwd=str(tmp_path))
        raw = __import__("json").dumps(payload)
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        first_ctx = first["hookSpecificOutput"]["additionalContext"]
        second_ctx = second["hookSpecificOutput"]["additionalContext"]

        assert "spawns a subprocess" in first_ctx
        assert second is not None
        assert "spawns a subprocess" not in second_ctx
        assert "Use instead" in second_ctx
        assert len(second_ctx) < len(first_ctx)

    def test_two_guard_chain_deduped_first_still_wins_slot(self, tmp_path, monkeypatch):
        entry_a = _advisory_entry("guard-a-higher-precedence", _ADVISORY_ENVELOPE_WITH_ALT)
        entry_b = _advisory_entry("guard-b-lower-precedence", _ADVISORY_ENVELOPE_OTHER_SHAPE)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry_a, entry_b])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        payload = _payload(cwd=str(tmp_path))
        raw = __import__("json").dumps(payload)
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        first_ctx = first["hookSpecificOutput"]["additionalContext"]
        second_ctx = second["hookSpecificOutput"]["additionalContext"]

        assert "Use instead" in first_ctx
        # guard-b (lower precedence) must NOT win the slot on the repeat --
        # its distinguishing text ("shape Y") must never appear.
        assert "shape Y" not in second_ctx
        assert "Use instead" in second_ctx


class TestSessionIdValidation:
    """`session_id` is used directly as a path component -- reject anything
    outside the safe charset rather than trusting it verbatim."""

    def test_valid_ids_accepted(self):
        assert _advisory_dedupe._valid_session_id("abc-DEF_123.456") is True
        assert _advisory_dedupe._valid_session_id("sess-dedupe") is True

    def test_traversal_and_separator_shaped_ids_rejected(self):
        assert _advisory_dedupe._valid_session_id("../escape") is False
        assert _advisory_dedupe._valid_session_id("a/b") is False
        assert _advisory_dedupe._valid_session_id("a\\b") is False
        assert _advisory_dedupe._valid_session_id("") is False

    def test_already_advised_false_for_invalid_session_id(self, tmp_path):
        assert _advisory_dedupe.already_advised(tmp_path, "../escape", "guard__aaa") is False

    def test_mark_advised_no_op_for_invalid_session_id(self, tmp_path):
        gitdir = tmp_path
        _advisory_dedupe.mark_advised(gitdir, "../escape", "guard__aaa")

        # No write anywhere -- specifically, nothing escapes the intended
        # `advisory-dedupe` subtree.
        assert not (gitdir.parent / "escape").exists()
        dedupe_root = gitdir / "advisory-dedupe"
        assert not dedupe_root.exists() or not any(dedupe_root.rglob("guard__aaa"))


class TestTheRewriteBlockDoesNotReKeyTheShape:
    """The `Example:` rewrite block is the SECOND inlining of the operator's
    command, so it varies per invocation and — before this — landed in the
    hash, giving a fresh key per firing and leaving dedupe inert for exactly
    the guard family the `Command:`-line strip was written to rescue.

    Origin: cross-repo/archive/2026-08-18-doe-claude-em-advisory-dedupe-inert.md,
    measured off 28 sessions' `.git/advisory-dedupe/` markers.
    """

    @staticmethod
    def _ctx(cmd: str) -> dict:
        return {
            "hookSpecificOutput": {
                "additionalContext": (
                    "This shape spawns a subprocess per iteration.\n\n"
                    f"Command: {cmd}\n\n"
                    f"Example: rewrite `{cmd}` as a single bounded call\n\n"
                    "See the wiki for this guard's override keys.\n"
                )
            }
        }

    def test_two_commands_of_one_shape_share_a_key(self):
        a = _advisory_dedupe.advisory_dedupe_key("g", self._ctx("grep -r foo ."))
        b = _advisory_dedupe.advisory_dedupe_key("g", self._ctx("grep -r bar /other/path"))

        assert a is not None
        assert a == b

    def test_a_different_explanation_still_keys_differently(self):
        shape_a = _advisory_dedupe.advisory_dedupe_key("g", self._ctx("grep -r foo ."))
        other = {
            "hookSpecificOutput": {
                "additionalContext": (
                    "This shape rewrites history irreversibly.\n\n"
                    "Command: grep -r foo .\n\n"
                    "Example: rewrite `grep -r foo .` as a single bounded call\n\n"
                    "See the wiki for this guard's override keys.\n"
                )
            }
        }

        assert shape_a != _advisory_dedupe.advisory_dedupe_key("g", other)

    def test_a_builder_that_echoes_nothing_still_keys(self):
        envelope = {
            "hookSpecificOutput": {"additionalContext": "No command echoed at all."}
        }

        assert _advisory_dedupe.advisory_dedupe_key("g", envelope) is not None
