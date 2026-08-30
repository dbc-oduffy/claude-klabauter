"""Point 3's INDIRECTION leg, narrowed by shell reality: a backtick or a `$(`
inside a QUOTED-delimiter heredoc body is inert text, and must stop denying a
command whose real write lands somewhere ungoverned.

Purpose: the indirection leg fired on `_has_indirection_marker(segment)` alone,
with no relation to any sink. A segment is not a line -- `_split_top_level_
segments` splits on shell operators -- so a whole block of prose inside a
heredoc body arrives as ONE segment, and any governed name mentioned in that
prose met any markdown code span in it:

    python - <<'PY'
    addition = \"\"\"  The reason is the one this repo's own <gov> states ...
      it pins `('X:/mirror', 'resolved-engine')` as the HEALTHY answer ...\"\"\"
    io.open('state/bug-backlog/x.yaml','w').write(s + addition)
    PY

...denied, though the only write lands in `state/`. Measured live 2026-08-30
(this session, amending a bug-backlog entry that quoted a governed filename).
This is the THIRD instance of one root cause -- a marker never related to its
sink -- after point 4 (closed at 78c7cef95) and point 3's interpreter leg
(`test_guard_doctrine_surface_point3_interpreter_by_sink.py`), one leg over.

WHY QUOTING IS THE DISCRIMINANT, and not "it looked like prose": with a quoted
delimiter the shell performs NO expansion inside the body -- POSIX, not a
heuristic -- so those bytes cannot become a command. An UNQUOTED heredoc body
IS expanded, and stays denied here on that difference alone.

The DENY corpus is the load-bearing half. A narrowing of a hard-deny guard is
only as good as what it still refuses: every write that actually reaches a
governed surface through a heredoc is pinned below, including the ones refused
because the guard CANNOT read them rather than because it understood them.

Negative-spec: this file does NOT assert on deny TEXT (the composer's own
concern), does NOT re-cover point 4, and does NOT cover the `python -c` payload
shape -- `test_guard_doctrine_surface_point3_interpreter_by_sink.py` owns that.
"""

from __future__ import annotations

import pytest

from coordinator_core.bash_guards import guard_doctrine_surface_bash_write as guard

from .test_guard_doctrine_surface_point4_by_sink import GOV, IDENTIFIERS

UNGOVERNED = "state/bug-backlog/x.yaml"


def _heredoc(body: str, *, delimiter: str = "'PY'", intro: str = "python - ") -> str:
    return f"{intro}<<{delimiter}\n{body}\n{delimiter.strip(chr(39)).strip(chr(34))}"


ALLOW_CASES = [
    (
        "backtick code span around a governed mention, write lands ungoverned",
        _heredoc(
            "import io\n"
            f"addition = 'the rule in `{GOV}` says the tree is never the root'\n"
            f"io.open('{UNGOVERNED}','w').write(addition)"
        ),
    ),
    (
        "command-substitution-shaped text in prose, write lands ungoverned",
        _heredoc(
            "import io\n"
            f"note = 'see {GOV}; the probe printed $(git rev-parse HEAD) at the time'\n"
            f"io.open('{UNGOVERNED}','w').write(note)"
        ),
    ),
    (
        "the word python beside a governed mention in prose",
        _heredoc(
            "import io\n"
            f"note = 'new automation is naked python, per {GOV}'\n"
            f"io.open('{UNGOVERNED}','w').write(note)"
        ),
    ),
    (
        "multi-line triple-quoted prose -- one segment, mention and marker both inside",
        _heredoc(
            "import io\n"
            'addition = """  AMENDED today.\n'
            f"\n  The reason is the one this repo's own {GOV} states from the other\n"
            "  side; it pins `('X:/mirror', 'resolved-engine')` as the healthy answer.\n"
            '  """\n'
            f"io.open('{UNGOVERNED}','w').write(addition)"
        ),
    ),
    (
        "double-quoted delimiter suppresses expansion too",
        _heredoc(
            "import io\n"
            f"note = 'the `{GOV}` rule'\n"
            f"io.open('{UNGOVERNED}','w').write(note)",
            delimiter='"PY"',
        ),
    ),
    (
        "quoted heredoc as DATA redirected to an ungoverned file",
        _heredoc(
            f"see `{GOV}` for the rule",
            delimiter="'EOF'",
            intro=f"cat > {UNGOVERNED} ",
        ),
    ),
    (
        # Denied before this narrowing, on the bare word "python" beside an
        # inert mention. There is no write anywhere in it -- the eval's own
        # payload is a printf -- so nothing here can reach a governed sink,
        # and the old verdict was the false positive, not the protection.
        "a live marker elsewhere, an inert mention, and no write at all",
        f"eval \"$(printf %s x)\" && python - <<'PY'\nnote = 'see {GOV}'\nPY",
    ),
]

