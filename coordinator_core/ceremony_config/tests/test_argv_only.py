
from __future__ import annotations

from coordinator_core.ceremony_config.argv_only import check_argv_only


def test_conformant_plain_command():
    verdict = check_argv_only("pnpm publish:fleet")
    assert verdict.conformant is True
    assert verdict.rule is None
    assert verdict.argv == ["pnpm", "publish:fleet"]


def test_w1_assignment_prefix_from_cockpit_memo():
    verdict = check_argv_only("COCKPIT_STATE_SOURCE=firestore pnpm publish:fleet")
    assert verdict.conformant is False
    assert verdict.rule == "W1-assignment-prefix"
    assert "COCKPIT_STATE_SOURCE=firestore" in verdict.detail
    assert verdict.argv is None


def test_w2_shell_metachar_and_and():
    verdict = check_argv_only("echo hi && echo bye")
    assert verdict.conformant is False
    assert verdict.rule == "W2-shell-metachar"
    assert verdict.argv is None


def test_w2_pipe():
    verdict = check_argv_only("cat file | grep foo")
    assert verdict.conformant is False
    assert verdict.rule == "W2-shell-metachar"


def test_w2_semicolon():
    verdict = check_argv_only("echo hi; echo bye")
    assert verdict.conformant is False
    assert verdict.rule == "W2-shell-metachar"


def test_w2_redirect():
    verdict = check_argv_only("echo hi > out.txt")
    assert verdict.conformant is False
    assert verdict.rule == "W2-shell-metachar"


def test_w2_command_substitution_dollar_paren():
    verdict = check_argv_only("echo $(whoami)")
    assert verdict.conformant is False
    assert verdict.rule == "W2-shell-metachar"


def test_w2_backtick():
    verdict = check_argv_only("echo `whoami`")
    assert verdict.conformant is False
    assert verdict.rule == "W2-shell-metachar"


def test_w2_unquoted_glob():
    verdict = check_argv_only("ls *.py")
    assert verdict.conformant is False
    assert verdict.rule == "W2-shell-metachar"


def test_w2_quoted_glob_is_conformant():
    verdict = check_argv_only("grep -r '*.py' src")
    assert verdict.conformant is True
    assert verdict.argv == ["grep", "-r", "*.py", "src"]


def test_w3_unterminated_quote():
    verdict = check_argv_only('echo "unterminated')
    assert verdict.conformant is False
    assert verdict.rule == "W3-unparseable"
    assert verdict.argv is None


def test_w4_empty_string():
    verdict = check_argv_only("")
    assert verdict.conformant is False
    assert verdict.rule == "W4-empty"


def test_w4_whitespace_only():
    verdict = check_argv_only("   ")
    assert verdict.conformant is False
    assert verdict.rule == "W4-empty"


def test_flag_equals_value_is_not_w1():
    verdict = check_argv_only("--flag=value")
    assert verdict.conformant is True
    assert verdict.rule is None


def test_flag_equals_value_mid_command_is_not_w1():
    verdict = check_argv_only("pytest --flag=value -k something")
    assert verdict.conformant is True


def test_example_store_repo_live_value_must_not_be_flagged():
    verdict = check_argv_only(
        "python3 bin/guard_store_plaintext.py --install-hook --quiet"
    )
    assert verdict.conformant is True
    assert verdict.rule is None
    assert verdict.argv == [
        "python3",
        "bin/guard_store_plaintext.py",
        "--install-hook",
        "--quiet",
    ]


def test_path_shaped_first_token_is_not_w1():
    verdict = check_argv_only("a/b=c somearg")
    assert verdict.conformant is True
    assert verdict.rule is None


def test_env_prefix_multiple_assignments_still_w1_on_first():
    verdict = check_argv_only("FOO=1 BAR=2 pytest")
    assert verdict.conformant is False
    assert verdict.rule == "W1-assignment-prefix"


class TestTheTwoW2GapsAreClosed:
    """`pnpm test &` parsed CONFORMANT and handed a literal `&` to the program
    instead of backgrounding anything; `~/bin/publish --fleet` parsed CONFORMANT
    and failed at fire time as an unresolvable `argv[0]`.

    Origin: cross-repo/archive/2026-08-06-example-cockpit-repo-em-argv-only-marker-
    answer-and-w2-gaps.md.
    """

    def test_a_lone_ampersand_is_a_metachar(self):
        v = check_argv_only("pnpm test &")

        assert v.conformant is False
        assert v.rule == "W2-shell-metachar"

    def test_a_chain_still_names_the_chain_not_the_ampersand(self):
        v = check_argv_only("echo x && echo y")

        assert v.conformant is False
        assert "'&&'" in v.detail

    def test_a_leading_tilde_is_flagged(self):
        v = check_argv_only("~/bin/publish --fleet")

        assert v.conformant is False
        assert v.rule == "W2-shell-metachar"

    def test_a_tilde_inside_a_token_is_an_ordinary_filename_character(self):
        assert check_argv_only("ls file~").conformant is True

    def test_quoting_resolves_both_exactly_as_it_does_the_rest_of_w2(self):
        assert check_argv_only("echo 'a & b'").conformant is True
        assert check_argv_only("echo '~/x'").conformant is True
