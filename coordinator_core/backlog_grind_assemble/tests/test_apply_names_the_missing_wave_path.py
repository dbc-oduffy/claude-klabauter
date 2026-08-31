"""A bug-blitz apply with no `--wave-path` says so, instead of halting mute.

bug-blitz's standing commit-readiness JP carries one disposition with an empty
`resolves`; `_wire_single_disposition_resolves` fills it from the directives
that `depends_on` it, and those exist only when `--wave-path` was supplied.
Without one the run halts reporting a bare JP id — a report indistinguishable
from the pre-`adb36b820d` defect where no `--decisions` value could open the
gate. A 2026-08-31 live run was read that way and filed as a regression against
a tree that carries the fix.

Origin: cross-repo/inbox/2026-08-31-doe-claude-em-blitz-apply-verb-emits-no-
commit-directive.md.
"""

from coordinator_core.backlog_grind_assemble import apply as apply_mod


def _run(argv, monkeypatch):
    calls = {}

    def _fake_apply(cadence, **kwargs):
        calls["cadence"] = cadence
        calls["extra_directives"] = kwargs.get("extra_directives")
        return 0, {"landed": []}

    monkeypatch.setattr(apply_mod, "apply", _fake_apply)
    rc = apply_mod.main_apply(argv)
    return rc, calls


def test_a_bug_blitz_apply_without_wave_path_names_the_omission(monkeypatch, capsys):
    _run(["bug-blitz"], monkeypatch)

    err = capsys.readouterr().err
    assert "--wave-path" in err
    assert "nothing to resolve" in err


def test_the_notice_does_not_fire_when_a_wave_path_is_given(monkeypatch, capsys):
    # Runs against this repo (path validation resolves a real git root); the
    # dispatch itself is stubbed, so nothing is staged or committed.
    _run(
        ["bug-blitz", "--wave-path", "README.md", "--granularity", "per-wave",
         "--message", "m"],
        monkeypatch,
    )

    assert "nothing to resolve" not in capsys.readouterr().err


def test_the_notice_is_scoped_to_bug_blitz(monkeypatch, capsys):
    _run(["mise-en-place"], monkeypatch)

    assert "nothing to resolve" not in capsys.readouterr().err