DENY_CASES = [
    (
        "UNQUOTED delimiter -- the shell expands this body",
        _heredoc(
            "import io\n"
            f"note = 'see {GOV}; head is $(git rev-parse HEAD)'\n"
            f"io.open('{UNGOVERNED}','w').write(note)",
            delimiter="PY",
        ),
    ),
    (
        "quoted body that actually writes the governed surface",
        _heredoc("import io\n" f"io.open('{GOV}','w').write('corrupted')"),
    ),
    (
        "quoted body appending to the governed surface",
        _heredoc("import io\n" f"io.open('{GOV}','a').write('x')"),
    ),
    (
        "quoted body writing the governed surface through pathlib",
        _heredoc("import pathlib\n" f"pathlib.Path('{GOV}').write_text('x')"),
    ),
    (
        "quoted body binding a file object -- unreadable, must fail closed",
        _heredoc("import io\n" f"f=io.open('{GOV}','w')\nf.write('x')"),
    ),
    (
        "quoted heredoc as DATA redirected AT the governed surface",
        _heredoc("corrupted", delimiter="'EOF'", intro=f"cat > {GOV} "),
    ),
    (
        "var-bound governed path written from inside a quoted body",
        _heredoc("import io\n" f"p = '{GOV}'\nio.open(p,'w').write('x')"),
    ),
    (
        "split-literal governed path -- the fold reaches it inside a quoted body",
        _heredoc("import io\n" "io.open('CLAU'+'DE.md','w').write('x')"),
    ),
    (
        "os.system smuggling a shell write out of a quoted body",
        _heredoc("import os\n" f"os.system('echo x > {GOV}')"),
    ),
    (
        "subprocess smuggling a governed write out of a quoted body",
        _heredoc("import subprocess\n" f"subprocess.run(['tee','{GOV}'])"),
    ),
    (
        "governed mention outside the heredoc, with a live marker",
        f"echo `cat {GOV}` > {UNGOVERNED}",
    ),
]


@pytest.mark.parametrize("label,cmd", ALLOW_CASES, ids=[c[0] for c in ALLOW_CASES])
def test_quoted_heredoc_indirection_is_inert(label, cmd):
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is False, label


@pytest.mark.parametrize("label,cmd", DENY_CASES, ids=[c[0] for c in DENY_CASES])
def test_the_deny_corpus_still_refuses(label, cmd):
    assert guard.is_denied_bash_write(cmd, IDENTIFIERS) is True, label


def test_containment_is_whole_segment_not_overlap():
    """A segment straddling the introducing line and the body is not contained
    in any body, so the introducing line's own markers keep their force."""
    bodies = ["note = 'see the rule'"]
    assert guard._lies_in_a_quoted_heredoc_body("note = 'see the rule'", bodies) is True
    assert guard._lies_in_a_quoted_heredoc_body("python - <<'PY'\nnote = 'see the rule'", bodies) is False
    assert guard._lies_in_a_quoted_heredoc_body("   ", bodies) is False
    assert guard._lies_in_a_quoted_heredoc_body("note = 'see the rule'", []) is False


def test_unterminated_quoted_heredoc_yields_no_inert_body():
    """No terminator line means no body is established, so nothing is treated
    as inert -- the fail-closed direction."""
    assert guard._quoted_heredoc_bodies("python - <<'PY'\nnote = 'x'") == []
    assert guard._quoted_heredoc_bodies("python - <<'PY'\nnote = 'x'\nPY") == ["note = 'x'"]
