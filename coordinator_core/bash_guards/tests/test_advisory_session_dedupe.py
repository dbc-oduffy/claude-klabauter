
from __future__ import annotations

import os

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
    def test_same_session_same_shape_second_call_is_silent(self, tmp_path, monkeypatch):
        """R6 (2026-09-26): a repeat firing of an "allow" advisory puts NO
        TEXT in context at all -- `_ADVISORY_ENVELOPE`'s `permissionDecision`
        is `"allow"`, so the second call strips `additionalContext` entirely
        while `permissionDecision` (and any other non-text field) survives.
        See `TestDegradeNotSilence` for the case where `updatedInput`
        survives, and the fully-empty-`{}` collapse case.
        """
        entry = _advisory_entry("fake-guard", _ADVISORY_ENVELOPE)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        payload = _payload(cwd=str(tmp_path))
        raw = __import__("json").dumps(payload)
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        assert first == _ADVISORY_ENVELOPE
        assert "additionalContext" not in second["hookSpecificOutput"]
        assert second["hookSpecificOutput"]["permissionDecision"] == "allow"


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

    def test_unwritable_dedupe_dir_never_suppresses(self, tmp_path, monkeypatch):
        """Simulates an unwritable dedupe dir via a monkeypatched `mark_advised`
        rather than POSIX permission bits (`os.chmod(..., 0o000)`) -- running
        as root (or, historically, on Windows) bypasses DAC permission bits
        entirely, so a chmod-based simulation silently stops testing anything
        under root: `mark_advised` succeeds despite the chmod, the marker is
        written, and the SECOND call is genuinely deduped rather than falling
        open. Patching the write call itself is fail-open-correct on every
        platform and every caller uid."""
        entry = _advisory_entry("fake-guard", _ADVISORY_ENVELOPE)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)
        monkeypatch.setattr(dispatch, "_mark_advised", lambda *a, **k: None)

        import json

        raw = json.dumps(_payload(cwd=str(tmp_path)))
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        assert first == _ADVISORY_ENVELOPE
        assert second == _ADVISORY_ENVELOPE


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

    def test_first_call_sweeps_once_then_throttles(self, tmp_path, monkeypatch):
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


_ADVISORY_ENVELOPE_ASK = {
    "hookSpecificOutput": {
        "permissionDecision": "ask",
        "additionalContext": "BASH-SPAWN ADVISORY (ask): shape X.",
    }
}

_ADVISORY_ENVELOPE_CONTEXT_ONLY = {
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "additionalContext": "BASH-SPAWN ADVISORY (non-blocking): shape X.",
    }
}

_ADVISORY_ENVELOPE_WITH_REWRITE = {
    "hookSpecificOutput": {
        "permissionDecision": "allow",
        "updatedInput": {"command": "rewritten command"},
        "additionalContext": (
            "BASH-SPAWN ADVISORY (non-blocking): shape X spawns a "
            "subprocess per iteration.\n\n"
            "Use instead: a single in-process pass\n"
        ),
    }
}


class TestDegradeNotSilence:
    """R6 (2026-09-26): a repeat non-blocking advisory on an allowed call
    puts NO TEXT in context -- `additionalContext` is stripped rather than
    shortened to a terse alternative. Class name kept (not renamed to avoid
    perturbing an unrelated node-id a peer chunk might cite), but every test
    body now asserts silence, not degrade-to-terse."""

    def test_repeat_firing_returns_alternative_not_prose(self, tmp_path, monkeypatch):
        """Rewritten (R6) to assert SILENCE, not a shortened alternative:
        the repeat firing carries no `additionalContext` at all."""
        entry = _advisory_entry("fake-guard", _ADVISORY_ENVELOPE_WITH_ALT)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        payload = _payload(cwd=str(tmp_path))
        raw = __import__("json").dumps(payload)
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        first_ctx = first["hookSpecificOutput"]["additionalContext"]
        assert "spawns a subprocess" in first_ctx
        assert "additionalContext" not in second["hookSpecificOutput"]
        assert second["hookSpecificOutput"]["permissionDecision"] == "allow"

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

        assert "Use instead" in first_ctx
        # The higher-precedence guard's slot is still won on the repeat --
        # guard-b's ("shape Y") text never appears, even though guard-b
        # never fired before this session.
        assert "additionalContext" not in second["hookSpecificOutput"]
        assert second["hookSpecificOutput"]["permissionDecision"] == "allow"

    def test_ask_decision_repeat_keeps_full_text(self, tmp_path, monkeypatch):
        """`"ask"` is exempt from silencing -- a repeat `ask` still needs its
        full text to make sense of the prompt."""
        entry = _advisory_entry("fake-guard-ask", _ADVISORY_ENVELOPE_ASK)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        payload = _payload(cwd=str(tmp_path))
        raw = __import__("json").dumps(payload)
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        assert first == _ADVISORY_ENVELOPE_ASK
        assert second == _ADVISORY_ENVELOPE_ASK

    def test_repeat_with_nothing_left_but_hookeventname_collapses_to_no_advisory(
        self, tmp_path, monkeypatch
    ):
        """A `context_only`-shaped envelope (no `permissionDecision`, no
        `updatedInput`) carries nothing besides `hookEventName` once
        `additionalContext` is stripped, so the repeat collapses all the way
        to `{}` (`no_advisory()`'s own shape) -- still RETURNED, never
        `continue`d."""
        entry = _advisory_entry("fake-guard-context-only", _ADVISORY_ENVELOPE_CONTEXT_ONLY)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        payload = _payload(cwd=str(tmp_path))
        raw = __import__("json").dumps(payload)
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        assert first == _ADVISORY_ENVELOPE_CONTEXT_ONLY
        assert second == {}

    def test_repeat_with_rewrite_and_context_keeps_rewrite_drops_context(
        self, tmp_path, monkeypatch
    ):
        """A repeat carrying both `updatedInput` and `additionalContext`
        returns the envelope, with `additionalContext` stripped and
        `updatedInput` intact -- never collapsed to `no_advisory()`."""
        entry = _advisory_entry("fake-guard-rewrite", _ADVISORY_ENVELOPE_WITH_REWRITE)
        monkeypatch.setattr(dispatch, "_build_guard_chain", lambda *a, **k: [entry])
        monkeypatch.setattr(dispatch, "_resolve_gitdir_for_dedupe", lambda cwd: tmp_path)

        payload = _payload(cwd=str(tmp_path))
        raw = __import__("json").dumps(payload)
        first = dispatch.evaluate_payload_json(raw)
        second = dispatch.evaluate_payload_json(raw)

        assert first == _ADVISORY_ENVELOPE_WITH_REWRITE
        assert second != {}
        assert "additionalContext" not in second["hookSpecificOutput"]
        assert second["hookSpecificOutput"]["updatedInput"] == {
            "command": "rewritten command"
        }
        assert second["hookSpecificOutput"]["permissionDecision"] == "allow"


class TestSessionIdValidation:

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

        assert not (gitdir.parent / "escape").exists()
        dedupe_root = gitdir / "advisory-dedupe"
        assert not dedupe_root.exists() or not any(dedupe_root.rglob("guard__aaa"))


class TestTheRewriteBlockDoesNotReKeyTheShape:

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
