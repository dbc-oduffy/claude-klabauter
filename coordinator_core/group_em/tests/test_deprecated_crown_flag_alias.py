"""The pre-2026-09-01 `--crown-session-id` spelling still parses, on both entry points.

WHY THIS IS A TRIPWIRE AND NOT HOUSEKEEPING. The PM retired "crown" from coordinator
vocabulary on 2026-09-01, and the rename is otherwise total: identifiers, docstrings,
test names, and the report's own rendered tokens all moved to Group-EM. The CLI flag is
the single exception, and it is an exception because it is the only one of them that is
a HARD PARSE BOUNDARY -- `argparse` exits 2 on an unknown option, before any handler runs.

The callers are not ours. DoE-claude's `coordinator/agents/fleet-watch.md` and
`coordinator/skills/group-em/SKILL.md` both instruct dispatched agents to pass
`--crown-session-id`, and those agents are running right now against a repo whose text we
do not own and cannot land a change into. A same-commit rename strands every live watcher:
the watch stops arming, and a watch that never arms is -- by that subsystem's own finding
-- indistinguishable from a quiet fleet. The memo asking DoE-claude to move their text is
`cross-repo/archive/2026-09-01-claude-klabauter-em-crown-nomenclature-retired.md`; this alias
is what makes that memo a courtesy rather than a deadline.

NEGATIVE SPEC:
  - This does NOT keep "crown" alive as vocabulary. The alias is hidden from `--help` on
    both parsers, and no other surface in this package accepts the old spelling.
  - This is NOT permanent. Delete this file and both alias legs once DoE-claude's text has
    moved. That is a deliberate, separately-decided cutover, which is exactly why it needs
    a test -- so the decision is made rather than made silently by a tidy-up.
  - Asserting the flag PARSES is not enough on its own, so the first case asserts the value
    reaches the handler. An alias parsed into the wrong dest exits 0, arms nothing on the
    Group-EM's behalf, and reports success -- strictly worse than refusing.

Both entry points build their parser inside `_cli`, so every case here goes through argv.
That is also the level DoE-claude's agents actually reach us at.

Run:
    pytest coordinator_core/group_em/tests/test_deprecated_crown_flag_alias.py -v
"""
from __future__ import annotations

import pytest

from coordinator_core.group_em import idle_report
from coordinator_core.group_em import watch

_SID = "group-em-1"
_SPELLINGS = ["--group-em-session-id", "--crown-session-id"]


@pytest.mark.parametrize("spelling", _SPELLINGS)
def test_watch_routes_both_spellings_to_the_same_handler_argument(
    spelling, tmp_path, monkeypatch
):
    """The value REACHES the handler, not merely the parser.

    Both the root resolver and `tick_once` are stubbed, so this asserts the argv wiring
    and nothing about what a watch over an empty fixture directory would decide. The
    assertion is on the captured keyword rather than the exit code deliberately: `_cli`
    returns 2 for an unresolvable repo root as well as for an argparse refusal, so an
    exit-code check cannot tell the failure this guards from an unrelated one.
    """
    seen: dict = {}
    monkeypatch.setattr(
        watch.repo_root_arg, "resolve_repo_root_arg", lambda v: str(tmp_path)
    )
    monkeypatch.setattr(
        watch, "tick_once", lambda *a, **k: seen.update(k) or 0
    )

    watch._cli(["--repo-root", str(tmp_path), spelling, _SID, "--once"])

    assert seen.get("group_em_session_id") == _SID, (
        f"{spelling} did not reach tick_once as group_em_session_id — an alias on the "
        f"wrong dest exits 0, arms nothing on the Group-EM's behalf, and reports "
        f"success. saw: {seen!r}"
    )


@pytest.mark.parametrize("spelling", _SPELLINGS)
def test_idle_report_does_not_refuse_either_spelling(spelling, tmp_path):
    """argv level on the second entry point.

    NOT an exit-code assertion, for the same reason as above: only an argparse REFUSAL
    is in scope here, and argparse signals that by raising SystemExit before any handler
    runs. Whatever the report then concludes about an empty directory is another test's
    business.
    """
    try:
        idle_report._cli(["--repo-root", str(tmp_path), spelling, _SID, "--json"])
    except SystemExit as exc:
        pytest.fail(f"{spelling} was refused by argparse (exit {exc.code})")


@pytest.mark.parametrize(
    "module", [watch, idle_report], ids=["watch", "idle_report"]
)
def test_the_retired_spelling_is_accepted_but_never_advertised(module, capsys):
    """Pins the half that keeps this from being vocabulary backsliding.

    An operator reading `--help` is never taught the retired word; only an agent that
    already had it keeps working.
    """
    with pytest.raises(SystemExit):
        module._cli(["--help"])
    out = capsys.readouterr().out
    assert "--group-em-session-id" in out, (
        f"{module.__name__} --help does not name the canonical spelling"
    )
    assert "--crown-session-id" not in out, (
        f"{module.__name__} --help advertises the retired spelling"
    )
